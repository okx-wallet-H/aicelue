from __future__ import annotations

from typing import Any

from app.llm_analyzer import LLMAnalyzer
from app.logger import reasoning_logger
from app.risk_manager import RiskManager

# 合法 action 枚举
VALID_ACTIONS = frozenset(
    ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "CLOSE", "HOLD", "SKIP"]
)

# 每轮必须出现的标的（缺失时补 SKIP）
REQUIRED_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")

# position_pct 上限
MAX_TOTAL_POSITION_PCT = 0.60


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

        # ── Action 合规化 ──────────────────────────────────────────────
        raw_action = str(normalized["action"]).upper().strip()
        if raw_action not in VALID_ACTIONS:
            reasoning_logger.warning(
                "LLM 输出了非预期 action=%r（symbol=%s），降级为 SKIP",
                raw_action,
                normalized["symbol"],
            )
            raw_action = "SKIP"
        normalized["action"] = raw_action

        # ── position_pct 范围校验 (0 ~ 0.60) ─────────────────────────
        try:
            pct = float(normalized["position_pct"])
        except (TypeError, ValueError):
            pct = 0.0
        if not (0.0 <= pct <= 0.60):
            reasoning_logger.warning(
                "position_pct=%s 超出范围 [0, 0.60]（symbol=%s），裁剪至合法范围",
                pct,
                normalized["symbol"],
            )
            pct = max(0.0, min(pct, 0.60))
        normalized["position_pct"] = pct

        # ── leverage 范围校验 (1 ~ 15) ────────────────────────────────
        try:
            lev = int(normalized["leverage"])
        except (TypeError, ValueError):
            lev = 1
        if not (1 <= lev <= 15):
            reasoning_logger.warning(
                "leverage=%s 超出范围 [1, 15]（symbol=%s），裁剪至合法范围",
                lev,
                normalized["symbol"],
            )
            lev = max(1, min(lev, 15))
        normalized["leverage"] = lev

        return normalized

    def _validate_and_fill_decisions(
        self, decisions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        确保每轮决策包含所有必须标的；缺失的补 SKIP。
        然后按置信度比例缩放，使 position_pct 总和不超过 MAX_TOTAL_POSITION_PCT。
        """
        symbol_map: dict[str, dict[str, Any]] = {}
        for d in decisions:
            sym = str(d.get("symbol", "NONE"))
            symbol_map[sym] = d

        # 补全缺失标的
        for sym in REQUIRED_SYMBOLS:
            if sym not in symbol_map:
                reasoning_logger.warning("LLM 未返回标的 %s 的决策，自动补充 SKIP", sym)
                symbol_map[sym] = {
                    "action": "SKIP",
                    "symbol": sym,
                    "confidence_score": 0.0,
                    "reasoning": "auto-filled: missing from LLM output",
                    "position_pct": 0.0,
                    "leverage": 1,
                }

        filled = list(symbol_map.values())

        # position_pct 总和约束
        total_pct = sum(float(d.get("position_pct", 0.0)) for d in filled)
        if total_pct > MAX_TOTAL_POSITION_PCT:
            reasoning_logger.warning(
                "所有标的 position_pct 之和 %.4f 超过上限 %.2f，按置信度比例缩放",
                total_pct,
                MAX_TOTAL_POSITION_PCT,
            )
            scale = MAX_TOTAL_POSITION_PCT / total_pct
            for d in filled:
                d["position_pct"] = round(float(d.get("position_pct", 0.0)) * scale, 4)

        return filled

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

        normalized = [self._normalize_decision(item) for item in decisions]
        return self._validate_and_fill_decisions(normalized)

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

