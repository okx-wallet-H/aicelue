from __future__ import annotations

from typing import Any

from app.config import settings
from app.evolution import EvolutionEngine
from app.execution_engine import ExecutionEngine
from app.indicator_engine import IndicatorEngine
from app.knowledge_base import KnowledgeBase
from app.logger import engine_logger, iteration_logger, reasoning_logger, trades_logger
from app.market_data import MarketDataCollector
from app.market_state import MarketStateRecognizer
from app.okx_cli import OKXClient
from app.review import DailyReviewWriter
from app.risk_manager import RiskManager
from app.strategy_engine import StrategyEngine
from app.utils import now_ts_ms, safe_float


class AgentTradeKitApp:
    def __init__(self) -> None:
        self.client = OKXClient()
        self.market_collector = MarketDataCollector(self.client)
        self.indicator_engine = IndicatorEngine()
        self.state_recognizer = MarketStateRecognizer()
        self.kb = KnowledgeBase()
        self.risk_manager = RiskManager(self.kb.state, self.kb.completed_trades, self.kb.adaptive_params)
        self.strategy_engine = StrategyEngine(self.kb.weights, self.risk_manager)
        self.execution_engine = ExecutionEngine(self.client)
        self.evolution_engine = EvolutionEngine(self.kb.weights, self.kb.adaptive_params)
        self.review_writer = DailyReviewWriter(settings.review_dir)

    def _maybe_evolve(self) -> None:
        last_eval = int(self.kb.state.get("last_evaluation_ts", 0) or 0)
        now = now_ts_ms()
        if now - last_eval < settings.evaluation_interval_hours * 3600 * 1000:
            return
        iteration = self.evaluate_and_evolve()
        self.kb.state["last_evaluation_ts"] = now
        self.kb.save_state()
        iteration_logger.info("已完成4小时自我进化: %s", iteration)

    def _account_balance_detail(self) -> dict[str, Any]:
        balances = self.client.get_account_balance()
        if not balances or not isinstance(balances[0], dict):
            return {}
        details = balances[0].get("details", [])
        for item in details:
            if isinstance(item, dict) and item.get("ccy") == "USDT":
                return item
        return {}

    def _account_equity(self) -> float:
        detail = self._account_balance_detail()
        equity = safe_float(detail.get("eqUsd") or detail.get("eq"))
        if equity > 0:
            return equity
        balances = self.client.get_account_balance()
        if not balances:
            return 0.0
        return safe_float((balances[0] or {}).get("totalEq"))

    def _symbol_priority(self, symbol: str) -> int:
        try:
            return settings.symbol_priority.index(symbol)
        except ValueError:
            return len(settings.symbol_priority)

    def _symbol_capital_ratio(self, symbol: str) -> float:
        """按标的返回专属资金占比上限。"""
        if symbol == "BTC-USDT-SWAP":
            return settings.btc_weathervane_capital_ratio
        if symbol == "SOL-USDT-SWAP":
            return settings.sol_main_attack_capital_ratio
        return settings.max_single_symbol_margin_ratio

    def _total_capital_limit_ratio(self) -> float:
        """确保 BTC+SOL 组合资金分配与历史总仓位上限兼容。"""
        return max(
            settings.max_total_margin_ratio,
            settings.btc_weathervane_capital_ratio + settings.sol_main_attack_capital_ratio,
        )

    def _reserve_capital_ratio(self) -> float:
        """统一手续费缓冲与最低可用余额保留比例。"""
        return max(settings.min_available_balance_ratio, settings.fee_buffer_capital_ratio)

    def _margin_budget_snapshot(self, equity_usdt: float | None = None) -> dict[str, Any]:
        detail = self._account_balance_detail()
        equity = safe_float(equity_usdt) or safe_float(detail.get("eqUsd") or detail.get("eq")) or self._account_equity()
        avail_balance = safe_float(detail.get("availBal") or detail.get("availEq"))
        symbol_margin: dict[str, float] = {}
        total_margin = 0.0
        for pos in self.client.get_positions():
            if not isinstance(pos, dict) or abs(safe_float(pos.get("pos"))) <= 0:
                continue
            symbol = str(pos.get("instId") or "")
            margin = safe_float(pos.get("margin"))
            if margin <= 0:
                margin = safe_float(pos.get("imr"))
            if margin <= 0:
                continue
            symbol_margin[symbol] = round(symbol_margin.get(symbol, 0.0) + margin, 6)
            total_margin += margin
        return {
            "equity": round(max(equity, 0.0), 6),
            "avail_balance": round(max(avail_balance, 0.0), 6),
            "total_margin": round(max(total_margin, 0.0), 6),
            "symbol_margin": symbol_margin,
        }

    def _allowed_additional_margin(self, budget: dict[str, Any], symbol: str, requested_margin: float) -> float:
        equity = safe_float(budget.get("equity"))
        if equity <= 0:
            return 0.0
        avail_balance = safe_float(budget.get("avail_balance"))
        total_margin = safe_float(budget.get("total_margin"))
        symbol_margin = safe_float((budget.get("symbol_margin") or {}).get(symbol))
        per_symbol_limit = equity * self._symbol_capital_ratio(symbol)
        total_limit = equity * self._total_capital_limit_ratio()
        reserve_floor = equity * self._reserve_capital_ratio()
        safety_buffer = settings.order_margin_safety_buffer_usdt
        allowed = min(
            max(requested_margin, 0.0),
            max(per_symbol_limit - symbol_margin, 0.0),
            max(total_limit - total_margin, 0.0),
            max(avail_balance - reserve_floor - safety_buffer, 0.0),
        )
        return round(max(allowed, 0.0), 6)

    def _consume_margin_budget(self, budget: dict[str, Any], symbol: str, used_margin: float) -> None:
        used_margin = max(safe_float(used_margin), 0.0)
        if used_margin <= 0:
            return
        symbol_margin = budget.setdefault("symbol_margin", {})
        symbol_margin[symbol] = round(safe_float(symbol_margin.get(symbol)) + used_margin, 6)
        budget["total_margin"] = round(safe_float(budget.get("total_margin")) + used_margin, 6)
        budget["avail_balance"] = round(max(safe_float(budget.get("avail_balance")) - used_margin, 0.0), 6)

    def _order_entry_succeeded(self, result: dict[str, Any] | None) -> bool:
        return bool(self._success_rows((result or {}).get("entry")))

    def _symbol_position_info(self, symbol: str) -> dict[str, Any]:
        positions = self.client.get_positions(symbol)
        if not positions:
            return {"symbol": symbol, "net_pos": 0.0, "avg_px": 0.0, "mark_px": 0.0, "upl_ratio": 0.0}
        active = max(
            [pos for pos in positions if isinstance(pos, dict)],
            key=lambda item: abs(safe_float(item.get("pos"))),
            default={},
        )
        pos = active or {}
        return {
            "symbol": symbol,
            "net_pos": safe_float(pos.get("pos")),
            "avg_px": safe_float(pos.get("avgPx")),
            "mark_px": safe_float(pos.get("markPx") or pos.get("last")),
            "upl_ratio": safe_float(pos.get("uplRatio") or pos.get("uplRatioLastPx")),
            "margin": safe_float(pos.get("margin") or pos.get("imr")),
        }

    def _active_campaign(self, symbol: str) -> dict[str, Any] | None:
        campaign = self.kb.state.setdefault("open_campaigns", {}).get(symbol)
        return campaign if isinstance(campaign, dict) else None

    def _build_indicators(self, snapshot: dict[str, Any]) -> dict[str, dict[str, float]]:
        return {tf: self.indicator_engine.calculate(candles) for tf, candles in snapshot["klines"].items()}

    @staticmethod
    def _btc_turn_alert_message(previous_status: str, current_status: str) -> str | None:
        prev = str(previous_status or "NEUTRAL").upper()
        curr = str(current_status or "NEUTRAL").upper()
        if prev == curr:
            return None

        label_map = {
            "BULLISH": "多头",
            "BEARISH": "空头",
            "NEUTRAL": "震荡",
        }
        prev_label = label_map.get(prev, prev)

        if curr == "BEARISH":
            return f"[BTC风向标] 警告：BTC趋势由{prev_label}转向空头 → BTC风向标转向[空头]，SOL多单风控升级，止盈收紧至1R，暂停加仓"
        if curr == "BULLISH":
            return f"[BTC风向标] 警告：BTC趋势由{prev_label}转向多头 → BTC风向标转向[多头]，SOL空单风控升级，止盈收紧至1R，暂停加仓"
        return f"[BTC风向标] 提示：BTC趋势由{prev_label}转为震荡 → SOL恢复常规决策，不做额外方向倾斜"

    def _pick_best_knife_candidate(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [r for r in records if bool(r.get("knife_attack_eligible")) and r.get("action") in {"OPEN_LONG", "OPEN_SHORT"}]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                safe_float(item.get("attack_score")),
                safe_float(item.get("weighted_score")),
                -self._symbol_priority(str(item.get("symbol") or "")),
            ),
        )

    @staticmethod
    def _success_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [row for row in (rows or []) if isinstance(row, dict) and str(row.get("sCode", "")) == "0"]

    def _extract_order_ids(self, result: dict[str, Any] | None) -> list[str]:
        if not isinstance(result, dict):
            return []
        ids: list[str] = []
        for row in self._success_rows(result.get("entry")):
            ord_id = row.get("ordId") or row.get("algoId") or row.get("clOrdId")
            if ord_id:
                ids.append(str(ord_id))
        return ids

    def _extract_algo_ids(self, result: dict[str, Any] | None) -> list[str]:
        if not isinstance(result, dict):
            return []
        ids: list[str] = []
        for row in self._success_rows(result.get("algo")):
            algo_id = row.get("algoId") or row.get("ordId")
            if algo_id:
                ids.append(str(algo_id))
        return ids

    def _register_open_campaign(self, record: dict[str, Any]) -> None:
        regular_order_ids = self._extract_order_ids(record.get("order_result"))
        knife_order_ids = self._extract_order_ids(record.get("knife_attack_result"))
        all_order_ids = regular_order_ids + knife_order_ids
        if not all_order_ids:
            return

        symbol = str(record["symbol"])
        existing = self._active_campaign(symbol)
        if existing and str(existing.get("side")) == str(record.get("side")):
            trades_logger.warning("%s 已存在同向开放战役，跳过重复登记。现有战役=%s", symbol, existing)
            self.kb.update_record(str(record.get("record_id")), {"trade_status": "duplicate_open_skipped", "entry_order_ids": all_order_ids})
            return

        campaign = {
            "record_id": record.get("record_id"),
            "symbol": symbol,
            "side": record.get("side"),
            "opened_at": record.get("timestamp", now_ts_ms()),
            "market_state": record.get("market_state"),
            "signal_grade": record.get("signal_grade"),
            "attack_score": record.get("attack_score"),
            "weighted_score": record.get("weighted_score"),
            "confidence_threshold": record.get("confidence_threshold"),
            "position_ratio": record.get("position_ratio"),
            "leverage": record.get("leverage"),
            "td_mode": record.get("td_mode", "isolated"),
            "sub_strategy_scores": record.get("sub_strategy_scores") or {},
            "reasoning": record.get("reasoning") or {},
            "llm_analysis": record.get("llm_analysis") or {},
            "llm_adjustment": record.get("llm_adjustment") or {},
            "requested_tag": "agentTradeKit",
            "stop_loss_pct": safe_float((record.get("order_result") or {}).get("stop_loss_pct")),
            "take_profit_pct": safe_float((record.get("order_result") or {}).get("take_profit_pct")),
            "entry_order_ids": all_order_ids,
            "algo_ids": self._extract_algo_ids(record.get("order_result")) + self._extract_algo_ids(record.get("knife_attack_result")),
            "entry_fills": [],
            "exit_fills": [],
            "entry_size": 0.0,
            "exit_size": 0.0,
            "entry_notional": 0.0,
            "exit_notional": 0.0,
            "realized_pnl": 0.0,
            "fees": 0.0,
            "close_reason": None,
            "close_order_ids": [],
            "knife_attack_selected": bool(record.get("knife_attack_selected")),
        }
        self.kb.state.setdefault("open_campaigns", {})[symbol] = campaign
        self.kb.update_record(str(record.get("record_id")), {"trade_status": "open", "entry_order_ids": all_order_ids})
        self.kb.save_state()
        trades_logger.info("已登记开放战役 %s: %s", symbol, campaign)

    def _normalize_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        fill_ts = int(safe_float(fill.get("fillTime") or fill.get("ts") or now_ts_ms()))
        fill_id = str(fill.get("fillId") or fill.get("tradeId") or fill.get("billId") or f"{fill.get('ordId', 'unknown')}_{fill_ts}")
        return {
            "fill_id": fill_id,
            "ord_id": str(fill.get("ordId") or ""),
            "symbol": fill.get("instId") or fill.get("inst_id"),
            "side": str(fill.get("side") or "").lower(),
            "size": safe_float(fill.get("fillSz") or fill.get("sz")),
            "price": safe_float(fill.get("fillPx") or fill.get("px") or fill.get("avgPx")),
            "fee": safe_float(fill.get("fee")),
            "realized_pnl": safe_float(fill.get("fillPnl") or fill.get("pnl")),
            "timestamp": fill_ts,
            "exchange_tag": str(fill.get("tag") or ""),
            "raw": fill,
        }

    def _infer_close_reason(self, campaign: dict[str, Any], entry_avg_px: float, exit_avg_px: float) -> str:
        side = str(campaign.get("side") or "")
        sl_pct = safe_float(campaign.get("stop_loss_pct"))
        tp_pct = safe_float(campaign.get("take_profit_pct"))
        if entry_avg_px <= 0 or exit_avg_px <= 0:
            return "unknown"
        if side == "buy":
            stop_trigger = entry_avg_px * (1 - sl_pct)
            tp_trigger = entry_avg_px * (1 + tp_pct)
            if sl_pct > 0 and exit_avg_px <= stop_trigger * 1.002:
                return "stop_loss"
            if tp_pct > 0 and exit_avg_px >= tp_trigger * 0.998:
                return "take_profit"
        elif side == "sell":
            stop_trigger = entry_avg_px * (1 + sl_pct)
            tp_trigger = entry_avg_px * (1 - tp_pct)
            if sl_pct > 0 and exit_avg_px >= stop_trigger * 0.998:
                return "stop_loss"
            if tp_pct > 0 and exit_avg_px <= tp_trigger * 1.002:
                return "take_profit"
        return "signal_exit"

    def _finalize_campaign(self, symbol: str, campaign: dict[str, Any]) -> dict[str, Any] | None:
        if safe_float(campaign.get("entry_size")) <= 0 or safe_float(campaign.get("exit_size")) <= 0:
            return None
        entry_avg_px = safe_float(campaign.get("entry_notional")) / max(safe_float(campaign.get("entry_size")), 1e-9)
        exit_avg_px = safe_float(campaign.get("exit_notional")) / max(safe_float(campaign.get("exit_size")), 1e-9)
        realized_pnl = safe_float(campaign.get("realized_pnl")) + safe_float(campaign.get("fees"))
        closed_at = max([campaign.get("opened_at", 0)] + [fill.get("timestamp", 0) for fill in campaign.get("exit_fills", [])])
        completed_trade = {
            "record_id": campaign.get("record_id"),
            "symbol": symbol,
            "side": campaign.get("side"),
            "opened_at": campaign.get("opened_at"),
            "closed_at": closed_at,
            "entry_avg_px": round(entry_avg_px, 6),
            "exit_avg_px": round(exit_avg_px, 6),
            "entry_size": round(safe_float(campaign.get("entry_size")), 6),
            "exit_size": round(safe_float(campaign.get("exit_size")), 6),
            "realized_pnl": round(realized_pnl, 6),
            "fees": round(safe_float(campaign.get("fees")), 6),
            "close_reason": campaign.get("close_reason") or self._infer_close_reason(campaign, entry_avg_px, exit_avg_px),
            "market_state": campaign.get("market_state"),
            "signal_grade": campaign.get("signal_grade"),
            "attack_score": campaign.get("attack_score"),
            "weighted_score": campaign.get("weighted_score"),
            "confidence_threshold": campaign.get("confidence_threshold"),
            "position_ratio": campaign.get("position_ratio"),
            "leverage": campaign.get("leverage"),
            "td_mode": campaign.get("td_mode", "isolated"),
            "sub_strategy_scores": campaign.get("sub_strategy_scores") or {},
            "reasoning": campaign.get("reasoning") or {},
            "llm_analysis": campaign.get("llm_analysis") or {},
            "llm_adjustment": campaign.get("llm_adjustment") or {},
            "entry_order_ids": campaign.get("entry_order_ids") or [],
            "algo_ids": campaign.get("algo_ids") or [],
            "entry_fills": campaign.get("entry_fills") or [],
            "exit_fills": campaign.get("exit_fills") or [],
            "knife_attack_selected": bool(campaign.get("knife_attack_selected")),
        }
        self.kb.append_completed_trade(completed_trade)
        if campaign.get("record_id"):
            self.kb.update_record(
                str(campaign.get("record_id")),
                {
                    "trade_status": "closed",
                    "realized_pnl": completed_trade["realized_pnl"],
                    "close_reason": completed_trade["close_reason"],
                    "closed_at": completed_trade["closed_at"],
                },
            )
        equity = self._account_equity()
        self.risk_manager.update_after_trade(current_equity=equity, realized_pnl=completed_trade["realized_pnl"])
        self.kb.state["last_trade_review_ts"] = closed_at
        self.kb.state.setdefault("open_campaigns", {}).pop(symbol, None)
        self.kb.save_state()
        trades_logger.info("已完成交易复盘 %s: %s", symbol, completed_trade)
        return completed_trade

    def _sync_completed_trades(self) -> list[dict[str, Any]]:
        closed_trades: list[dict[str, Any]] = []
        open_campaigns = self.kb.state.setdefault("open_campaigns", {})
        processed_fill_ids = set(self.kb.state.get("processed_fill_ids", []))

        for symbol in settings.symbols:
            campaign = open_campaigns.get(symbol)
            if not campaign:
                continue
            fills = self.client.get_fills(symbol, limit=settings.fills_scan_limit)
            normalized = [self._normalize_fill(fill) for fill in fills if isinstance(fill, dict)]
            normalized.sort(key=lambda item: item["timestamp"])

            for fill in normalized:
                if fill["fill_id"] in processed_fill_ids:
                    continue
                if fill["timestamp"] + 60_000 < int(campaign.get("opened_at", 0)):
                    continue
                # _normalize_fill 已经处理了 fillId/tradeId 的兼容性，这里直接使用 fill_id
                processed_fill_ids.add(fill["fill_id"])
                if fill["side"] == campaign.get("side"):
                    campaign.setdefault("entry_fills", []).append(fill)
                    campaign["entry_size"] = safe_float(campaign.get("entry_size")) + fill["size"]
                    campaign["entry_notional"] = safe_float(campaign.get("entry_notional")) + fill["size"] * fill["price"]
                else:
                    campaign.setdefault("exit_fills", []).append(fill)
                    campaign["exit_size"] = safe_float(campaign.get("exit_size")) + fill["size"]
                    campaign["exit_notional"] = safe_float(campaign.get("exit_notional")) + fill["size"] * fill["price"]
                    campaign["realized_pnl"] = safe_float(campaign.get("realized_pnl")) + fill["realized_pnl"]
                campaign["fees"] = safe_float(campaign.get("fees")) + fill["fee"]

            current_pos = abs(self.execution_engine.get_net_position(symbol))
            if current_pos <= 1e-8 and safe_float(campaign.get("exit_size")) > 0:
                completed_trade = self._finalize_campaign(symbol, campaign)
                if completed_trade:
                    closed_trades.append(completed_trade)

        self.kb.state["processed_fill_ids"] = list(processed_fill_ids)[-5000:]
        self.kb.save_state()
        if closed_trades:
            self.review_writer.write(self.kb.knowledge_records, self.kb.completed_trades, self.kb.adaptive_params, self.kb.iteration_history)
        return closed_trades

    def run_once(self, execute_orders: bool = False) -> list[dict[str, Any]]:
        self._sync_completed_trades()
        equity = self._account_equity()
        self.risk_manager.update_after_trade(current_equity=equity, realized_pnl=None)
        market_data = self.market_collector.collect()
        decisions: list[dict[str, Any]] = []
        runtime_records: list[dict[str, Any]] = []

        btc_weathervane = {
            "status": "NEUTRAL",
            "trend": "震荡",
            "reason": "[BTC风向标] BTC 行情缺失，按中性处理，不影响 SOL 正常决策。",
        }
        btc_turn_alert = None
        btc_snapshot = (market_data.get("symbols") or {}).get("BTC-USDT-SWAP")
        btc_tf_indicators: dict[str, dict[str, float]] | None = None
        btc_market_state: dict[str, Any] | None = None

        if isinstance(btc_snapshot, dict):
            btc_tf_indicators = self._build_indicators(btc_snapshot)
            btc_market_state = self.state_recognizer.recognize(
                symbol="BTC-USDT-SWAP",
                tf_indicators=btc_tf_indicators,
                funding_rate=float(btc_snapshot["funding_rate"]),
                oi_change_rate=float(btc_snapshot["oi_change_rate"]),
            )
            btc_weathervane = self.strategy_engine._btc_weathervane_signal(btc_tf_indicators)

        previous_btc_status = str(self.kb.state.get("btc_weathervane_status") or "NEUTRAL").upper()
        current_btc_status = str(btc_weathervane.get("status") or "NEUTRAL").upper()
        btc_turn_alert = self._btc_turn_alert_message(previous_btc_status, current_btc_status)
        self.kb.state["btc_weathervane_status"] = current_btc_status
        self.kb.state["btc_weathervane_snapshot"] = btc_weathervane

        if btc_turn_alert:
            sol_position = self._symbol_position_info("SOL-USDT-SWAP")
            if abs(safe_float(sol_position.get("net_pos"))) > 0:
                engine_logger.warning(
                    "%s 当前SOL净持仓=%.4f，浮盈率=%.4f。",
                    btc_turn_alert,
                    safe_float(sol_position.get("net_pos")),
                    safe_float(sol_position.get("upl_ratio")),
                )
            else:
                engine_logger.warning("%s 当前SOL无持仓，后续信号将按升级风控处理。", btc_turn_alert)

        recent_records_window = int(getattr(settings, "llm_recent_record_window", 12))
        recent_trades_window = int(getattr(settings, "llm_recent_trade_window", 20))
        recent_records = self.kb.knowledge_records[-recent_records_window:]
        recent_trades = self.kb.completed_trades[-recent_trades_window:]
        positions_context: dict[str, dict[str, Any]] = {}
        prepared_runtime_records: list[dict[str, Any]] = []
        llm_market_context: dict[str, dict[str, Any]] = {}

        for symbol, snapshot in market_data["symbols"].items():
            if symbol == "BTC-USDT-SWAP" and btc_tf_indicators and btc_market_state:
                tf_indicators = btc_tf_indicators
                market_state = btc_market_state
            else:
                tf_indicators = self._build_indicators(snapshot)
                market_state = self.state_recognizer.recognize(
                    symbol=symbol,
                    tf_indicators=tf_indicators,
                    funding_rate=float(snapshot["funding_rate"]),
                    oi_change_rate=float(snapshot["oi_change_rate"]),
                )
            position_info = self._symbol_position_info(symbol)
            positions_context[symbol] = position_info
            llm_market_context[symbol] = {
                "snapshot": snapshot,
                "tf_indicators": tf_indicators,
                "market_state": market_state,
            }
            prepared_runtime_records.append(
                {
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "tf_indicators": tf_indicators,
                    "market_state": market_state,
                    "position_info": position_info,
                }
            )

        for item in prepared_runtime_records:
            symbol = item["symbol"]
            snapshot = item["snapshot"]
            tf_indicators = item["tf_indicators"]
            market_state = item["market_state"]
            position_info = item["position_info"]
            decision = self.strategy_engine.decide(
                snapshot,
                tf_indicators,
                market_state,
                position_info=position_info,
                btc_weathervane=btc_weathervane,
                btc_turn_alert=btc_turn_alert if symbol == "SOL-USDT-SWAP" else None,
                market_context=llm_market_context,
                positions=positions_context,
                recent_records=recent_records,
                recent_trades=recent_trades,
            )
            reasoning_text = decision["reasoning"].to_markdown()
            reasoning_logger.info("%s\n%s", symbol, reasoning_text)

            record = {
                "timestamp": now_ts_ms(),
                "symbol": symbol,
                "market_state": market_state["overall_state"],
                "action": decision["action"],
                "side": decision["side"],
                "position_ratio": decision["position_ratio"],
                "leverage": decision["leverage"],
                "td_mode": decision.get("td_mode", "isolated"),
                "funding_rate": snapshot["funding_rate"],
                "oi_change_rate": snapshot["oi_change_rate"],
                "obi": snapshot["obi"],
                "weighted_score": decision["weighted_score"],
                "confidence_threshold": decision.get("confidence_threshold"),
                "signal_grade": decision.get("signal_grade", "C"),
                "attack_score": decision.get("attack_score", 0.0),
                "knife_attack_eligible": decision.get("knife_attack_eligible", False),
                "sub_strategy_scores": decision["sub_strategy_scores"],
                "reasoning": decision["reasoning"].to_dict(),
                "llm_analysis": decision.get("llm_analysis") or {},
                "llm_adjustment": decision.get("llm_adjustment") or {},
                "position_snapshot": decision.get("position_snapshot") or position_info,
                "btc_weathervane": decision.get("btc_weathervane") or btc_weathervane,
                "btc_turn_alert": btc_turn_alert if symbol == "SOL-USDT-SWAP" else None,
                "risk_overrides": {
                    "tight_take_profit_to_1r": bool(decision.get("tight_take_profit_to_1r")),
                    "pause_add_position": bool(decision.get("pause_add_position")),
                },
                "trade_status": "pending",
                "realized_pnl": None,
                "order_result": {"entry": [], "algo": []},
                "knife_attack_result": None,
            }

            runtime_records.append(
                {
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "tf_indicators": tf_indicators,
                    "market_state": market_state,
                    "decision": decision,
                    "record": record,
                }
            )

        budget_snapshot = self._margin_budget_snapshot(equity_usdt=equity)
        engine_logger.info(
            "资金分配限制已加载: BTC风向标仓位=%.0f%%, SOL主攻仓位=%.0f%%, 手续费缓冲=%.0f%%, 总保证金上限=%.0f%%, 优先级=%s, 当前权益=%.2f, 当前可用=%.2f, 当前总保证金=%.2f, 当前分标的=%s",
            settings.btc_weathervane_capital_ratio * 100,
            settings.sol_main_attack_capital_ratio * 100,
            self._reserve_capital_ratio() * 100,
            self._total_capital_limit_ratio() * 100,
            settings.symbol_priority,
            budget_snapshot["equity"],
            budget_snapshot["avail_balance"],
            budget_snapshot["total_margin"],
            budget_snapshot["symbol_margin"],
        )
        knife_candidate = self._pick_best_knife_candidate([item["record"] for item in runtime_records])
        knife_symbol = knife_candidate["symbol"] if knife_candidate else None
        runtime_records.sort(key=lambda item: self._symbol_priority(str(item["symbol"])))

        for item in runtime_records:
            symbol = item["symbol"]
            tf_indicators = item["tf_indicators"]
            market_state = item["market_state"]
            decision = item["decision"]
            record = item["record"]

            if execute_orders and decision["action"] in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}:
                if decision["action"] in {"CLOSE_LONG", "CLOSE_SHORT"}:
                    close_result = self.execution_engine.close_position(
                        symbol=symbol,
                        td_mode=str(decision.get("td_mode", "isolated")),
                        reason="signal_exit",
                    )
                    record["order_result"] = close_result
                    record["trade_status"] = "close_sent" if self._order_entry_succeeded(close_result) else "close_failed"
                    active_campaign = self._active_campaign(symbol)
                    if active_campaign and self._order_entry_succeeded(close_result):
                        active_campaign["close_reason"] = "signal_exit"
                        active_campaign["close_order_ids"] = self._extract_order_ids(close_result)
                        self.kb.save_state()
                    engine_logger.info("%s 主动平仓完成: %s", symbol, close_result)
                    record["knife_attack_selected"] = False
                else:
                    entry_price = tf_indicators["15M"]["close"]
                    stop_loss_pct = self.risk_manager.adaptive_stop_loss_pct(
                        symbol=symbol,
                        market_state=market_state["overall_state"],
                        atr_change_rate=float(market_state["atr_change_rate"]),
                    )
                    take_profit_pct = round(max(stop_loss_pct * 1.8, stop_loss_pct + 0.005), 4)
                    if bool(decision.get("tight_take_profit_to_1r")):
                        take_profit_pct = round(stop_loss_pct, 4)
                    requested_margin = round(equity * safe_float(decision["position_ratio"]), 6)
                    allowed_margin = self._allowed_additional_margin(budget_snapshot, symbol, requested_margin)
                    effective_position_ratio = 0.0 if equity <= 0 else allowed_margin / equity
                    record["budget_guard"] = {
                        "requested_margin": requested_margin,
                        "allowed_margin": round(allowed_margin, 6),
                        "equity": budget_snapshot["equity"],
                        "avail_balance_before": budget_snapshot["avail_balance"],
                        "total_margin_before": budget_snapshot["total_margin"],
                        "symbol_margin_before": safe_float(budget_snapshot["symbol_margin"].get(symbol)),
                        "per_symbol_limit": round(budget_snapshot["equity"] * self._symbol_capital_ratio(symbol), 6),
                        "total_limit": round(budget_snapshot["equity"] * self._total_capital_limit_ratio(), 6),
                        "reserve_floor": round(budget_snapshot["equity"] * self._reserve_capital_ratio(), 6),
                        "safety_buffer": settings.order_margin_safety_buffer_usdt,
                    }
                    minimum_required_margin = max(equity * settings.min_position_ratio_initial, 5.0)
                    if allowed_margin < minimum_required_margin:
                        record["trade_status"] = "skipped_budget_guard"
                        engine_logger.warning(
                            "%s 因资金分配限制跳过开仓: 请求保证金=%.2f, 允许保证金=%.2f, 标的资金上限=%.2f, 总保证金上限=%.2f, 手续费缓冲保留=%.2f, 安全缓冲=%.2f",
                            symbol,
                            requested_margin,
                            allowed_margin,
                            budget_snapshot["equity"] * self._symbol_capital_ratio(symbol),
                            budget_snapshot["equity"] * self._total_capital_limit_ratio(),
                            budget_snapshot["equity"] * self._reserve_capital_ratio(),
                            settings.order_margin_safety_buffer_usdt,
                        )
                    else:
                        size = self.execution_engine.estimate_order_size(
                            symbol=symbol,
                            equity_usdt=equity,
                            price=entry_price,
                            leverage=decision["leverage"],
                            position_ratio=effective_position_ratio,
                        )
                        record["position_ratio"] = round(effective_position_ratio, 6)
                        if size > 0:
                            order_result = self.execution_engine.place_entry_with_tpsl(
                                symbol=symbol,
                                side=str(decision["side"]),
                                size=size,
                                entry_price=entry_price,
                                stop_loss_pct=stop_loss_pct,
                                take_profit_pct=take_profit_pct,
                                td_mode=str(decision.get("td_mode", "isolated")),
                                leverage=int(decision["leverage"]),
                            )
                            record["order_result"] = order_result
                            if self._order_entry_succeeded(order_result):
                                self._consume_margin_budget(budget_snapshot, symbol, allowed_margin)
                                record["trade_status"] = "open_sent"
                            else:
                                record["trade_status"] = "open_failed"
                            engine_logger.info("%s 常规仓下单完成: %s", symbol, order_result)

                    if knife_symbol == symbol and settings.knife_attack_enabled and record["trade_status"] == "open_sent":
                        knife_requested_margin = settings.knife_attack_margin_usdt
                        knife_allowed_margin = self._allowed_additional_margin(budget_snapshot, symbol, knife_requested_margin)
                        record["budget_guard"]["knife_requested_margin"] = knife_requested_margin
                        record["budget_guard"]["knife_allowed_margin"] = knife_allowed_margin
                        if knife_allowed_margin + 1e-9 >= knife_requested_margin:
                            knife_result = self.execution_engine.place_knife_attack(
                                symbol=symbol,
                                side=str(decision["side"]),
                                entry_price=entry_price,
                                margin_usdt=knife_requested_margin,
                                leverage=settings.knife_attack_leverage,
                                stop_loss_pct=min(stop_loss_pct, settings.knife_attack_stop_loss_pct),
                                take_profit_pct=settings.knife_attack_take_profit_pct,
                            )
                            record["knife_attack_result"] = knife_result
                            record["knife_attack_selected"] = True
                            if self._order_entry_succeeded(knife_result):
                                self._consume_margin_budget(budget_snapshot, symbol, knife_requested_margin)
                            engine_logger.info("%s 尖刀连下单完成: %s", symbol, knife_result)
                        else:
                            record["knife_attack_selected"] = False
                            engine_logger.warning(
                                "%s 尖刀连因资金分配限制跳过: 请求保证金=%.2f, 允许保证金=%.2f",
                                symbol,
                                knife_requested_margin,
                                knife_allowed_margin,
                            )
                    else:
                        record["knife_attack_selected"] = False
            else:
                record["knife_attack_selected"] = knife_symbol == symbol and decision["action"] in {"OPEN_LONG", "OPEN_SHORT"}

            stored_record = self.kb.append_record(record)
            if execute_orders and decision["action"] in {"OPEN_LONG", "OPEN_SHORT"} and stored_record.get("trade_status") == "open_sent":
                self._register_open_campaign(stored_record)
            decisions.append(stored_record)

        self._sync_completed_trades()
        self._maybe_evolve()
        self.kb.save_state()
        self.kb.save_weights()
        self.kb.save_adaptive_params()
        return decisions

    def evaluate_and_evolve(self) -> dict[str, Any]:
        iteration = self.evolution_engine.update(self.kb.completed_trades)
        self.kb.save_weights()
        self.kb.save_adaptive_params()
        self.kb.append_iteration(iteration)
        self.review_writer.write(self.kb.knowledge_records, self.kb.completed_trades, self.kb.adaptive_params, self.kb.iteration_history)
        return iteration


if __name__ == "__main__":
    app = AgentTradeKitApp()
    result = app.run_once(execute_orders=True)
    engine_logger.info("单次运行完成，决策数=%s", len(result))
