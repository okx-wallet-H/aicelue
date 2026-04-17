from __future__ import annotations

import json
import re
import time
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]

from app.config import settings
from app.logger import iteration_logger, reasoning_logger
from app.utils import clamp, now_ts_ms, safe_float


class LLMAnalyzer:
    """负责调用大模型完成交易分析与决策。"""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "llm_enabled", True))
        self.model = str(getattr(settings, "llm_model", "qwen-plus-latest"))
        self.timeout_seconds = int(getattr(settings, "llm_timeout_seconds", 30))
        self.max_candles = int(getattr(settings, "llm_max_candles_per_tf", 24))
        self.temperature = float(getattr(settings, "llm_temperature", 0.2))
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
        
        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 去掉可能的markdown代码块包裹
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 尝试正则匹配最外层的 {}
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        
        raise ValueError(f"无法从响应中解析 JSON: {raw[:100]}...")

    def _compress_candles(self, candles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for row in (candles or [])[-self.max_candles :]:
            compact.append({
                "ts": str(row.get("ts") or ""),
                "o": round(safe_float(row.get("open")), 6),
                "h": round(safe_float(row.get("high")), 6),
                "l": round(safe_float(row.get("low")), 6),
                "c": round(safe_float(row.get("close")), 6),
                "v": round(safe_float(row.get("vol")), 2),
            })
        return compact

    def analyze_trade_decision(
        self,
        *,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        AI 核心决策入口。
        """
        if not self.enabled:
            return {"action": "SKIP", "reason": "LLM disabled"}

        # 压缩数据以节省 token
        compact_market = {}
        for symbol, data in market_context.items():
            compact_market[symbol] = {
                "funding_rate": data.get("funding_rate"),
                "oi_change_rate": data.get("oi_change_rate"),
                "obi": data.get("obi"),
                "klines_1h": self._compress_candles(data.get("klines", {}).get("1H")),
                "indicators_1h": data.get("indicators", {}).get("1H", {}),
            }

        payload = {
            "timestamp": now_ts_ms(),
            "market_data": compact_market,
            "account": account_context,
            "recent_trades": recent_trades[-5:],  # 只看最近5笔
        }

        system_prompt = (
            "你是一个顶级的加密货币量化交易员。请基于数据直接给出决策 JSON。\n"
            "【输出规范】\n"
            "禁止任何文字说明，只输出一个 JSON 对象：\n"
            "{\n"
            "  \"action\": \"OPEN_LONG\" | \"OPEN_SHORT\" | \"CLOSE\" | \"SKIP\",\n"
            "  \"symbol\": \"BTC-USDT-SWAP\" | \"SOL-USDT-SWAP\" | \"NONE\",\n"
            "  \"confidence_score\": 0.0-1.0,\n"
            "  \"position_pct\": 0.0-0.5,\n"
            "  \"leverage\": 1-15,\n"
            "  \"stop_loss\": 止损价,\n"
            "  \"trailing_callback\": 回调比例(如0.03表示3%),\n"
            "  \"active_px\": 移动止盈激活价\n"
            "}"
        )

        user_prompt = f"当前市场数据与账户状态：\n{json.dumps(payload, ensure_ascii=False)}"

        started = time.time()
        try:
            client = self._get_client()
            response = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"}
            )
            raw_response = response.choices[0].message.content
            latency_ms = int((time.time() - started) * 1000)
            decision = self._extract_json_block(raw_response)
            
            reasoning_logger.info(f"[AI决策] 耗时:{latency_ms}ms 响应:{raw_response}")
            return decision
        except Exception as exc:
            reasoning_logger.error(f"LLM 决策失败: {exc}")
            return {"action": "SKIP", "reason": f"Error: {exc}"}

    def review_recent_trades(self, **kwargs):
        # 暂时保留接口，后续按需重写
        return {"status": "skipped"}
