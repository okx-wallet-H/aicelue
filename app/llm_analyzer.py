from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
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
        self.timeout_seconds = int(getattr(settings, "llm_timeout_seconds", 30))
        self.max_candles = int(getattr(settings, "llm_max_candles_per_tf", 24))
        self.temperature = float(getattr(settings, "llm_temperature", 0.30))
        self.primary_provider = {
            "name": "qwen",
            "api_key": str(getattr(settings, "llm_primary_api_key", "")),
            "base_url": str(getattr(settings, "llm_primary_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            "model": str(getattr(settings, "llm_primary_model", "qwen-plus-latest")),
        }
        self.backup_provider = {
            "name": "deepseek",
            "api_key": str(getattr(settings, "llm_backup_api_key", "")),
            "base_url": str(getattr(settings, "llm_backup_base_url", "https://api.deepseek.com/v1")),
            "model": str(getattr(settings, "llm_backup_model", "deepseek-chat")),
        }
        self._clients: dict[str, OpenAI] = {}

    def _get_client(self, provider: dict[str, str]) -> OpenAI:
        if OpenAI is None:
            raise RuntimeError("openai 依赖未安装，无法调用大模型。")
        provider_name = provider["name"]
        client = self._clients.get(provider_name)
        if client is None:
            if not provider.get("api_key"):
                raise RuntimeError(f"{provider_name} API Key 未配置。")
            client = OpenAI(
                api_key=provider["api_key"],
                base_url=provider["base_url"],
                timeout=self.timeout_seconds,
            )
            self._clients[provider_name] = client
        return client

    @staticmethod
    def _extract_json_block(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("大模型返回为空。")

        for candidate in (raw, re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)):
            cleaned = re.sub(r'```\s*$', '', candidate, flags=re.MULTILINE).strip()
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"无法从响应中解析 JSON: {raw[:160]}...")

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

    def _competition_context(self, account_context: dict[str, Any]) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        competition_left = "未知"
        configured_end = str(getattr(settings, "competition_end_at_utc", "") or "").strip()
        if configured_end:
            try:
                end_at = datetime.fromisoformat(configured_end.replace("Z", "+00:00"))
                delta = end_at - now_utc
                competition_left = str(delta) if delta.total_seconds() > 0 else "已结束"
            except ValueError:
                competition_left = "配置格式错误"
        return {
            "current_time_utc": now_utc.isoformat(),
            "competition_time_left": competition_left,
            "equity_usdt": round(safe_float(account_context.get("equity")), 4),
            "available_usdt": round(safe_float(account_context.get("available")), 4),
        }

    @staticmethod
    def _normalize_action(action: str) -> str:
        normalized = str(action or "SKIP").upper().strip()
        if normalized in {"OPEN_LONG", "OPEN_SHORT", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "SKIP"}:
            return normalized
        return "SKIP"

    def _normalize_decisions(self, target_symbols: list[str], parsed: dict[str, Any]) -> list[dict[str, Any]]:
        decisions = parsed.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("AI 未返回 decisions 数组。")

        indexed: dict[str, dict[str, Any]] = {}
        for item in decisions:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper().strip()
            if symbol not in target_symbols:
                continue
            normalized = dict(item)
            normalized["symbol"] = symbol
            normalized["action"] = self._normalize_action(normalized.get("action", "SKIP"))
            normalized["confidence_score"] = round(safe_float(normalized.get("confidence_score")), 4)
            normalized["position_pct"] = max(0.0, min(0.6, safe_float(normalized.get("position_pct"))))
            normalized["leverage"] = max(1, int(safe_float(normalized.get("leverage"), 1)))
            indexed[symbol] = normalized

        final_decisions: list[dict[str, Any]] = []
        for symbol in target_symbols:
            final_decisions.append(
                indexed.get(
                    symbol,
                    {
                        "action": "SKIP",
                        "symbol": symbol,
                        "confidence_score": 0.0,
                        "reasoning": "模型未返回该标的决策，已自动跳过。",
                        "position_pct": 0.0,
                        "leverage": 1,
                    },
                )
            )
        return final_decisions

    def _skip_decisions(self, target_symbols: list[str], reason: str) -> dict[str, Any]:
        normalized_reason = str(reason or "未知错误")[:180]
        return {
            "decisions": [
                {
                    "action": "SKIP",
                    "symbol": symbol,
                    "confidence_score": 0.0,
                    "reasoning": normalized_reason,
                    "position_pct": 0.0,
                    "leverage": 1,
                }
                for symbol in target_symbols
            ]
        }

    def _request_once(self, provider: dict[str, str], system_prompt: str, user_prompt: str) -> tuple[str, int]:
        client = self._get_client(provider)
        started = time.time()
        response = client.chat.completions.create(
            model=provider["model"],
            temperature=self.temperature,
            timeout=self.timeout_seconds,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        latency_ms = int((time.time() - started) * 1000)
        raw_response = response.choices[0].message.content or ""
        reasoning_logger.info("[AI多标的决策] provider=%s latency_ms=%s response=%s", provider["name"], latency_ms, raw_response)
        return raw_response, latency_ms

    def analyze_trade_decision(
        self,
        *,
        market_context: dict[str, Any],
        account_context: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target_symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
        if not self.enabled:
            return self._skip_decisions(target_symbols, "LLM 已禁用")

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
            "strategy_context": self._competition_context(account_context),
            "target_symbols": target_symbols,
            "market_data": compact_market,
            "account": account_context,
            "recent_trades": recent_trades[-int(getattr(settings, "llm_recent_trade_window", 8)) :],
        }

        system_prompt = (
            "你是 OKX 交易赛中的专业加密货币量化交易员。AI 是唯一决策者，代码只负责执行与风控兜底。\n"
            "你必须同时分析 BTC-USDT-SWAP、ETH-USDT-SWAP、SOL-USDT-SWAP 三个标的，并分别输出独立决策。\n"
            "请基于输入中的当前 UTC 时间、比赛剩余时间、账户权益、可用余额、现有持仓、最近交易和多周期市场数据做判断。\n"
            "\n"
            "【必须执行的分析框架】\n"
            "1. 趋势分析：结合 4H、1H、15M 周期的均线关系、价格结构与波段方向。\n"
            "2. 动量分析：结合 RSI、成交量、资金费率，判断顺势还是衰竭。\n"
            "3. 市场结构：结合 OI 变化率与订单簿不平衡值 OBI，判断资金推动方向。\n"
            "4. 仓位约束：检查当前已有持仓，避免重复开同一标的同方向仓位。若当前持仓方向与判断方向相反，必须认真评估是否应直接反手。\n"
            "5. 风险收益：止损必须明确，position_pct 是拟使用的保证金占权益比例，总和不超过 0.60。\n"
            "6. 多空对称：不能只找做多机会，必须对每个标的同时评估做多与做空两种 alpha。若不存在可靠多头趋势，但存在明确下跌趋势、动量转弱、卖压主导或市场结构转空，应优先考虑 OPEN_SHORT。\n"
            "\n"
            "【动作枚举】\n"
            "- OPEN_LONG：开多\n"
            "- OPEN_SHORT：开空\n"
            "- CLOSE_LONG：平多\n"
            "- CLOSE_SHORT：平空\n"
            "- HOLD：保持当前仓位不动\n"
            "- SKIP：跳过，不开新仓也不建议调整\n"
            "\n"
            "【反手规则】\n"
            "- OPEN_LONG / OPEN_SHORT 表示该标的本轮结束后希望持有的目标方向，而不仅仅是单纯开仓。\n"
            "- 如果当前已有多单，但空头证据更强且应反手做空，必须直接输出 OPEN_SHORT，而不是只输出 CLOSE_LONG。\n"
            "- 如果当前已有空单，但多头证据更强且应反手做多，必须直接输出 OPEN_LONG，而不是只输出 CLOSE_SHORT。\n"
            "- 只有当你判断应该平仓后保持空仓时，才使用 CLOSE_LONG 或 CLOSE_SHORT。\n"
            "\n"
            "【风控约束】\n"
            "- 单笔交易真实风险必须可落在账户权益的 2% 以内，因此 stop_loss 必须可执行。\n"
            "- leverage 取值在 1 到 15 之间。\n"
            "- 若多空双方都没有清晰优势，优先输出 HOLD 或 SKIP，而不是强行交易。\n"
            "\n"
            "【输出要求】\n"
            "只输出一个 JSON 对象，禁止输出任何额外说明。格式如下：\n"
            "{\n"
            "  \"decisions\": [\n"
            "    {\n"
            "      \"action\": \"OPEN_LONG\" | \"OPEN_SHORT\" | \"CLOSE_LONG\" | \"CLOSE_SHORT\" | \"HOLD\" | \"SKIP\",\n"
            "      \"symbol\": \"BTC-USDT-SWAP\" | \"ETH-USDT-SWAP\" | \"SOL-USDT-SWAP\",\n"
            "      \"confidence_score\": 0.0-1.0,\n"
            "      \"reasoning\": \"50字以内，简洁说明核心依据\",\n"
            "      \"position_pct\": 0.0-0.6,\n"
            "      \"leverage\": 1-15,\n"
            "      \"stop_loss\": 止损触发价,\n"
            "      \"take_profit_price\": 固定止盈触发价（可选）,\n"
            "      \"trailing_callback\": 移动止盈回调比例（可选）,\n"
            "      \"active_px\": 移动止盈激活价（可选）\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "必须返回恰好 3 条 decisions，分别对应 BTC-USDT-SWAP、ETH-USDT-SWAP、SOL-USDT-SWAP。"
        )
        user_prompt = f"当前市场数据与账户状态：\n{json.dumps(payload, ensure_ascii=False)}"

        try:
            raw_response, _ = self._request_once(self.primary_provider, system_prompt, user_prompt)
            parsed = self._extract_json_block(raw_response)
            return {"decisions": self._normalize_decisions(target_symbols, parsed), "provider": self.primary_provider["name"]}
        except Exception as primary_exc:  # noqa: BLE001
            reasoning_logger.warning("Qwen 调用失败，准备切换 DeepSeek。原因: %s", primary_exc)
            try:
                raw_response, _ = self._request_once(self.backup_provider, system_prompt, user_prompt)
                parsed = self._extract_json_block(raw_response)
                reasoning_logger.warning("已从 Qwen 切换到 DeepSeek 完成决策。")
                return {
                    "decisions": self._normalize_decisions(target_symbols, parsed),
                    "provider": self.backup_provider["name"],
                    "fallback_reason": str(primary_exc),
                }
            except Exception as backup_exc:  # noqa: BLE001
                error_reason = f"Qwen失败:{primary_exc}; DeepSeek失败:{backup_exc}"
                reasoning_logger.error("LLM 主备均失败，返回 SKIP。原因: %s", error_reason)
                return self._skip_decisions(target_symbols, error_reason)

    def review_recent_trades(self, **kwargs):
        return {"status": "skipped"}
