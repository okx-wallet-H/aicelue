from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from app.config import settings
from app.utils import clamp, now_ts_ms, safe_float


class EvolutionEngine:
    def __init__(self, weights: dict[str, float], adaptive_params: dict[str, Any]) -> None:
        self.weights = weights
        self.adaptive_params = adaptive_params

    def summarize_performance(self, completed_trades: list[dict[str, Any]]) -> dict[str, Any]:
        window = completed_trades[-settings.evaluation_trade_window :]
        if not window:
            return {
                "trade_count": 0,
                "win_rate": 0.0,
                "payoff_ratio": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
                "stop_out_rate": 0.0,
                "by_symbol": {},
                "by_state": {},
                "strategy_edge": {name: 0.0 for name in self.weights},
            }

        wins = [trade for trade in window if safe_float(trade.get("realized_pnl")) > 0]
        losses = [trade for trade in window if safe_float(trade.get("realized_pnl")) < 0]
        gross_profit = sum(safe_float(trade.get("realized_pnl")) for trade in wins)
        gross_loss = abs(sum(safe_float(trade.get("realized_pnl")) for trade in losses))
        total_pnl = sum(safe_float(trade.get("realized_pnl")) for trade in window)
        payoff_ratio = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        stop_out_count = sum(1 for trade in window if trade.get("close_reason") == "stop_loss")

        by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "count": 0,
            "wins": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "accuracy": 0.0,
        })
        by_state: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "count": 0,
            "wins": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "stop_outs": 0,
        })
        strategy_edge = {name: 0.0 for name in self.weights}
        strategy_weight_sum = {name: 0.0 for name in self.weights}

        for trade in window:
            pnl = safe_float(trade.get("realized_pnl"))
            symbol = trade.get("symbol", "UNKNOWN")
            market_state = trade.get("market_state", "UNKNOWN")
            symbol_row = by_symbol[symbol]
            state_row = by_state[market_state]

            symbol_row["count"] += 1
            state_row["count"] += 1
            symbol_row["total_pnl"] += pnl
            state_row["total_pnl"] += pnl
            if pnl > 0:
                symbol_row["wins"] += 1
                state_row["wins"] += 1
            if trade.get("close_reason") == "stop_loss":
                state_row["stop_outs"] += 1

            sub_scores = trade.get("sub_strategy_scores") or {}
            for name, score in sub_scores.items():
                if name not in strategy_edge:
                    continue
                weighted_contribution = pnl * safe_float(score)
                strategy_edge[name] += weighted_contribution
                strategy_weight_sum[name] += abs(safe_float(score))

        for row in by_symbol.values():
            if row["count"]:
                row["avg_pnl"] = row["total_pnl"] / row["count"]
                row["accuracy"] = row["wins"] / row["count"]
        for row in by_state.values():
            if row["count"]:
                row["avg_pnl"] = row["total_pnl"] / row["count"]
                row["stop_out_rate"] = row["stop_outs"] / row["count"]

        for name in strategy_edge:
            denom = strategy_weight_sum.get(name, 0.0)
            strategy_edge[name] = strategy_edge[name] / denom if denom > 0 else 0.0

        return {
            "trade_count": len(window),
            "win_rate": len(wins) / len(window),
            "payoff_ratio": payoff_ratio,
            "avg_pnl": total_pnl / len(window),
            "total_pnl": total_pnl,
            "stop_out_rate": stop_out_count / len(window),
            "by_symbol": dict(by_symbol),
            "by_state": dict(by_state),
            "strategy_edge": strategy_edge,
        }

    def update(self, completed_trades: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.summarize_performance(completed_trades)
        if metrics["trade_count"] == 0:
            return {
                "timestamp": now_ts_ms(),
                "trade_count": 0,
                "reasoning": ["最近窗口内暂无已完成交易，自适应参数保持不变。"],
                "metrics": metrics,
                "weights_before": deepcopy(self.weights),
                "weights_after": deepcopy(self.weights),
                "params_before": deepcopy(self.adaptive_params),
                "params_after": deepcopy(self.adaptive_params),
            }

        weights_before = deepcopy(self.weights)
        params_before = deepcopy(self.adaptive_params)
        reasons: list[str] = []

        self._update_strategy_weights(metrics, reasons)
        self._update_confidence_and_position(metrics, reasons)
        self._update_symbol_and_state_scales(metrics, reasons)
        self._update_stop_loss(metrics, reasons)

        return {
            "timestamp": now_ts_ms(),
            "trade_count": metrics["trade_count"],
            "reasoning": reasons,
            "metrics": metrics,
            "weights_before": weights_before,
            "weights_after": deepcopy(self.weights),
            "params_before": params_before,
            "params_after": deepcopy(self.adaptive_params),
        }

    def _update_strategy_weights(self, metrics: dict[str, Any], reasons: list[str]) -> None:
        learning_rate = settings.strategy_learning_rate
        alpha = settings.ewma_alpha
        updated_weights: dict[str, float] = {}
        total = 0.0

        for name, current_weight in self.weights.items():
            edge = safe_float(metrics["strategy_edge"].get(name))
            old_edge = safe_float(self.adaptive_params.setdefault("strategy_edge", {}).get(name, 0.0))
            ewma_edge = old_edge * (1 - alpha) + edge * alpha
            self.adaptive_params["strategy_edge"][name] = round(ewma_edge, 6)
            candidate = current_weight * (1 + learning_rate * ewma_edge)
            candidate = max(settings.min_strategy_weight, candidate)
            updated_weights[name] = candidate
            total += candidate

        total = total or 1.0
        for name in updated_weights:
            self.weights[name] = round(updated_weights[name] / total, 4)
        reasons.append("已根据最近完成交易对各子策略边际贡献做 EWMA 平滑，并重分配趋势、均值回归、突破与动量确认权重。")

    def _update_confidence_and_position(self, metrics: dict[str, Any], reasons: list[str]) -> None:
        confidence = safe_float(self.adaptive_params.get("confidence_threshold"), settings.confidence_threshold_default)
        position_scale = safe_float(self.adaptive_params.get("overall_position_scale"), 1.0)
        leverage_scale = safe_float(self.adaptive_params.get("overall_leverage_scale"), 1.0)

        if metrics["win_rate"] >= 0.58 and metrics["payoff_ratio"] >= 1.10:
            confidence -= 0.02
            position_scale += 0.05
            leverage_scale += 0.04
            reasons.append("最近交易胜率和盈亏比同时改善，整体置信度门槛下调并适度提高仓位与杠杆利用率。")
        elif metrics["win_rate"] <= 0.45 or metrics["payoff_ratio"] <= 0.90:
            confidence += 0.02
            position_scale -= 0.05
            leverage_scale -= 0.05
            reasons.append("最近交易质量走弱，整体置信度门槛上调，并适度压低仓位和杠杆系数。")
        else:
            reasons.append("最近交易质量中性，整体置信度门槛、仓位和杠杆维持平滑微调。")

        self.adaptive_params["confidence_threshold"] = round(clamp(confidence, settings.confidence_threshold_min, settings.confidence_threshold_max), 4)
        self.adaptive_params["overall_position_scale"] = round(clamp(position_scale, settings.overall_position_scale_min, settings.overall_position_scale_max), 4)
        self.adaptive_params["overall_leverage_scale"] = round(clamp(leverage_scale, settings.leverage_scale_min, settings.leverage_scale_max), 4)

    def _update_symbol_and_state_scales(self, metrics: dict[str, Any], reasons: list[str]) -> None:
        alpha = settings.ewma_alpha
        symbol_position_scale = self.adaptive_params.setdefault("symbol_position_scale", {})
        symbol_leverage_scale = self.adaptive_params.setdefault("symbol_leverage_scale", {})
        state_position_scale = self.adaptive_params.setdefault("state_position_scale", {})
        state_confidence_bonus = self.adaptive_params.setdefault("state_confidence_bonus", {})

        for symbol in settings.symbols:
            row = metrics["by_symbol"].get(symbol)
            if not row:
                continue
            edge = row["accuracy"] - 0.50
            current_pos_scale = safe_float(symbol_position_scale.get(symbol), 1.0)
            current_lev_scale = safe_float(symbol_leverage_scale.get(symbol), 1.0)
            target_pos_scale = clamp(1.0 + edge * 0.8, 0.75, 1.25)
            target_lev_scale = clamp(1.0 + edge * 0.6, settings.leverage_scale_min, settings.leverage_scale_max)
            symbol_position_scale[symbol] = round(current_pos_scale * (1 - alpha) + target_pos_scale * alpha, 4)
            symbol_leverage_scale[symbol] = round(current_lev_scale * (1 - alpha) + target_lev_scale * alpha, 4)

        for market_state, row in metrics["by_state"].items():
            current_pos_scale = safe_float(state_position_scale.get(market_state), 1.0)
            current_bonus = safe_float(state_confidence_bonus.get(market_state), 0.0)
            target_pos_scale = clamp(1.0 + row["avg_pnl"] * 0.15, 0.70, 1.20)
            target_bonus = clamp((0.50 - (row["wins"] / row["count"] if row["count"] else 0.50)) * 0.10, -0.08, 0.08)
            state_position_scale[market_state] = round(current_pos_scale * (1 - alpha) + target_pos_scale * alpha, 4)
            state_confidence_bonus[market_state] = round(current_bonus * (1 - alpha) + target_bonus * alpha, 4)

        reasons.append("已按币种胜率与不同市场状态的单位交易收益，分别更新币种尺度和状态门槛偏置。")

    def _update_stop_loss(self, metrics: dict[str, Any], reasons: list[str]) -> None:
        state_stop_loss_pct = self.adaptive_params.setdefault("state_stop_loss_pct", {})
        for market_state, current_value in list(state_stop_loss_pct.items()):
            row = metrics["by_state"].get(market_state, {})
            stop_out_rate = safe_float(row.get("stop_out_rate"))
            updated_value = safe_float(current_value, settings.adaptive_stop_loss_default)
            if row:
                if stop_out_rate >= 0.55:
                    updated_value += 0.001
                elif stop_out_rate <= 0.25 and safe_float(row.get("avg_pnl")) > 0:
                    updated_value -= 0.001
            state_stop_loss_pct[market_state] = round(clamp(updated_value, settings.adaptive_stop_loss_min, settings.adaptive_stop_loss_max), 4)
        reasons.append("已根据不同市场状态的止损触发率，自适应微调各状态止损宽度。")
