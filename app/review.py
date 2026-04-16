from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from app.utils import format_ts_ms, safe_float


class DailyReviewWriter:
    def __init__(self, review_dir: Path) -> None:
        self.review_dir = review_dir

    def write(
        self,
        records: list[dict[str, Any]],
        completed_trades: list[dict[str, Any]],
        adaptive_params: dict[str, Any],
        iteration_history: list[dict[str, Any]],
    ) -> Path:
        day_records = records[-200:]
        day_trades = completed_trades[-100:]
        state_counter = Counter(r.get("market_state", "未知") for r in day_records)
        total_count = len(day_records)
        hold_count = sum(1 for r in day_records if r.get("action") == "HOLD")

        win_trades = [t for t in day_trades if safe_float(t.get("realized_pnl")) > 0]
        loss_trades = [t for t in day_trades if safe_float(t.get("realized_pnl")) < 0]
        gross_profit = sum(safe_float(t.get("realized_pnl")) for t in win_trades)
        gross_loss = abs(sum(safe_float(t.get("realized_pnl")) for t in loss_trades))
        total_realized = sum(safe_float(t.get("realized_pnl")) for t in day_trades)
        win_rate = len(win_trades) / len(day_trades) if day_trades else 0.0
        payoff_ratio = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

        symbol_stats: dict[str, dict[str, float]] = {}
        for trade in day_trades:
            symbol = trade.get("symbol", "UNKNOWN")
            row = symbol_stats.setdefault(symbol, {"count": 0, "wins": 0, "pnl": 0.0})
            pnl = safe_float(trade.get("realized_pnl"))
            row["count"] += 1
            row["pnl"] += pnl
            if pnl > 0:
                row["wins"] += 1

        latest_iteration = iteration_history[-1] if iteration_history else {}
        reasons = latest_iteration.get("reasoning", []) if isinstance(latest_iteration, dict) else []
        params_after = latest_iteration.get("params_after", adaptive_params) if isinstance(latest_iteration, dict) else adaptive_params
        weights_after = latest_iteration.get("weights_after", {}) if isinstance(latest_iteration, dict) else {}

        today = format_ts_ms(day_records[-1]["timestamp"] if day_records else 0, "%Y-%m-%d")
        target = self.review_dir / f"review_{today}.md"
        content: list[str] = [
            f"# 每日复盘报告 {today}",
            "",
            "## 核心结果",
            "",
            f"- 决策记录数：{total_count}",
            f"- 已完成交易数：{len(day_trades)}",
            f"- HOLD 占比：{(hold_count / total_count * 100) if total_count else 0:.2f}%",
            f"- 已实现盈亏：{total_realized:.4f} USDT",
            f"- 胜率：{win_rate:.2%}",
            f"- 盈亏比：{payoff_ratio:.2f}",
            "",
            "## 市场状态分布",
            "",
        ]
        for state, count in state_counter.items():
            content.append(f"- {state}: {count}")

        content.extend(["", "## 币种表现", ""])
        if symbol_stats:
            for symbol, row in symbol_stats.items():
                accuracy = row["wins"] / row["count"] if row["count"] else 0.0
                content.append(f"- {symbol}: 交易数={row['count']}，胜率={accuracy:.2%}，已实现盈亏={row['pnl']:.4f} USDT")
        else:
            content.append("- 当日暂无已完成交易，继续积累样本。")

        content.extend(["", "## 当前自适应参数", ""])
        content.append(f"- confidence_threshold: {safe_float(params_after.get('confidence_threshold')):.4f}")
        content.append(f"- overall_position_scale: {safe_float(params_after.get('overall_position_scale'), 1.0):.4f}")
        content.append(f"- overall_leverage_scale: {safe_float(params_after.get('overall_leverage_scale'), 1.0):.4f}")
        content.append(f"- state_stop_loss_pct: {params_after.get('state_stop_loss_pct', {})}")

        content.extend(["", "## 当前策略权重", ""])
        if weights_after:
            for name, value in weights_after.items():
                content.append(f"- {name}: {safe_float(value):.4f}")
        else:
            content.append("- 本日尚未触发新的权重更新。")

        content.extend(["", "## 最近一次迭代依据", ""])
        if reasons:
            for reason in reasons:
                content.append(f"- {reason}")
        else:
            content.append("- 尚未形成新的参数迭代结论。")

        target.write_text("\n".join(content) + "\n", encoding="utf-8")
        return target
