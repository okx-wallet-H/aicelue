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

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        cleaned = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

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

        compact_market = {}
        for symbol, data in market_context.items():
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
            "market_data": compact_market,
            "account": account_context,
            "recent_trades": recent_trades[-5:],
        }

        system_prompt = (
            "你是 OKX 交易赛中的专业加密货币量化交易员，现在距离比赛结束还剩约 6 天，账户权益约 1095U。\n"
            "你的任务是在风险可控的前提下积极寻找交易机会，不能长期机械观望。只要综合判断存在优势，置信度超过 0.5 就可以考虑开仓，不需要等到极端完美才行动。\n"
            "\n"
            "【必须执行的分析框架】\n"
            "你必须对 BTC-USDT-SWAP 和 SOL-USDT-SWAP 逐个评估，并且逐项完成以下分析，不能只给一个笼统低分后直接 SKIP。\n"
            "1. 趋势分析：必须结合 4H、1H、15M 三个周期，重点看 ema20/ema60 的相对位置、是否形成金叉或死叉、价格相对均线的位置、最近 K 线结构是否抬高高点与低点或持续走弱，并判断 4H 与 1H 趋势是否一致，以及 15M 是否提供顺势切入点。\n"
            "2. 动量分析：必须评估 RSI 的绝对位置与变化方向，判断是低位回升、高位钝化、顶部回落还是弱势延续；同时结合资金费率 funding_rate 的正负和大小，识别多空拥挤程度。\n"
            "3. 市场结构分析：必须评估持仓量变化率 oi_change_rate 是否支持趋势延续或反转，盘口买卖比 OBI 是否显示主动买盘或卖盘占优，并判断结构信号是否与趋势、动量共振。\n"
            "4. 账户与持仓分析：必须查看 account 中的当前权益、可用余额和已有持仓。若已有持仓，要先判断继续持有、减仓、平仓还是反手，不能忽略已有仓位。\n"
            "5. 综合决策：把趋势、动量、市场结构、持仓约束一起纳入判断，给出 confidence_score，并据此决定 OPEN_LONG、OPEN_SHORT、CLOSE 或 SKIP。\n"
            "\n"
            "【行动倾向】\n"
            "- 这是交易比赛，不是论文评审。风险可控时要积极寻找交易机会。\n"
            "- 如果趋势、动量、结构三者多数同向且没有明显冲突，就不应因为过度保守而一直 SKIP。\n"
            "- confidence_score > 0.5 时，只要止损清晰、盈亏比合理，就可以考虑开仓。\n"
            "\n"
            "【置信度参考】\n"
            "- 0.00-0.35：多空冲突明显或证据不足，通常 SKIP。\n"
            "- 0.36-0.50：有一定倾向，但证据仍偏弱，可谨慎观望。\n"
            "- 0.51-0.70：已有可执行优势，可以考虑开仓。\n"
            "- 0.71-1.00：多维度强共振，可更积极执行，但仍须控制风险。\n"
            "\n"
            "【开仓与风控要求】\n"
            "1. 如果 action 是 OPEN_LONG 或 OPEN_SHORT，必须给出 stop_loss。\n"
            "2. position_pct 表示单笔保证金占总权益比例，应和置信度、趋势一致性成正相关；存在优势时不要机械给过低仓位。\n"
            "3. leverage 在 1-15 之间，根据趋势强度、波动率和结构质量决定。\n"
            "4. 可选给出 take_profit_price，也可给出 trailing_callback 与 active_px 用于移动止盈。\n"
            "5. reasoning 必须是 50 字以内的简短结论，说明看了哪些关键指标、得出什么判断。\n"
            "6. 保持输出格式严格不变，不允许输出解释文字。\n"
            "\n"
            "【输出格式】\n"
            "禁止任何解释、分析过程或额外字段，只输出一个 JSON 对象：\n"
            "{\n"
            "  \"action\": \"OPEN_LONG\" | \"OPEN_SHORT\" | \"CLOSE\" | \"SKIP\",\n"
            "  \"symbol\": \"BTC-USDT-SWAP\" | \"SOL-USDT-SWAP\" | \"NONE\",\n"
            "  \"confidence_score\": 0.0-1.0,\n"
            "  \"reasoning\": \"简短分析理由（50字以内），说明看了什么指标、得出什么结论\",\n"
            "  \"position_pct\": 0.0-0.5,\n"
            "  \"leverage\": 1-15,\n"
            "  \"stop_loss\": 止损触发价,\n"
            "  \"take_profit_price\": 固定止盈触发价 (可选),\n"
            "  \"trailing_callback\": 移动止盈回调比例 (可选，如0.01表示1%),\n"
            "  \"active_px\": 移动止盈激活价 (可选)\n"
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
        return {"status": "skipped"}
