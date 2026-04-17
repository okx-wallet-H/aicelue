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
        open_symbols = {str(p.get("symbol")) for p in account_context.get("positions", [])}

        for decision in decisions:
            action = str(decision.get("action", "SKIP")).upper()
            symbol = str(decision.get("symbol", "NONE"))
            if action == "CLOSE" and symbol in open_symbols:
                try:
                    engine_logger.info(f"AI 建议平仓: {symbol}")
                    self.execution_engine.close_position(symbol)
                except Exception as e:
                    engine_logger.error(f"执行平仓失败 {symbol}: {e}")

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
            if symbol in open_symbols:
                engine_logger.info(f"{symbol} 已有持仓，本轮跳过重复开仓。")
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
                engine_logger.warning(f"{symbol} 缺少有效入场价或止损价，跳过。")
                continue

            risk_per_unit = abs(entry_px - sl_px) / entry_px
            if risk_per_unit <= 0:
                engine_logger.warning(f"{symbol} 风险距离异常，跳过。")
                continue

            max_risk_margin = (equity * 0.02) / risk_per_unit
            if margin > max_risk_margin:
                engine_logger.warning(f"{symbol} 触发 2% 风险保护，保证金从 {margin:.2f} 缩减至 {max_risk_margin:.2f}")
                margin = max_risk_margin

            if margin < 10:
                engine_logger.warning(f"{symbol} 可用保证金不足 10U，跳过。")
                continue

            trailing = None
            if decision.get("trailing_callback"):
                trailing = {
                    "active_px": decision.get("active_px"),
                    "callback_ratio": decision.get("trailing_callback"),
                }

            try:
                engine_logger.info(f"执行 AI 开仓: {action} {symbol} 保证金:{margin:.2f} 剩余额度:{remaining_margin_budget:.2f}")
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
                engine_logger.info(f"{symbol} 开仓结果: {result['status']}")
                remaining_margin_budget -= margin
                open_symbols.add(symbol)
            except Exception as e:
                engine_logger.error(f"执行开仓失败 {symbol}: {e}")


def clamp(val, min_val, max_val):
    return max(min_val, min(val, max_val))
