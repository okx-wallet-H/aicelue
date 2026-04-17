from __future__ import annotations

import argparse
import atexit
import fcntl
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from app.config import settings
from app.logger import engine_logger
from app.main import AgentTradeKitApp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Agent TradeKit 策略引擎")
    parser.add_argument("--execute", action="store_true", help="是否执行真实下单")
    parser.add_argument("--loop", action="store_true", help="是否循环运行")
    return parser.parse_args()


@dataclass
class ProcessLock:
    """运行时单实例锁，防止多个调度进程并行。"""

    lock_file: Path
    handle: TextIO | None = None

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"已有 run_engine 进程在运行，锁文件: {self.lock_file}") from exc

        self.handle.seek(0)
        self.handle.truncate()
        payload = {
            "pid": os.getpid(),
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.handle.write(str(payload))
        self.handle.flush()
        atexit.register(self.release)

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def next_utc_2h_boundary(now_utc: datetime) -> datetime:
    aligned = now_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if aligned.hour % 2 == 0 and aligned == now_utc.astimezone(timezone.utc):
        return aligned + timedelta(hours=2)

    next_hour = aligned.hour + (2 - aligned.hour % 2)
    if next_hour >= 24:
        aligned = aligned.replace(hour=0) + timedelta(days=1)
    else:
        aligned = aligned.replace(hour=next_hour)
    return aligned


def sleep_until_boundary(target_utc: datetime) -> None:
    while True:
        now_utc = datetime.now(timezone.utc)
        remaining = (target_utc - now_utc).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def run_aligned_loop(app: AgentTradeKitApp, execute_orders: bool) -> None:
    while True:
        next_run_utc = next_utc_2h_boundary(datetime.now(timezone.utc))
        engine_logger.info(
            "调度器已对齐到下一个 2 小时整点，当前时间=%s，下次执行=%s，执行下单=%s",
            datetime.now(timezone.utc).isoformat(),
            next_run_utc.isoformat(),
            execute_orders,
        )
        sleep_until_boundary(next_run_utc)
        try:
            engine_logger.info("到达 2 小时整点，开始执行本轮策略。boundary_utc=%s", next_run_utc.isoformat())
            app.run_once(execute_orders=execute_orders)
        except Exception as exc:  # noqa: BLE001
            engine_logger.exception("整点调度运行异常: %s", exc)


def main() -> None:
    args = parse_args()
    process_lock = ProcessLock(settings.engine_pid_file)
    process_lock.acquire()
    app = AgentTradeKitApp()

    if not args.loop:
        app.run_once(execute_orders=args.execute)
        return

    run_aligned_loop(app, execute_orders=args.execute)


if __name__ == "__main__":
    main()
