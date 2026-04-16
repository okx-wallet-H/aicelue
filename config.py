from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Settings:
    """策略引擎全局配置。"""

    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    data_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    review_dir: Path = field(init=False)
    trade_snapshot_dir: Path = field(init=False)
    iteration_dir: Path = field(init=False)

    okx_profile: str = field(default_factory=lambda: os.getenv("OKX_PROFILE", "live"))
    okx_use_demo: bool = field(default_factory=lambda: os.getenv("OKX_USE_DEMO", "false").lower() == "true")
    okx_api_key: str = field(default_factory=lambda: os.getenv("OKX_API_KEY", ""))
    okx_secret_key: str = field(default_factory=lambda: os.getenv("OKX_SECRET_KEY", ""))
    okx_passphrase: str = field(default_factory=lambda: os.getenv("OKX_PASSPHRASE", ""))

    symbols: tuple[str, ...] = ("BTC-USDT-SWAP", "SOL-USDT-SWAP")
    banned_symbols: tuple[str, ...] = ("ETH-USDT-SWAP",)
    timeframes: dict[str, str] = field(default_factory=lambda: {
        "4H": "4H",
        "1H": "1H",
        "15M": "15m",
    })

    candle_limit: int = 120
    main_loop_interval_seconds: int = 300
    evaluation_interval_hours: int = 4
    evaluation_trade_window: int = 40
    fills_scan_limit: int = 200

    adx_low: float = 20.0
    adx_high: float = 25.0
    funding_crowded_threshold: float = 0.001
    boll_width_low: float = 2.0
    boll_width_high: float = 6.0
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    atr_extreme_change: float = 0.18

    kelly_window: int = 20
    kelly_fraction_default: float = 0.25
    kelly_fraction_aggressive: float = 0.50
    min_position_ratio: float = 0.0
    max_position_ratio: float = 0.40
    min_position_ratio_initial: float = 0.05
    max_single_symbol_margin_ratio: float = 0.40
    max_total_margin_ratio: float = 0.70
    min_available_balance_ratio: float = 0.12
    order_margin_safety_buffer_usdt: float = 12.0
    duplicate_entry_cooldown_seconds: int = 900

    single_loss_pct: float = 0.02
    single_loss_pct_strong_trend: float = 0.015
    daily_loss_fuse_pct: float = 0.06
    total_drawdown_fuse_pct: float = 0.20
    consecutive_loss_half: int = 2
    consecutive_loss_stop: int = 3
    max_concurrent_positions: int = 2

    btc_leverage_normal: int = 3
    btc_leverage_strong: int = 5
    sol_leverage_normal: int = 3
    sol_leverage_strong: int = 5
    symbol_priority: tuple[str, ...] = ("BTC-USDT-SWAP", "SOL-USDT-SWAP")

    knife_attack_enabled: bool = True
    knife_attack_margin_usdt: float = 30.0
    knife_attack_leverage: int = 15
    knife_attack_td_mode: str = "isolated"
    knife_attack_stop_loss_pct: float = 0.015
    knife_attack_take_profit_pct: float = 0.03
    knife_attack_min_score: float = 0.88
    knife_attack_min_weighted_score: float = 0.55

    strategy_learning_rate: float = 0.10
    min_strategy_weight: float = 0.10
    ewma_alpha: float = 0.35

    confidence_threshold_default: float = 0.42
    confidence_threshold_min: float = 0.25
    confidence_threshold_max: float = 0.75
    overall_position_scale_min: float = 0.60
    overall_position_scale_max: float = 1.40
    leverage_scale_min: float = 0.70
    leverage_scale_max: float = 1.50
    adaptive_stop_loss_min: float = 0.012
    adaptive_stop_loss_max: float = 0.020
    adaptive_stop_loss_default: float = 0.016

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

    def __post_init__(self) -> None:
        self.data_dir = self.project_root / "data"
        self.logs_dir = self.project_root / "logs"
        self.review_dir = self.data_dir / "daily_reviews"
        self.trade_snapshot_dir = self.data_dir / "trade_snapshots"
        self.iteration_dir = self.data_dir / "iterations"
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

        for path in (self.data_dir, self.logs_dir, self.review_dir, self.trade_snapshot_dir, self.iteration_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
