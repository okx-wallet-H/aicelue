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

# 合法的 action 枚举集合
VALID_ACTIONS = frozenset({"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "CLOSE", "HOLD", "SKIP"})
CLOSE_ACTIONS = frozenset({"CLOSE", "CLOSE_LONG", "CLOSE_SHORT"})
OPEN_ACTIONS = frozenset({"OPEN_LONG", "OPEN_SHORT"})


def _normalize_action(raw: str) -> str:
    """将 LLM 输出的 action 规范化为合法枚举，未知值返回 SKIP。"""
    normalized = str(raw or "").strip().upper()
    if normalized in VALID_ACTIONS:
        return normalized
    engine_logger.warning("未知 action 值 '%s'，已规范化为 SKIP", raw)
    return "SKIP"


def _resolve_close_side(action: str, symbol: str, open_positions: dict[str, str]) -> str | None:
    """
    根据 action 和当前持仓确定实际平仓方向。
    CLOSE_LONG → 'long', CLOSE_SHORT → 'short', CLOSE → 根据实际持仓方向决定。
    若无持仓则返回 None（不操作）。
    """
    if action == "CLOSE_LONG":
        return "long"
    if action == "CLOSE_SHORT":
        return "short"
    if action == "CLOSE":
        return open_positions.get(symbol)  # None if no position
    return None


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
                    is_open = action in OPEN_ACTIONS
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

        # 交易开关检查
        if execute_orders and not settings.trading_enabled:
            engine_logger.warning(
                "已传入 --execute，但 TRADING_ENABLED 未设置为 true，拒绝下单。"
                "如需真实交易请在 .env 中设置 TRADING_ENABLED=true。"
            )
            execute_orders = False

        try:
            raw_data = self.market_collector.collect()
            symbols_data = raw_data.get("symbols", {})
        except Exception as e:
            engine_logger.error(f"采集市场数据失败: {e}")
            return

        market_context = {}
        for symbol, data in symbols_data.items():
            klines = data.get("klines", {})
            indicators = {}
            for tf, k in klines.items():
                try:
                    indicators[tf] = self.indicator_engine.calculate(k)
                except Exception as e:
                    engine_logger.warning(f"{symbol} 周期 {tf} 指标计算失败: {e}，使用空指标")
                    indicators[tf] = {}
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

        # 规范化 action 枚举，未知值 → SKIP
        for d in decisions:
            d["action"] = _normalize_action(d.get("action", "SKIP"))

        self._log_ai_decisions(decisions, account_context["equity"])

        if not execute_orders:
            engine_logger.info("未开启执行模式，跳过下单。")
            return

        # 获取当前持仓 map: symbol -> side (long/short/net)
        open_positions: dict[str, str] = {}
        for p in account_context.get("positions", []):
            sym = str(p.get("symbol", ""))
            side = str(p.get("side", ""))
            if sym and side:
                open_positions[sym] = side
        open_symbols = set(open_positions.keys())

        threshold = safe_float(settings.confidence_threshold_default)
        equity = safe_float(account_context.get("equity"))
        used_margin = safe_float(account_context.get("used_margin"))
        total_margin_cap = equity * settings.total_margin_cap_ratio
        remaining_margin_budget = max(0.0, total_margin_cap - used_margin)

        # 先处理平仓信号
        for decision in decisions:
            action = decision["action"]
            symbol = str(decision.get("symbol", "NONE"))
            if action not in CLOSE_ACTIONS:
                continue
            close_side = _resolve_close_side(action, symbol, open_positions)
            if close_side is None:
                engine_logger.info(f"[CLOSE] {symbol} 无持仓，跳过平仓。")
                continue
            try:
                engine_logger.info(f"AI 建议平仓: {action} {symbol} (当前方向: {close_side})")
                result = self.execution_engine.close_position(symbol)
                engine_logger.info(f"{symbol} 平仓结果: {result['status']}")
                open_symbols.discard(symbol)
                open_positions.pop(symbol, None)
            except Exception as e:
                engine_logger.error(f"执行平仓失败 {symbol}: {e}")

        # 校验并钳制 position_pct 总和 ≤ total_margin_cap_ratio
        open_candidates = []
        for decision in decisions:
            action = decision["action"]
            symbol = str(decision.get("symbol", "NONE"))
            confidence = safe_float(decision.get("confidence_score"))
            if action not in OPEN_ACTIONS:
                continue
            if symbol == "NONE" or confidence < threshold:
                continue
            open_candidates.append(decision)

        open_candidates.sort(key=lambda x: safe_float(x.get("confidence_score")), reverse=True)

        # 对 position_pct 总和做重归一化（如超出上限）
        total_pct = sum(safe_float(d.get("position_pct")) for d in open_candidates)
        if total_pct > settings.total_margin_cap_ratio and total_pct > 0:
            scale = settings.total_margin_cap_ratio / total_pct
            engine_logger.warning(
                "position_pct 总和 %.3f 超出上限 %.3f，按比例缩减（scale=%.3f）",
                total_pct, settings.total_margin_cap_ratio, scale
            )
            for d in open_candidates:
                d["position_pct"] = safe_float(d.get("position_pct")) * scale

        if not open_candidates:
            engine_logger.info("没有达到阈值的开仓标的。")
            return

        for decision in open_candidates:
            action = decision["action"]
            symbol = str(decision.get("symbol", "NONE"))
            if symbol in open_symbols:
                engine_logger.info(f"{symbol} 已有持仓，本轮跳过重复开仓。")
                continue

            if remaining_margin_budget < 10:
                engine_logger.info("总保证金额度已接近上限，停止继续开仓。")
                break

            pos_pct = clamp(safe_float(decision.get("position_pct")), 0.0, settings.total_margin_cap_ratio)
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

            max_risk_margin = (equity * settings.max_trade_risk_pct) / risk_per_unit
            if margin > max_risk_margin:
                engine_logger.warning(
                    f"{symbol} 触发单笔风险保护 ({settings.max_trade_risk_pct*100:.1f}%)，"
                    f"保证金从 {margin:.2f} 缩减至 {max_risk_margin:.2f}"
                )
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
                if result["status"] == "success":
                    remaining_margin_budget -= margin
                    open_symbols.add(symbol)
            except Exception as e:
                engine_logger.error(f"执行开仓失败 {symbol}: {e}")


def clamp(val, min_val, max_val):
    return max(min_val, min(val, max_val))
