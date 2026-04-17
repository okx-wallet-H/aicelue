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
from app.logger import reasoning_logger
from app.utils import now_ts_ms, safe_float


class LLMAnalyzer:
    """负责调用大模型完成多标的交易分析与决策。"""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "llm_enabled", True))
        self.model = str(getattr(settings, "llm_model", "qwen-plus-latest"))
        self.timeout_seconds = int(getattr(settings, "llm_timeout_seconds", 30))
        self.max_candles = int(getattr(settings, "llm_max_candles_per_tf", 24))
        self.temperature = float(getattr(settings, "llm_temperature", 0.35))
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

        for candidate in (raw, re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE),):
            cleaned = re.sub(r'```\s*$', '', candidate, flags=re.MULTILINE).strip()
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"无法从响应中解析 JSON: {raw[:120]}...")

    def _compress_candles(self, candles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for row in (candles or [])[-self.max_candles :]:
            compact.append(
                {
                    "ts": str(row.get("ts") or ""),
                    "o": round(safe_float(row.get("open")), 6),
                    "h": round(safe_float(row.get("high")), 6),
                    "l": round(safe_float(row.get("low")), 6),
                    "c": round(safe_float(row.get("close")), 6),
                    "v": round(safe_float(row.get("vol")), 2),
                }
            )
        return compact

    def analyze_trade_decision(
        self,
        *,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"decisions": []}

        target_symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
        compact_market: dict[str, Any] = {}
        for symbol in target_symbols:
            data = market_context.get(symbol, {})
            compact_market[symbol] = {
                "funding_rate": data.get("funding_rate"),
                "oi_change_rate": data.get("oi_change_rate"),
                "obi": data.get("obi"),
                "klines_4h": self._compress_candles(data.get("klines", {}).get("4H")),
                "klines_1h": self._compress_candles(data.get("klines", {}).get("1H")),
                "klines_15m": self._compress_candles(data.get("klines", {}).get("15M")),
                "indicators_4h": data.get("indicators", {}).get("4H", {}),
                "indicators_1h": data.get("indicators", {}).get("1H", {}),
                "indicators_15m": data.get("indicators", {}).get("15M", {}),
            }

        payload = {
            "timestamp": now_ts_ms(),
            "target_symbols": target_symbols,
            "market_data": compact_market,
            "account": account_context,
            "recent_trades": recent_trades[-8:],
        }

        system_prompt = (
            "你是 OKX 交易赛中的专业加密货币量化交易员，现在距离比赛结束还剩约 6 天，账户权益约 1095U。\n"
            "你的任务是在风险可控的前提下积极寻找交易机会，提高资金利用率。你必须同时评估 BTC-USDT-SWAP、ETH-USDT-SWAP、SOL-USDT-SWAP 三个标的，并分别给出独立决策。\n"
            "只要某个标的综合优势明确且置信度超过阈值，就可以开仓；多个标的同时满足条件时，可以同时开仓。不要因为其中一个标的不确定，就让全部标的一起 SKIP。\n"
            "\n"
            "【必须执行的分析框架】\n"
            "对每个标的都必须逐项分析并独立打分：\n"
            "1. 趋势分析：结合 4H、1H、15M 三个周期，查看 EMA20/EMA60 的相对位置、交叉方向、价格相对均线位置，以及 K 线结构是否顺势。\n"
            "2. 动量分析：评估 RSI 的绝对位置与方向，结合资金费率 funding_rate 的正负和大小，判断多空拥挤与趋势延续性。\n"
            "3. 市场结构：评估持仓量变化率 oi_change_rate 与盘口买卖比 OBI，判断是否存在资金推动和主动买卖盘偏向。\n"
            "4. 持仓与账户约束：查看当前已有持仓，避免同方向过度重复开仓；若已有持仓则可给出 CLOSE 或 SKIP。\n"
            "5. 综合评分：综合趋势、动量、结构与账户约束，给出独立置信度和执行建议。\n"
            "\n"
            "【行动原则】\n"
            "- 这是交易比赛，风险可控时应积极利用资金。\n"
            "- 对每个标的独立判断，不能笼统输出一个总观点。\n"
            "- 置信度超过 0.5 且止损清晰、盈亏比合理时，可以考虑开仓。\n"
            "- 如果多个标的都满足条件，可以同时开仓。\n"
            "\n"
            "【风控约束】\n"
            "- 单笔交易风险不得超过账户权益的 2%，因此 stop_loss 必须合理。\n"
            "- 多个标的合计建议的 position_pct 不应超过 0.60。\n"
            "- position_pct 表示该标的拟使用的保证金占总权益比例；若多个标的信号都强，可分配不同仓位，但总和不要超过 0.60。\n"
            "- leverage 在 1-15 之间。\n"
            "\n"
            "【输出格式】\n"
            "禁止任何解释文字，只输出一个 JSON 对象，格式如下：\n"
            "{\n"
            "  \"decisions\": [\n"
            "    {\n"
            "      \"action\": \"OPEN_LONG\" | \"OPEN_SHORT\" | \"CLOSE\" | \"SKIP\",\n"
            "      \"symbol\": \"BTC-USDT-SWAP\" | \"ETH-USDT-SWAP\" | \"SOL-USDT-SWAP\",\n"
            "      \"confidence_score\": 0.0-1.0,\n"
            "      \"reasoning\": \"简短分析理由（50字以内），说明看了什么指标、得出什么结论\",\n"
            "      \"position_pct\": 0.0-0.6,\n"
            "      \"leverage\": 1-15,\n"
            "      \"stop_loss\": 止损触发价,\n"
            "      \"take_profit_price\": 固定止盈触发价 (可选),\n"
            "      \"trailing_callback\": 移动止盈回调比例 (可选),\n"
            "      \"active_px\": 移动止盈激活价 (可选)\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "必须返回 3 条 decisions，分别对应 BTC-USDT-SWAP、ETH-USDT-SWAP、SOL-USDT-SWAP。"
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
                response_format={"type": "json_object"},
            )
            raw_response = response.choices[0].message.content
            latency_ms = int((time.time() - started) * 1000)
            reasoning_logger.info(f"[AI多标的决策] 耗时:{latency_ms}ms 响应:{raw_response}")
            parsed = self._extract_json_block(raw_response)
            decisions = parsed.get("decisions")
            if not isinstance(decisions, list):
                raise ValueError("AI 未返回 decisions 数组")
            return {"decisions": decisions}
        except Exception as exc:
            reasoning_logger.error(f"LLM 决策失败: {exc}")
            return {
                "decisions": [
                    {"action": "SKIP", "symbol": symbol, "confidence_score": 0.0, "reasoning": f"Error: {exc}", "position_pct": 0.0, "leverage": 1}
                    for symbol in target_symbols
                ]
            }

    def review_recent_trades(self, **kwargs):
        return {"status": "skipped"}
