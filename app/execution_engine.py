from __future__ import annotations

import math
from typing import Any

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
        normalized = math.floor(contracts)
        return float(max(normalized, 0))

    def _contract_notional_usdt(self, symbol: str, price: float) -> float:
        if price <= 0:
            return 0.0
        return price * self._contract_size(symbol)

    def _contracts_from_margin(self, symbol: str, margin_usdt: float, price: float, leverage: int, safety_haircut: float = 0.90) -> float:
        if margin_usdt <= 0 or leverage <= 0:
            return 0.0
        contract_notional_usdt = self._contract_notional_usdt(symbol, price)
        if contract_notional_usdt <= 0:
            return 0.0
        notional_usdt = margin_usdt * leverage
        raw_contracts = notional_usdt / contract_notional_usdt
        safe_contracts = raw_contracts * safety_haircut
        return self._normalize_contracts(safe_contracts)

    def estimate_order_size(self, symbol: str, equity_usdt: float, price: float, leverage: int, position_ratio: float) -> float:
        margin_usdt = equity_usdt * position_ratio
        return self._contracts_from_margin(symbol=symbol, margin_usdt=margin_usdt, price=price, leverage=leverage)

    def estimate_fixed_margin_order_size(self, symbol: str, margin_usdt: float, price: float, leverage: int) -> float:
        return self._contracts_from_margin(symbol=symbol, margin_usdt=margin_usdt, price=price, leverage=leverage)

    def cap_order_size_by_margin(self, symbol: str, requested_size: float, margin_usdt: float, price: float, leverage: int) -> float:
        max_size = self.estimate_fixed_margin_order_size(symbol=symbol, margin_usdt=margin_usdt, price=price, leverage=leverage)
        requested_size = self._normalize_contracts(requested_size)
        if max_size <= 0:
            return 0.0
        return min(requested_size, max_size)

    def get_net_position(self, symbol: str) -> float:
        positions = self.client.get_positions(symbol)
        if not positions:
            return 0.0
        return safe_float(positions[0].get("pos"))

    def close_with_reverse_market(self, symbol: str, tag: str = "agentTradeKit", td_mode: str = "isolated") -> list[dict[str, Any]]:
        net_pos = self.get_net_position(symbol)
        if net_pos == 0:
            return []
        side = "sell" if net_pos > 0 else "buy"
        return self.client.place_order(symbol, side=side, size=abs(net_pos), ord_type="market", td_mode=td_mode, tag=tag, reduce_only=True)

    def close_position(
        self,
        symbol: str,
        td_mode: str = "isolated",
        tag: str = "agentTradeKit",
        reason: str | None = None,
    ) -> dict[str, Any]:
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) <= 1e-8:
            return {"entry": [], "algo": [], "closed_size": 0.0, "reason": reason or "flat_position"}
        result = self.close_with_reverse_market(symbol=symbol, tag=tag, td_mode=td_mode)
        return {
            "entry": result,
            "algo": [],
            "closed_size": abs(net_pos),
            "reason": reason or "signal_exit",
            "td_mode": td_mode,
        }

    def _okx_max_avail_size_cap(self, symbol: str, size: float, td_mode: str = "isolated", leverage: int | None = None) -> tuple[float, list[dict[str, Any]]]:
        requested_size = self._normalize_contracts(size)
        if requested_size <= 0:
            return 0.0, []
        try:
            rows = self.client.get_max_avail_size(inst_id=symbol, td_mode=td_mode, lever=leverage)
        except Exception as exc:
            engine_logger.warning("%s 查询 OKX 最大可开张数失败，回退到代码侧张数控制: %s", symbol, exc)
            return requested_size, []

        max_size = 0.0
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            for value in (
                row.get("maxBuy"),
                row.get("maxSell"),
                row.get("availBuy"),
                row.get("availSell"),
                row.get("maxAvailSz"),
                row.get("maxSz"),
                row.get("availSz"),
            ):
                max_size = max(max_size, safe_float(value))

        if max_size <= 0:
            return requested_size, rows if isinstance(rows, list) else []
        okx_safe_size = self._normalize_contracts(max_size * 0.90)
        if okx_safe_size <= 0:
            return 0.0, rows if isinstance(rows, list) else []
        return min(requested_size, okx_safe_size), rows if isinstance(rows, list) else []

    def place_entry_with_tpsl(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        tag: str = "agentTradeKit",
        td_mode: str = "isolated",
        leverage: int | None = None,
    ) -> dict[str, Any]:
        leverage_result: list[dict[str, Any]] = []
        if leverage is not None:
            leverage_result = self.client.set_leverage(symbol, lever=leverage, mgn_mode=td_mode)

        final_size, max_avail_rows = self._okx_max_avail_size_cap(symbol=symbol, size=size, td_mode=td_mode, leverage=leverage)
        if final_size <= 0:
            return {
                "leverage": leverage_result,
                "entry": [],
                "algo": [],
                "max_avail_size": max_avail_rows,
                "requested_size": size,
                "final_size": 0.0,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "td_mode": td_mode,
            }
        if final_size < self._normalize_contracts(size):
            engine_logger.info("%s 下单前双保险生效：请求sz=%.2f，按 OKX 最大可开张数九折后缩减至 %.2f。", symbol, size, final_size)

        order_result = self.client.place_order(
            symbol,
            side=side,
            size=final_size,
            ord_type="market",
            td_mode=td_mode,
            tag=tag,
            reduce_only=False,
        )
        if not order_result:
            return {"leverage": leverage_result, "entry": [], "algo": []}

        entry_success = any(str(row.get("sCode", "")) == "0" for row in order_result if isinstance(row, dict))
        if not entry_success:
            return {
                "leverage": leverage_result,
                "entry": order_result,
                "algo": [],
                "max_avail_size": max_avail_rows,
                "requested_size": size,
                "final_size": final_size,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "td_mode": td_mode,
            }

        if side == "buy":
            sl_trigger = round(entry_price * (1 - stop_loss_pct), 4)
            tp_trigger = round(entry_price * (1 + take_profit_pct), 4)
        else:
            sl_trigger = round(entry_price * (1 + stop_loss_pct), 4)
            tp_trigger = round(entry_price * (1 - take_profit_pct), 4)

        exit_side = "sell" if side == "buy" else "buy"
        algo_result = self.client.place_algo_order(
            inst_id=symbol,
            td_mode=td_mode,
            algo_ord_type="oco",
            side=exit_side,
            sz=final_size,
            tp_trigger_px=tp_trigger,
            sl_trigger_px=sl_trigger,
            tag=tag,
            reduce_only=True,
        )
        return {
            "leverage": leverage_result,
            "entry": order_result,
            "algo": algo_result,
            "max_avail_size": max_avail_rows,
            "requested_size": size,
            "final_size": final_size,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "td_mode": td_mode,
        }

    def place_knife_attack(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        margin_usdt: float,
        leverage: int,
        stop_loss_pct: float,
        take_profit_pct: float,
        tag: str = "agentTradeKit",
    ) -> dict[str, Any]:
        size = self.estimate_fixed_margin_order_size(symbol, margin_usdt=margin_usdt, price=entry_price, leverage=leverage)
        if size <= 0:
            return {
                "leverage": [],
                "entry": [],
                "algo": [],
                "size": 0.0,
                "margin_usdt": margin_usdt,
                "leverage_requested": leverage,
                "td_mode": "isolated",
            }
        result = self.place_entry_with_tpsl(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            tag=tag,
            td_mode="isolated",
            leverage=leverage,
        )
        result["size"] = size
        result["margin_usdt"] = margin_usdt
        result["leverage_requested"] = leverage
        result["td_mode"] = "isolated"
        return result
