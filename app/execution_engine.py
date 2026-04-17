from __future__ import annotations

import math
from typing import Any

from app.config import settings
from app.logger import engine_logger
from app.okx_cli import OKXClient
from app.utils import safe_float


def validate_okx_rows(rows: Any, label: str = "order") -> bool:
    """校验 OKX CLI 返回的 rows 中是否所有行都 sCode == '0'。"""
    if not rows or not isinstance(rows, list):
        engine_logger.error("[%s] OKX CLI 返回空或非列表结果: %s", label, rows)
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        s_code = str(row.get("sCode", row.get("code", "")) or "")
        if s_code and s_code != "0":
            engine_logger.error("[%s] OKX CLI 返回错误码 sCode=%s msg=%s", label, s_code, row.get("sMsg", row.get("msg", "")))
            return False
    return True


class ExecutionEngine:
    def __init__(self, client: OKXClient) -> None:
        self.client = client

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

    def close_position(self, symbol: str, tag: str = "agentTradeKit") -> dict[str, Any]:
        """
        安全平仓：
        1. 先确认净仓位方向，使用反向市价单平仓（不传 reduceOnly 以确保 CLI 兼容性）。
        2. 平仓后验证持仓接近 0，如首次未归零则重试一次。
        """
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) <= 1e-8:
            return {"status": "no_position"}

        side = "sell" if net_pos > 0 else "buy"
        engine_logger.info("[close_position] %s net_pos=%.4f side=%s", symbol, net_pos, side)

        result = self.client.place_order(
            symbol, side=side, size=abs(net_pos), ord_type="market", tag=tag, reduce_only=False
        )

        # 验证平仓后持仓是否归零
        remaining = self.get_net_position(symbol)
        if abs(remaining) > 1e-8:
            engine_logger.warning(
                "[close_position] %s 平仓后仍有剩余持仓 %.4f，尝试再次平仓", symbol, remaining
            )
            retry_side = "sell" if remaining > 0 else "buy"
            retry_result = self.client.place_order(
                symbol, side=retry_side, size=abs(remaining), ord_type="market", tag=tag, reduce_only=False
            )
            remaining2 = self.get_net_position(symbol)
            if abs(remaining2) > 1e-8:
                engine_logger.error(
                    "[close_position] %s 重试平仓后持仓仍不为零 %.4f，请人工干预！", symbol, remaining2
                )
            return {"status": "closed_with_retry", "result": result, "retry_result": retry_result, "size": abs(net_pos)}

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
        任何关键保护单（止损）失败时，立刻执行保护性平仓，避免裸仓。
        """
        # 1. 设置杠杆（杠杆不超过上限）
        capped_leverage = min(leverage, settings.max_leverage_cap)
        if capped_leverage != leverage:
            engine_logger.warning("[execute_ai_open] %s 杠杆 %d 超过上限 %d，已截断", symbol, leverage, capped_leverage)
        self.client.set_leverage(symbol, lever=capped_leverage)

        # 2. 计算张数
        size = self._contracts_from_margin(symbol, margin_usdt, entry_price, capped_leverage)
        if size <= 0:
            return {"status": "failed", "reason": "size_too_small"}

        # 3. 下市价单开仓
        side = "buy" if action == "OPEN_LONG" else "sell"
        order_result = self.client.place_order(
            symbol, side=side, size=size, ord_type="market", tag=tag
        )

        # 检查开仓是否成功
        if not validate_okx_rows(order_result, label=f"entry:{symbol}"):
            return {"status": "failed", "reason": "order_failed", "result": order_result}

        # 4. 挂止损单（必须成功，否则保护性平仓）
        exit_side = "sell" if side == "buy" else "buy"
        sl_result = self.client.place_algo_order(
            inst_id=symbol,
            td_mode="isolated",
            algo_ord_type="stop",
            side=exit_side,
            sz=size,
            sl_trigger_px=stop_loss_price,
            tag=tag,
        )

        if not validate_okx_rows(sl_result, label=f"stop_loss:{symbol}"):
            engine_logger.critical(
                "[execute_ai_open] %s 止损单挂单失败！立即执行保护性平仓，避免裸仓。sl_result=%s",
                symbol, sl_result
            )
            close_result = self.close_position(symbol, tag=tag)
            return {
                "status": "failed_sl_protective_close",
                "reason": "stop_loss_failed",
                "entry": order_result,
                "stop_loss": sl_result,
                "close_result": close_result,
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
            )
            if not validate_okx_rows(trailing_result, label=f"trailing:{symbol}"):
                engine_logger.warning("[execute_ai_open] %s 移动止盈挂单失败（非致命），继续持仓。trailing_result=%s", symbol, trailing_result)
                trailing_result = None

        # 6. 挂固定止盈（如果 AI 提供且没有移动止盈）
        tp_result = None
        if take_profit_price and not trailing_result:
            tp_result = self.client.place_algo_order(
                inst_id=symbol,
                td_mode="isolated",
                algo_ord_type="stop",
                side=exit_side,
                sz=size,
                tp_trigger_px=take_profit_price,
                tag=tag,
            )
            if not validate_okx_rows(tp_result, label=f"take_profit:{symbol}"):
                engine_logger.warning("[execute_ai_open] %s 止盈挂单失败（非致命），继续持仓。tp_result=%s", symbol, tp_result)
                tp_result = None

        return {
            "status": "success",
            "entry": order_result,
            "stop_loss": sl_result,
            "trailing_stop": trailing_result,
            "take_profit": tp_result,
            "size": size
        }

