from __future__ import annotations

from typing import Any

from app.config import settings
from app.utils import clamp, safe_float


class RiskManager:
    def __init__(self, state: dict[str, Any], completed_trades: list[dict[str, Any]], adaptive_params: dict[str, Any]) -> None:
        self.state = state
        self.completed_trades = completed_trades
        self.adaptive_params = adaptive_params

    def should_stop_new_trades(self) -> bool:
        return bool(self.state.get("daily_fuse_triggered") or self.state.get("drawdown_fuse_triggered"))

    def reset_daily_stats(self) -> None:
        """每日熔断重置逻辑。"""
        self.state["daily_pnl"] = 0.0
        self.state["consecutive_losses"] = 0
        self.state["daily_fuse_triggered"] = False
        # 不重置 equity_peak，以保持对总回撤的监控

    def _recent_symbol_trades(self, symbol: str) -> list[dict[str, Any]]:
        completed = [r for r in self.completed_trades if r.get("symbol") == symbol and r.get("realized_pnl") is not None]
        return completed[-settings.kelly_window:]

    def compute_kelly_fraction(self, symbol: str) -> float:
        trades = self._recent_symbol_trades(symbol)
        if len(trades) < 5:
            return 0.08

        wins = [r for r in trades if safe_float(r.get("realized_pnl")) > 0]
        losses = [r for r in trades if safe_float(r.get("realized_pnl")) < 0]
        if not wins or not losses:
            return 0.10

        w = len(wins) / max(1, len(wins) + len(losses))
        avg_win = sum(safe_float(r.get("realized_pnl")) for r in wins) / len(wins)
        avg_loss = abs(sum(safe_float(r.get("realized_pnl")) for r in losses) / len(losses))
        if avg_loss <= 0:
            return 0.10

        r_ratio = avg_win / avg_loss
        raw = w - (1 - w) / r_ratio
        return max(0.0, raw)

    def position_ratio(
        self,
        symbol: str,
        market_state: str,
        funding_rate: float,
        atr_change_rate: float,
        orderbook_factor: float,
    ) -> float:
        base = self.compute_kelly_fraction(symbol)
        kelly_fraction = settings.kelly_fraction_default
        if market_state in {"强势上涨", "强势下跌"}:
            kelly_fraction = settings.kelly_fraction_aggressive

        funding_factor = 0.5 if abs(funding_rate) >= settings.funding_crowded_threshold else 1.0
        atr_factor = 0.6 if abs(atr_change_rate) >= settings.atr_extreme_change else 1.0
        drawdown_factor = 1.0
        if self.state.get("consecutive_losses", 0) >= settings.consecutive_loss_half:
            drawdown_factor *= 0.5
        if self.state.get("consecutive_losses", 0) >= settings.consecutive_loss_stop:
            drawdown_factor = 0.0

        overall_scale = safe_float(self.adaptive_params.get("overall_position_scale"), 1.0)
        symbol_scale = safe_float(self.adaptive_params.get("symbol_position_scale", {}).get(symbol), 1.0)
        state_scale = safe_float(self.adaptive_params.get("state_position_scale", {}).get(market_state), 1.0)

        ratio = base * kelly_fraction * funding_factor * atr_factor * drawdown_factor * orderbook_factor
        ratio *= overall_scale * symbol_scale * state_scale
        
        # Hotfix 1: 增加最小开仓保底逻辑
        equity = max(safe_float(self.state.get("current_equity")), 1.0)
        if ratio * equity < 20:
            ratio = 20 / equity
            
        return clamp(ratio, settings.min_position_ratio_initial, settings.max_position_ratio)

    def adaptive_stop_loss_pct(self, symbol: str, market_state: str, atr_change_rate: float) -> float:
        state_stop = safe_float(
            self.adaptive_params.get("state_stop_loss_pct", {}).get(market_state),
            settings.adaptive_stop_loss_default,
        )
        pnl = safe_float(self.state.get("daily_pnl"))
        current_equity = max(safe_float(self.state.get("current_equity")), 1.0)
        daily_pnl_ratio = pnl / current_equity

        volatility_adjustment = 0.0015 if abs(atr_change_rate) >= settings.atr_extreme_change else 0.0
        pnl_adjustment = 0.001 if daily_pnl_ratio > 0.015 else (-0.001 if daily_pnl_ratio < -0.015 else 0.0)
        symbol_scale = safe_float(self.adaptive_params.get("symbol_position_scale", {}).get(symbol), 1.0)
        symbol_adjustment = -0.0005 if symbol_scale > 1.05 else (0.0005 if symbol_scale < 0.95 else 0.0)

        stop_loss_pct = state_stop + volatility_adjustment - pnl_adjustment + symbol_adjustment
        return round(clamp(stop_loss_pct, settings.adaptive_stop_loss_min, settings.adaptive_stop_loss_max), 4)

    def leverage_scale(self, symbol: str) -> float:
        overall_scale = safe_float(self.adaptive_params.get("overall_leverage_scale"), 1.0)
        symbol_scale = safe_float(self.adaptive_params.get("symbol_leverage_scale", {}).get(symbol), 1.0)
        return clamp(overall_scale * symbol_scale, settings.leverage_scale_min, settings.leverage_scale_max)

    def update_after_trade(self, current_equity: float, realized_pnl: float | None = None) -> None:
        peak = safe_float(self.state.get("equity_peak"))
        if peak <= 0:
            peak = current_equity
        peak = max(peak, current_equity)
        self.state["equity_peak"] = peak
        self.state["current_equity"] = current_equity

        if realized_pnl is not None:
            self.state["daily_pnl"] = safe_float(self.state.get("daily_pnl")) + realized_pnl
            if realized_pnl < 0:
                self.state["consecutive_losses"] = int(self.state.get("consecutive_losses", 0)) + 1
            elif realized_pnl > 0:
                self.state["consecutive_losses"] = 0

        drawdown = 0.0 if peak <= 0 else (peak - current_equity) / peak
        self.state["drawdown_fuse_triggered"] = drawdown >= settings.total_drawdown_fuse_pct
        self.state["daily_fuse_triggered"] = abs(min(safe_float(self.state.get("daily_pnl")), 0.0)) >= peak * settings.daily_loss_fuse_pct
