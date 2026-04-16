from __future__ import annotations

import json
import re
import time
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - 依赖缺失时走降级
    OpenAI = None  # type: ignore[assignment]

from app.config import settings
from app.logger import iteration_logger, reasoning_logger
from app.utils import clamp, now_ts_ms, safe_float


class LLMAnalyzer:
    """负责调用大模型完成交易分析与自我进化复盘。"""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "llm_enabled", True))
        self.model = str(getattr(settings, "llm_model", "qwen-plus-latest"))
        self.timeout_seconds = int(getattr(settings, "llm_timeout_seconds", 12))
        self.max_candles = int(getattr(settings, "llm_max_candles_per_tf", 24))
        self.recent_record_window = int(getattr(settings, "llm_recent_record_window", 12))
        self.recent_trade_window = int(getattr(settings, "llm_recent_trade_window", 20))
        self.temperature = float(getattr(settings, "llm_temperature", 0.15))
        self.review_temperature = float(getattr(settings, "llm_review_temperature", 0.10))
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if OpenAI is None:
            raise RuntimeError("openai 依赖未安装，无法调用大模型。")
        if self._client is None:
            self._client = OpenAI()
        return self._client

    @staticmethod
    def _extract_json_block(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("大模型返回为空。")
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # 去掉可能的markdown代码块包裹
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise ValueError("未在大模型响应中找到 JSON 对象。")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("大模型响应不是 JSON 对象。")
        return parsed

    def _compress_candles(self, candles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for row in (candles or [])[-self.max_candles :]:
            if not isinstance(row, dict):
                continue
            compact.append(
                {
                    "ts": str(row.get("ts") or ""),
                    "open": round(safe_float(row.get("open")), 6),
                    "high": round(safe_float(row.get("high")), 6),
                    "low": round(safe_float(row.get("low")), 6),
                    "close": round(safe_float(row.get("close")), 6),
                    "vol": round(safe_float(row.get("vol")), 6),
                }
            )
        return compact

    def _compress_symbol_market_context(self, symbol_payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = symbol_payload.get("snapshot") or {}
        indicators = symbol_payload.get("tf_indicators") or {}
        market_state = symbol_payload.get("market_state") or {}
        return {
            "funding_rate": round(safe_float(snapshot.get("funding_rate")), 8),
            "open_interest": round(safe_float(snapshot.get("open_interest")), 6),
            "oi_change_rate": round(safe_float(snapshot.get("oi_change_rate")), 6),
            "obi": round(safe_float(snapshot.get("obi")), 6),
            "klines": {
                "1H": self._compress_candles((snapshot.get("klines") or {}).get("1H")),
                "15M": self._compress_candles((snapshot.get("klines") or {}).get("15M")),
            },
            "indicators": {
                "1H": indicators.get("1H") or {},
                "15M": indicators.get("15M") or {},
            },
            "market_state": market_state,
        }

    def _normalize_recent_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "timestamp": int(safe_float(record.get("timestamp") or 0)),
            "symbol": str(record.get("symbol") or ""),
            "market_state": str(record.get("market_state") or ""),
            "action": str(record.get("action") or ""),
            "side": str(record.get("side") or ""),
            "weighted_score": round(safe_float(record.get("weighted_score")), 6),
            "signal_grade": str(record.get("signal_grade") or ""),
            "attack_score": round(safe_float(record.get("attack_score")), 6),
            "trade_status": str(record.get("trade_status") or ""),
            "realized_pnl": round(safe_float(record.get("realized_pnl")), 6),
            "llm_analysis": record.get("llm_analysis") or {},
        }

    def _normalize_recent_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_id": str(trade.get("trade_id") or trade.get("record_id") or ""),
            "symbol": str(trade.get("symbol") or ""),
            "side": str(trade.get("side") or ""),
            "market_state": str(trade.get("market_state") or ""),
            "opened_at": int(safe_float(trade.get("opened_at") or 0)),
            "closed_at": int(safe_float(trade.get("closed_at") or 0)),
            "realized_pnl": round(safe_float(trade.get("realized_pnl")), 6),
            "close_reason": str(trade.get("close_reason") or ""),
            "weighted_score": round(safe_float(trade.get("weighted_score")), 6),
            "signal_grade": str(trade.get("signal_grade") or ""),
            "attack_score": round(safe_float(trade.get("attack_score")), 6),
            "llm_analysis": trade.get("llm_analysis") or {},
        }

    @staticmethod
    def _default_decision_result(target_symbol: str, error: str) -> dict[str, Any]:
        return {
            "enabled": False,
            "degraded": True,
            "target_symbol": target_symbol,
            "timestamp": now_ts_ms(),
            "model": str(getattr(settings, "llm_model", "qwen-plus-latest")),
            "latency_ms": 0,
            "error": error,
            "market_analysis": {
                "overall_trend": "未知",
                "trend_strength": "未知",
                "key_supports": [],
                "key_resistances": [],
                "state_interpretation": "大模型不可用，回退到纯技术指标模式。",
            },
            "trade_advice": {
                "action": "HOLD",
                "direction": "none",
                "confidence": 0.0,
                "thesis": "大模型调用失败或未启用，本轮不提供额外方向加权。",
                "alignment_note": "降级模式",
            },
            "risk_assessment": {
                "level": "中",
                "warnings": [error],
                "positioning_note": "沿用原有技术指标与风控逻辑。",
            },
            "reasoning_process": [error],
            "raw_response": "",
        }

    @staticmethod
    def _default_review_result(error: str) -> dict[str, Any]:
        return {
            "enabled": False,
            "degraded": True,
            "timestamp": now_ts_ms(),
            "model": str(getattr(settings, "llm_model", "qwen-plus-latest")),
            "latency_ms": 0,
            "error": error,
            "review_summary": "大模型复盘不可用，沿用规则自适应结果。",
            "strengths": [],
            "mistakes": [error],
            "parameter_adjustments": {
                "confidence_threshold_delta": 0.0,
                "overall_position_scale_delta": 0.0,
                "overall_leverage_scale_delta": 0.0,
                "strategy_weight_bias": {},
                "state_confidence_bonus_delta": {},
                "state_stop_loss_delta": {},
            },
            "reasoning_process": [error],
            "raw_response": "",
        }

    def _normalize_decision_result(self, target_symbol: str, parsed: dict[str, Any], raw_response: str, latency_ms: int) -> dict[str, Any]:
        market_analysis = parsed.get("market_analysis") or {}
        trade_advice = parsed.get("trade_advice") or {}
        risk_assessment = parsed.get("risk_assessment") or {}
        reasoning_process = parsed.get("reasoning_process") or []
        if isinstance(reasoning_process, str):
            reasoning_process = [reasoning_process]

        action = str(trade_advice.get("action") or "HOLD").upper()
        if action not in {"OPEN_LONG", "OPEN_SHORT", "HOLD"}:
            action = "HOLD"
        direction = str(trade_advice.get("direction") or "none").lower()
        if direction not in {"buy", "sell", "none"}:
            direction = "buy" if action == "OPEN_LONG" else "sell" if action == "OPEN_SHORT" else "none"

        return {
            "enabled": True,
            "degraded": False,
            "target_symbol": target_symbol,
            "timestamp": now_ts_ms(),
            "model": self.model,
            "latency_ms": latency_ms,
            "error": None,
            "market_analysis": {
                "overall_trend": str(market_analysis.get("overall_trend") or "未知"),
                "trend_strength": str(market_analysis.get("trend_strength") or "未知"),
                "key_supports": market_analysis.get("key_supports") or [],
                "key_resistances": market_analysis.get("key_resistances") or [],
                "state_interpretation": str(market_analysis.get("state_interpretation") or ""),
            },
            "trade_advice": {
                "action": action,
                "direction": direction,
                "confidence": round(clamp(safe_float(trade_advice.get("confidence")), 0.0, 1.0), 4),
                "thesis": str(trade_advice.get("thesis") or ""),
                "alignment_note": str(trade_advice.get("alignment_note") or ""),
            },
            "risk_assessment": {
                "level": str(risk_assessment.get("level") or "中"),
                "warnings": risk_assessment.get("warnings") or [],
                "positioning_note": str(risk_assessment.get("positioning_note") or ""),
            },
            "reasoning_process": [str(item) for item in reasoning_process if str(item).strip()],
            "raw_response": raw_response,
        }

    def _normalize_review_result(self, parsed: dict[str, Any], raw_response: str, latency_ms: int) -> dict[str, Any]:
        parameter_adjustments = parsed.get("parameter_adjustments") or {}
        return {
            "enabled": True,
            "degraded": False,
            "timestamp": now_ts_ms(),
            "model": self.model,
            "latency_ms": latency_ms,
            "error": None,
            "review_summary": str(parsed.get("review_summary") or ""),
            "strengths": [str(item) for item in (parsed.get("strengths") or []) if str(item).strip()],
            "mistakes": [str(item) for item in (parsed.get("mistakes") or []) if str(item).strip()],
            "parameter_adjustments": {
                "confidence_threshold_delta": round(clamp(safe_float(parameter_adjustments.get("confidence_threshold_delta")), -0.05, 0.05), 4),
                "overall_position_scale_delta": round(clamp(safe_float(parameter_adjustments.get("overall_position_scale_delta")), -0.20, 0.20), 4),
                "overall_leverage_scale_delta": round(clamp(safe_float(parameter_adjustments.get("overall_leverage_scale_delta")), -0.20, 0.20), 4),
                "strategy_weight_bias": parameter_adjustments.get("strategy_weight_bias") or {},
                "state_confidence_bonus_delta": parameter_adjustments.get("state_confidence_bonus_delta") or {},
                "state_stop_loss_delta": parameter_adjustments.get("state_stop_loss_delta") or {},
            },
            "reasoning_process": [str(item) for item in (parsed.get("reasoning_process") or []) if str(item).strip()],
            "raw_response": raw_response,
        }

    def analyze_trade_decision(
        self,
        *,
        target_symbol: str,
        market_context: dict[str, Any],
        positions: dict[str, Any],
        recent_records: list[dict[str, Any]],
        recent_trades: list[dict[str, Any]],
        btc_weathervane: dict[str, Any] | None = None,
        btc_turn_alert: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._default_decision_result(target_symbol, "LLM 功能已关闭，使用纯技术指标模式。")

        compact_market_context = {
            symbol: self._compress_symbol_market_context(payload)
            for symbol, payload in (market_context or {}).items()
            if isinstance(payload, dict)
        }
        payload = {
            "timestamp": now_ts_ms(),
            "target_symbol": target_symbol,
            "btc_weathervane": btc_weathervane or {},
            "btc_turn_alert": btc_turn_alert,
            "market_context": compact_market_context,
            "positions": positions,
            "recent_records": [self._normalize_recent_record(item) for item in (recent_records or [])[-self.recent_record_window :]],
            "recent_trades": [self._normalize_recent_trade(item) for item in (recent_trades or [])[-self.recent_trade_window :]],
        }
        system_prompt = (
            "你是 OKX 合约量化策略的资深交易分析师。"
            "请基于给定的 BTC 与 SOL 多周期行情、技术指标、持仓、资金费率、未平仓量变化、"
            "市场状态识别结果、近期交易记录与盈亏情况，对目标标的给出严格 JSON。"
            "【重要】你必须只输出一个纯JSON对象，不要输出任何其他文字、Markdown、代码块或解释。"
            "不要用```json包裹，直接输出{开头}结尾的JSON。"
            "trade_advice.action 只能是 OPEN_LONG、OPEN_SHORT、HOLD 之一；"
            "trade_advice.direction 只能是 buy、sell、none 之一；"
            "trade_advice.confidence 必须是 0 到 1 之间的小数。"
            "【比赛背景】当前正在参加OKX合约交易赛，比赛2026年4月23日16:00截止，本金1500U，剩余不到6天。"
            "核心策略是高频小仓位交易、积少成多，每笔赚2-5U即可。"
            "你应该倾向于给出OPEN_LONG或OPEN_SHORT的建议，积极寻找交易机会。"
            "只有在市场极度危险（如暴跌暴涨超5%、流动性枯竭）时才建议HOLD。"
            "不要过于保守，比赛需要不停交易来积累收益。"
        )
        user_prompt = (
            "请分析目标标的并仅返回 JSON，结构如下："
            "{"
            '"market_analysis":{"overall_trend":"字符串","trend_strength":"字符串","key_supports":[数字或字符串],"key_resistances":[数字或字符串],"state_interpretation":"字符串"},'
            '"trade_advice":{"action":"OPEN_LONG|OPEN_SHORT|HOLD","direction":"buy|sell|none","confidence":0.0,"thesis":"字符串","alignment_note":"字符串"},'
            '"risk_assessment":{"level":"低|中|高","warnings":["字符串"],"positioning_note":"字符串"},'
            '"reasoning_process":["字符串1","字符串2","字符串3"]'
            "}。"
            f"以下是输入数据：{json.dumps(payload, ensure_ascii=False)}"
        )

        started = time.time()
        try:
            client = self._get_client()
            response = client.with_options(timeout=self.timeout_seconds).chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw_response = str(((response.choices[0].message or {}).content) if isinstance(response.choices[0].message, dict) else response.choices[0].message.content)
            latency_ms = int((time.time() - started) * 1000)
            parsed = self._extract_json_block(raw_response)
            result = self._normalize_decision_result(target_symbol, parsed, raw_response, latency_ms)
            reasoning_logger.info(
                "[LLM决策分析] 标的=%s 输入=%s 输出=%s",
                target_symbol,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            )
            return result
        except Exception as exc:  # pragma: no cover - 外部 API 调用存在不确定性
            result = self._default_decision_result(target_symbol, f"LLM 决策分析失败，已自动降级：{exc}")
            reasoning_logger.warning(
                "[LLM决策分析降级] 标的=%s 输入=%s 错误=%s",
                target_symbol,
                json.dumps(payload, ensure_ascii=False),
                exc,
            )
            return result

    def review_recent_trades(
        self,
        *,
        completed_trades: list[dict[str, Any]],
        performance_metrics: dict[str, Any],
        current_weights: dict[str, float],
        adaptive_params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._default_review_result("LLM 功能已关闭，复盘阶段使用规则策略。")

        payload = {
            "timestamp": now_ts_ms(),
            "performance_metrics": performance_metrics,
            "current_weights": current_weights,
            "adaptive_params": adaptive_params,
            "recent_trades": [self._normalize_recent_trade(item) for item in (completed_trades or [])[-self.recent_trade_window :]],
        }
        system_prompt = (
            "你是量化交易策略复盘顾问。"
            "请从最近交易中识别做对与做错的地方，并给出保守、可执行、可量化的参数调整建议。"
            "只能返回严格 JSON，不要输出 Markdown 或代码块。"
        )
        user_prompt = (
            "请仅返回 JSON，结构如下："
            "{"
            '"review_summary":"字符串",'
            '"strengths":["字符串"],'
            '"mistakes":["字符串"],'
            '"parameter_adjustments":{'
            '"confidence_threshold_delta":-0.02,'
            '"overall_position_scale_delta":0.05,'
            '"overall_leverage_scale_delta":0.04,'
            '"strategy_weight_bias":{"trend_following":0.03,"mean_reversion":-0.02},'
            '"state_confidence_bonus_delta":{"强势上涨":-0.01},'
            '"state_stop_loss_delta":{"区间震荡":0.001}'
            "},"
            '"reasoning_process":["字符串1","字符串2","字符串3"]'
            "}。"
            f"以下是输入数据：{json.dumps(payload, ensure_ascii=False)}"
        )

        started = time.time()
        try:
            client = self._get_client()
            response = client.with_options(timeout=self.timeout_seconds).chat.completions.create(
                model=self.model,
                temperature=self.review_temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw_response = str(((response.choices[0].message or {}).content) if isinstance(response.choices[0].message, dict) else response.choices[0].message.content)
            latency_ms = int((time.time() - started) * 1000)
            parsed = self._extract_json_block(raw_response)
            result = self._normalize_review_result(parsed, raw_response, latency_ms)
            iteration_logger.info(
                "[LLM复盘分析] 输入=%s 输出=%s",
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            )
            return result
        except Exception as exc:  # pragma: no cover - 外部 API 调用存在不确定性
            result = self._default_review_result(f"LLM 复盘失败，已自动降级：{exc}")
            iteration_logger.warning("[LLM复盘降级] 输入=%s 错误=%s", json.dumps(payload, ensure_ascii=False), exc)
            return result
