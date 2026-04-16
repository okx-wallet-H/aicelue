from __future__ import annotations

from typing import Any

from app.config import settings
from app.reasoning import ReasoningChain
from app.risk_manager import RiskManager
from app.utils import clamp, safe_float


class StrategyEngine:
    def __init__(self, weights: dict[str, float], risk_manager: RiskManager) -> None:
        self.weights = weights
        self.risk_manager = risk_manager

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
        state_name: str,
        h1: dict[str, float],
        m15: dict[str, float],
        weighted_score: float,
        confidence_threshold: float,
        position_snapshot: dict[str, Any],
    ) -> tuple[str | None, str]:
        pos_side = position_snapshot["side"]
        if not pos_side:
            return None, "当前无持仓，无需平仓。"

        upl_ratio = position_snapshot["upl_ratio"]
        low_confidence = weighted_score < max(confidence_threshold * 0.92, confidence_threshold - 0.06)
        long_reversal = h1["ema20"] < h1["ema60"] or (m15["ema20"] < m15["ema60"] and m15["rsi14"] < 48)
        short_reversal = h1["ema20"] > h1["ema60"] or (m15["ema20"] > m15["ema60"] and m15["rsi14"] > 52)

        # Hotfix 4: 增加"反向强信号（Score > 0.6）强制平仓"逻辑
        if pos_side == "buy":
            # 如果当前是多单，但市场状态是下跌且加权分较高（说明空头信号强）
            if state_name in {"强势下跌", "弱势下跌"} and weighted_score > 0.6:
                return "CLOSE_LONG", f"检测到反向强信号(Score={weighted_score:.2f})，多单强制平仓。"
            
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
    ) -> dict[str, Any]:
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
            symbol=symbol_snapshot["symbol"],
            market_state=state_name,
            funding_rate=funding_rate,
            atr_change_rate=float(market_state["atr_change_rate"]),
            orderbook_factor=orderbook_factor,
        )
        leverage = self._leverage(symbol_snapshot["symbol"], state_name, weighted_score=weighted_score)
        position_ratio = clamp(position_ratio, settings.min_position_ratio, settings.max_position_ratio)
        position_snapshot = self._position_snapshot(position_info)

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
            state_name=state_name,
            h1=h1,
            m15=m15,
            weighted_score=weighted_score,
            confidence_threshold=confidence_threshold,
            position_snapshot=position_snapshot,
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
        elif position_snapshot["side"] and action in {"OPEN_LONG", "OPEN_SHORT"}:
            action = "HOLD"
            side = None
            entry_bias = "等待反手前先平旧仓"
            final_reason = f"当前存在反向持仓 {position_snapshot['abs_pos']:.2f} 张，需先完成平仓，再考虑反手。"

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

        final_reason = (
            f"动作={action}，方向={side or 'none'}，常规仓位比例={position_ratio:.4f}，常规杠杆={leverage}，"
            f"加权分={weighted_score:.4f}，自适应门槛={confidence_threshold:.4f}，信号等级={signal_grade}，进攻分={attack_score:.4f}。"
            f" 当前净持仓={position_snapshot['net_pos']:.2f}。说明：{final_reason}"
        )
        if crowded:
            final_reason += " 当前资金费率提示拥挤。"
        if extreme_volatility:
            final_reason += " 当前波动处于扩张阶段。"
        if knife_attack_eligible:
            final_reason += " 满足尖刀连A+触发条件，可参与30U逐仓50倍进攻仓。"

        reasoning = ReasoningChain(
            market_state=(
                f"4H状态={state_name}，ADX={float(market_state['adx']):.2f}，"
                f"EMA结构={market_state['ema_structure']}，波动={market_state['volatility_state']}。"
            ),
            symbol_selection=f"仅在 BTC 与 SOL 中择优，当前标的={symbol_snapshot['symbol']}，并排除 ETH。",
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
            rootdata=(
                f"热度排名={rootdata_metrics.get('heat_rank', 'N/A')}，"
                f"影响力={rootdata_metrics.get('influence_index', 'N/A')}，"
                f"增长指数={rootdata_metrics.get('growth_index', 'N/A')}。"
                if rootdata_metrics else "RootData 数据不可用，使用默认值。"
            ),
            final_action=final_reason,
        )

        return {
            "symbol": symbol_snapshot["symbol"],
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
        }
