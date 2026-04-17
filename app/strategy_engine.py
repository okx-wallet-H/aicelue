from __future__ import annotations

from typing import Any

from app.llm_analyzer import LLMAnalyzer
from app.logger import reasoning_logger
from app.risk_manager import RiskManager


class StrategyEngine:
    """策略引擎：作为 AI 多标的决策的包装层。"""

    def __init__(self, weights: dict[str, float], risk_manager: RiskManager) -> None:
        self.weights = weights
        self.risk_manager = risk_manager
        self.llm_analyzer = LLMAnalyzer()

    def _normalize_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(decision or {})
        normalized.setdefault("action", "SKIP")
        normalized.setdefault("symbol", "NONE")
        normalized.setdefault("confidence_score", 0.0)
        normalized.setdefault("reasoning", "No reasoning provided")
        normalized.setdefault("position_pct", 0.0)
        normalized.setdefault("leverage", 1)
        return normalized

    def get_ai_decisions(
        self,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        reasoning_logger.info("正在请求 AI 多标的决策...")
        result = self.llm_analyzer.analyze_trade_decision(
            market_context=market_context,
            account_context=account_context,
            recent_trades=recent_trades,
        )
        decisions = result.get("decisions") if isinstance(result, dict) else None
        if not isinstance(decisions, list):
            decisions = [result if isinstance(result, dict) else {}]
        return [self._normalize_decision(item) for item in decisions]

    def get_ai_decision(
        self,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        decisions = self.get_ai_decisions(
            market_context=market_context,
            account_context=account_context,
            recent_trades=recent_trades,
        )
        return decisions[0] if decisions else self._normalize_decision({})
