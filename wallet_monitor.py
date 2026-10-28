from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import requests

from config_store import ConfigValidationError


logger = logging.getLogger("liquidation-alert.wallets")
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
BEIJING_TZ = timezone(timedelta(hours=8))
MAX_WALLETS = 20
MAX_EVENTS = 1000
FILL_PAGE_SIZE = 2000
OVERLAP_MS = 60_000
# 只有"开单 / 关单"呼叫电话；加仓、减仓仍记录在事件流里，但不拨号。
ALERT_ACTIONS = {"open", "close", "reverse"}
ACTION_LABELS = {"open": "开仓", "close": "平仓", "reverse": "反手"}
DEFAULT_ALERT_COOLDOWN_SECONDS = 600
MIN_ALERT_COOLDOWN_SECONDS = 30
MAX_ALERT_COOLDOWN_SECONDS = 86_400


class ConfigConflictError(RuntimeError):
    """Raised when a conditional update was based on an older configuration."""


def _config_version(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalise_address(value: Any) -> str:
    if not isinstance(value, str) or not ADDRESS_PATTERN.fullmatch(value.strip()):
        raise ConfigValidationError("地址必须是 0x 开头的 40 位十六进制 HL 钱包地址")
    return value.strip().lower()


def validate_alert_cooldown(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_ALERT_COOLDOWN_SECONDS <= value <= MAX_ALERT_COOLDOWN_SECONDS
    ):
        raise ConfigValidationError(
            f"电话告警冷却必须是 {MIN_ALERT_COOLDOWN_SECONDS} 到 "
            f"{MAX_ALERT_COOLDOWN_SECONDS} 之间的整数秒"
        )
    return value


def legacy_alert_defaults(payload: Any) -> tuple[bool, int]:
    """兼容 2026-10-28 短暂使用的全局 alerts 配置：未单独设置的地址沿用它。"""
    enabled, cooldown = True, DEFAULT_ALERT_COOLDOWN_SECONDS
    if not isinstance(payload, dict):
        return enabled, cooldown
    if isinstance(payload.get("phone_enabled"), bool):
        enabled = payload["phone_enabled"]
    try:
        cooldown = validate_alert_cooldown(payload.get("cooldown_seconds", cooldown))
    except ConfigValidationError:
        pass
    return enabled, cooldown


def validate_wallet_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigValidationError("地址监控配置必须是对象")
    interval = payload.get("poll_interval_seconds", 15)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 300:
        raise ConfigValidationError("轮询间隔必须是 5 到 300 之间的整数秒")
    legacy_enabled, legacy_cooldown = legacy_alert_defaults(payload.get("alerts"))
    wallets = payload.get("wallets", [])
    if not isinstance(wallets, list) or len(wallets) > MAX_WALLETS:
        raise ConfigValidationError(f"wallets 必须是数组，最多监控 {MAX_WALLETS} 个地址")
    result = []
    seen = set()
    for wallet in wallets:
        if not isinstance(wallet, dict):
            raise ConfigValidationError("每个地址配置必须是对象")
        address = normalise_address(wallet.get("address"))
        if address in seen:
            raise ConfigValidationError(f"地址重复：{address}")
        seen.add(address)
        label = wallet.get("label", "")
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise ConfigValidationError("地址备注必须是最多 80 个字符的字符串")
        enabled = wallet.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigValidationError("enabled 必须是 true 或 false")
        alert_enabled = wallet.get("alert_enabled", legacy_enabled)
        if not isinstance(alert_enabled, bool):
            raise ConfigValidationError("alert_enabled 必须是 true 或 false")
        alert_cooldown = validate_alert_cooldown(
            wallet.get("alert_cooldown_seconds", legacy_cooldown)
        )
        result.append({
            "address": address,
            "label": label.strip(),
            "enabled": enabled,
            "alert_enabled": alert_enabled,
            "alert_cooldown_seconds": alert_cooldown,
        })
    return {"poll_interval_seconds": interval, "wallets": result}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"HL 返回无效的 {field}") from exc
    if not number.is_finite():
        raise RuntimeError(f"HL 返回无效的 {field}")
    return number


