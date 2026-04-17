from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.config import settings
from app.execution_engine import ExecutionEngine
from app.indicator_engine import IndicatorEngine
from app.knowledge_base import KnowledgeBase
from app.logger import engine_logger
from app.market_data import MarketDataCollector
from app.okx_cli import OKXClient
from app.risk_manager import RiskManager
from app.strategy_engine import StrategyEngine
from app.utils import safe_float


class AgentTradeKitApp:
    def __init__(self) -> None:
        self.client = OKXClient()
        self.market_collector = MarketDataCollector(self.client)
        self.indicator_engine = IndicatorEngine()
        self.kb = KnowledgeBase()
        self.risk_manager = RiskManager(self.kb.state, self.kb.completed_trades, self.kb.adaptive_params)
        self.strategy_engine = StrategyEngine(self.kb.weights, self.risk_manager)
        self.execution_engine = ExecutionEngine(self.client)

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
        for p in raw_positions:
            pos_size = abs(safe_float(p.get("pos")))
            if pos_size <= 0:
                continue
            margin_usdt = safe_float(p.get("imr") or p.get("margin") or p.get("marginUsd") or p.get("margin_usdt"))
            used_margin += margin_usdt
            pos_list.append(
                {
                    "symbol": p.get("instId"),
                    "side": p.get("posSide"),
                    "pos": p.get("pos"),
                    "avg_px": p.get("avgPx"),
                    "upl": p.get("upl"),
                    "upl_ratio": p.get("uplRatio"),
                    "margin_usdt": margin_usdt,
                }
            )

        return {
            "equity": equity,
            "available": avail,
            "positions": pos_list,
            "used_margin": round(used_margin, 4),
        }

    def _log_ai_decisions(self, decisions: list[dict[str, Any]], equity: float) -> None:
        log_file = settings.ai_decision_log_file
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                for decision in decisions:
                    action = str(decision.get("action", "SKIP")).upper()
                    is_open = action in ["OPEN_LONG", "OPEN_SHORT"]
                    pos_pct = safe_float(decision.get("position_pct"), 0.0) if is_open else 0.0
                    leverage = int(decision.get("leverage", 0)) if is_open else 0
                    entry = {
                        "timestamp": datetime.now().isoformat(),
                        "action": action,
                        "symbol": decision.get("symbol", "NONE"),
                        "confidence_score": round(safe_float(decision.get("confidence_score"), 0.0), 4),
                        "reasoning": str(decision.get("reasoning", "") or "")[:50],
                        "position_amount_usdt": round(equity * pos_pct, 2),
                        "leverage": leverage,
                        "pnl": 0.0 if not is_open else None,
                    }
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            engine_logger.error(f"写入决策日志失败: {e}")

        engine_logger.info(f"AI 多标的决策已记录，共 {len(decisions)} 条")

    def run_once(self, execute_orders: bool = False) -> None:
        engine_logger.info("开始新一轮 AI 决策循环...")

        try:
            raw_data = self.market_collector.collect()
            symbols_data = raw_data.get("symbols", {})
        except Exception as e:
            engine_logger.error(f"采集市场数据失败: {e}")
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
        except Exception as e:
            engine_logger.error(f"获取账户信息失败: {e}")
            return

        recent_trades = self.kb.completed_trades

        try:
            decisions = self.strategy_engine.get_ai_decisions(
                market_context=market_context,
                account_context=account_context,
                recent_trades=recent_trades,
            )
        except Exception as e:
            engine_logger.error(f"AI 决策过程异常: {e}")
            decisions = [
                {"action": "SKIP", "symbol": symbol, "confidence_score": 0.0, "reasoning": str(e), "position_pct": 0.0, "leverage": 1}
                for symbol in settings.symbol_priority
            ]

        self._log_ai_decisions(decisions, account_context["equity"])

        if not execute_orders:
            engine_logger.info("未开启执行模式，跳过下单。")
            return

        threshold = safe_float(settings.confidence_threshold_default)
        equity = safe_float(account_context.get("equity"))
        used_margin = safe_float(account_context.get("used_margin"))
        total_margin_cap = equity * 0.60
        remaining_margin_budget = max(0.0, total_margin_cap - used_margin)

        # 构建当前持仓映射：symbol -> side（"long"/"short"）
        open_positions: dict[str, str] = {}
        for p in account_context.get("positions", []):
            sym = str(p.get("symbol"))
            side = str(p.get("side", "")).lower()
            open_positions[sym] = side

        open_symbols = set(open_positions.keys())

        # 本轮因平仓验证失败而被阻止再次开仓的标的
        blocked_open_symbols: set[str] = set()

        # ── 1. 先处理平仓信号 ─────────────────────────────────────────
        for decision in decisions:
            action = str(decision.get("action", "SKIP")).upper()
            symbol = str(decision.get("symbol", "NONE"))

            # 标准化 CLOSE -> CLOSE_LONG / CLOSE_SHORT（根据当前持仓方向）
            if action == "CLOSE":
                pos_side = open_positions.get(symbol, "")
                if pos_side in ("long", "net"):
                    action = "CLOSE_LONG"
                elif pos_side == "short":
                    action = "CLOSE_SHORT"
                else:
                    engine_logger.info(
                        "收到 CLOSE 信号但 %s 无持仓记录（当前持仓方向=%r），跳过平仓",
                        symbol, pos_side,
                    )
                    continue

            if action in ("CLOSE_LONG", "CLOSE_SHORT") and symbol in open_symbols:
                try:
                    engine_logger.info("AI 建议平仓: %s（action=%s）", symbol, action)
                    self.execution_engine.close_position(symbol)
                    # 平仓后验证仓位是否归零
                    if not self.execution_engine.verify_position_closed(symbol):
                        engine_logger.error(
                            "平仓验证失败：%s 仓位未归零，本轮阻止该标的再次开仓", symbol
                        )
                        blocked_open_symbols.add(symbol)
                    else:
                        open_symbols.discard(symbol)
                        open_positions.pop(symbol, None)
                except Exception as e:
                    engine_logger.error("执行平仓失败 %s: %s", symbol, e)

        # ── 2. 收集开仓候选 ───────────────────────────────────────────
        open_candidates = []
        for decision in decisions:
            action = str(decision.get("action", "SKIP")).upper()
            symbol = str(decision.get("symbol", "NONE"))
            confidence = safe_float(decision.get("confidence_score"))
            if action not in ["OPEN_LONG", "OPEN_SHORT"]:
                continue
            if symbol == "NONE" or confidence < threshold:
                continue
            open_candidates.append(decision)

        open_candidates.sort(key=lambda x: safe_float(x.get("confidence_score")), reverse=True)

        if not open_candidates:
            engine_logger.info("没有达到阈值的开仓标的。")
            return

        for decision in open_candidates:
            action = str(decision.get("action", "SKIP")).upper()
            symbol = str(decision.get("symbol", "NONE"))

            if symbol in blocked_open_symbols:
                engine_logger.warning("%s 因平仓验证失败被本轮阻止开仓，跳过。", symbol)
                continue

            if symbol in open_symbols:
                engine_logger.info("%s 已有持仓，本轮跳过重复开仓。", symbol)
                continue

            if remaining_margin_budget < 10:
                engine_logger.info("总保证金额度已接近上限，停止继续开仓。")
                break

            pos_pct = clamp(safe_float(decision.get("position_pct")), 0.0, 0.60)
            requested_margin = equity * pos_pct
            margin = min(requested_margin, remaining_margin_budget)

            entry_px = safe_float(market_context.get(symbol, {}).get("indicators", {}).get("15M", {}).get("close"))
            sl_px = safe_float(decision.get("stop_loss"))
            if entry_px <= 0 or sl_px <= 0:
                engine_logger.warning("%s 缺少有效入场价或止损价，跳过。", symbol)
                continue

            risk_per_unit = abs(entry_px - sl_px) / entry_px
            if risk_per_unit <= 0:
                engine_logger.warning("%s 风险距离异常，跳过。", symbol)
                continue

            max_risk_margin = (equity * 0.015) / risk_per_unit  # 单笔风险上限 1.5%
            if margin > max_risk_margin:
                engine_logger.warning(
                    "%s 触发 1.5%% 风险保护，保证金从 %.2f 缩减至 %.2f",
                    symbol, margin, max_risk_margin,
                )
                margin = max_risk_margin

            if margin < 10:
                engine_logger.warning("%s 可用保证金不足 10U，跳过。", symbol)
                continue

            trailing = None
            if decision.get("trailing_callback"):
                trailing = {
                    "active_px": decision.get("active_px"),
                    "callback_ratio": decision.get("trailing_callback"),
                }

            try:
                engine_logger.info(
                    "执行 AI 开仓: %s %s 保证金:%.2f 剩余额度:%.2f",
                    action, symbol, margin, remaining_margin_budget,
                )
                result = self.execution_engine.execute_ai_open(
                    symbol=symbol,
                    action=action,
                    margin_usdt=margin,
                    leverage=int(decision.get("leverage", 5)),
                    entry_price=entry_px,
                    stop_loss_price=sl_px,
                    trailing_stop=trailing,
                    take_profit_price=decision.get("take_profit_price"),
                )
                engine_logger.info("%s 开仓结果: %s", symbol, result["status"])
                if result["status"] == "success":
                    remaining_margin_budget -= margin
                    open_symbols.add(symbol)
            except Exception as e:
                engine_logger.error("执行开仓失败 %s: %s", symbol, e)


def clamp(val, min_val, max_val):
    return max(min_val, min(val, max_val))
