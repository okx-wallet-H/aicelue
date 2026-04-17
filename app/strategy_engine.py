from __future__ import annotations

from typing import Any
from app.llm_analyzer import LLMAnalyzer
from app.risk_manager import RiskManager
from app.logger import reasoning_logger

class StrategyEngine:
    """
    策略引擎：现在作为 AI 决策的包装层。
    不再包含硬编码的 if-else 交易规则。
    """
    def __init__(self, weights: dict[str, float], risk_manager: RiskManager) -> None:
        self.weights = weights
        self.risk_manager = risk_manager
        self.llm_analyzer = LLMAnalyzer()

    def get_ai_decision(
        self,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        获取 AI 的结构化决策。
        """
        reasoning_logger.info("正在请求 AI 决策...")
        decision = self.llm_analyzer.analyze_trade_decision(
            market_context=market_context,
            account_context=account_context,
            recent_trades=recent_trades
        )
        
        # 基础格式校验与补全
        decision.setdefault("action", "SKIP")
        decision.setdefault("symbol", "NONE")
        decision.setdefault("confidence_score", 0.0)
        decision.setdefault("reasoning", "No reasoning provided")
        decision.setdefault("position_pct", 0.0)
        decision.setdefault("leverage", 1)
        
        return decision
