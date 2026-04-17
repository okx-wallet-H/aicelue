from __future__ import annotations

import json
import subprocess
from typing import Any

from app.config import settings
from app.logger import engine_logger

DEFAULT_ORDER_TAG = "agentTradeKit"


class OKXClient:
    def __init__(self) -> None:
        self.profile = settings.okx_profile
        self.use_demo = settings.okx_use_demo

    def _base_command(self) -> list[str]:
        mode_flag = "--demo" if self.use_demo else "--live"
        okx_path = "/home/ubuntu/.local/share/pnpm/okx"
        return [okx_path, mode_flag, "--json"]

    def _parse_json_output(self, raw: str) -> Any:
        raw = (raw or "").strip()
        if not raw:
            return []
        return json.loads(raw)

    def run(self, args: list[str]) -> Any:
        command = self._base_command() + args
        engine_logger.info("执行 OKX CLI 命令: %s", " ".join(command))
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return self._parse_json_output(result.stdout)
        except FileNotFoundError as exc:
            raise RuntimeError("未检测到 okx 命令，请先安装 @okx_ai/okx-trade-cli。") from exc
        except subprocess.CalledProcessError as exc:
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            if stdout:
                try:
                    engine_logger.warning("OKX CLI 返回非零退出码，但stdout中包含可解析JSON，继续按有效结果处理。")
                    return self._parse_json_output(stdout)
                except Exception:
                    pass
            raise RuntimeError(f"OKX CLI 调用失败: {stderr or stdout or exc}") from exc

    @staticmethod
    def _attach_requested_tag(data: Any, tag: str) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            return []
        for row in data:
            if isinstance(row, dict) and not row.get("requestedTag"):
                row["requestedTag"] = tag
        return data

    def get_account_balance(self) -> list[dict[str, Any]]:
        data = self.run(["account", "balance"]) or []
        return data if isinstance(data, list) else []

    def get_candles(self, inst_id: str, bar: str = "1H", limit: int = 100) -> list[dict[str, Any]]:
        data = self.run(["market", "candles", inst_id, "--bar", bar, "--limit", str(limit)]) or []
        if not isinstance(data, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, dict):
                normalized.append(row)
                continue
            if isinstance(row, list) and len(row) >= 6:
                normalized.append(
                    {
                        "ts": row[0],
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "vol": row[5],
                        "volCcy": row[6] if len(row) > 6 else None,
                        "volCcyQuote": row[7] if len(row) > 7 else None,
                        "confirm": row[8] if len(row) > 8 else None,
                    }
                )
        return normalized

    def get_orderbook(self, inst_id: str, size: int = 5) -> dict[str, Any]:
        data = self.run(["market", "orderbook", inst_id, "--sz", str(size)]) or {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def get_funding_rate(self, inst_id: str) -> dict[str, Any]:
        data = self.run(["market", "funding-rate", inst_id, "--limit", "1"]) or {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def get_open_interest(self, inst_id: str) -> dict[str, Any]:
        data = self.run(["market", "open-interest", "--instType", "SWAP", "--instId", inst_id]) or {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def get_positions(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        args = ["swap", "positions"]
        if inst_id:
            args.append(inst_id)
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def get_orders(self, inst_id: str | None = None, history: bool = False, archive: bool = False) -> list[dict[str, Any]]:
        args = ["swap", "orders"]
        if inst_id:
            args.extend(["--instId", inst_id])
        if history:
            args.append("--history")
        if archive:
            args.append("--archive")
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def get_order(self, inst_id: str, ord_id: str) -> list[dict[str, Any]]:
        data = self.run(["swap", "get", "--instId", inst_id, "--ordId", ord_id]) or []
        return data if isinstance(data, list) else []

    def get_fills(self, inst_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        args = ["swap", "fills", "--limit", str(limit)]
        if inst_id:
            args.extend(["--instId", inst_id])
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "isolated", pos_side: str = "net") -> list[dict[str, Any]]:
        args = [
            "swap", "leverage",
            "--instId", inst_id,
            "--lever", str(lever),
            "--mgnMode", mgn_mode,
            "--posSide", pos_side,
        ]
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def get_leverage(self, inst_id: str, mgn_mode: str = "isolated") -> list[dict[str, Any]]:
        args = ["swap", "get-leverage", "--instId", inst_id, "--mgnMode", mgn_mode]
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def get_max_avail_size(self, inst_id: str, td_mode: str = "isolated", lever: int | None = None) -> list[dict[str, Any]]:
        args = ["account", "max-avail-size", "--instId", inst_id, "--tdMode", td_mode]
        if lever is not None:
            args.extend(["--lever", str(lever)])
        data = self.run(args) or []
        return data if isinstance(data, list) else []

    def place_order(
        self,
        inst_id: str,
        side: str,
        size: float,
        ord_type: str = "market",
        td_mode: str = "isolated",
        tag: str = DEFAULT_ORDER_TAG,
        reduce_only: bool = False,
        pos_side: str | None = None,
        px: float | None = None,
    ) -> list[dict[str, Any]]:
        args = [
            "swap", "place",
            "--instId", inst_id,
            "--side", side,
            "--ordType", ord_type,
            "--sz", str(size),
            "--tdMode", td_mode,
            "--tag", tag,
        ]
        if pos_side:
            args.extend(["--posSide", pos_side])
        if px is not None:
            args.extend(["--px", str(px)])
        if reduce_only:
            engine_logger.warning("当前OKX CLI版本未稳定支持 swap place 的 reduceOnly 参数，已改为按净仓方向与数量发出反向市价单平仓。")
        data = self.run(args) or []
        return self._attach_requested_tag(data, tag)

    def place_algo_order(
        self,
        inst_id: str,
        td_mode: str,
        algo_ord_type: str,
        side: str,
        sz: float,
        tp_trigger_px: float | None = None,
        sl_trigger_px: float | None = None,
        tp_ord_px: float = -1,
        sl_ord_px: float = -1,
        tag: str = DEFAULT_ORDER_TAG,
        pos_side: str | None = None,
        reduce_only: bool = False,
        callback_ratio: float | None = None,
        active_px: float | None = None,
    ) -> list[dict[str, Any]]:
        args = [
            "swap", "algo", "place",
            "--instId", inst_id,
            "--tdMode", td_mode,
            "--ordType", algo_ord_type,
            "--side", side,
            "--sz", str(sz),
            "--tag", tag,
        ]
        if pos_side:
            args.extend(["--posSide", pos_side])
        if reduce_only:
            args.append("--reduceOnly")
        if tp_trigger_px is not None:
            args.extend(["--tpTriggerPx", str(tp_trigger_px), f"--tpOrdPx={tp_ord_px}"])
        if sl_trigger_px is not None:
            args.extend(["--slTriggerPx", str(sl_trigger_px), f"--slOrdPx={sl_ord_px}"])
        if callback_ratio is not None:
            args.extend(["--callbackRatio", str(callback_ratio)])
        if active_px is not None:
            args.extend(["--activePx", str(active_px)])
        data = self.run(args) or []
        return self._attach_requested_tag(data, tag)
