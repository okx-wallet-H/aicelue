from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from app.config import settings
from app.execution_engine import ExecutionEngine
from app.indicator_engine import IndicatorEngine
from app.knowledge_base import KnowledgeBase
from app.logger import engine_logger, trades_logger
from app.market_data import MarketDataCollector
from app.okx_cli import OKXClient
from app.risk_manager import RiskManager
from app.strategy_engine import StrategyEngine
from app.utils import now_ts_ms, safe_float


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
        
        positions = self.client.get_positions()
        pos_list = []
        for p in positions:
            if abs(safe_float(p.get("pos"))) > 0:
                pos_list.append({
                    "symbol": p.get("instId"),
                    "side": p.get("posSide"),
                    "pos": p.get("pos"),
                    "avg_px": p.get("avgPx"),
                    "upl": p.get("upl"),
                    "upl_ratio": p.get("uplRatio")
                })
        
        return {
            "equity": equity,
            "available": avail,
            "positions": pos_list
        }

    def _log_ai_decision(self, decision: dict[str, Any], equity: float) -> None:
        """记录老板要求的 5 个核心字段，采用 JSON Lines 格式。"""
        log_file = settings.ai_decision_log_file
        
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
            "pnl": 0.0 if not is_open else None  # 开仓记录 pnl 为 None，跳过记录 pnl 为 0
        }
        
        # 追加到 JSON Lines 文件
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            engine_logger.error(f"写入决策日志失败: {e}")
        
        engine_logger.info(f"AI 决策已记录: {action} {decision.get('symbol', 'NONE')}")

    def run_once(self, execute_orders: bool = False) -> None:
        engine_logger.info("开始新一轮 AI 决策循环...")
        
        # 1. 采集数据
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
                "indicators": indicators
            }
        
        try:
            account_context = self._get_account_context()
        except Exception as e:
            engine_logger.error(f"获取账户信息失败: {e}")
            return
            
        recent_trades = self.kb.completed_trades
        
        # 2. AI 决策
        try:
            decision = self.strategy_engine.get_ai_decision(
                market_context=market_context,
                account_context=account_context,
                recent_trades=recent_trades
            )
        except Exception as e:
            engine_logger.error(f"AI 决策过程异常: {e}")
            decision = {"action": "SKIP", "reason": str(e)}
        
        # 3. 记录日志 (无论是否开仓都记录)
        self._log_ai_decision(decision, account_context["equity"])
        
        if not execute_orders:
            engine_logger.info("未开启执行模式，跳过下单。")
            return

        # 4. 执行决策
        action = str(decision.get("action", "SKIP")).upper()
        symbol = decision.get("symbol")
        
        if action == "SKIP" or symbol == "NONE":
            engine_logger.info("AI 建议观望。")
            return

        if action == "CLOSE":
            engine_logger.info(f"AI 建议平仓: {symbol}")
            self.execution_engine.close_position(symbol)
            return

        if action in ["OPEN_LONG", "OPEN_SHORT"]:
            equity = account_context["equity"]
            pos_pct = clamp(safe_float(decision.get("position_pct")), 0.0, 0.5)
            margin = equity * pos_pct
            
            # 基础价格数据
            entry_px = safe_float(market_context.get(symbol, {}).get("indicators", {}).get("15M", {}).get("close"))
            sl_px = safe_float(decision.get("stop_loss"))
            
            # 单笔风险校验 (2%)
            if entry_px > 0 and sl_px > 0:
                risk_per_unit = abs(entry_px - sl_px) / entry_px
                if risk_per_unit > 0:
                    max_risk_margin = (equity * 0.02) / risk_per_unit
                    if margin > max_risk_margin:
                        engine_logger.warning(f"触发 2% 风险保护，保证金从 {margin:.2f} 缩减至 {max_risk_margin:.2f}")
                        margin = max_risk_margin

            if margin < 10:
                engine_logger.warning("计算出的保证金过小，取消开仓。")
                return

            engine_logger.info(f"执行 AI 开仓: {action} {symbol} 保证金:{margin:.2f}")
            
            trailing = None
            if decision.get("trailing_callback"):
                trailing = {
                    "active_px": decision.get("active_px"),
                    "callback_ratio": decision.get("trailing_callback")
                }

            try:
                result = self.execution_engine.execute_ai_open(
                    symbol=symbol,
                    action=action,
                    margin_usdt=margin,
                    leverage=int(decision.get("leverage", 5)),
                    entry_price=entry_px,
                    stop_loss_price=sl_px,
                    trailing_stop=trailing,
                    take_profit_price=decision.get("take_profit_price")
                )
                engine_logger.info(f"开仓结果: {result['status']}")
            except Exception as e:
                engine_logger.error(f"执行开仓失败: {e}")

def clamp(val, min_val, max_val):
    return max(min_val, min(val, max_val))
