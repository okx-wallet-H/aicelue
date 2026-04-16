from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_ts_ms() -> int:
    return int(utc_now().timestamp() * 1000)


def format_ts_ms(timestamp_ms: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc).strftime(fmt)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: str | Path, default: Any) -> Any:
    file_path = Path(path)
    if not file_path.exists() or file_path.stat().st_size == 0:
        return default
    with file_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: str | Path, data: Any) -> None:
    file_path = Path(path)
    ensure_parent(file_path)
    with file_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

