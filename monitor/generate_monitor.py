#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import math
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from okx import Account, PublicData, Trade

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = Path(__file__).resolve().with_name("template.html")
OUTPUT_PATH = Path("/var/www/okx_monitor/index.html")
ACCOUNT_NAME = "温暖小号"
PAGE_TITLE = "OKX交易赛监控面板"
LOG_LINES = 50
TRADE_LIMIT = 20


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "None"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_num(value: Any, digits: int = 4) -> str:
    number = safe_float(value, 0.0)
    if math.isfinite(number):
        return f"{number:,.{digits}f}"
    return "0.0000"


def fmt_price(value: Any) -> str:
    return fmt_num(value, 4)


def fmt_qty(value: Any) -> str:
    return fmt_num(value, 4)


def fmt_money(value: Any) -> str:
    return fmt_num(value, 4)


def fmt_percent(value: Any, digits: int = 2) -> str:
    number = safe_float(value, 0.0)
    if abs(number) <= 1:
        number *= 100
    return f"{number:.{digits}f}%"


def css_class_for_number(value: Any) -> str:
    number = safe_float(value, 0.0)
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return "muted"


def badge_for_position(pos: Any) -> str:
    position = safe_float(pos, 0.0)
    if position > 0:
        return '<span class="badge long">做多</span>'
    if position < 0:
        return '<span class="badge short">做空</span>'
    return '<span class="badge flat">空仓</span>'


def load_credentials() -> tuple[str, str, str]:
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    api_key = os.environ.get("OKX_API_KEY", "").strip()
    secret_key = os.environ.get("OKX_SECRET_KEY", "").strip()
    passphrase = os.environ.get("OKX_PASSPHRASE", "").strip()
    if not all([api_key, secret_key, passphrase]):
        raise RuntimeError(f"缺少 OKX API 环境变量，请检查 {env_path}")
    return api_key, secret_key, passphrase


