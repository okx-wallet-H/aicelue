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
    def is_success_rows(rows: list[dict[str, Any]] | None) -> bool:
        """判断 OKX CLI 返回的 rows 中是否存在 sCode == '0' 的成功行。"""
        if not rows:
            return False
        return any(str(row.get("sCode", "")) == "0" for row in rows)

    @staticmethod
    def _contract_size(symbol: str) -> float:
        if symbol == "BTC-USDT-SWAP":
            return 0.01
        if symbol == "ETH-USDT-SWAP":
            return 0.1
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

    def verify_position_closed(self, symbol: str) -> bool:
        """
        验证某标的净仓位是否已归零（或接近 0）。
        返回 True 表示仓位已清；False 表示仍有残余仓位。
        """
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) > 1e-8:
            engine_logger.error(
                "平仓验证失败：%s 净仓位 %s 未归零，可能存在残余持仓！",
                symbol,
                net_pos,
            )
            return False
        return True

    def close_position(self, symbol: str, tag: str = "agentTradeKit") -> dict[str, Any]:
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) <= 1e-8:
            return {"status": "no_position"}

        side = "sell" if net_pos > 0 else "buy"
        # 注意：CLI 对 --reduceOnly 的支持存在不确定性（已知部分版本不支持）。
        # 此处保留参数，但在日志中明确提示，并依赖 verify_position_closed() 兜底。
        engine_logger.info(
            "执行平仓 %s，方向=%s，数量=%s（注：CLI --reduceOnly 支持存在版本差异，"
            "将通过仓位验证兜底）",
            symbol, side, abs(net_pos),
        )
        result = self.client.place_order(
            symbol, side=side, size=abs(net_pos), ord_type="market", tag=tag, reduce_only=True
        )
        return {"status": "closed", "result": result, "size": abs(net_pos)}

    def _emergency_close(self, symbol: str, size: float, exit_side: str, tag: str) -> None:
        """保护性紧急平仓：止损挂单失败时立即市价平掉刚开的仓位，避免裸仓。"""
        engine_logger.error(
            "🚨 触发保护性紧急平仓：%s 方向=%s 数量=%s（止损挂单失败，避免裸仓）",
            symbol, exit_side, size,
        )
        try:
            emergency_result = self.client.place_order(
                symbol,
                side=exit_side,
                size=size,
                ord_type="market",
                tag=tag,
                reduce_only=True,
            )
            if self.is_success_rows(emergency_result):
                engine_logger.error("保护性紧急平仓成功：%s", symbol)
            else:
                engine_logger.error(
                    "保护性紧急平仓指令已发出，但回执未确认成功：%s，结果=%s",
                    symbol,
                    emergency_result,
                )
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("保护性紧急平仓异常：%s：%s", symbol, exc)

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
        止损（SL）为强制约束：SL 挂单失败时立即执行保护性市价平仓并返回失败状态。
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
        if not self.is_success_rows(order_result):
            return {"status": "failed", "reason": "order_failed", "result": order_result}

        # 4. 挂止损单（SL 为强制约束）
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

        if not self.is_success_rows(sl_result):
            engine_logger.error(
                "🚨 止损（SL）挂单失败！symbol=%s 触发保护性平仓，结果=%s",
                symbol,
                sl_result,
            )
            self._emergency_close(symbol, size, exit_side, tag)
            return {
                "status": "failed",
                "reason": "sl_failed_emergency_closed",
                "entry": order_result,
                "stop_loss": sl_result,
            }

        # 5. 挂移动止盈单（如果 AI 提供）
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
            if not self.is_success_rows(trailing_result):
                engine_logger.warning(
                    "移动止盈（trailing）挂单失败，symbol=%s，结果=%s；仓位保留，但无移动止盈保护。",
                    symbol,
                    trailing_result,
                )

        # 6. 挂固定止盈（如果 AI 提供且没有移动止盈）
        tp_result = None
        if take_profit_price and not trailing_result:
            tp_result = self.client.place_algo_order(
                inst_id=symbol,
                td_mode="isolated",
                algo_ord_type="limit",
                side=exit_side,
                sz=size,
                tp_trigger_px=take_profit_price,
                tag=tag,
                reduce_only=True
            )
            if not self.is_success_rows(tp_result):
                engine_logger.warning(
                    "固定止盈（TP）挂单失败，symbol=%s，结果=%s；仓位保留，但无止盈保护。",
                    symbol,
                    tp_result,
                )

        return {
            "status": "success",
            "entry": order_result,
            "stop_loss": sl_result,
            "trailing_stop": trailing_result,
            "take_profit": tp_result,
            "size": size
        }

