from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from config_store import (
    SUPPORTED_DEXES,
    ConfigValidationError,
    read_coins_config,
    validate_coins_config,
    write_coins_config,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("liquidation-alert")

BASE_DIR = Path(__file__).resolve().parent
ADMIN_PAGE_PATH = BASE_DIR / "admin.html"

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows 精简 Python 环境可能没有系统时区库，退回固定 UTC+8 保证服务可启动。
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def strip_env_quotes(value: str) -> str:
    """去掉 .env 值两侧成对的引号，避免 JSON 配置解析失败。"""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_multiline_env_value(value: str) -> bool:
    """判断当前 .env 值是否是未闭合的多行引号值。"""
    value = value.strip()
    if not value or value[0] not in {"'", '"'}:
        return False

    quote = value[0]
    if len(value) == 1:
        return True
    return value[-1] != quote


def load_dotenv() -> None:
    """加载同目录 .env，支持 COINS_CONFIG 这类多行 JSON 配置。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if is_multiline_env_value(value):
            quote = value[0]
            parts = [value]
            while index < len(lines):
                next_line = lines[index]
                index += 1
                parts.append(next_line)
                if next_line.rstrip().endswith(quote):
                    break
            value = "\n".join(parts)

        os.environ.setdefault(key, strip_env_quotes(value))


load_dotenv()

# ==================== 配置 ====================
HYPERLIQUID_INFO_URL = os.getenv("HYPERLIQUID_INFO_URL", "https://api.hyperliquid.xyz/info")
FWALERT_URL = os.getenv("LIQUIDATION_FWALERT_URL", os.getenv("FWALERT_URL", ""))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))
VOLATILITY_WINDOW_SECONDS = int(
    os.getenv("VOLATILITY_WINDOW_SECONDS", os.getenv("WINDOW_SECONDS", "60"))
)
VOLATILITY_THRESHOLD_PERCENT = float(
    os.getenv("VOLATILITY_THRESHOLD_PERCENT", os.getenv("PERCENT_CHANGE_THRESHOLD", "1"))
)
ENABLE_MONITORING = os.getenv("ENABLE_MONITORING", "true").lower() == "true"

raw_config_path = Path(os.getenv("COINS_CONFIG_PATH", "coins_config.json"))
COINS_CONFIG_PATH = raw_config_path if raw_config_path.is_absolute() else BASE_DIR / raw_config_path
ALERTS_LOG_PATH = BASE_DIR / "alerts_log.jsonl"
COINS_CONFIG_RAW = os.getenv("COINS_CONFIG", "[]")
# ==============================================

if POLL_INTERVAL_SECONDS <= 0:
    raise RuntimeError("POLL_INTERVAL_SECONDS 必须大于 0")
if COOLDOWN_SECONDS < 0:
    raise RuntimeError("COOLDOWN_SECONDS 不能小于 0")
if VOLATILITY_WINDOW_SECONDS <= 0:
    raise RuntimeError("VOLATILITY_WINDOW_SECONDS 必须大于 0")
if VOLATILITY_THRESHOLD_PERCENT <= 0:
    raise RuntimeError("VOLATILITY_THRESHOLD_PERCENT 必须大于 0")


state_lock = threading.RLock()
config_lock = threading.RLock()
alert_log_lock = threading.Lock()
monitor_stop_event = threading.Event()
monitor_wakeup_event = threading.Event()
monitor_thread: threading.Thread | None = None

state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_cycle_at": None,
    "loop_count": 0,
    "coins": {},
    "last_alert_time": {},
    "last_volatility_alert_time": {},
    "volatility_alert_armed": {},
    "volatility_stats": {},
    "market_fetch": {},
    "last_error": None,
    "config_error": None,
    "config_source": None,
    "config_revision": 0,
}

price_history: dict[str, deque[dict[str, float]]] = {}


class AssetCatalogUnavailableError(RuntimeError):
    """Raised when a configuration cannot be checked against Hyperliquid."""


try:
    coins_config, initial_config_source = read_coins_config(COINS_CONFIG_PATH, COINS_CONFIG_RAW)
except (OSError, json.JSONDecodeError, ConfigValidationError) as exc:
    coins_config = []
    initial_config_source = "invalid"
    state["config_error"] = str(exc)
    state["last_error"] = f"配置加载失败: {exc}"
    logger.error("配置加载失败，可通过管理页面修复: %s", exc)

state["config_source"] = initial_config_source


def market_key(dex: str, symbol: str) -> str:
    return f"{dex.lower()}:{symbol.upper()}"


def get_coins_config_snapshot() -> list[dict[str, Any]]:
    with config_lock:
        return copy.deepcopy(coins_config)


def validate_config_assets(config: list[dict[str, Any]]) -> None:
    """Verify enabled markets against the current Hyperliquid asset catalogs."""
    symbols_by_dex: dict[str, set[str]] = defaultdict(set)
    for coin in config:
        if coin["enabled"]:
            symbols_by_dex[coin["dex"]].add(coin["symbol"])

    missing_markets: list[str] = []
    for dex, configured_symbols in symbols_by_dex.items():
        try:
            _, assets = request_dex_snapshot(dex)
        except Exception as exc:
            raise AssetCatalogUnavailableError(
                f"暂时无法验证 {SUPPORTED_DEXES[dex]} 的币种列表，请稍后重试"
            ) from exc

        available_symbols = {
            asset["symbol"]
            for asset in assets
            if not asset["is_delisted"]
        }
        missing_markets.extend(
            market_key(dex, symbol)
            for symbol in configured_symbols - available_symbols
        )

    if missing_markets:
        missing = "、".join(sorted(missing_markets))
        raise ConfigValidationError(f"以下币种不存在或已经下架：{missing}")


def sync_state_with_config(config: list[dict[str, Any]]) -> None:
    configured_keys = {market_key(coin["dex"], coin["symbol"]) for coin in config}

    with state_lock:
        removed_keys = set(state["coins"]) - configured_keys
        for removed_key in removed_keys:
            state["coins"].pop(removed_key, None)
            state["last_alert_time"].pop(removed_key, None)
            state["last_volatility_alert_time"].pop(removed_key, None)
            state["volatility_alert_armed"].pop(removed_key, None)
            state["volatility_stats"].pop(removed_key, None)
            price_history.pop(removed_key, None)

        for coin in config:
            key = market_key(coin["dex"], coin["symbol"])
            snapshot = state["coins"].setdefault(
                key,
                {
                    "dex": coin["dex"],
                    "symbol": coin["symbol"],
                    "price": None,
                    "targets": [],
                    "enabled": coin["enabled"],
                    "volatility_enabled": coin["volatility_enabled"],
                    "volatility": {
                        "sample_count": 0,
                        "window_seconds": VOLATILITY_WINDOW_SECONDS,
                        "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
                    },
                },
            )
            snapshot["dex"] = coin["dex"]
            snapshot["symbol"] = coin["symbol"]
            snapshot["targets"] = copy.deepcopy(coin["targets"])
            snapshot["enabled"] = coin["enabled"]
            snapshot["volatility_enabled"] = coin["volatility_enabled"]
            state["last_alert_time"].setdefault(key, 0)
            state["last_volatility_alert_time"].setdefault(key, 0)
            state["volatility_alert_armed"].setdefault(key, True)

            if not coin["volatility_enabled"]:
                price_history.pop(key, None)
                state["volatility_stats"].pop(key, None)
                snapshot["volatility"] = {
                    "sample_count": 0,
                    "window_seconds": VOLATILITY_WINDOW_SECONDS,
                    "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
                    "disabled": True,
                }


sync_state_with_config(coins_config)


def replace_coins_config(raw_config: Any) -> list[dict[str, Any]]:
    """Validate, atomically persist and hot-apply a new monitoring configuration."""
    global coins_config

    normalised = validate_coins_config(raw_config)
    validate_config_assets(normalised)
    with config_lock:
        write_coins_config(COINS_CONFIG_PATH, normalised)
        coins_config = copy.deepcopy(normalised)
        sync_state_with_config(coins_config)
        with state_lock:
            state["config_source"] = "file"
            state["config_error"] = None
            state["config_revision"] += 1
        monitor_wakeup_event.set()
    return copy.deepcopy(normalised)


def set_last_error(message: str) -> None:
    with state_lock:
        state["last_error"] = message


def append_alert_record(record: dict[str, Any]) -> None:
    try:
        ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with alert_log_lock:
            with open(ALERTS_LOG_PATH, "a", encoding="utf-8") as alert_file:
                alert_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        set_last_error(f"写入告警日志失败: {exc}")
        logger.exception("Failed to append alert log")


def trigger_phone_alert(
    dex: str,
    symbol: str,
    price: float,
    target_price: float,
    direction: str,
) -> bool:
    key = market_key(dex, symbol)
    record = {
        "event": "price_reached",
        "market": key,
        "dex": dex,
        "symbol": symbol,
        "price": price,
        "target_price": target_price,
        "direction": direction,
        "timestamp": time.time(),
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
    }

    if not FWALERT_URL:
        record["error"] = "missing_fwalert_url"
        set_last_error("missing_fwalert_url")
        append_alert_record(record)
        logger.error("Missing FWALERT_URL")
        return False

    now = time.time()
    with state_lock:
        last_time = state["last_alert_time"].get(key, 0)
    if now - last_time < COOLDOWN_SECONDS:
        logger.info("[%s] Alert suppressed due to cooldown", key)
        return False

    try:
        response = requests.get(FWALERT_URL, timeout=15)
        response.raise_for_status()
        record["status_code"] = response.status_code
        with state_lock:
            state["last_alert_time"][key] = now
        append_alert_record(record)
        logger.warning("[%s] Phone alert triggered! price=%s (%s)", key, price, direction)
        return True
    except Exception as exc:
        record["error"] = type(exc).__name__
        set_last_error(f"[{key}] 电话告警失败 ({type(exc).__name__})")
        append_alert_record(record)
        logger.exception("[%s] Failed to trigger phone alert", key)
        return False


def record_price_sample(key: str, price: float, timestamp: float | None = None) -> None:
    """记录价格样本，并清理超过波动窗口的旧数据。"""
    now = timestamp if timestamp is not None else time.time()
    with state_lock:
        samples = price_history.setdefault(key, deque())
        samples.append({"timestamp": now, "price": price})

        cutoff = now - VOLATILITY_WINDOW_SECONDS
        while samples and samples[0]["timestamp"] < cutoff:
            samples.popleft()


def calculate_volatility_stats(key: str) -> dict[str, float | int] | None:
    """计算指定市场在窗口内的价格振幅百分比。"""
    with state_lock:
        samples = list(price_history.get(key, []))
    if not samples:
        return None

    prices = [sample["price"] for sample in samples if sample.get("price", 0) > 0]
    if len(prices) < 2:
        return {
            "sample_count": len(prices),
            "window_seconds": VOLATILITY_WINDOW_SECONDS,
            "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
        }

    window_max = max(prices)
    window_min = min(prices)
    percent_move = ((window_max - window_min) / window_min) * 100
    return {
        "sample_count": len(prices),
        "window_seconds": VOLATILITY_WINDOW_SECONDS,
        "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
        "window_max": window_max,
        "window_min": window_min,
        "percent_move": percent_move,
    }


def trigger_volatility_alert(
    dex: str,
    symbol: str,
    price: float,
    stats: dict[str, float | int],
) -> bool:
    key = market_key(dex, symbol)
    now = time.time()
    record = {
        "event": "volatility_reached",
        "market": key,
        "dex": dex,
        "symbol": symbol,
        "price": price,
        "timestamp": now,
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
        **stats,
    }

    if not FWALERT_URL:
        record["error"] = "missing_fwalert_url"
        set_last_error("missing_fwalert_url")
        append_alert_record(record)
        logger.error("Missing FWALERT_URL")
        return False

    try:
        response = requests.get(FWALERT_URL, timeout=15)
        response.raise_for_status()
        record["status_code"] = response.status_code
        with state_lock:
            state["last_volatility_alert_time"][key] = now
        append_alert_record(record)
        logger.warning("[%s] Volatility alert triggered! price=%s, stats=%s", key, price, stats)
        return True
    except Exception as exc:
        record["error"] = type(exc).__name__
        set_last_error(f"[{key}] 波动告警失败 ({type(exc).__name__})")
        append_alert_record(record)
        logger.exception("[%s] Failed to trigger volatility alert", key)
        return False


def evaluate_volatility_alert(dex: str, symbol: str, price: float) -> None:
    """评估当前市场是否满足波动告警条件。"""
    key = market_key(dex, symbol)
    record_price_sample(key, price)
    stats = calculate_volatility_stats(key)
    if stats is None:
        return

    with state_lock:
        state["volatility_stats"][key] = stats
        if key in state["coins"]:
            state["coins"][key]["volatility"] = stats

    percent_move = stats.get("percent_move")
    if percent_move is None or percent_move < VOLATILITY_THRESHOLD_PERCENT:
        with state_lock:
            state["volatility_alert_armed"][key] = True
        return

    now = time.time()
    with state_lock:
        last_time = state["last_volatility_alert_time"].get(key, 0)
        armed = state["volatility_alert_armed"].get(key, True)
    if last_time and now - last_time < COOLDOWN_SECONDS:
        return
    if not armed:
        return

    logger.warning(
        "[%s] volatility reached target! percent_move=%s, threshold=%s",
        key,
        percent_move,
        VOLATILITY_THRESHOLD_PERCENT,
    )
    # Only disarm after a successful webhook. A transient failure remains retryable.
    if trigger_volatility_alert(dex, symbol, price, stats):
        with state_lock:
            state["volatility_alert_armed"][key] = False


def _price_from_context(context: dict[str, Any]) -> float | None:
    impact_prices = context.get("impactPxs") or []
    if len(impact_prices) >= 2:
        try:
            bid = float(impact_prices[0])
            ask = float(impact_prices[1])
            price = (bid + ask) / 2
            if math.isfinite(price) and price > 0:
                return price
        except (TypeError, ValueError):
            pass

    for field in ("midPx", "markPx", "oraclePx"):
        if context.get(field) is not None:
            try:
                price = float(context[field])
                if math.isfinite(price) and price > 0:
                    return price
            except (TypeError, ValueError):
                continue
    return None


def request_dex_snapshot(dex: str) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Fetch every asset for one HIP-3 DEX with a single Hyperliquid request."""
    response = requests.post(
        HYPERLIQUID_INFO_URL,
        json={"type": "metaAndAssetCtxs", "dex": dex},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list) or len(data) < 2:
        raise ValueError("Hyperliquid 返回结构异常")

    meta, asset_contexts = data[0], data[1]
    if not isinstance(meta, dict) or not isinstance(asset_contexts, list):
        raise ValueError("Hyperliquid 返回字段类型异常")

    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        raise ValueError("Hyperliquid universe 字段类型异常")

    prices: dict[str, float] = {}
    assets: list[dict[str, Any]] = []
    for index, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue

        full_name = str(asset.get("name", ""))
        symbol = full_name.split(":", 1)[1] if ":" in full_name else full_name
        symbol = symbol.upper()
        if not symbol:
            continue

        assets.append(
            {
                "symbol": symbol,
                "name": full_name,
                "is_delisted": bool(asset.get("isDelisted", False)),
            }
        )
        if index >= len(asset_contexts) or not isinstance(asset_contexts[index], dict):
            continue

        price = _price_from_context(asset_contexts[index])
        if price is not None and price > 0:
            prices[symbol] = price

    return prices, assets


def run_monitor_cycle() -> None:
    """Run one complete polling cycle for all currently enabled DEXes."""
    cycle_config = [coin for coin in get_coins_config_snapshot() if coin["enabled"]]
    coins_by_dex: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for coin in cycle_config:
        coins_by_dex[coin["dex"]].append(coin)

    for dex in coins_by_dex:
        try:
            prices, _ = request_dex_snapshot(dex)
            with state_lock:
                state["market_fetch"][dex] = {
                    "name": SUPPORTED_DEXES[dex],
                    "last_success_at": time.time(),
                    "last_error": None,
                }
        except Exception as exc:
            message = f"[{dex}] 获取价格失败: {exc}"
            set_last_error(message)
            with state_lock:
                state["market_fetch"][dex] = {
                    "name": SUPPORTED_DEXES[dex],
                    "last_success_at": state["market_fetch"].get(dex, {}).get(
                        "last_success_at"
                    ),
                    "last_error": str(exc),
                }
            logger.error(message)
            continue

        # Re-read this DEX after the network request so a configuration
        # edit cannot trigger an alert from a stale in-flight snapshot.
        current_dex_coins = [
            coin
            for coin in get_coins_config_snapshot()
            if coin["enabled"] and coin["dex"] == dex
        ]
        for coin in current_dex_coins:
            symbol = coin["symbol"]
            key = market_key(dex, symbol)
            price = prices.get(symbol)
            if price is None:
                logger.warning("[%s] 未在 Hyperliquid 响应中找到有效价格", key)
                continue

            with state_lock:
                if key not in state["coins"]:
                    # The configuration was removed while this request was in flight.
                    continue
                state["coins"][key]["price"] = price
                state["coins"][key]["updated_at"] = time.time()

            if coin["volatility_enabled"]:
                evaluate_volatility_alert(dex, symbol, price)

            for target in coin["targets"]:
                target_price = target["price"]
                direction = target["direction"]
                triggered = (
                    direction == "up" and price >= target_price
                ) or (
                    direction == "down" and price <= target_price
                )
                if triggered:
                    logger.warning(
                        "[%s] price reached target! price=%s, target=%s, direction=%s",
                        key,
                        price,
                        target_price,
                        direction,
                    )
                    trigger_phone_alert(dex, symbol, price, target_price, direction)


def monitor_loop() -> None:
    with state_lock:
        state["running"] = True
        state["started_at"] = time.time()

    try:
        while not monitor_stop_event.is_set():
            try:
                run_monitor_cycle()
            except Exception as exc:
                set_last_error(f"监控轮次失败: {exc}")
                logger.exception("Monitor cycle failed; retrying")
            finally:
                with state_lock:
                    state["loop_count"] += 1
                    state["last_cycle_at"] = time.time()

            if monitor_stop_event.is_set():
                break
            monitor_wakeup_event.wait(POLL_INTERVAL_SECONDS)
            monitor_wakeup_event.clear()
    finally:
        with state_lock:
            state["running"] = False


app = FastAPI(title="liquidation-alert")


@app.on_event("startup")
def startup_event() -> None:
    global monitor_thread
    if not ENABLE_MONITORING:
        logger.info("liquidation-alert started (monitoring disabled)")
        return

    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_stop_event.clear()
        monitor_wakeup_event.clear()
        monitor_thread = threading.Thread(
            target=monitor_loop,
            name="liquidation-monitor",
            daemon=True,
        )
        monitor_thread.start()
    logger.info("liquidation-alert started (monitoring enabled)")


@app.on_event("shutdown")
def shutdown_event() -> None:
    monitor_stop_event.set()
    monitor_wakeup_event.set()
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=5)