def _number(value: Any) -> float | None:
    if value is None:
        return None
    result = float(_decimal(value, "数值"))
    if not math.isfinite(result):
        raise RuntimeError("HL 返回超出范围的数值")
    return result


def fill_to_event(fill: dict[str, Any], wallet: dict[str, Any]) -> dict[str, Any] | None:
    """Use signed starting position to distinguish partial closes and reversals."""
    coin = fill.get("coin")
    if not isinstance(coin, str) or not coin or coin.startswith("@") or "/" in coin:
        return None  # Spot trades have no perpetual position to open or close.
    # HL perpetual fills have these fields; unrelated/non-position fill kinds are ignored.
    if "startPosition" not in fill or fill.get("side") not in {"B", "A"}:
        return None
    start = _decimal(fill["startPosition"], "startPosition")
    size = _decimal(fill.get("sz"), "sz")
    price = _decimal(fill.get("px"), "px")
    if size <= 0 or price <= 0:
        raise RuntimeError("HL 成交数量和价格必须大于 0")
    change = size if fill["side"] == "B" else -size
    end = start + change
    if start == 0:
        action = "open"
    elif end == 0:
        action = "close"
    elif (start > 0) != (end > 0):
        action = "reverse"
    elif abs(end) > abs(start):
        action = "increase"
    else:
        action = "reduce"
    direction_position = start if action in {"close", "reduce"} else end
    timestamp_ms = int(fill["time"])
    identity = json.dumps(
        [wallet["address"], coin, fill.get("tid"), fill.get("hash"),
         fill.get("oid"), timestamp_ms, fill.get("side"), str(size), str(price), str(start)],
        separators=(",", ":"),
    )
    dex, symbol = coin.split(":", 1) if ":" in coin else ("", coin)
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "address": wallet["address"], "label": wallet["label"],
        "coin": coin, "dex": dex, "symbol": symbol,
        "action": action, "side": "long" if direction_position > 0 else "short",
        "size": float(size), "price": float(price),
        "start_position": float(start), "end_position": float(end),
        "closed_pnl": _number(fill.get("closedPnl", "0")),
        "timestamp": timestamp_ms / 1000,
        "beijing_time": datetime.fromtimestamp(timestamp_ms / 1000, BEIJING_TZ).isoformat(),
        "order_id": fill.get("oid"), "trade_id": fill.get("tid"),
    }