def api_data(response: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            return data
    return []


def get_okx_clients() -> tuple[Any, Any, Any]:
    api_key, secret_key, passphrase = load_credentials()
    account_api = Account.AccountAPI(api_key, secret_key, passphrase, False, "0")
    trade_api = Trade.TradeAPI(api_key, secret_key, passphrase, False, "0")
    public_api = PublicData.PublicAPI(flag="0")
    return account_api, trade_api, public_api


def fetch_balance_summary(account_api: Any) -> dict[str, str]:
    balance_resp = account_api.get_account_balance()
    balance_items = api_data(balance_resp)
    account = balance_items[0] if balance_items else {}
    details = account.get("details") or []

    total_equity = safe_float(account.get("totalEq"))
    available_balance = safe_float(account.get("availEq"))
    if available_balance == 0 and details:
        available_balance = sum(safe_float(item.get("availBal")) for item in details)

    frozen_balance = safe_float(account.get("ordFroz"))
    if frozen_balance == 0 and details:
        frozen_balance = sum(safe_float(item.get("frozenBal")) + safe_float(item.get("ordFrozen")) for item in details)
    if frozen_balance == 0 and total_equity and available_balance:
        frozen_balance = max(total_equity - available_balance, 0.0)

    return {
        "TOTAL_EQUITY": fmt_money(total_equity),
        "AVAILABLE_BALANCE": fmt_money(available_balance),
        "FROZEN_BALANCE": fmt_money(frozen_balance),
    }


def normalize_position(item: dict[str, Any]) -> dict[str, Any]:
    pos = safe_float(item.get("pos"))
    avg_px = item.get("avgPx") or item.get("openAvgPx") or item.get("uplAvgPx") or "0"
    mark_px = item.get("markPx") or item.get("last") or item.get("lastPx") or "0"
    margin = item.get("margin") or item.get("imr") or item.get("marginFrozen") or item.get("mgnRatio") or "0"
    upl = item.get("upl") or item.get("uplLastPx") or "0"
    upl_ratio_value = item.get("uplRatio")
    if upl_ratio_value in (None, ""):
        margin_value = safe_float(margin)
        upl_value = safe_float(upl)
        upl_ratio_value = upl_value / margin_value if margin_value else 0.0

    return {
        "inst_id": item.get("instId", "-"),
        "pos": pos,
        "direction_html": badge_for_position(pos),
        "qty": fmt_qty(abs(pos)),
        "avg_px": fmt_price(avg_px),
        "mark_px": fmt_price(mark_px),
        "margin": fmt_money(margin),
        "margin_value": safe_float(margin),
        "upl": fmt_money(upl),
        "upl_class": css_class_for_number(upl),
        "upl_ratio": fmt_percent(upl_ratio_value),
        "upl_ratio_class": css_class_for_number(upl_ratio_value),
        "lever": html.escape(str(item.get("lever") or "-")),
        "liq_px": fmt_price(item.get("liqPx") or 0),
    }


def fetch_positions(account_api: Any) -> tuple[list[dict[str, Any]], str, str]:
    positions_resp = account_api.get_positions()
    raw_positions = api_data(positions_resp)
    positions = [normalize_position(item) for item in raw_positions if abs(safe_float(item.get("pos"))) > 0]

    long_margin = sum(item["margin_value"] for item in positions if item["pos"] > 0)
    short_margin = sum(item["margin_value"] for item in positions if item["pos"] < 0)
    return positions, fmt_money(long_margin), fmt_money(short_margin)


def fetch_open_interest(public_api: Any, inst_id: str) -> str:
    response = public_api.get_open_interest(instType="SWAP", instId=inst_id)
    items = api_data(response)
    if not items:
        return "0.0000"
    first = items[0]
    return fmt_num(first.get("oi") or first.get("openInterest") or 0, 2)


def format_fill_time(value: Any) -> str:
    if value in (None, ""):
        return "-"
    text = str(value)
    try:
        ts = int(float(text))
        if ts > 10_000_000_000:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return html.escape(text)


def normalize_fill(item: dict[str, Any]) -> dict[str, str]:
    pnl_value = item.get("fillPnl")
    if pnl_value in (None, ""):
        pnl_value = item.get("pnl") or 0
    side = item.get("side") or item.get("execType") or "-"
    return {
        "time": format_fill_time(item.get("fillTime") or item.get("ts") or item.get("uTime")),
        "inst_id": html.escape(str(item.get("instId") or "-")),
        "side": html.escape(str(side)),
        "price": fmt_price(item.get("fillPx") or item.get("px") or 0),
        "qty": fmt_qty(item.get("fillSz") or item.get("sz") or 0),
        "pnl": fmt_money(pnl_value),
        "pnl_class": css_class_for_number(pnl_value),
    }


def fetch_recent_fills(trade_api: Any) -> list[dict[str, str]]:
    response = trade_api.get_fills(instType="SWAP")
    items = api_data(response)
    fills = [normalize_fill(item) for item in items[:TRADE_LIMIT]]
    return fills


def extract_log_lines(text: str) -> list[str]:
    keywords = [
        "signal",
        "策略",
        "连接",
        "direction",
        "score",
        "风向",
        "btc",
        "decision",
        "开仓",
        "平仓",
        "做多",
        "做空",
    ]
    selected: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(keyword in lower for keyword in ["signal", "direction", "score", "btc", "decision"]) or any(
            keyword in line for keyword in ["策略", "连接", "风向", "开仓", "平仓", "做多", "做空"]
        ):
            selected.append(line)
    if not selected:
        selected = [line.strip() for line in text.splitlines() if line.strip()]
    return selected[-8:]


def fetch_strategy_logs() -> list[str]:
    try:
        result = subprocess.run(
            ["pm2", "logs", "okx-agent-tradekit", "--lines", str(LOG_LINES), "--nostream"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        lines = extract_log_lines(text)
        return lines or ["最近 50 行日志中未解析到明显策略信号。"]
    except Exception as exc:  # noqa: BLE001
        return [f"日志读取失败：{exc}"]


def render_positions_table(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return '<table><thead><tr><th>状态</th></tr></thead><tbody><tr><td>当前无持仓</td></tr></tbody></table>'

    rows = []
    for item in positions:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['inst_id'])}</td>"
            f"<td>{item['direction_html']}</td>"
            f"<td>{item['qty']}</td>"
            f"<td>{item['avg_px']}</td>"
            f"<td>{item['mark_px']}</td>"
            f"<td>{item['margin']}</td>"
            f"<td class=\"{item['upl_class']}\">{item['upl']}</td>"
            f"<td class=\"{item['upl_ratio_class']}\">{item['upl_ratio']}</td>"
            f"<td>{item['lever']}</td>"
            f"<td>{item['liq_px']}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>标的</th><th>方向</th><th>数量</th><th>均价</th><th>标记价格</th>"
        "<th>保证金</th><th>浮盈浮亏</th><th>盈亏率</th><th>杠杆</th><th>强平价</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_trades_table(fills: list[dict[str, str]]) -> str:
    if not fills:
        return '<table><thead><tr><th>状态</th></tr></thead><tbody><tr><td>暂无最近交易记录</td></tr></tbody></table>'

    rows = []
    for item in fills:
        rows.append(
            "<tr>"
            f"<td>{item['time']}</td>"
            f"<td>{item['inst_id']}</td>"
            f"<td>{item['side']}</td>"
            f"<td>{item['price']}</td>"
            f"<td>{item['qty']}</td>"
            f"<td class=\"{item['pnl_class']}\">{item['pnl']}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>时间</th><th>标的</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_log_items(lines: list[str]) -> str:
    return "".join(f'<div class="log-item">{html.escape(line)}</div>' for line in lines)


def render_html(context: dict[str, str]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in context.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def collect_dashboard_data() -> dict[str, str]:
    account_api, trade_api, public_api = get_okx_clients()
    balance_summary = fetch_balance_summary(account_api)
    positions, long_margin, short_margin = fetch_positions(account_api)
    fills = fetch_recent_fills(trade_api)
    logs = fetch_strategy_logs()

    context = {
        "PAGE_TITLE": PAGE_TITLE,
        "LAST_UPDATED": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "TOTAL_EQUITY": balance_summary["TOTAL_EQUITY"],
        "AVAILABLE_BALANCE": balance_summary["AVAILABLE_BALANCE"],
        "FROZEN_BALANCE": balance_summary["FROZEN_BALANCE"],
        "LONG_MARGIN": long_margin,
        "SHORT_MARGIN": short_margin,
        "BTC_OI": fetch_open_interest(public_api, "BTC-USDT-SWAP"),
        "SOL_OI": fetch_open_interest(public_api, "SOL-USDT-SWAP"),
        "POSITIONS_TABLE": render_positions_table(positions),
        "TRADES_TABLE": render_trades_table(fills),
        "LOG_ITEMS": render_log_items(logs),
    }
    return context


def main() -> None:
    context = collect_dashboard_data()
    html_text = render_html(context)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html_text, encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(OUTPUT_PATH), "account": ACCOUNT_NAME}, ensure_ascii=False))


if __name__ == "__main__":
    main()
