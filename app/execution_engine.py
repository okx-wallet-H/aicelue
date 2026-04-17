from __future__ import annotations

import math
from typing import Any

from app.config import settings
from app.logger import engine_logger
from app.okx_cli import OKXClient
from app.utils import safe_float


class ExecutionEngine:
    def __init__(self, client: OKXClient) -> None:
        self.client = client

    @staticmethod
    def _contract_size(symbol: str) -> float:
        if symbol == "BTC-USDT-SWAP":
            return 0.01
        if symbol == "SOL-USDT-SWAP":
            return 1.0
        return 1.0

    def _normalize_contracts(self, contracts: float) -> float:
        if contracts <= 0:
            return 0.0
        return float(math.floor(contracts))

    def _contract_notional_usdt(self, symbol: str, price: float) -> float:
        if price <= 0:
            return 0.0
        return price * self._contract_size(symbol)

    def _contracts_from_margin(self, symbol: str, margin_usdt: float, price: float, leverage: int) -> float:
        if margin_usdt <= 0 or leverage <= 0 or price <= 0:
            return 0.0
        contract_notional = self._contract_notional_usdt(symbol, price)
        if contract_notional <= 0:
            return 0.0
        raw_contracts = (margin_usdt * leverage) / contract_notional
        return self._normalize_contracts(raw_contracts * 0.95)  # 95% 冗余

    def get_net_position(self, symbol: str) -> float:
        positions = self.client.get_positions(symbol)
        if not positions:
            return 0.0
        return safe_float(positions[0].get("pos"))

    def close_position(self, symbol: str, tag: str = "agentTradeKit") -> dict[str, Any]:
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) <= 1e-8:
            return {"status": "no_position"}
        
        side = "sell" if net_pos > 0 else "buy"
        result = self.client.place_order(
            symbol, side=side, size=abs(net_pos), ord_type="market", tag=tag, reduce_only=True
        )
        return {"status": "closed", "result": result, "size": abs(net_pos)}

    def execute_ai_open(
        self,
        symbol: str,
        action: str,
        margin_usdt: float,
        leverage: int,
        entry_price: float,
        stop_loss_price: float,
        trailing_stop: dict[str, Any] | None = None,
        take_profit_price: float | None = None,
        tag: str = "agentTradeKit"
    ) -> dict[str, Any]:
        """
        执行 AI 的开仓决策，包括设置杠杆、下单、挂止损和移动止盈。
        """
        # 1. 设置杠杆
        self.client.set_leverage(symbol, lever=leverage)
        
        # 2. 计算张数
        size = self._contracts_from_margin(symbol, margin_usdt, entry_price, leverage)
        if size <= 0:
            return {"status": "failed", "reason": "size_too_small"}

        # 3. 下市价单开仓
        side = "buy" if action == "OPEN_LONG" else "sell"
        order_result = self.client.place_order(
            symbol, side=side, size=size, ord_type="market", tag=tag
        )
        
        # 检查开仓是否成功
        success = any(str(row.get("sCode", "")) == "0" for row in (order_result or []))
        if not success:
            return {"status": "failed", "reason": "order_failed", "result": order_result}

        # 4. 挂止损单 (OCO 或 独立止损)
        exit_side = "sell" if side == "buy" else "buy"
        sl_result = self.client.place_algo_order(
            inst_id=symbol,
            td_mode="isolated",
            algo_ord_type="stop",
            side=exit_side,
            sz=size,
            sl_trigger_px=stop_loss_price,
            tag=tag,
            reduce_only=True
        )

        # 5. 挂移动止盈单 (如果 AI 提供)
        trailing_result = None
        if trailing_stop and trailing_stop.get("active_px") and trailing_stop.get("callback_ratio"):
            trailing_result = self.client.place_algo_order(
                inst_id=symbol,
                td_mode="isolated",
                algo_ord_type="move_order_stop",
                side=exit_side,
                sz=size,
                active_px=trailing_stop["active_px"],
                callback_ratio=trailing_stop["callback_ratio"],
                tag=tag,
                reduce_only=True
            )
        
        # 6. 挂固定止盈 (如果 AI 提供且没有移动止盈)
        tp_result = None
        if take_profit_price and not trailing_result:
            tp_result = self.client.place_algo_order(
                inst_id=symbol,
                td_mode="isolated",
                algo_ord_type="limit", # 或者在OKX中用止盈单
                side=exit_side,
                sz=size,
                tp_trigger_px=take_profit_price,
                tag=tag,
                reduce_only=True
            )

        return {
            "status": "success",
            "entry": order_result,
            "stop_loss": sl_result,
            "trailing_stop": trailing_result,
            "take_profit": tp_result,
            "size": size
        }
