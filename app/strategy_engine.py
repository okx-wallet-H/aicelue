from typing import Any

from app.config import settings
from app.llm_analyzer import LLMAnalyzer
from app.reasoning import ReasoningChain
from app.risk_manager import RiskManager
from app.utils import clamp, safe_float


class StrategyEngine:
    def __init__(self, weights: dict[str, float], risk_manager: RiskManager) -> None:
        self.weights = weights
        self.risk_manager = risk_manager
        self.llm_analyzer = LLMAnalyzer()

    def _strategy_scores(self, state: str, tf_indicators: dict[str, dict[str, float]]) -> dict[str, float]:
        h4 = tf_indicators.get("4H", tf_indicators.get("1H", {}))
        h1 = tf_indicators["1H"]
        m15 = tf_indicators["15M"]

        adx = h1.get("adx", h4.get("adx", 0.0))
        if state in {"强势上涨", "强势下跌"}:
            trend_score = min(0.6 + (adx - 25) / 50, 1.0) if adx > 25 else 0.4
        elif state in {"弱势上涨", "弱势下跌"}:
            trend_score = min(0.3 + (adx - 20) / 60, 0.6)
        else:
            trend_score = 0.1

        rsi = m15["rsi14"]
        if state == "区间震荡":
            if rsi <= 30 or rsi >= 70:
                mr_score = 0.9
            elif rsi <= 38 or rsi >= 62:
                mr_score = 0.6
            else:
                mr_score = 0.3
        else:
            mr_score = max(0.0, 0.3 - abs(rsi - 50) / 100)

        boll_ratio = m15["bollinger_width"] / max(h1["bollinger_width"], 0.001)
        if boll_ratio >= 0.95:
            breakout_score = min(0.5 + boll_ratio * 0.3, 1.0)
        elif boll_ratio >= 0.7:
            breakout_score = 0.3 + (boll_ratio - 0.7) * 0.8
        else:
            breakout_score = max(boll_ratio * 0.4, 0.05)

        h1_aligned = h1["ema20"] > h1["ema60"]
        m15_aligned = m15["ema20"] > m15["ema60"]
        if state in {"强势上涨", "弱势上涨"}:
            if h1_aligned and m15_aligned:
                mom_score = 0.9
            elif h1_aligned:
                mom_score = 0.5
            else:
                mom_score = 0.1
        elif state in {"强势下跌", "弱势下跌"}:
            if not h1_aligned and not m15_aligned:
                mom_score = 0.9
            elif not h1_aligned:
                mom_score = 0.5
            else:
                mom_score = 0.1
        else:
            mom_score = 0.3

        return {
            "trend_following": round(clamp(trend_score, 0.0, 1.0), 4),
            "mean_reversion": round(clamp(mr_score, 0.0, 1.0), 4),
            "breakout": round(clamp(breakout_score, 0.0, 1.0), 4),
            "momentum_confirmation": round(clamp(mom_score, 0.0, 1.0), 4),
        }

    def _weighted_score(self, scores: dict[str, float]) -> float:
        return sum(self.weights.get(name, 0.0) * value for name, value in scores.items())

    def _leverage(self, symbol: str, market_state: str, weighted_score: float = 0.0) -> int:
        if symbol == "BTC-USDT-SWAP":
            base = settings.btc_leverage_strong if market_state in {"强势上涨", "强势下跌"} else settings.btc_leverage_normal
        else:
            base = settings.sol_leverage_strong if market_state in {"强势上涨", "强势下跌"} else settings.sol_leverage_normal

        if weighted_score >= 0.8:
            signal_multiplier = 1.5
        elif weighted_score >= 0.6:
            signal_multiplier = 1.2
        elif weighted_score >= 0.4:
            signal_multiplier = 1.0
        else:
            signal_multiplier = 0.8

        scale = self.risk_manager.leverage_scale(symbol)
        final_leverage = max(2, min(int(round(base * scale * signal_multiplier)), 10))
        return final_leverage

    def _grade_signal(self, attack_score: float) -> str:
        if attack_score >= settings.knife_attack_min_score:
            return "A+"
        if attack_score >= 0.78:
            return "A"
        if attack_score >= 0.66:
            return "B"
        return "C"

    def _attack_score(
        self,
        state_name: str,
        weighted_score: float,
        h1: dict[str, float],
        m15: dict[str, float],
        market_state: dict[str, Any],
        oi_change_rate: float,
        obi: float,
    ) -> float:
        score = 0.0
        strong_long = state_name == "强势上涨" and h1["ema20"] > h1["ema60"] and m15["ema20"] > m15["ema60"]
        strong_short = state_name == "强势下跌" and h1["ema20"] < h1["ema60"] and m15["ema20"] < m15["ema60"]

        if strong_long or strong_short:
            score += 0.34
        elif state_name in {"弱势上涨", "弱势下跌"}:
            score += 0.18
        elif state_name == "区间震荡":
            score += 0.06

        score += min(max(weighted_score, 0.0), 1.0) * 0.22

        adx = float(market_state.get("adx", 0.0))
        score += min(max((adx - settings.adx_high) / 12.0, 0.0), 1.0) * 0.16

        atr_change_rate = abs(float(market_state.get("atr_change_rate", 0.0)))
        score += min(atr_change_rate / max(settings.atr_extreme_change, 0.01), 1.0) * 0.10

        if strong_long:
            if 52 <= m15["rsi14"] <= 74:
                score += 0.08
            if obi >= 0.08:
                score += 0.05
            if oi_change_rate > 0:
                score += 0.05
        elif strong_short:
            if 26 <= m15["rsi14"] <= 48:
                score += 0.08
            if obi <= -0.08:
                score += 0.05
            if oi_change_rate > 0:
                score += 0.05

        return clamp(score, 0.0, 1.0)

    def _position_snapshot(self, position_info: dict[str, Any] | None) -> dict[str, Any]:
        info = position_info or {}
        net_pos = safe_float(info.get("net_pos"), 0.0)
        if net_pos > 0:
            side = "buy"
        elif net_pos < 0:
            side = "sell"
        else:
            side = None
        return {
            "net_pos": net_pos,
            "side": side,
            "abs_pos": abs(net_pos),
            "entry_px": safe_float(info.get("avg_px"), 0.0),
            "mark_px": safe_float(info.get("mark_px"), 0.0),
            "upl_ratio": safe_float(info.get("upl_ratio"), 0.0),
        }

    def _technical_direction(self, state_name: str, h1: dict[str, float], m15: dict[str, float], obi: float) -> str:
        """根据技术指标提炼当前技术方向，用于与 LLM 建议做一致性比较。"""
        if state_name in {"强势上涨", "弱势上涨"}:
            return "OPEN_LONG"
        if state_name in {"强势下跌", "弱势下跌"}:
            return "OPEN_SHORT"
        if h1["ema20"] > h1["ema60"] and m15["ema20"] >= m15["ema60"]:
            return "OPEN_LONG"
        if h1["ema20"] < h1["ema60"] and m15["ema20"] <= m15["ema60"]:
            return "OPEN_SHORT"
        if obi >= 0.08:
            return "OPEN_LONG"
        if obi <= -0.08:
            return "OPEN_SHORT"
        return "HOLD"

    def _apply_llm_weighting(
        self,
        weighted_score: float,
        technical_direction: str,
        llm_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把 LLM 方向与置信度映射为技术分的附加加减权。"""
        analysis = llm_analysis or {}
        trade_advice = analysis.get("trade_advice") or {}
        llm_action = str(trade_advice.get("action") or "HOLD").upper()
        llm_confidence = clamp(safe_float(trade_advice.get("confidence")), 0.0, 1.0)
        adjustment = {
            "llm_action": llm_action,
            "llm_confidence": round(llm_confidence, 4),
            "technical_direction": technical_direction,
            "relation": "degraded",
            "score_delta": 0.0,
            "adjusted_weighted_score": round(weighted_score, 4),
            "should_skip_entry": False,
            "notes": [],
        }

        if not analysis or bool(analysis.get("degraded")) or not bool(analysis.get("enabled")):
            adjustment["notes"].append("LLM 当前不可用，本轮沿用纯技术指标模式。")
            return adjustment

        delta = llm_confidence * safe_float(settings.llm_confidence_weight)
        aligned_boost = llm_confidence * safe_float(settings.llm_alignment_boost)
        conflict_penalty = llm_confidence * safe_float(settings.llm_conflict_penalty)
        hold_penalty = llm_confidence * safe_float(settings.llm_hold_penalty)
        adjusted_score = weighted_score

        if llm_action == technical_direction and llm_action in {"OPEN_LONG", "OPEN_SHORT"}:
            adjusted_score += delta + aligned_boost
            adjustment["relation"] = "aligned"
            adjustment["notes"].append(f"LLM 与技术方向一致（{llm_action}），提高总分。")
        elif technical_direction == "HOLD" and llm_action in {"OPEN_LONG", "OPEN_SHORT"}:
            adjusted_score += delta * 0.5
            adjustment["relation"] = "lead"
            adjustment["notes"].append(f"技术面暂中性，但 LLM 给出 {llm_action} 倾向，轻度提高总分。")
        elif llm_action == "HOLD" and technical_direction in {"OPEN_LONG", "OPEN_SHORT"}:
            adjusted_score -= hold_penalty
            adjustment["relation"] = "hold_conflict"
            adjustment["notes"].append("LLM 建议观望，适度压低技术分。")
        elif llm_action in {"OPEN_LONG", "OPEN_SHORT"} and technical_direction in {"OPEN_LONG", "OPEN_SHORT"} and llm_action != technical_direction:
            adjusted_score -= delta + conflict_penalty
            adjustment["relation"] = "conflict"
            adjustment["notes"].append(f"LLM 与技术方向冲突（技术={technical_direction}, LLM={llm_action}），降低总分。")
        else:
            adjustment["relation"] = "neutral"
            adjustment["notes"].append("LLM 未提供明确增益方向，维持原技术分。")

        adjusted_score = clamp(adjusted_score, 0.0, 1.0)
        adjustment["score_delta"] = round(adjusted_score - weighted_score, 4)
        adjustment["adjusted_weighted_score"] = round(adjusted_score, 4)
        adjustment["should_skip_entry"] = adjustment["relation"] in {"conflict", "hold_conflict"} and llm_confidence >= safe_float(settings.llm_skip_conflict_threshold)
        return adjustment

    @staticmethod
    def _llm_summary_text(llm_analysis: dict[str, Any] | None = None, llm_adjustment: dict[str, Any] | None = None) -> str:
        analysis = llm_analysis or {}
        adjustment = llm_adjustment or {}
        trade_advice = analysis.get("trade_advice") or {}
        market_analysis = analysis.get("market_analysis") or {}
        risk_assessment = analysis.get("risk_assessment") or {}
        reasoning_process = analysis.get("reasoning_process") or []
        if isinstance(reasoning_process, str):
            reasoning_process = [reasoning_process]

        if bool(analysis.get("degraded")) or not analysis:
            notes = adjustment.get("notes") or [analysis.get("error") or "LLM 不可用"]
            note_text = "；".join(str(item) for item in notes if str(item).strip())
            return f"LLM降级，说明={note_text}"

        trend = str(market_analysis.get("overall_trend") or "未知")
        confidence = safe_float(trade_advice.get("confidence"))
        action = str(trade_advice.get("action") or "HOLD")
        risk_level = str(risk_assessment.get("level") or "中")
        relation = str(adjustment.get("relation") or "neutral")
        core_reason = "；".join(str(item) for item in list(reasoning_process)[:3])
        return f"趋势={trend}，建议={action}，置信度={confidence:.2f}，风险={risk_level}，关系={relation}，推理={core_reason}"

    def _btc_weathervane_signal(self, btc_tf_indicators: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
        """基于 BTC 1H ADX、均线与 RSI 生成全市场风向标。"""
        default_signal = {
            "status": "NEUTRAL",
            "trend": "震荡",
            "adx": 0.0,
            "rsi": 50.0,
            "ema20": 0.0,
            "ema60": 0.0,
            "close": 0.0,
            "ma_signal": "MA20=MA60",
            "reason": "[BTC风向标] BTC 信号获取失败，按中性处理，不影响 SOL 正常决策。",
        }
        if not btc_tf_indicators:
            return default_signal

        try:
            h1 = btc_tf_indicators.get("1H") or {}
            if not h1:
                return default_signal

            adx = safe_float(h1.get("adx"), 0.0)
            rsi = safe_float(h1.get("rsi14"), 50.0)
            ema20 = safe_float(h1.get("ema20"), 0.0)
            ema60 = safe_float(h1.get("ema60"), 0.0)
            close = safe_float(h1.get("close"), 0.0)

            if ema20 > ema60:
                ma_signal = "MA20>MA60"
            elif ema20 < ema60:
                ma_signal = "MA20<MA60"
            else:
                ma_signal = "MA20=MA60"

            bullish = adx >= settings.adx_high and ema20 > ema60 and rsi >= 55 and close >= ema20
            bearish = adx >= settings.adx_high and ema20 < ema60 and rsi <= 45 and close <= ema20

            # 对临界值做一次宽松容错，避免 BTC 短暂抖动导致风向标频繁翻转
            if not bullish and not bearish:
                bullish = adx >= settings.adx_low and ema20 > ema60 and rsi >= 52
                bearish = adx >= settings.adx_low and ema20 < ema60 and rsi <= 48

            if bullish:
                status = "BULLISH"
                trend = "多头"
            elif bearish:
                status = "BEARISH"
                trend = "空头"
            else:
                status = "NEUTRAL"
                trend = "震荡"

            return {
                "status": status,
                "trend": trend,
                "adx": round(adx, 4),
                "rsi": round(rsi, 4),
                "ema20": round(ema20, 4),
                "ema60": round(ema60, 4),
                "close": round(close, 4),
                "ma_signal": ma_signal,
                "reason": f"[BTC风向标] BTC 1H趋势：{trend}（ADX={adx:.0f}, {ma_signal}, RSI={rsi:.0f}）",
            }
        except Exception:
            return default_signal

    @staticmethod
    def _unique_notes(notes: list[str]) -> list[str]:
        ordered: list[str] = []
        for note in notes:
            text = str(note or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        return ordered

    def _sol_btc_weathervane_adjustment(
        self,
        state_name: str,
        h1: dict[str, float],
        m15: dict[str, float],
        btc_weathervane: dict[str, Any] | None = None,
        btc_turn_alert: str | None = None,
    ) -> dict[str, Any]:
        """将 BTC 风向标映射为 SOL 的置信度、仓位与止盈风控调整。"""
        signal = btc_weathervane or {}
        status = str(signal.get("status") or "NEUTRAL").upper()
        notes: list[str] = [str(signal.get("reason") or "[BTC风向标] BTC 信号缺失，SOL 按常规逻辑决策。")]

        adjustment = {
            "confidence_delta": 0.0,
            "position_scale": 1.0,
            "tight_take_profit_to_1r": False,
            "pause_add_position": False,
            "notes": notes,
        }

        if btc_turn_alert:
            adjustment["notes"].append(btc_turn_alert)

        if status == "NEUTRAL":
            return adjustment

        sol_long_bias = state_name in {"强势上涨", "弱势上涨"} or (h1["ema20"] > h1["ema60"] and m15["ema20"] > m15["ema60"])
        sol_short_bias = state_name in {"强势下跌", "弱势下跌"} or (h1["ema20"] < h1["ema60"] and m15["ema20"] < m15["ema60"])

        if status == "BULLISH":
            if sol_long_bias:
                adjustment["confidence_delta"] = 0.05
                adjustment["position_scale"] = 1.15
                adjustment["notes"].append("[BTC风向标] BTC 偏多 → SOL做多信号获得+0.05置信度加分，并允许更大仓位。")
            elif sol_short_bias:
                adjustment["confidence_delta"] = -0.05
                adjustment["position_scale"] = 0.85
                adjustment["tight_take_profit_to_1r"] = True
                adjustment["pause_add_position"] = True
                adjustment["notes"].append("[BTC风向标] BTC 偏多 → SOL做空信号削减0.05置信度，止盈收紧至1R并暂停加仓。")
        elif status == "BEARISH":
            if sol_short_bias:
                adjustment["confidence_delta"] = 0.05
                adjustment["position_scale"] = 1.15
                adjustment["notes"].append("[BTC风向标] BTC 偏空 → SOL做空信号获得+0.05置信度加分，并允许更大仓位。")
            elif sol_long_bias:
                adjustment["confidence_delta"] = -0.05
                adjustment["position_scale"] = 0.85
                adjustment["tight_take_profit_to_1r"] = True
                adjustment["pause_add_position"] = True
                adjustment["notes"].append("[BTC风向标] BTC 偏空 → SOL做多信号削减0.05置信度，止盈收紧至1R并暂停加仓。")

        return adjustment

    def _entry_signal(
        self,
        state_name: str,
        h1: dict[str, float],
        m15: dict[str, float],
        obi: float,
        position_ratio: float,
        weighted_score: float,
        confidence_threshold: float,
        crowded: bool,
        extreme_volatility: bool,
    ) -> tuple[str, str | None, float, str, str]:
        action = "HOLD"
        side = None
        entry_bias = "无有效入场偏向"
        final_reason = "当前暂无可执行信号。"

        if self.risk_manager.should_stop_new_trades():
            return action, side, position_ratio, entry_bias, "当前触发账户级风控熔断，仅允许观望。"
        if crowded and extreme_volatility:
            return action, side, position_ratio, entry_bias, "当前同时出现拥挤与极端波动风险，主动跳过新开仓。"
        if position_ratio <= 0:
            return action, side, position_ratio, entry_bias, "凯利公式给出的仓位比例为0，当前不新开仓。"
        if weighted_score < confidence_threshold:
            return action, side, position_ratio, entry_bias, f"当前加权分 {weighted_score:.4f} 低于自适应置信度门槛 {confidence_threshold:.4f}，暂不新开仓。"

        if state_name == "强势上涨":
            if h1["ema20"] > h1["ema60"] and m15["ema20"] > m15["ema60"] and m15["rsi14"] <= 82:
                action, side = "OPEN_LONG", "buy"
                entry_bias = "强势上涨趋势跟踪做多"
                final_reason = "强势上涨，多周期多头共振，允许正常仓位追随趋势。"
            elif h1["ema20"] > h1["ema60"] and m15["rsi14"] <= 74:
                action, side = "OPEN_LONG", "buy"
                position_ratio = clamp(position_ratio * 0.55, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "强势上涨先试探后加仓"
                final_reason = "强势上涨但15M未完全共振，先以小仓位试探上车。"
            else:
                final_reason = "强势上涨但短周期过热或结构不足，暂不追价。"
        elif state_name == "弱势上涨":
            if h1["ema20"] > h1["ema60"] and m15["rsi14"] <= 72:
                action, side = "OPEN_LONG", "buy"
                position_ratio = clamp(position_ratio * 0.35, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "弱势上涨小仓位跟随做多"
                final_reason = "弱势上涨阶段，采用小仓位跟随做多积累数据。"
            else:
                final_reason = "弱势上涨但追价性价比不足，等待更好节奏。"
        elif state_name == "区间震荡":
            if m15["rsi14"] <= 38 and obi > -0.12:
                action, side = "OPEN_LONG", "buy"
                position_ratio = clamp(position_ratio * 0.40, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "震荡下沿均值回归做多"
                final_reason = "区间震荡接近超卖，执行小仓位低吸。"
            elif m15["rsi14"] >= 62 and obi < 0.12:
                action, side = "OPEN_SHORT", "sell"
                position_ratio = clamp(position_ratio * 0.40, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "震荡上沿均值回归做空"
                final_reason = "区间震荡接近超买，执行小仓位高抛。"
            elif obi >= 0:
                action, side = "OPEN_LONG", "buy"
                position_ratio = clamp(position_ratio * 0.22, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "震荡中性偏多试探"
                final_reason = "震荡中部也保持资金流动，以更小试探仓参与。"
            else:
                action, side = "OPEN_SHORT", "sell"
                position_ratio = clamp(position_ratio * 0.22, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "震荡中性偏空试探"
                final_reason = "震荡中部也保持资金流动，以更小试探仓参与。"
        elif state_name == "弱势下跌":
            if h1["ema20"] < h1["ema60"] and m15["rsi14"] >= 28:
                action, side = "OPEN_SHORT", "sell"
                position_ratio = clamp(position_ratio * 0.35, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "弱势下跌小仓位跟随做空"
                final_reason = "弱势下跌阶段，采用小仓位跟随做空积累数据。"
            else:
                final_reason = "弱势下跌但短线过冷，暂缓继续追空。"
        elif state_name == "强势下跌":
            if h1["ema20"] < h1["ema60"] and m15["ema20"] < m15["ema60"] and m15["rsi14"] >= 18:
                action, side = "OPEN_SHORT", "sell"
                entry_bias = "强势下跌趋势跟踪做空"
                final_reason = "强势下跌，多周期空头共振，允许正常仓位追随趋势。"
            elif h1["ema20"] < h1["ema60"] and m15["rsi14"] >= 26:
                action, side = "OPEN_SHORT", "sell"
                position_ratio = clamp(position_ratio * 0.55, settings.min_position_ratio_initial, position_ratio)
                entry_bias = "强势下跌先试探后加仓"
                final_reason = "强势下跌但15M未完全共振，先以小仓位试探做空。"
            else:
                final_reason = "强势下跌但短周期过冷，暂不在最低点追空。"
        else:
            final_reason = "未知市场状态，维持观望。"

        return action, side, position_ratio, entry_bias, final_reason

    def _close_signal(
        self,
        symbol: str,
        state_name: str,
        h1: dict[str, float],
        m15: dict[str, float],
        weighted_score: float,
        confidence_threshold: float,
        position_snapshot: dict[str, Any],
        btc_weathervane: dict[str, Any] | None = None,
        btc_turn_alert: str | None = None,
    ) -> tuple[str | None, str]:
        pos_side = position_snapshot["side"]
        if not pos_side:
            return None, "当前无持仓，无需平仓。"

        upl_ratio = position_snapshot["upl_ratio"]
        low_confidence = weighted_score < max(confidence_threshold * 0.92, confidence_threshold - 0.06)
        long_reversal = h1["ema20"] < h1["ema60"] or (m15["ema20"] < m15["ema60"] and m15["rsi14"] < 48)
        short_reversal = h1["ema20"] > h1["ema60"] or (m15["ema20"] > m15["ema60"] and m15["rsi14"] > 52)
        btc_status = str((btc_weathervane or {}).get("status") or "NEUTRAL").upper()
        alert_prefix = f"{btc_turn_alert} " if btc_turn_alert else ""

        btc_pressure_on_sol_long = symbol == "SOL-USDT-SWAP" and pos_side == "buy" and btc_status == "BEARISH"
        btc_pressure_on_sol_short = symbol == "SOL-USDT-SWAP" and pos_side == "sell" and btc_status == "BULLISH"

        # Hotfix 4: 增加"反向强信号（Score > 0.6）强制平仓"逻辑
        if pos_side == "buy":
            # 如果当前是多单，但市场状态是下跌且加权分较高（说明空头信号强）
            if state_name in {"强势下跌", "弱势下跌"} and weighted_score > 0.6:
                return "CLOSE_LONG", f"检测到反向强信号(Score={weighted_score:.2f})，多单强制平仓。"

            if btc_pressure_on_sol_long and upl_ratio >= 0.01:
                return "CLOSE_LONG", f"{alert_prefix}BTC风向标偏空，SOL多单止盈收紧至1R，当前浮盈{upl_ratio * 100:.1f}%后执行保护性离场。"
            if btc_pressure_on_sol_long and (long_reversal or low_confidence):
                return "CLOSE_LONG", f"{alert_prefix}BTC风向标偏空，且SOL多头结构转弱，多单风控升级后主动平仓。"
            if state_name in {"强势下跌", "弱势下跌"}:
                return "CLOSE_LONG", "市场状态已转入下跌区间，多单按策略退出。"
            if long_reversal and low_confidence:
                return "CLOSE_LONG", "1H/15M 多头结构失效，且当前置信度不足，多单主动平仓。"
            if upl_ratio >= 0.03 and m15["rsi14"] >= 70:
                return "CLOSE_LONG", f"多单浮盈{upl_ratio * 100:.1f}%且15M RSI过热({m15['rsi14']:.0f})，执行移动止盈。"
            if upl_ratio >= 0.02 and m15["ema20"] < m15["ema60"]:
                return "CLOSE_LONG", f"多单浮盈{upl_ratio * 100:.1f}%且15M EMA死叉，执行保护性止盈。"
            if upl_ratio <= -0.025:
                return "CLOSE_LONG", f"多单浮亏{abs(upl_ratio) * 100:.1f}%超过软止损线，主动平仓止损。"
            return None, "持有多单，当前结构未触发退出条件。"

        if pos_side == "sell":
            # 如果当前是空单，但市场状态是上涨且加权分较高（说明多头信号强）
            if state_name in {"强势上涨", "弱势上涨"} and weighted_score > 0.6:
                return "CLOSE_SHORT", f"检测到反向强信号(Score={weighted_score:.2f})，空单强制平仓。"

            if btc_pressure_on_sol_short and upl_ratio >= 0.01:
                return "CLOSE_SHORT", f"{alert_prefix}BTC风向标偏多，SOL空单止盈收紧至1R，当前浮盈{upl_ratio * 100:.1f}%后执行保护性离场。"
            if btc_pressure_on_sol_short and (short_reversal or low_confidence):
                return "CLOSE_SHORT", f"{alert_prefix}BTC风向标偏多，且SOL空头结构转弱，空单风控升级后主动平仓。"
            if state_name in {"强势上涨", "弱势上涨"}:
                return "CLOSE_SHORT", "市场状态已转入上涨区间，空单按策略退出。"
            if short_reversal and low_confidence:
                return "CLOSE_SHORT", "1H/15M 空头结构失效，且当前置信度不足，空单主动平仓。"
            if upl_ratio >= 0.03 and m15["rsi14"] <= 30:
                return "CLOSE_SHORT", f"空单浮盈{upl_ratio * 100:.1f}%且15M RSI过冷({m15['rsi14']:.0f})，执行移动止盈。"
            if upl_ratio >= 0.02 and m15["ema20"] > m15["ema60"]:
                return "CLOSE_SHORT", f"空单浮盈{upl_ratio * 100:.1f}%且15M EMA金叉，执行保护性止盈。"
            if upl_ratio <= -0.025:
                return "CLOSE_SHORT", f"空单浮亏{abs(upl_ratio) * 100:.1f}%超过软止损线，主动平仓止损。"
            return None, "持有空单，当前结构未触发退出条件。"

        return None, "当前无持仓，无需平仓。"

    def decide(
        self,
        symbol_snapshot: dict[str, Any],
        tf_indicators: dict[str, dict[str, float]],
        market_state: dict[str, Any],
        position_info: dict[str, Any] | None = None,
        rootdata_metrics: dict[str, Any] | None = None,
        btc_weathervane: dict[str, Any] | None = None,
        btc_turn_alert: str | None = None,
        market_context: dict[str, Any] | None = None,
        positions: dict[str, Any] | None = None,
        recent_records: list[dict[str, Any]] | None = None,
        recent_trades: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        symbol = str(symbol_snapshot["symbol"])
        h1 = tf_indicators["1H"]
        m15 = tf_indicators["15M"]
        state_name = str(market_state["overall_state"])
        funding_rate = float(symbol_snapshot["funding_rate"])
        oi_change_rate = float(symbol_snapshot["oi_change_rate"])
        obi = float(symbol_snapshot["obi"])
        crowded = bool(market_state.get("crowding_risk"))
        extreme_volatility = bool(market_state.get("extreme_volatility_risk"))

        orderbook_factor = 1.0 if abs(obi) >= 0.10 else 0.85
        adaptive_params = self.risk_manager.adaptive_params
        state_confidence_bonus = safe_float(adaptive_params.get("state_confidence_bonus", {}).get(state_name), 0.0)
        scores = self._strategy_scores(state_name, tf_indicators)

        # 集成 RootData 信号
        rd_bonus = 0.0
        if rootdata_metrics:
            # 热度排名变化作为趋势确认辅助
            if rootdata_metrics.get("heat_rank", 999) <= 10:
                rd_bonus += 0.05
            # 影响力指数作为基本面参考
            if rootdata_metrics.get("influence_index", 0) > 80:
                rd_bonus += 0.03
            # 增长指数
            if rootdata_metrics.get("growth_index", 0) > 2.0:
                rd_bonus += 0.02

        weighted_score = clamp(self._weighted_score(scores) + rd_bonus, 0.0, 1.0)
        confidence_threshold = clamp(
            safe_float(adaptive_params.get("confidence_threshold"), settings.confidence_threshold_default) + state_confidence_bonus,
            settings.confidence_threshold_min,
            settings.confidence_threshold_max,
        )
        position_ratio = self.risk_manager.position_ratio(
            symbol=symbol,
            market_state=state_name,
            funding_rate=funding_rate,
            atr_change_rate=float(market_state["atr_change_rate"]),
            orderbook_factor=orderbook_factor,
        )
        leverage = self._leverage(symbol, state_name, weighted_score=weighted_score)
        position_ratio = clamp(position_ratio, settings.min_position_ratio, settings.max_position_ratio)
        position_snapshot = self._position_snapshot(position_info)
        llm_analysis: dict[str, Any] = {
            "enabled": False,
            "degraded": True,
            "trade_advice": {"action": "HOLD", "direction": "none", "confidence": 0.0},
            "reasoning_process": ["尚未触发 LLM 分析。"],
        }
        llm_adjustment: dict[str, Any] = {
            "llm_action": "HOLD",
            "llm_confidence": 0.0,
            "technical_direction": "HOLD",
            "relation": "degraded",
            "score_delta": 0.0,
            "adjusted_weighted_score": round(weighted_score, 4),
            "should_skip_entry": False,
            "notes": ["尚未应用 LLM 加权。"],
        }

        weathervane_notes: list[str] = []
        sol_risk_overrides = {
            "tight_take_profit_to_1r": False,
            "pause_add_position": False,
            "confidence_delta": 0.0,
        }

        if symbol == "BTC-USDT-SWAP":
            if btc_weathervane:
                weathervane_notes.append("[BTC风向标] 当前标的即 BTC，自身趋势同时作为全市场锚点信号。")
                weathervane_notes.append(str(btc_weathervane.get("reason") or ""))
        elif symbol == "SOL-USDT-SWAP":
            sol_adjustment = self._sol_btc_weathervane_adjustment(
                state_name=state_name,
                h1=h1,
                m15=m15,
                btc_weathervane=btc_weathervane,
                btc_turn_alert=btc_turn_alert,
            )
            sol_risk_overrides["confidence_delta"] = safe_float(sol_adjustment.get("confidence_delta"), 0.0)
            sol_risk_overrides["tight_take_profit_to_1r"] = bool(sol_adjustment.get("tight_take_profit_to_1r"))
            sol_risk_overrides["pause_add_position"] = bool(sol_adjustment.get("pause_add_position"))
            weighted_score = clamp(weighted_score + sol_risk_overrides["confidence_delta"], 0.0, 1.0)
            position_ratio = clamp(
                position_ratio * safe_float(sol_adjustment.get("position_scale"), 1.0),
                settings.min_position_ratio,
                settings.max_position_ratio,
            )
            weathervane_notes.extend(sol_adjustment.get("notes") or [])

        technical_direction = self._technical_direction(state_name=state_name, h1=h1, m15=m15, obi=obi)
        llm_analysis = self.llm_analyzer.analyze_trade_decision(
            target_symbol=symbol,
            market_context=market_context or {},
            positions=positions or {symbol: position_info or {}},
            recent_records=recent_records or [],
            recent_trades=recent_trades or [],
            btc_weathervane=btc_weathervane,
            btc_turn_alert=btc_turn_alert,
        )
        llm_adjustment = self._apply_llm_weighting(
            weighted_score=weighted_score,
            technical_direction=technical_direction,
            llm_analysis=llm_analysis,
        )
        weighted_score = safe_float(llm_adjustment.get("adjusted_weighted_score"), weighted_score)

        action, side, position_ratio, entry_bias, final_reason = self._entry_signal(
            state_name=state_name,
            h1=h1,
            m15=m15,
            obi=obi,
            position_ratio=position_ratio,
            weighted_score=weighted_score,
            confidence_threshold=confidence_threshold,
            crowded=crowded,
            extreme_volatility=extreme_volatility,
        )

        close_action, close_reason = self._close_signal(
            symbol=symbol,
            state_name=state_name,
            h1=h1,
            m15=m15,
            weighted_score=weighted_score,
            confidence_threshold=confidence_threshold,
            position_snapshot=position_snapshot,
            btc_weathervane=btc_weathervane,
            btc_turn_alert=btc_turn_alert,
        )
        if close_action:
            action = close_action
            side = "sell" if close_action == "CLOSE_LONG" else "buy"
            entry_bias = "持仓退出"
            position_ratio = 0.0
            final_reason = close_reason
        elif position_snapshot["side"] == side and action in {"OPEN_LONG", "OPEN_SHORT"}:
            action = "HOLD"
            side = None
            entry_bias = "持仓续持"
            final_reason = f"当前已持有同向仓位 {position_snapshot['abs_pos']:.2f} 张，避免重复加仓，继续观察。"
            if symbol == "SOL-USDT-SWAP" and sol_risk_overrides["pause_add_position"]:
                final_reason += " BTC风向标处于逆风方向，暂停 SOL 同向加仓。"
        elif position_snapshot["side"] and action in {"OPEN_LONG", "OPEN_SHORT"}:
            action = "HOLD"
            side = None
            entry_bias = "等待反手前先平旧仓"
            final_reason = f"当前存在反向持仓 {position_snapshot['abs_pos']:.2f} 张，需先完成平仓，再考虑反手。"

        if close_action is None and action in {"OPEN_LONG", "OPEN_SHORT"} and bool(llm_adjustment.get("should_skip_entry")):
            llm_action = str((llm_analysis.get("trade_advice") or {}).get("action") or "HOLD")
            llm_confidence = safe_float((llm_analysis.get("trade_advice") or {}).get("confidence"))
            action = "HOLD"
            side = None
            position_ratio = 0.0
            entry_bias = "LLM冲突过滤"
            final_reason = f"大模型以 {llm_confidence:.2f} 的高置信度给出 {llm_action}，与技术方向明显冲突，本轮跳过新开仓。"

        attack_score = self._attack_score(
            state_name=state_name,
            weighted_score=weighted_score,
            h1=h1,
            m15=m15,
            market_state=market_state,
            oi_change_rate=oi_change_rate,
            obi=obi,
        )
        signal_grade = self._grade_signal(attack_score)
        knife_attack_eligible = (
            settings.knife_attack_enabled
            and action in {"OPEN_LONG", "OPEN_SHORT"}
            and signal_grade == "A+"
            and weighted_score >= settings.knife_attack_min_weighted_score
            and state_name in {"强势上涨", "强势下跌"}
            and position_snapshot["abs_pos"] == 0
        )

        # BTC 风向标逆风时，不允许 SOL 继续使用更激进的尖刀连进攻仓
        if symbol == "SOL-USDT-SWAP" and sol_risk_overrides["pause_add_position"]:
            knife_attack_eligible = False

        unique_weathervane_notes = self._unique_notes(weathervane_notes)
        if unique_weathervane_notes:
            final_reason = f"{' '.join(unique_weathervane_notes)} {final_reason}"

        llm_summary = self._llm_summary_text(llm_analysis=llm_analysis, llm_adjustment=llm_adjustment)
        final_reason = (
            f"动作={action}，方向={side or 'none'}，常规仓位比例={position_ratio:.4f}，常规杠杆={leverage}，"
            f"加权分={weighted_score:.4f}，自适应门槛={confidence_threshold:.4f}，信号等级={signal_grade}，进攻分={attack_score:.4f}，"
            f"LLM分数增量={safe_float(llm_adjustment.get('score_delta')):.4f}。当前净持仓={position_snapshot['net_pos']:.2f}。"
            f"说明：{final_reason} LLM摘要：{llm_summary}"
        )
        if crowded:
            final_reason += " 当前资金费率提示拥挤。"
        if extreme_volatility:
            final_reason += " 当前波动处于扩张阶段。"
        if knife_attack_eligible:
            final_reason += " 满足尖刀连A+触发条件，可参与30U逐仓50倍进攻仓。"

        if rootdata_metrics:
            rootdata_summary = (
                f"RootData：热度排名={rootdata_metrics.get('heat_rank', 'N/A')}，"
                f"影响力={rootdata_metrics.get('influence_index', 'N/A')}，"
                f"增长指数={rootdata_metrics.get('growth_index', 'N/A')}。"
            )
        else:
            rootdata_summary = "RootData 数据不可用，使用默认值。"

        if unique_weathervane_notes:
            btc_reasoning_summary = " ".join(unique_weathervane_notes)
        else:
            btc_reasoning_summary = "[BTC风向标] 当前未触发额外联动调整。"

        reasoning = ReasoningChain(
            market_state=(
                f"4H状态={state_name}，ADX={float(market_state['adx']):.2f}，"
                f"EMA结构={market_state['ema_structure']}，波动={market_state['volatility_state']}。"
            ),
            symbol_selection=(
                f"仅在 BTC 与 SOL 中择优，当前标的={symbol}，并排除 ETH。"
                f" {btc_reasoning_summary} {rootdata_summary} LLM摘要={llm_summary}"
            ),
            rhythm_1h=(
                f"1H EMA20={h1['ema20']:.2f}，EMA60={h1['ema60']:.2f}，RSI={h1['rsi14']:.2f}，"
                f"用于判断节奏与趋势延续性。"
            ),
            entry_15m=(
                f"15M EMA20={m15['ema20']:.2f}，EMA60={m15['ema60']:.2f}，RSI={m15['rsi14']:.2f}，"
                f"用于确认入场与短线反转窗口。"
            ),
            crowding=(
                f"资金费率={funding_rate:.6f}，持仓量变化率={oi_change_rate:.4f}，"
                f"拥挤风险={'高' if crowded else '正常'}。"
            ),
            orderbook=(
                f"OBI={obi:.4f}，盘口因子={orderbook_factor:.2f}，决策偏向={entry_bias}，"
                f"尖刀连进攻分={attack_score:.4f}。"
            ),
            final_action=final_reason,
        )

        return {
            "symbol": symbol,
            "action": action,
            "side": side,
            "position_ratio": position_ratio,
            "leverage": leverage,
            "td_mode": "isolated",
            "weighted_score": weighted_score,
            "confidence_threshold": confidence_threshold,
            "signal_grade": signal_grade,
            "attack_score": attack_score,
            "knife_attack_eligible": knife_attack_eligible,
            "sub_strategy_scores": scores,
            "reasoning": reasoning,
            "position_snapshot": position_snapshot,
            "btc_weathervane": btc_weathervane or {"status": "NEUTRAL", "trend": "震荡"},
            "tight_take_profit_to_1r": bool(sol_risk_overrides["tight_take_profit_to_1r"]),
            "pause_add_position": bool(sol_risk_overrides["pause_add_position"]),
            "llm_analysis": llm_analysis,
            "llm_adjustment": llm_adjustment,
        }
