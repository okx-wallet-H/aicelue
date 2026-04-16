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
    kelly_fraction_default: float = 0.50
    kelly_fraction_aggressive: float = 0.80
    min_position_ratio: float = 0.0
    max_position_ratio: float = 0.70
    min_position_ratio_initial: float = 0.02
    max_single_symbol_margin_ratio: float = 0.60
    max_total_margin_ratio: float = 0.90
    min_available_balance_ratio: float = 0.03
    order_margin_safety_buffer_usdt: float = 5.0

    # BTC 风向标资金分配：BTC 12%，SOL 83%，预留 5% 作为手续费与缓冲
    btc_weathervane_capital_ratio: float = 0.12
    sol_main_attack_capital_ratio: float = 0.88
    fee_buffer_capital_ratio: float = 0.03
    duplicate_entry_cooldown_seconds: int = 180

    single_loss_pct: float = 0.01
    single_loss_pct_strong_trend: float = 0.008
    daily_loss_fuse_pct: float = 0.08
    total_drawdown_fuse_pct: float = 0.20
    consecutive_loss_half: int = 4
    consecutive_loss_stop: int = 6
    max_concurrent_positions: int = 2

    btc_leverage_normal: int = 8
    btc_leverage_strong: int = 12
    sol_leverage_normal: int = 15
    sol_leverage_strong: int = 20
    symbol_priority: tuple[str, ...] = ("BTC-USDT-SWAP", "SOL-USDT-SWAP")

    knife_attack_enabled: bool = True
    knife_attack_margin_usdt: float = 60.0
    knife_attack_leverage: int = 30
    knife_attack_td_mode: str = "isolated"
    knife_attack_stop_loss_pct: float = 0.008
    knife_attack_take_profit_pct: float = 0.015
    knife_attack_min_score: float = 0.75
    knife_attack_min_weighted_score: float = 0.35

    strategy_learning_rate: float = 0.10
    min_strategy_weight: float = 0.10
    ewma_alpha: float = 0.35

    confidence_threshold_default: float = 0.15
    confidence_threshold_min: float = 0.08
    confidence_threshold_max: float = 0.50
    overall_position_scale_min: float = 0.60
    overall_position_scale_max: float = 1.40
    leverage_scale_min: float = 0.70
    leverage_scale_max: float = 1.50
    adaptive_stop_loss_min: float = 0.006
    adaptive_stop_loss_max: float = 0.012
    adaptive_stop_loss_default: float = 0.008

    # Hotfix: 权重调整
    trend_following_weight: float = 0.30
    mean_reversion_weight: float = 0.15
    breakout_weight: float = 0.35
    momentum_confirmation_weight: float = 0.20

    # RootData API 配置
    rootdata_api_key: str = field(default_factory=lambda: os.getenv("ROOTDATA_API_KEY", ""))

    # LLM 分析配置
    llm_enabled: bool = field(default_factory=lambda: os.getenv("LLM_ENABLED", "true").lower() == "true")
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen-plus-latest"))
    llm_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    llm_max_candles_per_tf: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_CANDLES_PER_TF", "24")))
    llm_recent_record_window: int = field(default_factory=lambda: int(os.getenv("LLM_RECENT_RECORD_WINDOW", "12")))
    llm_recent_trade_window: int = field(default_factory=lambda: int(os.getenv("LLM_RECENT_TRADE_WINDOW", "20")))
    llm_confidence_weight: float = field(default_factory=lambda: float(os.getenv("LLM_CONFIDENCE_WEIGHT", "0.10")))
    llm_alignment_boost: float = field(default_factory=lambda: float(os.getenv("LLM_ALIGNMENT_BOOST", "0.08")))
    llm_conflict_penalty: float = field(default_factory=lambda: float(os.getenv("LLM_CONFLICT_PENALTY", "0.05")))
    llm_hold_penalty: float = field(default_factory=lambda: float(os.getenv("LLM_HOLD_PENALTY", "0.03")))
    llm_skip_conflict_threshold: float = field(default_factory=lambda: float(os.getenv("LLM_SKIP_CONFLICT_THRESHOLD", "0.95")))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.15")))
    llm_review_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_REVIEW_TEMPERATURE", "0.10")))
    llm_review_bias_scale: float = field(default_factory=lambda: float(os.getenv("LLM_REVIEW_BIAS_SCALE", "0.50")))

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