@app.get("/")
def root() -> dict[str, Any]:
    with state_lock:
        state_snapshot = copy.deepcopy(state)
    return {
        "service": "liquidation-alert",
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "fwalert_configured": bool(FWALERT_URL),
        "supported_dexes": SUPPORTED_DEXES,
        "configured_markets": len(get_coins_config_snapshot()),
        **state_snapshot,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "volatility_window_seconds": VOLATILITY_WINDOW_SECONDS,
        "volatility_threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    if not ADMIN_PAGE_PATH.exists():
        raise HTTPException(status_code=500, detail="管理页面文件不存在")
    return HTMLResponse(
        ADMIN_PAGE_PATH.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    with state_lock:
        config_metadata = {
            "source": state["config_source"],
            "revision": state["config_revision"],
            "error": state["config_error"],
        }
    return {
        "coins": get_coins_config_snapshot(),
        "supported_dexes": SUPPORTED_DEXES,
        "config": config_metadata,
        "settings": {
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "volatility_window_seconds": VOLATILITY_WINDOW_SECONDS,
            "volatility_threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
        },
    }


@app.put("/api/config")
async def update_config(request: Request) -> dict[str, Any]:
    try:
        request_body = await request.body()
        if len(request_body) > 256 * 1024:
            raise HTTPException(status_code=413, detail="配置内容过大")
        payload = json.loads(request_body)
        updated = await run_in_threadpool(replace_coins_config, payload)
    except (json.JSONDecodeError, ConfigValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AssetCatalogUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {exc}") from exc

    logger.info("Monitoring configuration updated by admin user")
    return {
        "ok": True,
        "coins": updated,
        "message": "配置已保存并实时生效，无需重启服务",
    }


@app.get("/api/assets")
def get_available_assets() -> dict[str, Any]:
    markets: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for dex, display_name in SUPPORTED_DEXES.items():
        try:
            _prices, assets = request_dex_snapshot(dex)
            markets[dex] = {
                "name": display_name,
                "assets": sorted(
                    [asset for asset in assets if not asset["is_delisted"]],
                    key=lambda asset: asset["symbol"],
                ),
            }
        except Exception as exc:
            errors[dex] = str(exc)
            markets[dex] = {"name": display_name, "assets": []}

    return {"markets": markets, "errors": errors}
