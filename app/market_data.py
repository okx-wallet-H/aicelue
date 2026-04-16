from __future__ import annotations

from typing import Any

from app.config import settings
from app.okx_cli import OKXClient
from app.utils import load_json, now_ts_ms, safe_float, save_json


class MarketDataCollector:
    def __init__(self, client: OKXClient) -> None:
        self.client = client
        self.oi_cache: dict[str, list[dict[str, float]]] = load_json(settings.oi_cache_file, {})

    def _update_oi_cache(self, symbol: str, current_oi: float) -> float:
        records = self.oi_cache.setdefault(symbol, [])
        current_ts = now_ts_ms()
        records.append({"timestamp": current_ts, "open_interest": current_oi})
        self.oi_cache[symbol] = records[-500:]
        save_json(settings.oi_cache_file, self.oi_cache)

        one_hour_ago = None
        for row in reversed(self.oi_cache[symbol][:-1]):
            if current_ts - int(row["timestamp"]) >= 3600 * 1000:
                one_hour_ago = safe_float(row["open_interest"])
                break
        if one_hour_ago in (None, 0):
            return 0.0
        return (current_oi - one_hour_ago) / one_hour_ago

    @staticmethod
    def compute_obi(orderbook: dict[str, Any]) -> float:
        bids = orderbook.get("bids", []) or []
        asks = orderbook.get("asks", []) or []
        bid_vol = sum(safe_float(row[1]) for row in bids[:5] if len(row) >= 2)
        ask_vol = sum(safe_float(row[1]) for row in asks[:5] if len(row) >= 2)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def collect_symbol(self, symbol: str) -> dict[str, Any]:
        klines = {
            label: self.client.get_candles(symbol, bar, settings.candle_limit)
            for label, bar in settings.timeframes.items()
        }
        funding = self.client.get_funding_rate(symbol)
        oi = self.client.get_open_interest(symbol)
        orderbook = self.client.get_orderbook(symbol, size=5)

        oi_value = safe_float(oi.get("oi") or oi.get("oiCcy"))
        oi_change_rate = self._update_oi_cache(symbol, oi_value) if oi_value else 0.0
        funding_rate = safe_float(funding.get("fundingRate"))
        obi = self.compute_obi(orderbook)

        return {
            "symbol": symbol,
            "klines": klines,
            "funding_rate": funding_rate,
            "open_interest": oi_value,
            "oi_change_rate": oi_change_rate,
            "orderbook": orderbook,
            "obi": obi,
        }

    def collect(self) -> dict[str, Any]:
        return {
            "timestamp": now_ts_ms(),
            "symbols": {symbol: self.collect_symbol(symbol) for symbol in settings.symbols},
        }


