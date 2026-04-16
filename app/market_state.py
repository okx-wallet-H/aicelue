from __future__ import annotations

from app.config import settings


class MarketStateRecognizer:
    @staticmethod
    def _ema_structure(ema20: float, ema60: float) -> str:
        if ema20 > ema60:
            return "多头排列"
        if ema20 < ema60:
            return "空头排列"
        return "缠绕"

    def recognize(self, symbol: str, tf_indicators: dict[str, dict[str, float]], funding_rate: float, oi_change_rate: float) -> dict[str, object]:
        h4 = tf_indicators["4H"]
        ema_structure = self._ema_structure(h4["ema20"], h4["ema60"])

        trend_strength = "无趋势或弱趋势"
        if h4["adx14"] >= settings.adx_high:
            trend_strength = "强趋势"
        elif h4["adx14"] >= settings.adx_low:
            trend_strength = "趋势形成中"

        volatility = "常态波动"
        if h4["bollinger_width"] <= settings.boll_width_low and h4["atr_change_rate"] <= 0:
            volatility = "震荡压缩"
        elif h4["bollinger_width"] >= settings.boll_width_high or h4["atr_change_rate"] >= settings.atr_extreme_change:
            volatility = "波动扩张"

        state = "区间震荡"
        if trend_strength == "强趋势" and ema_structure == "多头排列" and h4["plus_di14"] > h4["minus_di14"]:
            state = "强势上涨"
        elif trend_strength == "趋势形成中" and ema_structure == "多头排列" and h4["plus_di14"] > h4["minus_di14"]:
            state = "弱势上涨"
        elif trend_strength == "强趋势" and ema_structure == "空头排列" and h4["minus_di14"] > h4["plus_di14"]:
            state = "强势下跌"
        elif trend_strength == "趋势形成中" and ema_structure == "空头排列" and h4["minus_di14"] > h4["plus_di14"]:
            state = "弱势下跌"

        crowding_risk = abs(funding_rate) >= settings.funding_crowded_threshold
        extreme_volatility_risk = volatility == "波动扩张" and abs(h4["atr_change_rate"]) >= settings.atr_extreme_change
        skip_new_trade = bool(crowding_risk and extreme_volatility_risk)

        return {
            "symbol": symbol,
            "overall_state": state,
            "trend_strength": trend_strength,
            "volatility_state": volatility,
            "ema_structure": ema_structure,
            "adx": h4["adx14"],
            "bollinger_width": h4["bollinger_width"],
            "atr_change_rate": h4["atr_change_rate"],
            "funding_rate": funding_rate,
            "oi_change_rate": oi_change_rate,
            "crowding_risk": crowding_risk,
            "extreme_volatility_risk": extreme_volatility_risk,
            "skip_new_trade": skip_new_trade,
        }