class WalletMonitor:
    def __init__(
        self, base_dir: Path, info_url: str, config_path: Path | None = None,
        state_path: Path | None = None, env_config: str = "{}",
        fwalert_url: str = "", alert_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.info_url = info_url
        self.config_path = config_path or base_dir / "wallets_config.json"
        self.state_path = state_path or base_dir / "wallet_monitor_state.json"
        # 电话告警复用波动告警的同一个 fwalert 链接（由 app.py 注入）。
        self.fwalert_url = fwalert_url
        self._alert_recorder = alert_recorder
        self._last_alert_at: dict[str, float] = {}
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_cycle_at: float | None = None
        self._last_error: str | None = None
        self._config_meta = {"source": "environment", "error": None, "revision": 0}
        self._sessions: dict[str, dict[str, Any]] = {}
        try:
            if self.config_path.exists():
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                self._config_meta["source"] = "file"
            else:
                raw = json.loads(env_config or "{}")
            self._config = validate_wallet_config(raw)
            sessions = raw.get("_monitoring_sessions", {})
            if not isinstance(sessions, dict):
                raise ValueError("地址监控会话配置格式无效")
            for address, session in sessions.items():
                normalise_address(address)
                if not isinstance(session, dict) or not isinstance(session.get("start_ms"), int) or not isinstance(session.get("session"), str):
                    raise ValueError("地址监控会话配置格式无效")
            self._sessions = sessions
        except (OSError, ValueError, TypeError) as exc:
            self._config = validate_wallet_config({})
            self._config_meta.update(source="invalid", error=str(exc))
        self._config_meta["version"] = _config_version(self._config)
        self._events: list[dict[str, Any]] = []
        self._checkpoints: dict[str, dict[str, Any]] = {}
        try:
            if self.state_path.exists():
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or not isinstance(saved.get("checkpoints"), dict) or not isinstance(saved.get("events"), list):
                    raise ValueError("地址监控状态文件格式无效")
                # Validate checkpoints before trusting their cursor or deduplication window.
                for address, point in saved["checkpoints"].items():
                    normalise_address(address)
                    if not isinstance(point, dict) or not isinstance(point.get("start_ms"), int) or not isinstance(point.get("watermark_ms"), int) or not isinstance(point.get("seen"), list):
                        raise ValueError("地址监控检查点格式无效")
                    for seen in point["seen"]:
                        if not isinstance(seen, dict) or not isinstance(seen.get("id"), str) or not isinstance(seen.get("time"), int):
                            raise ValueError("地址监控去重记录格式无效")
                if not all(
                    isinstance(event, dict)
                    and isinstance(event.get("timestamp"), (int, float))
                    and math.isfinite(event["timestamp"])
                    and isinstance(event.get("id"), str)
                    and isinstance(event.get("address"), str)
                    and ADDRESS_PATTERN.fullmatch(event["address"])
                    for event in saved["events"]
                ):
                    raise ValueError("地址监控事件记录格式无效")
                self._events = saved["events"][:MAX_EVENTS]
                self._checkpoints = saved["checkpoints"]
        except (OSError, ValueError, TypeError) as exc:
            self._last_error = f"监控状态加载失败，当前进程从启动时刻监控：{exc}"
            self._events = []
            self._checkpoints = {}
        self._wallets: dict[str, dict[str, Any]] = {}
        self._generations: dict[str, int] = {}
        self._dexes: list[str] | None = None
        self._dexes_updated_at = 0.0
        now_ms = int(time.time() * 1000)
        initial_points_changed = False
        for wallet in self._config["wallets"]:
            address = wallet["address"]
            self._wallets[address] = self._empty_wallet(wallet)
            self._generations[address] = 1
            if wallet["enabled"]:
                session = self._sessions.get(address)
                point = self._checkpoints.get(address)
                if session is not None and (point is None or point.get("session") != session["session"]):
                    self._checkpoints[address] = self._new_checkpoint(session["start_ms"], session["session"])
                    initial_points_changed = True
                elif point is None:
                    self._checkpoints[address] = self._new_checkpoint(now_ms)
                    initial_points_changed = True
                self._sessions[address] = {
                    "start_ms": self._checkpoints[address]["start_ms"],
                    "session": self._checkpoints[address].setdefault("session", uuid.uuid4().hex),
                }
                self._wallets[address]["initialized"] = self._checkpoints[address].get("initialized", False)
            else:
                self._checkpoints.pop(address, None)
        active = set(self._wallets)
        self._checkpoints = {address: point for address, point in self._checkpoints.items() if address in active}
        if initial_points_changed:
            try:
                _atomic_write(self.state_path, {"version": 1, "checkpoints": self._checkpoints, "events": self._events})
            except OSError as exc:
                self._last_error = f"保存监控起点失败，重启可能丢失尚未同步的成交：{exc}"

    @staticmethod
    def _new_checkpoint(now_ms: int, session: str | None = None) -> dict[str, Any]:
        return {"start_ms": now_ms, "watermark_ms": now_ms, "seen": [], "initialized": False, "session": session or uuid.uuid4().hex}

    @staticmethod
    def _empty_wallet(wallet: dict[str, Any]) -> dict[str, Any]:
        return {
            **wallet, "positions": [], "positions_updated_at": None,
            "positions_error": None, "last_error": None, "initialized": False,
        }

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return {**copy.deepcopy(self._config), "config": copy.deepcopy(self._config_meta)}

    def replace_config(self, payload: Any) -> dict[str, Any]:
        config = validate_wallet_config(payload)
        if "expected_version" in payload and not isinstance(payload["expected_version"], str):
            raise ConfigValidationError("expected_version 必须是配置版本字符串")
        expected_version = payload.get("expected_version")
        now_ms = int(time.time() * 1000)
        with self._lock:
            if expected_version is not None and expected_version != self._config_meta["version"]:
                raise ConfigConflictError("配置已在其他页面更新，请重新载入配置后再保存；当前草稿已保留")
            next_points = copy.deepcopy(self._checkpoints)
            next_sessions = {}
            for wallet in config["wallets"]:
                address = wallet["address"]
                old = self._wallets.get(address)
                if old is None or old["enabled"] != wallet["enabled"]:
                    next_points.pop(address, None)
                    if wallet["enabled"]:
                        next_points[address] = self._new_checkpoint(now_ms)
                if wallet["enabled"]:
                    point = next_points[address]
                    next_sessions[address] = {"start_ms": point["start_ms"], "session": point["session"]}
            # File write completes before replacing the live configuration.
            # Activation IDs are persisted with configuration. On restart they override
            # stale state from before a pause/re-enable even if no poll succeeded yet.
            _atomic_write(self.config_path, {"version": 1, **config, "_monitoring_sessions": next_sessions})
            next_wallets = {}
            for wallet in config["wallets"]:
                address = wallet["address"]
                old = self._wallets.get(address)
                changed_enabled = old is None or old["enabled"] != wallet["enabled"]
                if changed_enabled:
                    self._generations[address] = self._generations.get(address, 0) + 1
                next_wallets[address] = {**(old or self._empty_wallet(wallet)), **wallet}
                if changed_enabled:
                    next_wallets[address]["initialized"] = False
            for address in self._wallets.keys() - next_wallets.keys():
                self._generations[address] = self._generations.get(address, 0) + 1
            self._wallets = next_wallets
            self._checkpoints = {address: point for address, point in next_points.items() if address in next_sessions}
            self._sessions = next_sessions
            self._config = config
            self._config_meta.update(
                source="file", error=None, revision=self._config_meta["revision"] + 1,
                version=_config_version(config),
            )
            self._wake.set()
            return self.get_config()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running, "last_cycle_at": self._last_cycle_at,
                "last_error": self._last_error,
                "poll_interval_seconds": self._config["poll_interval_seconds"],
                "effective_poll_interval_seconds": self._effective_interval(),
                "wallets": copy.deepcopy(self._wallets),
                "events": copy.deepcopy(self._events[:100]),
                "config": copy.deepcopy(self._config_meta),
            }

    def get_events(self, address: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if address is not None:
            address = normalise_address(address)
        with self._lock:
            events = self._events if address is None else [event for event in self._events if event["address"] == address]
            return copy.deepcopy(events[:max(1, min(limit, MAX_EVENTS))])

    def _request(self, payload: dict[str, Any]) -> Any:
        # 到 HL 的国际链路偶发抖动（实测 TLS 握手可到 3.6s），连接/读取时限各留余量。
        response = requests.post(self.info_url, json=payload, timeout=(8, 20))
        response.raise_for_status()
        return response.json()

    def _request_fills(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        result = []
        cursor = start_ms
        for _ in range(6):
            page = self._request({
                "type": "userFillsByTime", "user": address,
                "startTime": cursor, "endTime": end_ms, "aggregateByTime": False,
            })
            if not isinstance(page, list) or not all(isinstance(fill, dict) for fill in page):
                raise RuntimeError("HL 成交接口返回格式无效")
            result.extend(page)
            if len(page) < FILL_PAGE_SIZE:
                return result
            latest = max(int(fill["time"]) for fill in page)
            if latest <= cursor:
                raise RuntimeError("单毫秒成交达到接口分页上限，无法保证完整读取；检查该地址成交记录")
            # Inclusive boundary plus event IDs prevents dropping other fills in this millisecond.
            cursor = latest
        raise RuntimeError("成交历史超过接口分页范围（HL 仅保留最近 10000 条）；暂停后重新启用可从当前时刻监控")

    def _discover_dexes(self) -> list[str]:
        with self._lock:
            if self._dexes is not None and time.time() - self._dexes_updated_at < 300:
                return list(self._dexes)
        payload = self._request({"type": "perpDexs"})
        if not isinstance(payload, list):
            raise RuntimeError("HL 市场列表返回格式无效")
        names = [""]
        for dex in payload:
            if dex is None:
                continue
            if not isinstance(dex, dict) or not isinstance(dex.get("name"), str) or not dex["name"]:
                raise RuntimeError("HL 市场列表包含无效市场")
            if dex["name"] not in names:
                names.append(dex["name"])
        with self._lock:
            self._dexes = names
            self._dexes_updated_at = time.time()
        return names

    def _request_positions(self, address: str) -> list[dict[str, Any]]:
        def fetch_dex(dex: str) -> list[dict[str, Any]]:
            result = []
            if self._stop.is_set():
                raise RuntimeError("地址监控正在停止")
            payload = {"type": "clearinghouseState", "user": address}
            if dex:
                payload["dex"] = dex
            data = self._request(payload)
            if not isinstance(data, dict) or not isinstance(data.get("assetPositions"), list):
                raise RuntimeError(f"{dex or 'HL'} 持仓返回格式无效")
            for wrapper in data["assetPositions"]:
                position = wrapper.get("position") if isinstance(wrapper, dict) else None
                if not isinstance(position, dict) or not isinstance(position.get("coin"), str):
                    raise RuntimeError(f"{dex or 'HL'} 持仓明细格式无效")
                size = _decimal(position.get("szi"), "szi")
                if not size:
                    continue
                coin = position["coin"]
                if dex and ":" not in coin:
                    coin = f"{dex}:{coin}"
                actual_dex, symbol = coin.split(":", 1) if ":" in coin else ("", coin)
                leverage = position.get("leverage") or {}
                result.append({
                    "coin": coin, "dex": actual_dex, "symbol": symbol,
                    "side": "long" if size > 0 else "short", "size": float(abs(size)),
                    "entry_price": _number(position.get("entryPx")),
                    "position_value": _number(position.get("positionValue")),
                    "unrealized_pnl": _number(position.get("unrealizedPnl")),
                    "liquidation_price": _number(position.get("liquidationPx")),
                    "leverage": _number(leverage.get("value")),
                    "leverage_type": leverage.get("type"),
                })
            return result
        result = []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wallet-positions") as pool:
            for positions in pool.map(fetch_dex, self._discover_dexes()):
                result.extend(positions)
        return sorted(result, key=lambda position: position["coin"])

    def _current(self, address: str, generation: int, require_enabled: bool = True) -> bool:
        wallet = self._wallets.get(address)
        return bool(wallet and (wallet["enabled"] or not require_enabled) and self._generations.get(address) == generation)

    def get_positions(self, address: str, refresh: bool = False) -> dict[str, Any]:
        address = normalise_address(address)
        with self._lock:
            if address not in self._wallets:
                raise ConfigValidationError("请先在地址监控页配置该地址")
            generation = self._generations[address]
        if refresh:
            try:
                positions = self._request_positions(address)
            except Exception as exc:
                with self._lock:
                    if self._current(address, generation, require_enabled=False):
                        self._wallets[address]["positions_error"] = str(exc)
                raise
            with self._lock:
                if not self._current(address, generation, require_enabled=False):
                    raise ConfigValidationError("地址配置已经变更，请刷新后重试")
                self._wallets[address].update(
                    positions=positions, positions_updated_at=time.time(), positions_error=None,
                )
        with self._lock:
            if not self._current(address, generation, require_enabled=False):
                raise ConfigValidationError("地址配置已经变更，请刷新后重试")
            wallet = self._wallets[address]
            updated = wallet["positions_updated_at"]
            stale = updated is None or time.time() - updated > self._effective_interval() * 2 or bool(wallet["positions_error"])
            return copy.deepcopy({
                "address": address, "label": wallet["label"], "positions": wallet["positions"],
                "updated_at": updated, "positions_updated_at": updated,
                "error": wallet["positions_error"], "positions_error": wallet["positions_error"],
                "stale": stale,
            })

    def _commit_fills(self, wallet: dict[str, Any], generation: int, fills: list[dict[str, Any]], end_ms: int) -> list[dict[str, Any]] | None:
        address = wallet["address"]
        with self._lock:
            if not self._current(address, generation):
                return None
            point = copy.deepcopy(self._checkpoints[address])
            seen = {entry["id"] for entry in point["seen"]}
            new_events = []
            for fill in sorted(fills, key=lambda item: int(item["time"])):
                if not point["start_ms"] <= int(fill["time"]) <= end_ms:
                    continue
                event = fill_to_event(fill, self._wallets[address])
                if event is None or event["id"] in seen:
                    continue
                seen.add(event["id"])
                new_events.append(event)
                point["seen"].append({"id": event["id"], "time": int(fill["time"])})
            point["watermark_ms"] = end_ms
            point["initialized"] = True
            point["seen"] = [entry for entry in point["seen"] if entry["time"] >= end_ms - OVERLAP_MS][-10_000:]
            points = {**self._checkpoints, address: point}
            events = sorted(new_events + self._events, key=lambda event: event["timestamp"], reverse=True)[:MAX_EVENTS]
            _atomic_write(self.state_path, {"version": 1, "checkpoints": points, "events": events})
            self._checkpoints = points
            self._events = events
            self._wallets[address]["initialized"] = True
            return new_events

    @staticmethod
    def _compact(value: float | int | None) -> str:
        if value is None:
            return "-"
        return f"{float(value):.10g}"

    def _alert_summary(self, events: list[dict[str, Any]]) -> str:
        parts = []
        for event in events:
            side_text = "多" if event["side"] == "long" else "空"
            action_text = ACTION_LABELS.get(event["action"], event["action"])
            parts.append(
                f"{action_text} {event['coin']} {side_text} "
                f"{self._compact(event.get('size'))} @ {self._compact(event.get('price'))}"
            )
        return "；".join(parts)

    def _record_alert(self, record: dict[str, Any]) -> None:
        recorder = self._alert_recorder
        if recorder is None:
            return
        try:
            recorder(record)
        except Exception:
            logger.exception("Failed to persist wallet alert record")

    def _persist_state(self) -> None:
        try:
            with self._lock:
                _atomic_write(
                    self.state_path,
                    {"version": 1, "checkpoints": self._checkpoints, "events": self._events},
                )
        except OSError as exc:
            with self._lock:
                self._last_error = f"保存电话告警标记失败：{exc}"
            logger.warning("Failed to persist phone-alert marks: %s", exc)

    def _mark_alert_events(self, events: list[dict[str, Any]], status: str, at: float) -> None:
        """在触发判断的成交事件上留下拨号痕迹，供页面追溯（随状态文件持久化）。"""
        for event in events:
            event["alert"] = {"status": status, "at": at}
        self._persist_state()

    def _maybe_alert_open_close(
        self, wallet: dict[str, Any], generation: int, events: list[dict[str, Any]],
    ) -> bool:
        """开单 / 关单（含反手）时拨电话；每个地址用自己的开关和冷却窗口。"""
        interesting = [event for event in events if event["action"] in ALERT_ACTIONS]
        if not interesting:
            return False
        if not wallet.get("alert_enabled", True):
            self._mark_alert_events(interesting, "disabled", time.time())
            return False
        address = wallet["address"]
        cooldown = int(wallet.get("alert_cooldown_seconds", DEFAULT_ALERT_COOLDOWN_SECONDS))
        now = time.time()
        with self._lock:
            if not self._current(address, generation):
                return False
            last = self._last_alert_at.get(address, 0.0)
            remaining = cooldown - (now - last)
            if last and remaining > 0:
                logger.info(
                    "[%s] Wallet open/close alert suppressed by cooldown (%.0fs left): %s",
                    address, remaining, self._alert_summary(interesting),
                )
                self._mark_alert_events(interesting, "cooldown", now)
                return False

        record = {
            "event": "wallet_open_close",
            "market": wallet.get("label") or f"{address[:6]}…{address[-4:]}",
            "address": address,
            "label": wallet.get("label") or "",
            "symbol": ",".join(dict.fromkeys(str(event["coin"]) for event in interesting)),
            # 一批多笔时价格没有单一含义，交给 detail 描述，避免页面出现误导性单价。
            "price": interesting[-1]["price"] if len(interesting) == 1 else None,
            "actions": [event["action"] for event in interesting],
            "detail": self._alert_summary(interesting),
            "event_ids": [event["id"] for event in interesting],
            "timestamp": now,
            "beijing_time": datetime.fromtimestamp(now, BEIJING_TZ).isoformat(),
        }

        if not self.fwalert_url:
            record["error"] = "missing_fwalert_url"
            self._record_alert(record)
            self._mark_alert_events(interesting, "missing_url", now)
            logger.error("[%s] Missing FWALERT_URL, wallet open/close alert not sent", address)
            return False

        with self._lock:
            # Claim the cooldown slot before dialling so a concurrent cycle can't double-dial,
            # but re-check first because the check above happened outside the request.
            last = self._last_alert_at.get(address, 0.0)
            if last and cooldown - (time.time() - last) > 0:
                logger.info("[%s] Wallet open/close alert suppressed by cooldown", address)
                self._mark_alert_events(interesting, "cooldown", time.time())
                return False
            self._last_alert_at[address] = now

        try:
            response = requests.get(self.fwalert_url, timeout=15)
            response.raise_for_status()
        except Exception as exc:
            with self._lock:
                # A failed dial must not eat the window: the next open/close still gets a call.
                if self._last_alert_at.get(address) == now:
                    self._last_alert_at.pop(address, None)
            record["error"] = type(exc).__name__
            self._record_alert(record)
            self._mark_alert_events(interesting, "failed", now)
            logger.exception("[%s] Failed to dial wallet open/close alert", address)
            return False

        record["status_code"] = response.status_code
        self._record_alert(record)
        self._mark_alert_events(interesting, "dialled", now)
        logger.warning("[%s] Wallet open/close phone alert dialled: %s", address, record["detail"])
        return True

    def run_cycle(self) -> None:
        # On-demand refresh has its own snapshot; only one fill poll can advance cursors.
        with self._cycle_lock:
            with self._lock:
                jobs = [(copy.deepcopy(wallet), self._generations[address]) for address, wallet in self._wallets.items() if wallet["enabled"]]
            errors = []
            for wallet, generation in jobs:
                if self._stop.is_set():
                    break
                address = wallet["address"]
                with self._lock:
                    if not self._current(address, generation):
                        continue
                    point = copy.deepcopy(self._checkpoints[address])
                end_ms = int(time.time() * 1000)
                start_ms = max(point["start_ms"], point["watermark_ms"] - OVERLAP_MS)
                wallet_errors = []
                try:
                    fills = self._request_fills(address, start_ms, end_ms)
                    new_events = self._commit_fills(wallet, generation, fills, end_ms)
                    if new_events is None:
                        continue
                    if new_events:
                        # 在新成交落盘之后拨号：即使 webhook 失败，事件也不会丢。
                        self._maybe_alert_open_close(wallet, generation, new_events)
                except Exception as exc:
                    wallet_errors.append(f"成交同步失败：{exc}")
                    logger.warning("[%s] Fill sync failed: %s", address, exc)
                if self._stop.is_set():
                    break
                try:
                    positions = self._request_positions(address)
                    with self._lock:
                        if self._current(address, generation):
                            self._wallets[address].update(positions=positions, positions_updated_at=time.time(), positions_error=None)
                except Exception as exc:
                    wallet_errors.append(f"持仓同步失败：{exc}")
                    with self._lock:
                        if self._current(address, generation):
                            self._wallets[address]["positions_error"] = str(exc)
                    logger.warning("[%s] Position sync failed: %s", address, exc)
                with self._lock:
                    if self._current(address, generation):
                        self._wallets[address]["last_error"] = "；".join(wallet_errors) or None
                if wallet_errors:
                    errors.append(f"{wallet['label'] or address}: {'；'.join(wallet_errors)}")
            with self._lock:
                self._last_cycle_at = time.time()
                self._last_error = "；".join(errors) or None

    def _effective_interval(self) -> int:
        # Native + HIP-3 state requests and fills share HL's 1200 weight/minute/IP budget.
        # Leave headroom for price monitoring as the watched address list grows.
        enabled = sum(wallet["enabled"] for wallet in self._wallets.values())
        return max(self._config["poll_interval_seconds"], enabled * 3)

    def _loop(self) -> None:
        with self._lock:
            self._running = True
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    self.run_cycle()
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"地址监控轮次失败：{exc}"
                    logger.exception("Wallet monitoring cycle failed")
                with self._lock:
                    interval = self._effective_interval()
                if not self._stop.is_set():
                    self._wake.wait(interval)
        finally:
            with self._lock:
                self._running = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="wallet-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
