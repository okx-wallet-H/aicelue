from __future__ import annotations

import logging
from pathlib import Path

from app.config import settings


def _build_logger(name: str, log_path: Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


engine_logger = _build_logger("engine", settings.engine_log_file)
reasoning_logger = _build_logger("reasoning", settings.reasoning_log_file)
trades_logger = _build_logger("trades", settings.trades_log_file)
iteration_logger = _build_logger("iteration", settings.iteration_log_file)
