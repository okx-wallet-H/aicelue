from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, TextIO

from app.config import settings
from app.execution_engine import ExecutionEngine
from app.indicator_engine import IndicatorEngine
from app.knowledge_base import KnowledgeBase
from app.logger import engine_logger
from app.market_data import MarketDataCollector
from app.okx_cli import OKXClient
from app.risk_manager import RiskManager
from app.strategy_engine import StrategyEngine
from app.utils import clamp, safe_float


class AgentTradeKitApp:
    def __init__(self) -> None:
        self.client = OKXClient()
        self.market_collector = MarketDataCollector(self.client)
        self.indicator_engine = IndicatorEngine()
        self.kb = KnowledgeBase()
        self.risk_manager = RiskManager(self.kb.state, self.kb.completed_trades, self.kb.adaptive_params)
        self.strategy_engine = StrategyEngine(self.kb.weights, self.risk_manager)
        self.execution_engine = ExecutionEngine(self.client)

    @staticmethod
    def _normalize_position_direction(position: dict[str, Any]) -> str:
        pos = safe_float(position.get("pos"))
        if pos > 0:
            return "LONG"
        if pos < 0:
            return "SHORT"
        pos_side = str(position.get("posSide", "") or "").lower()
        if pos_side in {"long", "buy"}:
            return "LONG"
        if pos_side in {"short", "sell"}:
            return "SHORT"
        return "FLAT"

    @staticmethod
    def _normalize_action(action: Any) -> str:
        normalized = str(action or "SKIP").upper().strip()
        if normalized in {"OPEN_LONG", "OPEN_SHORT", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "SKIP"}:
            return normalized
        return "SKIP"

    def _position_map(self, positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        for position in positions:
            symbol = str(position.get("symbol", "") or "")
            if not symbol:
                continue
            mapped[symbol] = position
        return mapped

    def _get_account_context(self) -> dict[str, Any]:
        balances = self.client.get_account_balance()
        equity = 0.0
        avail = 0.0
        if balances and isinstance(balances[0], dict):
            equity = safe_float(balances[0].get("totalEq"))
            details = balances[0].get("details", [])
            for item in details:
                if item.get("ccy") == "USDT":
                    avail = safe_float(item.get("availBal"))
                    break

        raw_positions = self.client.get_positions()
        pos_list = []
        used_margin = 0.0
        for raw_position in raw_positions:
            if not isinstance(raw_position, dict):
                continue
            pos_size = safe_float(raw_position.get("pos"))
            if abs(pos_size) <= 0:
                continue
            margin_usdt = safe_float(
                raw_position.get("imr")
                or raw_position.get("margin")
                or raw_position.get("marginUsd")
                or raw_position.get("margin_usdt")
            )
            used_margin += margin_usdt
            pos_list.append(
                {
                    "symbol": raw_position.get("instId"),
                    "side": raw_position.get("posSide"),
                    "direction": self._normalize_position_direction(raw_position),
                    "pos": raw_position.get("pos"),
                    "avg_px": raw_position.get("avgPx"),
                    "upl": raw_position.get("upl"),
                    "upl_ratio": raw_position.get("uplRatio"),
                    "margin_usdt": margin_usdt,
                }
            )

        return {
            "equity": equity,
            "available": avail,
            "positions": pos_list,
            "used_margin": round(used_margin, 4),
        }

    def _remaining_margin_budget(self, account_context: dict[str, Any]) -> float:
        equity = safe_float(account_context.get("equity"))
        used_margin = safe_float(account_context.get("used_margin"))
        total_margin_cap = equity * settings.max_total_margin_ratio
        return max(0.0, total_margin_cap - used_margin)

    def _sync_open_campaigns(self, positions: list[dict[str, Any]]) -> None:
        live_symbols = {str(position.get("symbol")) for position in positions}
        campaigns = dict(self.kb.state.get("open_campaigns", {}))
        changed = False

        for symbol in list(campaigns.keys()):
            if symbol not in live_symbols:
                campaigns.pop(symbol, None)
                changed = True

        for position in positions:
            symbol = str(position.get("symbol"))
            campaigns[symbol] = {
                "symbol": symbol,
                "direction": position.get("direction"),
                "avg_px": safe_float(position.get("avg_px")),
                "margin_usdt": safe_float(position.get("margin_usdt")),
                "updated_at": datetime.utcnow().isoformat(),
                "source": "live_position_sync",
            }
            changed = True

        self.kb.state["open_campaigns"] = campaigns
        if changed:
            self.kb.save_state()

    def _register_open_campaign(self, symbol: str, action: str, decision: dict[str, Any], result: dict[str, Any]) -> None:
        campaigns = dict(self.kb.state.get("open_campaigns", {}))
        campaigns[symbol] = {
            "symbol": symbol,
            "direction": "LONG" if action == "OPEN_LONG" else "SHORT",
            "action": action,
            "decision": {
                "confidence_score": safe_float(decision.get("confidence_score")),
                "reasoning": str(decision.get("reasoning", "") or "")[:120],
                "position_pct": safe_float(decision.get("position_pct")),
                "leverage": int(safe_float(decision.get("leverage"), 1)),
                "stop_loss": safe_float(decision.get("stop_loss")),
                "take_profit_price": safe_float(decision.get("take_profit_price")),
                "trailing_callback": safe_float(decision.get("trailing_callback")),
                "active_px": safe_float(decision.get("active_px")),
            },
            "actual_entry_price": safe_float(result.get("actual_entry_price")),
            "actual_margin_usdt": safe_float(result.get("actual_margin_usdt")),
            "actual_risk_usdt": safe_float(result.get("actual_risk_usdt")),
            "entry_order_id": str(result.get("entry_order_id", "")),
            "opened_at": datetime.utcnow().isoformat(),
        }
        self.kb.state["open_campaigns"] = campaigns
        self.kb.save_state()

    def _clear_open_campaign(self, symbol: str) -> None:
        campaigns = dict(self.kb.state.get("open_campaigns", {}))
        if symbol in campaigns:
            campaigns.pop(symbol, None)
            self.kb.state["open_campaigns"] = campaigns
            self.kb.save_state()

    def _update_risk_state_from_equity(self, account_context: dict[str, Any]) -> None:
        equity = safe_float(account_context.get("equity"))
        if equity > 0:
            self.risk_manager.update_after_trade(current_equity=equity, realized_pnl=None)
            self.kb.save_state()

    def _log_ai_decisions(self, decisions: list[dict[str, Any]], equity: float) -> None:
        log_file = settings.ai_decision_log_file
        try:
            with open(log_file, "a", encoding="utf-8") as file:
                for decision in decisions:
                    action = self._normalize_action(decision.get("action", "SKIP"))
                    is_open = action in {"OPEN_LONG", "OPEN_SHORT"}
                    pos_pct = safe_float(decision.get("position_pct"), 0.0) if is_open else 0.0
                    leverage = int(safe_float(decision.get("leverage"), 0)) if is_open else 0
                    entry = {
                        "timestamp": datetime.utcnow().isoformat(),
                        "action": action,
                        "symbol": decision.get("symbol", "NONE"),
                        "confidence_score": round(safe_float(decision.get("confidence_score"), 0.0), 4),
                        "reasoning": str(decision.get("reasoning", "") or "")[:120],
                        "position_amount_usdt": round(equity * pos_pct, 2),
                        "leverage": leverage,
                    }
                    file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("写入决策日志失败: %s", exc)

        engine_logger.info("AI 多标的决策已记录，共 %s 条", len(decisions))

    def _persist_iteration(self, account_context: dict[str, Any], decisions: list[dict[str, Any]], execution_results: list[dict[str, Any]]) -> None:
        try:
            self.kb.append_iteration(
                {
                    "account_summary": {
                        "equity": round(safe_float(account_context.get("equity")), 4),
                        "available": round(safe_float(account_context.get("available")), 4),
                        "used_margin": round(safe_float(account_context.get("used_margin")), 4),
                    },
                    "decisions": decisions,
                    "execution_results": execution_results,
                }
            )
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("写入迭代记录失败: %s", exc)

    @contextmanager
    def _entry_lock(self, symbol: str) -> Iterator[TextIO | None]:
        lock_file = Path(settings.entry_lock_dir) / f"{symbol}.lock"
        handle = lock_file.open("a+", encoding="utf-8")
        acquired = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps({"symbol": symbol, "pid": "runtime", "ts": datetime.utcnow().isoformat()}, ensure_ascii=False))
                handle.flush()
                yield handle
            except BlockingIOError:
                yield None
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _should_close_decision(action: str, position: dict[str, Any] | None) -> bool:
        if position is None:
            return False
        direction = str(position.get("direction", "FLAT"))
        if action == "CLOSE":
            return True
        if action == "CLOSE_LONG":
            return direction == "LONG"
        if action == "CLOSE_SHORT":
            return direction == "SHORT"
        return False

    @staticmethod
    def _build_trailing_stop(decision: dict[str, Any]) -> dict[str, Any] | None:
        has_callback = decision.get("trailing_callback") is not None
        has_active = decision.get("active_px") is not None
        if not has_callback and not has_active:
            return None
        return {
            "callback_ratio": decision.get("trailing_callback"),
            "active_px": decision.get("active_px"),
        }

    def run_once(self, execute_orders: bool = False) -> None:
        engine_logger.info("开始新一轮 AI 决策循环...")
        execution_results: list[dict[str, Any]] = []

        try:
            raw_data = self.market_collector.collect()
            symbols_data = raw_data.get("symbols", {})
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("采集市场数据失败: %s", exc)
            return

        market_context = {}
        for symbol, data in symbols_data.items():
            klines = data.get("klines", {})
            indicators = {tf: self.indicator_engine.calculate(k) for tf, k in klines.items()}
            market_context[symbol] = {
                "funding_rate": safe_float(data.get("funding_rate")),
                "oi_change_rate": safe_float(data.get("oi_change_rate")),
                "obi": safe_float(data.get("obi")),
                "klines": klines,
                "indicators": indicators,
            }

        try:
            account_context = self._get_account_context()
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("获取账户信息失败: %s", exc)
            return

        self._update_risk_state_from_equity(account_context)
        self._sync_open_campaigns(account_context.get("positions", []))
        recent_trades = self.kb.completed_trades

        try:
            decisions = self.strategy_engine.get_ai_decisions(
                market_context=market_context,
                account_context=account_context,
                recent_trades=recent_trades,
            )
        except Exception as exc:  # noqa: BLE001
            engine_logger.error("AI 决策过程异常: %s", exc)
            decisions = [
                {
                    "action": "SKIP",
                    "symbol": symbol,
                    "confidence_score": 0.0,
                    "reasoning": str(exc),
                    "position_pct": 0.0,
                    "leverage": 1,
                }
                for symbol in settings.symbol_priority
            ]

        self._log_ai_decisions(decisions, account_context["equity"])

        if not execute_orders:
            engine_logger.info("未开启执行模式，跳过下单。")
            self._persist_iteration(account_context, decisions, execution_results)
            return

        position_map = self._position_map(account_context.get("positions", []))
        open_symbols = set(position_map.keys())
        blocked_open_symbols: set[str] = set()

        for decision in decisions:
            action = self._normalize_action(decision.get("action"))
            symbol = str(decision.get("symbol", "NONE"))
            if action not in {"CLOSE", "CLOSE_LONG", "CLOSE_SHORT"}:
                continue
            # open_symbols used only as a quick pre-filter; direction is authoritative from net position
            if symbol not in open_symbols:
                engine_logger.info("AI 建议平仓 %s 但当前无已知持仓，跳过。", symbol)
                continue
            net_pos = self.execution_engine.get_net_position(symbol)
            if abs(net_pos) <= 1e-8:
                engine_logger.info("AI 建议平仓 %s 但净仓位为零，跳过。", symbol)
                continue
            if action == "CLOSE_LONG" and net_pos < 0:
                engine_logger.warning("%s CLOSE_LONG 但实际为空仓(net_pos=%.6f)，跳过。", symbol, net_pos)
                continue
            if action == "CLOSE_SHORT" and net_pos > 0:
                engine_logger.warning("%s CLOSE_SHORT 但实际为多仓(net_pos=%.6f)，跳过。", symbol, net_pos)
                continue
            try:
                engine_logger.info("AI 建议平仓: action=%s symbol=%s net_pos=%.6f", action, symbol, net_pos)
                close_result = self.execution_engine.close_position(symbol)
                execution_results.append({"symbol": symbol, "action": action, "result": close_result})
                close_status = close_result.get("status", "")
                if close_status in {"close_unverified", "close_failed"}:
                    blocked_open_symbols.add(symbol)
                    engine_logger.warning("%s 平仓未验证成功(status=%s)，本轮阻止该标的开仓。", symbol, close_status)
                self._clear_open_campaign(symbol)
                account_context = self._get_account_context()
                self._update_risk_state_from_equity(account_context)
                self._sync_open_campaigns(account_context.get("positions", []))
                position_map = self._position_map(account_context.get("positions", []))
            except Exception as exc:  # noqa: BLE001
                engine_logger.error("执行平仓失败 %s: %s", symbol, exc)
                execution_results.append({"symbol": symbol, "action": action, "status": "failed", "reason": str(exc)})
                blocked_open_symbols.add(symbol)

        if self.risk_manager.should_stop_new_trades():
            engine_logger.warning("账户已触发熔断，停止新开仓。")
            self._persist_iteration(account_context, decisions, execution_results)
            return

        threshold = safe_float(settings.confidence_threshold_default)
        open_candidates = []
        for decision in decisions:
            action = self._normalize_action(decision.get("action"))
            symbol = str(decision.get("symbol", "NONE"))
            confidence = safe_float(decision.get("confidence_score"))
            if action not in {"OPEN_LONG", "OPEN_SHORT"}:
                continue
            if symbol == "NONE" or confidence < threshold:
                continue
            open_candidates.append(decision)

        open_candidates.sort(key=lambda item: safe_float(item.get("confidence_score")), reverse=True)

        if not open_candidates:
            engine_logger.info("没有达到阈值的开仓标的。")
            self._persist_iteration(account_context, decisions, execution_results)
            return

        for decision in open_candidates:
            if self.risk_manager.should_stop_new_trades():
                engine_logger.warning("开仓前检测到熔断已触发，终止后续开仓。")
                break

            account_context = self._get_account_context()
            self._update_risk_state_from_equity(account_context)
            self._sync_open_campaigns(account_context.get("positions", []))
            position_map = self._position_map(account_context.get("positions", []))
            remaining_margin_budget = self._remaining_margin_budget(account_context)

            if len(position_map) >= settings.max_concurrent_positions:
                engine_logger.info("当前持仓数已达到上限 %s，停止继续开仓。", settings.max_concurrent_positions)
                break

            action = self._normalize_action(decision.get("action"))
            symbol = str(decision.get("symbol", "NONE"))
            if symbol in position_map:
                engine_logger.info("%s 已有持仓，本轮跳过重复开仓。", symbol)
                continue

            if symbol in blocked_open_symbols:
                engine_logger.info("%s 本轮因平仓未验证成功而阻止开仓。", symbol)
                execution_results.append({"symbol": symbol, "action": action, "status": "skipped", "reason": "blocked_after_close"})
                continue

            if remaining_margin_budget < settings.min_order_margin_usdt:
                engine_logger.info("真实保证金额度不足，停止继续开仓。剩余额度=%.4f", remaining_margin_budget)
                break

            with self._entry_lock(symbol) as lock_handle:
                if lock_handle is None:
                    engine_logger.warning("%s 开仓锁已被其他实例占用，跳过本次信号。", symbol)
                    execution_results.append({"symbol": symbol, "action": action, "status": "skipped", "reason": "entry_lock_occupied"})
                    continue

                account_context = self._get_account_context()
                self._update_risk_state_from_equity(account_context)
                self._sync_open_campaigns(account_context.get("positions", []))
                position_map = self._position_map(account_context.get("positions", []))
                remaining_margin_budget = self._remaining_margin_budget(account_context)
                campaigns = self.kb.state.get("open_campaigns", {}) or {}

                if symbol in position_map or symbol in campaigns:
                    engine_logger.info("%s 已存在实时持仓或持久化战役锁，跳过重复开仓。", symbol)
                    execution_results.append({"symbol": symbol, "action": action, "status": "skipped", "reason": "campaign_or_position_exists"})
                    continue

                equity = safe_float(account_context.get("equity"))
                pos_pct = clamp(safe_float(decision.get("position_pct")), 0.0, settings.max_total_margin_ratio)
                requested_margin = equity * pos_pct
                margin = min(requested_margin, remaining_margin_budget)
                if margin < settings.min_order_margin_usdt:
                    engine_logger.warning("%s 可用保证金不足 %.2fU，跳过。", symbol, settings.min_order_margin_usdt)
                    execution_results.append({"symbol": symbol, "action": action, "status": "skipped", "reason": "margin_too_small"})
                    continue

                entry_px = safe_float(market_context.get(symbol, {}).get("indicators", {}).get("15M", {}).get("close"))
                sl_px = safe_float(decision.get("stop_loss"))
                if entry_px <= 0 or sl_px <= 0:
                    engine_logger.warning("%s 缺少有效入场价或止损价，跳过。", symbol)
                    execution_results.append({"symbol": symbol, "action": action, "status": "skipped", "reason": "invalid_entry_or_stop_loss"})
                    continue

                trailing = self._build_trailing_stop(decision)

                try:
                    engine_logger.info(
                        "执行 AI 开仓: action=%s symbol=%s requested_margin=%.4f remaining_budget=%.4f",
                        action,
                        symbol,
                        margin,
                        remaining_margin_budget,
                    )
                    result = self.execution_engine.execute_ai_open(
                        symbol=symbol,
                        action=action,
                        margin_usdt=margin,
                        leverage=int(safe_float(decision.get("leverage"), 5)),
                        entry_price=entry_px,
                        stop_loss_price=sl_px,
                        account_equity=equity,
                        trailing_stop=trailing,
                        take_profit_price=safe_float(decision.get("take_profit_price")) or None,
                    )
                    execution_results.append({"symbol": symbol, "action": action, "result": result})
                    engine_logger.info("%s 开仓结果: %s", symbol, result.get("status"))
                    if result.get("status") == "success":
                        self._register_open_campaign(symbol, action, decision, result)
                        account_context = self._get_account_context()
                        self._update_risk_state_from_equity(account_context)
                        self._sync_open_campaigns(account_context.get("positions", []))
                    else:
                        self._clear_open_campaign(symbol)
                except Exception as exc:  # noqa: BLE001
                    engine_logger.error("执行开仓失败 %s: %s", symbol, exc)
                    execution_results.append({"symbol": symbol, "action": action, "status": "failed", "reason": str(exc)})
                    self._clear_open_campaign(symbol)

        account_context = self._get_account_context()
        self._update_risk_state_from_equity(account_context)
        self._sync_open_campaigns(account_context.get("positions", []))
        self._persist_iteration(account_context, decisions, execution_results)
