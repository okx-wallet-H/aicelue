from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Settings:
    """策略引擎统一配置。"""

    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    data_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    review_dir: Path = field(init=False)
    trade_snapshot_dir: Path = field(init=False)
    iteration_dir: Path = field(init=False)
    runtime_dir: Path = field(init=False)
    lock_dir: Path = field(init=False)
    entry_lock_dir: Path = field(init=False)
    engine_pid_file: Path = field(init=False)

    okx_profile: str = field(default_factory=lambda: os.getenv("OKX_PROFILE", "live"))
    okx_use_demo: bool = field(default_factory=lambda: os.getenv("OKX_USE_DEMO", "false").lower() == "true")
    okx_public_api_base: str = field(default_factory=lambda: os.getenv("OKX_PUBLIC_API_BASE", "https://www.okx.com"))
    okx_api_key: str = field(default_factory=lambda: os.getenv("OKX_API_KEY", ""))
    okx_secret_key: str = field(default_factory=lambda: os.getenv("OKX_SECRET_KEY", ""))
    okx_passphrase: str = field(default_factory=lambda: os.getenv("OKX_PASSPHRASE", ""))

    symbols: list[str] = field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"])
    banned_symbols: list[str] = field(default_factory=list)
    symbol_priority: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
    timeframes: dict[str, str] = field(default_factory=lambda: {
        "4H": "4H",
        "1H": "1H",
        "15M": "15m",
    })

    candle_limit: int = 120
    main_loop_interval_seconds: int = 14400
    evaluation_interval_hours: int = 4
    fills_scan_limit: int = 200

    max_total_margin_ratio: float = 0.60
    max_single_symbol_margin_ratio: float = 0.60
    min_available_balance_ratio: float = 0.03
    min_order_margin_usdt: float = 10.0
    order_margin_safety_buffer_usdt: float = 2.0
    duplicate_entry_cooldown_seconds: int = 180
    single_trade_max_risk_pct: float = 0.02
    max_concurrent_positions: int = 3

    confidence_threshold_default: float = 0.28
    confidence_threshold_min: float = 0.25
    confidence_threshold_max: float = 0.30

    daily_loss_fuse_pct: float = 0.15
    total_drawdown_fuse_pct: float = 0.25
    consecutive_loss_half: int = 4
    consecutive_loss_stop: int = 6
    kelly_window: int = 20
    kelly_fraction_default: float = 0.50
    kelly_fraction_aggressive: float = 0.80
    min_position_ratio_initial: float = 0.20
    max_position_ratio: float = 0.70
    funding_crowded_threshold: float = 0.001
    atr_extreme_change: float = 0.18
    adaptive_stop_loss_default: float = 0.018
    adaptive_stop_loss_min: float = 0.015
    adaptive_stop_loss_max: float = 0.025
    leverage_scale_min: float = 0.70
    leverage_scale_max: float = 1.50

    rootdata_api_key: str = field(default_factory=lambda: os.getenv("ROOTDATA_API_KEY", ""))

    llm_enabled: bool = field(default_factory=lambda: os.getenv("LLM_ENABLED", "true").lower() == "true")
    llm_primary_api_key: str = field(default_factory=lambda: os.getenv("QWEN_API_KEY", ""))
    llm_primary_base_url: str = field(default_factory=lambda: os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    llm_primary_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-plus-latest"))
    llm_backup_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    llm_backup_base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    llm_backup_model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))
    llm_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    llm_max_candles_per_tf: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_CANDLES_PER_TF", "24")))
    llm_recent_trade_window: int = field(default_factory=lambda: int(os.getenv("LLM_RECENT_TRADE_WINDOW", "8")))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.30")))
    competition_end_at_utc: str = field(default_factory=lambda: os.getenv("COMPETITION_END_AT_UTC", ""))

    engine_log_file: Path = field(init=False)
    reasoning_log_file: Path = field(init=False)
    trades_log_file: Path = field(init=False)
    iteration_log_file: Path = field(init=False)
    knowledge_base_file: Path = field(init=False)
    completed_trades_file: Path = field(init=False)
    adaptive_params_file: Path = field(init=False)
    iteration_history_file: Path = field(init=False)
    state_file: Path = field(init=False)
    strategy_weights_file: Path = field(init=False)
    oi_cache_file: Path = field(init=False)
    ai_decision_log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = self.project_root / "data"
        self.logs_dir = self.project_root / "logs"
        self.review_dir = self.data_dir / "daily_reviews"
        self.trade_snapshot_dir = self.data_dir / "trade_snapshots"
        self.iteration_dir = self.data_dir / "iterations"
        self.runtime_dir = self.project_root / "runtime"
        self.lock_dir = self.runtime_dir / "locks"
        self.entry_lock_dir = self.lock_dir / "entry"
        self.engine_pid_file = self.lock_dir / "run_engine.pid"

        self.engine_log_file = self.logs_dir / "engine.log"
        self.reasoning_log_file = self.logs_dir / "reasoning.log"
        self.trades_log_file = self.logs_dir / "trades.log"
        self.iteration_log_file = self.logs_dir / "iteration.log"
        self.knowledge_base_file = self.data_dir / "knowledge_base.json"
        self.completed_trades_file = self.data_dir / "completed_trades.json"
        self.adaptive_params_file = self.data_dir / "adaptive_params.json"
        self.iteration_history_file = self.data_dir / "iteration_history.json"
        self.state_file = self.data_dir / "state.json"
        self.strategy_weights_file = self.data_dir / "strategy_weights.json"
        self.oi_cache_file = self.data_dir / "oi_cache.json"
        self.ai_decision_log_file = self.data_dir / "ai_decisions.jsonl"

        for path in (
            self.data_dir,
            self.logs_dir,
            self.review_dir,
            self.trade_snapshot_dir,
            self.iteration_dir,
            self.runtime_dir,
            self.lock_dir,
            self.entry_lock_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
