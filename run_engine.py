from __future__ import annotations

import argparse
import time

from app.config import settings
from app.logger import engine_logger
from app.main import AgentTradeKitApp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Agent TradeKit 策略引擎")
    parser.add_argument("--execute", action="store_true", help="是否执行真实下单")
    parser.add_argument("--loop", action="store_true", help="是否循环运行")
    parser.add_argument("--interval", type=int, default=settings.main_loop_interval_seconds, help="循环间隔秒数")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    execute = args.execute
    if execute and not settings.trading_enabled:
        engine_logger.error(
            "--execute 已传入，但环境变量 TRADING_ENABLED 未设置为 true。"
            " 拒绝真实下单，请先设置 TRADING_ENABLED=true 再重试。"
        )
        execute = False

    if execute:
        engine_logger.info(
            "⚠️  真实下单模式已启用 (TRADING_ENABLED=true)，demo=%s",
            settings.okx_use_demo,
        )
    else:
        engine_logger.info("仅分析模式（不下单）。如需下单请设置 TRADING_ENABLED=true 并传入 --execute。")

    app = AgentTradeKitApp()
    if not args.loop:
        app.run_once(execute_orders=execute)
        return

    engine_logger.info("策略引擎进入循环模式，间隔=%s 秒，执行下单=%s", args.interval, execute)
    while True:
        try:
            app.run_once(execute_orders=execute)
        except Exception as exc:  # noqa: BLE001
            engine_logger.exception("策略循环运行异常: %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
