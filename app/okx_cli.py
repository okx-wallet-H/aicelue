from __future__ import annotations

import json
import subprocess
from typing import Any

import requests

from app.config import settings
from app.logger import engine_logger

DEFAULT_ORDER_TAG = "agentTradeKit"


class OKXCLIError(RuntimeError):
    """OKX CLI 调用异常，区分可恢复错误与致命错误。"""

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool,
        command: list[str],
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.recoverable = recoverable
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class OKXClient:
    def __init__(self) -> None:
        self.profile = settings.okx_profile
        self.use_demo = settings.okx_use_demo
        self._instrument_cache: dict[str, dict[str, Any]] = {}

    def _base_command(self) -> list[str]:
        mode_flag = "--demo" if self.use_demo else "--live"
        okx_path = "/usr/bin/okx"
        return [okx_path, mode_flag, "--json"]

    def _parse_json_output(self, raw: str) -> Any:
        raw = (raw or "").strip()
        if not raw:
            return []
        return json.loads(raw)

    @staticmethod
    def _is_fatal_error(message: str) -> bool:
        lowered = (message or "").lower()
        fatal_keywords = (
            "api key",
            "passphrase",
            "signature",
            "unauthorized",
            "forbidden",
            "permission denied",
            "invalid auth",
            "authentication",
            "unknown command",
            "no such file",
            "not found",
        )
        return any(keyword in lowered for keyword in fatal_keywords)

    def run(self, args: list[str]) -> Any:
        command = self._base_command() + args
        engine_logger.info("执行 OKX CLI 命令: %s", " ".join(command))
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return self._parse_json_output(result.stdout)
        except FileNotFoundError as exc:
            raise OKXCLIError(
                "未检测到 okx 命令，请先安装 @okx_ai/okx-trade-cli。",
                recoverable=False,
                command=command,
            ) from exc
        except subprocess.CalledProcessError as exc:
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            message = stderr or stdout or str(exc)
            parsed_output: Any | None = None
            if stdout:
                try:
                    parsed_output = self._parse_json_output(stdout)
                except Exception:
                    parsed_output = None

            fatal = self._is_fatal_error(message)
            if parsed_output is not None and not fatal:
                engine_logger.warning(
                    "OKX CLI 返回非零退出码，判定为可恢复错误，继续解析 JSON。returncode=%s message=%s",
                    exc.returncode,
                    message[:240],
                )
                return parsed_output

            raise OKXCLIError(
                f"OKX CLI 调用失败（{'可恢复' if not fatal else '致命'}）: {message}",
                recoverable=not fatal,
                command=command,
                returncode=exc.returncode,
                stdout=stdout,
                stderr=stderr,
            ) from exc

    @staticmethod
    def _attach_requested_tag(data: Any, tag: str) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            return []
        for row in data:
            if isinstance(row, dict) and not row.get("requestedTag"):
                row["requestedTag"] = tag
        return data

    @staticmethod
    def _assert_algo_success(data: Any, operation_name: str) -> list[dict[str, Any]]:
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"{operation_name} 返回为空，无法确认算法单成功。")

        failed_rows: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                failed_rows.append({"sCode": "INVALID_ROW", "sMsg": str(row)})
                continue
            s_code = str(row.get("sCode", "")).strip()
            if s_code != "0":
                failed_rows.append({
                    "sCode": s_code or "MISSING",
                    "sMsg": str(row.get("sMsg", "缺少 sMsg")),
                    "ordId": str(row.get("ordId", "")),
                    "algoId": str(row.get("algoId", "")),
                })

        if failed_rows:
            raise RuntimeError(f"{operation_name} 失败: {failed_rows}")
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

    def get_instrument_spec(self, inst_id: str) -> dict[str, Any]:
        cached = self._instrument_cache.get(inst_id)
        if cached:
            return cached

        response = requests.get(
            f"{settings.okx_public_api_base.rstrip('/')}/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": inst_id},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"未获取到 {inst_id} 的合约规格。")
        spec = data[0]
        if not isinstance(spec, dict):
            raise RuntimeError(f"{inst_id} 合约规格格式异常: {spec}")
        self._instrument_cache[inst_id] = spec
        return spec

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
            "swap",
            "leverage",
            "--instId",
            inst_id,
            "--lever",
            str(lever),
            "--mgnMode",
            mgn_mode,
            "--posSide",
            pos_side,
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
            "swap",
            "place",
            "--instId",
            inst_id,
            "--side",
            side,
            "--ordType",
            ord_type,
            "--sz",
            str(size),
            "--tdMode",
            td_mode,
            "--tag",
            tag,
        ]
        if pos_side:
            args.extend(["--posSide", pos_side])
        if px is not None:
            args.extend(["--px", str(px)])
        if reduce_only:
            args.append("--reduceOnly")
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
        if algo_ord_type == "move_order_stop":
            args = [
                "swap",
                "algo",
                "trail",
                "--instId",
                inst_id,
                "--side",
                side,
                "--sz",
                str(sz),
                "--callbackRatio",
                str(callback_ratio),
                "--tdMode",
                td_mode,
                "--tag",
                tag,
            ]
            if active_px is not None:
                args.extend(["--activePx", str(active_px)])
        else:
            args = [
                "swap",
                "algo",
                "place",
                "--instId",
                inst_id,
                "--tdMode",
                td_mode,
                "--side",
                side,
                "--sz",
                str(sz),
                "--tag",
                tag,
            ]
            cli_ord_type = "conditional" if algo_ord_type in {"stop", "limit"} else algo_ord_type
            args.extend(["--ordType", cli_ord_type])

            if tp_trigger_px is not None:
                args.extend(["--tpTriggerPx", str(tp_trigger_px), f"--tpOrdPx={tp_ord_px}"])
            if sl_trigger_px is not None:
                args.extend(["--slTriggerPx", str(sl_trigger_px), f"--slOrdPx={sl_ord_px}"])

        if pos_side:
            args.extend(["--posSide", pos_side])
        if reduce_only:
            args.append("--reduceOnly")

        data = self.run(args) or []
        data = self._attach_requested_tag(data, tag)
        return self._assert_algo_success(data, f"{inst_id} {algo_ord_type} 算法单")
