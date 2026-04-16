from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import settings
from app.utils import load_json, now_ts_ms, save_json


DEFAULT_STATE = {
    "equity_peak": 0.0,
    "current_equity": 0.0,
    "daily_pnl": 0.0,
    "consecutive_losses": 0,
    "daily_fuse_triggered": False,
    "drawdown_fuse_triggered": False,
    "last_daily_reset": 0,
    "last_evaluation_ts": 0,
    "open_campaigns": {},
    "processed_fill_ids": [],
    "last_trade_review_ts": 0,
}

DEFAULT_WEIGHTS = {
    "trend_following": 0.35,
    "mean_reversion": 0.20,
    "breakout": 0.25,
    "momentum_confirmation": 0.20,
}

DEFAULT_ADAPTIVE_PARAMS = {
    "confidence_threshold": settings.confidence_threshold_default,
    "overall_position_scale": 1.0,
    "overall_leverage_scale": 1.0,
    "symbol_position_scale": {
        "BTC-USDT-SWAP": 1.0,
        "SOL-USDT-SWAP": 1.0,
    },
    "symbol_leverage_scale": {
        "BTC-USDT-SWAP": 1.0,
        "SOL-USDT-SWAP": 1.0,
    },
    "state_position_scale": {
        "强势上涨": 1.10,
        "弱势上涨": 0.90,
        "区间震荡": 1.0,
        "弱势下跌": 0.85,
        "强势下跌": 1.05,
    },
    "state_confidence_bonus": {
        "强势上涨": -0.03,
        "弱势上涨": 0.00,
        "区间震荡": 0.00,
        "弱势下跌": 0.00,
        "强势下跌": -0.02,
    },
    "state_stop_loss_pct": {
        "强势上涨": 0.015,
        "弱势上涨": 0.017,
        "区间震荡": 0.014,
        "弱势下跌": 0.017,
        "强势下跌": 0.015,
    },
    "strategy_edge": {
        "trend_following": 0.0,
        "mean_reversion": 0.0,
        "breakout": 0.0,
        "momentum_confirmation": 0.0,
    },
    "last_updated_ts": 0,
}


class KnowledgeBase:
    def __init__(self) -> None:
        self.knowledge_records: list[dict[str, Any]] = load_json(settings.knowledge_base_file, [])
        self.completed_trades: list[dict[str, Any]] = load_json(settings.completed_trades_file, [])
        self.iteration_history: list[dict[str, Any]] = load_json(settings.iteration_history_file, [])
        self.state: dict[str, Any] = self._merge_dict(DEFAULT_STATE, load_json(settings.state_file, {}))
        self.weights: dict[str, float] = self._merge_dict(DEFAULT_WEIGHTS, load_json(settings.strategy_weights_file, {}))
        self.adaptive_params: dict[str, Any] = self._merge_dict(DEFAULT_ADAPTIVE_PARAMS, load_json(settings.adaptive_params_file, {}))

    def _merge_dict(self, default: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(default)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def append_record(self, record: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(record)
        snapshot.setdefault("timestamp", now_ts_ms())
        snapshot.setdefault("record_id", f"record_{snapshot['timestamp']}_{len(self.knowledge_records) + 1}")
        snapshot.setdefault("trade_status", "pending")
        self.knowledge_records.append(snapshot)
        save_json(settings.knowledge_base_file, self.knowledge_records)
        return snapshot

    def update_record(self, record_id: str, updates: dict[str, Any]) -> None:
        for record in reversed(self.knowledge_records):
            if record.get("record_id") == record_id:
                record.update(updates)
                save_json(settings.knowledge_base_file, self.knowledge_records)
                return

    def append_completed_trade(self, trade: dict[str, Any]) -> None:
        snapshot = dict(trade)
        snapshot.setdefault("trade_id", f"trade_{snapshot.get('closed_at', now_ts_ms())}_{len(self.completed_trades) + 1}")
        self.completed_trades.append(snapshot)
        save_json(settings.completed_trades_file, self.completed_trades)
        trade_file = Path(settings.trade_snapshot_dir) / f"{snapshot['trade_id']}.json"
        save_json(trade_file, snapshot)

    def append_iteration(self, iteration_entry: dict[str, Any]) -> None:
        snapshot = dict(iteration_entry)
        snapshot.setdefault("timestamp", now_ts_ms())
        snapshot.setdefault("iteration_id", f"iteration_{snapshot['timestamp']}_{len(self.iteration_history) + 1}")
        self.iteration_history.append(snapshot)
        save_json(settings.iteration_history_file, self.iteration_history)
        iteration_file = Path(settings.iteration_dir) / f"{snapshot['iteration_id']}.json"
        save_json(iteration_file, snapshot)

    def save_state(self) -> None:
        save_json(settings.state_file, self.state)

    def save_weights(self) -> None:
        save_json(settings.strategy_weights_file, self.weights)

    def save_adaptive_params(self) -> None:
        self.adaptive_params["last_updated_ts"] = now_ts_ms()
        save_json(settings.adaptive_params_file, self.adaptive_params)
