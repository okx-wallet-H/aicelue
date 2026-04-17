from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from app.config import settings
from app.logger import engine_logger
from app.okx_cli import OKXClient
from app.utils import safe_float


class ExecutionEngine:
    def __init__(self, client: OKXClient) -> None:
        self.client = client

    @staticmethod
    def _fallback_contract_size(symbol: str) -> float:
        if symbol == "BTC-USDT-SWAP":
            return 0.01
        if symbol == "ETH-USDT-SWAP":
            return 0.1
        if symbol == "SOL-USDT-SWAP":
            return 1.0
        return 1.0

    def _get_instrument_spec(self, symbol: str) -> dict[str, Any]:
        spec = self.client.get_instrument_spec(symbol)
        if not isinstance(spec, dict):
            raise RuntimeError(f"{symbol} 合约规格格式异常: {spec}")
        return spec

    def _contract_size(self, symbol: str, spec: dict[str, Any] | None = None) -> float:
        source = spec or self._get_instrument_spec(symbol)
        return safe_float(source.get("ctVal"), self._fallback_contract_size(symbol))

    @staticmethod
    def _normalize_contracts(raw_contracts: float, lot_sz: float, min_sz: float) -> float:
        if raw_contracts <= 0 or lot_sz <= 0 or min_sz <= 0:
            return 0.0

        raw_dec = Decimal(str(raw_contracts))
        lot_dec = Decimal(str(lot_sz))
        min_dec = Decimal(str(min_sz))
        normalized = (raw_dec / lot_dec).to_integral_value(rounding=ROUND_DOWN) * lot_dec
        if normalized < min_dec:
            return 0.0
        return float(normalized.normalize())

    def _contract_notional_usdt(self, symbol: str, price: float, spec: dict[str, Any] | None = None) -> float:
        if price <= 0:
            return 0.0
        return price * self._contract_size(symbol, spec)

    def _contracts_from_margin(self, symbol: str, margin_usdt: float, price: float, leverage: int) -> float:
        if margin_usdt <= 0 or leverage <= 0 or price <= 0:
            return 0.0
        spec = self._get_instrument_spec(symbol)
        contract_notional = self._contract_notional_usdt(symbol, price, spec)
        if contract_notional <= 0:
            return 0.0
        raw_contracts = (margin_usdt * leverage * 0.95) / contract_notional
        lot_sz = safe_float(spec.get("lotSz"), 0.01)
        min_sz = safe_float(spec.get("minSz"), lot_sz)
        normalized = self._normalize_contracts(raw_contracts, lot_sz=lot_sz, min_sz=min_sz)
        engine_logger.info(
            "%s 张数计算: margin=%.4f leverage=%s price=%.4f ctVal=%s lotSz=%s minSz=%s raw=%.6f normalized=%.6f",
            symbol,
            margin_usdt,
            leverage,
            price,
            spec.get("ctVal"),
            spec.get("lotSz"),
            spec.get("minSz"),
            raw_contracts,
            normalized,
        )
        return normalized

    @staticmethod
    def _has_success_row(rows: list[dict[str, Any]] | None) -> bool:
        return any(str(row.get("sCode", "")).strip() == "0" for row in (rows or []) if isinstance(row, dict))

    @staticmethod
    def _extract_first_success_row(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        for row in rows or []:
            if isinstance(row, dict) and str(row.get("sCode", "")).strip() == "0":
                return row
        return None

    def _validate_set_leverage(self, symbol: str, leverage: int, result: list[dict[str, Any]]) -> None:
        if not isinstance(result, list) or not result:
            raise RuntimeError(f"{symbol} 设置杠杆返回为空。")
        matched = False
        for row in result:
            if not isinstance(row, dict):
                continue
            s_code = str(row.get("sCode", "")).strip()
            if s_code and s_code != "0":
                raise RuntimeError(f"{symbol} 设置杠杆失败: {row}")
            row_inst = str(row.get("instId", symbol))
            row_lever = int(safe_float(row.get("lever"), leverage))
            if row_inst == symbol and row_lever == leverage:
                matched = True
        if not matched:
            raise RuntimeError(f"{symbol} 设置杠杆回执未确认到目标杠杆 {leverage}: {result}")

    def _fetch_position_snapshot(self, symbol: str, retries: int = 5, delay_seconds: float = 1.0) -> dict[str, Any] | None:
        for _ in range(retries):
            positions = self.client.get_positions(symbol)
            for position in positions:
                if not isinstance(position, dict):
                    continue
                pos_size = abs(safe_float(position.get("pos")))
                if pos_size > 0:
                    return position
            time.sleep(delay_seconds)
        return None

    def _risk_usdt(self, symbol: str, entry_price: float, stop_loss_price: float, size: float, spec: dict[str, Any] | None = None) -> float:
        if entry_price <= 0 or stop_loss_price <= 0 or size <= 0:
            return 0.0
        ct_val = self._contract_size(symbol, spec)
        return abs(entry_price - stop_loss_price) * ct_val * size

    def _validate_trailing_stop(
        self,
        *,
        action: str,
        entry_price: float,
        trailing_stop: dict[str, Any] | None,
    ) -> tuple[bool, str, dict[str, float] | None]:
        if not trailing_stop:
            return True, "未提供移动止盈", None

        callback_ratio = safe_float(trailing_stop.get("callback_ratio"))
        active_px = safe_float(trailing_stop.get("active_px"))
        if not (0 < callback_ratio < 1):
            return False, f"callback_ratio 非法: {callback_ratio}", None
        if active_px <= 0:
            return False, f"active_px 非法: {active_px}", None
        if action == "OPEN_LONG" and active_px <= entry_price:
            return False, f"多单 active_px={active_px} 必须高于 entry_px={entry_price}", None
        if action == "OPEN_SHORT" and active_px >= entry_price:
            return False, f"空单 active_px={active_px} 必须低于 entry_px={entry_price}", None
        return True, "通过", {"callback_ratio": callback_ratio, "active_px": active_px}

    def get_net_position(self, symbol: str) -> float:
        positions = self.client.get_positions(symbol)
        if not positions:
            return 0.0
        for row in positions:
            if not isinstance(row, dict):
                continue
            pos = safe_float(row.get("pos"))
            if abs(pos) > 0:
                return pos
        return 0.0

    def close_position(self, symbol: str, tag: str = "agentTradeKit") -> dict[str, Any]:
        net_pos = self.get_net_position(symbol)
        if abs(net_pos) <= 1e-8:
            return {"status": "no_position", "attempts": 0, "net_pos_after": net_pos}

        side = "sell" if net_pos > 0 else "buy"
        result = self.client.place_order(
            symbol,
            side=side,
            size=abs(net_pos),
            ord_type="market",
            tag=tag,
            reduce_only=True,
        )
        if not self._has_success_row(result):
            engine_logger.error("%s 平仓下单失败: %s", symbol, result)
            return {"status": "close_failed", "attempts": 1, "net_pos_after": net_pos, "result": result}

        # Wait for exchange to reflect the fill before verifying
        time.sleep(0.3)

        net_pos_after = self.get_net_position(symbol)
        if abs(net_pos_after) <= 1e-8:
            return {"status": "closed", "attempts": 1, "net_pos_after": net_pos_after, "result": result, "size": abs(net_pos)}

        # Verify failed – retry once with updated size/side
        engine_logger.warning("%s 平仓后仍有仓位(net_pos=%.6f)，重试一次...", symbol, net_pos_after)
        retry_side = "sell" if net_pos_after > 0 else "buy"
        retry_result = self.client.place_order(
            symbol,
            side=retry_side,
            size=abs(net_pos_after),
            ord_type="market",
            tag=tag,
            reduce_only=True,
        )
        time.sleep(0.3)
        net_pos_final = self.get_net_position(symbol)
        if abs(net_pos_final) <= 1e-8:
            return {
                "status": "closed",
                "attempts": 2,
                "net_pos_after": net_pos_final,
                "result": result,
                "retry_result": retry_result,
                "size": abs(net_pos),
            }

        engine_logger.error("%s 平仓重试后仍有残余仓位: net_pos=%.6f", symbol, net_pos_final)
        return {
            "status": "close_unverified",
            "attempts": 2,
            "net_pos_after": net_pos_final,
            "result": result,
            "retry_result": retry_result,
            "size": abs(net_pos),
        }

    def _force_flatten(self, symbol: str, reason: str, tag: str) -> dict[str, Any]:
        engine_logger.error("%s 触发强制平仓: %s", symbol, reason)
        try:
            close_result = self.close_position(symbol, tag=f"{tag}-forced-close")
            if close_result.get("status") in {"close_failed", "close_unverified"}:
                engine_logger.error("%s 强制平仓未能验证成功: %s", symbol, close_result)
                return {"status": "forced_flatten_failed", "reason": reason, "close_result": close_result}
            return {"status": "forced_flattened", "reason": reason, "close_result": close_result}
        except Exception as exc:  # noqa: BLE001
            engine_logger.exception("%s 强制平仓失败: %s", symbol, exc)
            return {
                "status": "forced_flatten_failed",
                "reason": reason,
                "close_error": str(exc),
            }

    def execute_ai_open(
        self,
        symbol: str,
        action: str,
        margin_usdt: float,
        leverage: int,
        entry_price: float,
        stop_loss_price: float,
        account_equity: float,
        trailing_stop: dict[str, Any] | None = None,
        take_profit_price: float | None = None,
        tag: str = "agentTradeKit",
    ) -> dict[str, Any]:
        """执行 AI 开仓，并对止损与真实风险进行强校验。"""
        if action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return {"status": "failed", "reason": f"unsupported_action:{action}"}
        if margin_usdt <= 0 or leverage <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            return {"status": "failed", "reason": "invalid_trade_parameters"}

        spec = self._get_instrument_spec(symbol)

        try:
            leverage_result = self.client.set_leverage(symbol, lever=leverage)
            self._validate_set_leverage(symbol, leverage, leverage_result)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "reason": f"set_leverage_failed:{exc}"}

        size = self._contracts_from_margin(symbol, margin_usdt, entry_price, leverage)
        if size <= 0:
            return {"status": "failed", "reason": "size_too_small"}

        side = "buy" if action == "OPEN_LONG" else "sell"
        exit_side = "sell" if side == "buy" else "buy"

        order_result = self.client.place_order(symbol, side=side, size=size, ord_type="market", tag=tag)
        if not self._has_success_row(order_result):
            return {"status": "failed", "reason": "order_failed", "result": order_result}

        entry_row = self._extract_first_success_row(order_result) or {}
        position_snapshot = self._fetch_position_snapshot(symbol)
        actual_entry_price = safe_float(position_snapshot.get("avgPx") if position_snapshot else None, entry_price)
        actual_position_size = abs(safe_float(position_snapshot.get("pos") if position_snapshot else None, size))
        actual_margin = safe_float(
            (position_snapshot or {}).get("imr")
            or (position_snapshot or {}).get("margin")
            or (position_snapshot or {}).get("marginUsd")
            or (position_snapshot or {}).get("margin_usdt"),
            margin_usdt,
        )

        try:
            stop_loss_result = self.client.place_algo_order(
                inst_id=symbol,
                td_mode="isolated",
                algo_ord_type="stop",
                side=exit_side,
                sz=actual_position_size,
                sl_trigger_px=stop_loss_price,
                tag=tag,
                reduce_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            flatten_result = self._force_flatten(symbol, f"止损挂单失败:{exc}", tag)
            return {
                "status": "failed",
                "reason": f"stop_loss_order_failed:{exc}",
                "entry": order_result,
                "forced_flatten": flatten_result,
            }

        actual_risk_usdt = self._risk_usdt(symbol, actual_entry_price, stop_loss_price, actual_position_size, spec)
        max_allowed_risk = max(0.0, account_equity * settings.single_trade_max_risk_pct)
        if max_allowed_risk > 0 and actual_risk_usdt > max_allowed_risk:
            flatten_result = self._force_flatten(
                symbol,
                f"真实成交价风险超限 actual_risk={actual_risk_usdt:.4f} > limit={max_allowed_risk:.4f}",
                tag,
            )
            return {
                "status": "failed",
                "reason": "real_fill_risk_exceeded",
                "entry": order_result,
                "stop_loss": stop_loss_result,
                "actual_entry_price": actual_entry_price,
                "actual_risk_usdt": actual_risk_usdt,
                "forced_flatten": flatten_result,
            }

        trailing_result = None
        trailing_warning = None
        trailing_ok, trailing_message, normalized_trailing = self._validate_trailing_stop(
            action=action,
            entry_price=actual_entry_price,
            trailing_stop=trailing_stop,
        )
        if normalized_trailing is not None and trailing_ok:
            try:
                trailing_result = self.client.place_algo_order(
                    inst_id=symbol,
                    td_mode="isolated",
                    algo_ord_type="move_order_stop",
                    side=exit_side,
                    sz=actual_position_size,
                    active_px=normalized_trailing["active_px"],
                    callback_ratio=normalized_trailing["callback_ratio"],
                    tag=tag,
                    reduce_only=True,
                )
            except Exception as exc:  # noqa: BLE001
                trailing_warning = f"移动止盈挂单失败:{exc}"
                engine_logger.warning("%s %s", symbol, trailing_warning)
        elif trailing_stop:
            trailing_warning = f"移动止盈参数校验失败:{trailing_message}"
            engine_logger.warning("%s %s", symbol, trailing_warning)

        take_profit_result = None
        take_profit_warning = None
        if take_profit_price and take_profit_price > 0 and trailing_result is None:
            try:
                take_profit_result = self.client.place_algo_order(
                    inst_id=symbol,
                    td_mode="isolated",
                    algo_ord_type="limit",
                    side=exit_side,
                    sz=actual_position_size,
                    tp_trigger_px=take_profit_price,
                    tag=tag,
                    reduce_only=True,
                )
            except Exception as exc:  # noqa: BLE001
                take_profit_warning = f"固定止盈挂单失败:{exc}"
                engine_logger.warning("%s %s", symbol, take_profit_warning)

        return {
            "status": "success",
            "entry": order_result,
            "entry_order_id": str(entry_row.get("ordId", "")),
            "stop_loss": stop_loss_result,
            "trailing_stop": trailing_result,
            "take_profit": take_profit_result,
            "trailing_warning": trailing_warning,
            "take_profit_warning": take_profit_warning,
            "size": actual_position_size,
            "requested_size": size,
            "actual_entry_price": actual_entry_price,
            "actual_margin_usdt": actual_margin,
            "actual_risk_usdt": actual_risk_usdt,
            "stop_loss_price": stop_loss_price,
        }
