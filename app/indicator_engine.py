from __future__ import annotations

import numpy as np
import pandas as pd


class IndicatorEngine:
    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.astype(float).ewm(span=period, adjust=False).mean()

    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        series = series.astype(float)
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().replace(0.0, np.nan)
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0)

    def _atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        high = high.astype(float)
        low = low.astype(float)
        close = close.astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    def _adx(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
        high = high.astype(float)
        low = low.astype(float)
        close = close.astype(float)
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr = self._atr(high, low, close, period).replace(0.0, np.nan)
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        di_sum = (plus_di + minus_di).replace(0.0, np.nan)
        dx = ((plus_di - minus_di).abs() / di_sum) * 100
        adx = dx.astype(float).ewm(alpha=1 / period, adjust=False).mean()
        return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)

    def _boll_width(self, series: pd.Series, period: int = 20, std_num: int = 2) -> tuple[pd.Series, pd.Series, pd.Series]:
        series = series.astype(float)
        mid = series.rolling(period).mean()
        std = series.rolling(period).std(ddof=1)
        upper = mid + std_num * std
        lower = mid - std_num * std
        width = ((upper - lower) / mid.replace(0.0, np.nan)) * 100
        return upper.fillna(series), lower.fillna(series), width.fillna(0.0)

    def calculate(self, candles: list[dict[str, str]]) -> dict[str, float]:
        df = pd.DataFrame(candles)
        numeric_cols = ["ts", "open", "high", "low", "close", "vol"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("ts").dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        if df.empty:
            raise ValueError("K线数据为空，无法计算指标。")

        ema20 = self._ema(df["close"], 20)
        ema60 = self._ema(df["close"], 60)
        rsi14 = self._rsi(df["close"], 14)
        atr14 = self._atr(df["high"], df["low"], df["close"], 14)
        adx14, plus_di14, minus_di14 = self._adx(df["high"], df["low"], df["close"], 14)
        upper, lower, width = self._boll_width(df["close"], 20, 2)

        atr_change_rate = 0.0
        if len(atr14) >= 2 and pd.notna(atr14.iloc[-2]) and atr14.iloc[-2] != 0:
            atr_change_rate = float((atr14.iloc[-1] - atr14.iloc[-2]) / atr14.iloc[-2])

        return {
            "close": float(df["close"].iloc[-1]),
            "ema20": float(ema20.iloc[-1]),
            "ema60": float(ema60.iloc[-1]),
            "rsi14": float(rsi14.iloc[-1]),
            "atr14": float(atr14.iloc[-1]),
            "atr_change_rate": atr_change_rate,
            "adx14": float(adx14.iloc[-1]),
            "plus_di14": float(plus_di14.iloc[-1]),
            "minus_di14": float(minus_di14.iloc[-1]),
            "bollinger_upper": float(upper.iloc[-1]),
            "bollinger_lower": float(lower.iloc[-1]),
            "bollinger_width": float(width.iloc[-1]),
        }
