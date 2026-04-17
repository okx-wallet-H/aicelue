from __future__ import annotations

from typing import Any

from app.llm_analyzer import LLMAnalyzer
from app.logger import reasoning_logger
from app.risk_manager import RiskManager
from app.utils import safe_float


class StrategyEngine:
    """策略引擎：仅负责承接 AI 决策并做轻量规范化。"""

    VALID_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "SKIP"}

    def __init__(self, weights: dict[str, float], risk_manager: RiskManager) -> None:
        self.weights = weights
        self.risk_manager = risk_manager
        self.llm_analyzer = LLMAnalyzer()

    def _normalize_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(decision or {})
        action = str(normalized.get("action", "SKIP") or "SKIP").upper().strip()
        normalized["action"] = action if action in self.VALID_ACTIONS else "SKIP"
        normalized["symbol"] = str(normalized.get("symbol", "NONE") or "NONE").upper()
        normalized["confidence_score"] = round(safe_float(normalized.get("confidence_score")), 4)
        normalized["reasoning"] = str(normalized.get("reasoning", "No reasoning provided") or "No reasoning provided")[:120]
        normalized["position_pct"] = max(0.0, min(0.6, safe_float(normalized.get("position_pct"))))
        normalized["leverage"] = max(1, int(safe_float(normalized.get("leverage"), 1)))
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
