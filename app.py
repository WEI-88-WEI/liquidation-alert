from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("liquidation-alert")

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows 精简 Python 环境可能没有系统时区库，退回固定 UTC+8 保证服务可启动。
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
ALERTS_LOG_PATH = Path(__file__).with_name("alerts_log.jsonl")


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
    """加载同目录 .env，支持 README 中 COINS_CONFIG 这类多行 JSON 配置。"""
    env_path = Path(__file__).with_name(".env")
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

        # 兼容 COINS_CONFIG='[ ... ]' 这种跨多行写法。
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
FWALERT_URL = os.getenv("LIQUIDATION_FWALERT_URL", os.getenv("FWALERT_URL", ""))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))
VOLATILITY_WINDOW_SECONDS = int(os.getenv("VOLATILITY_WINDOW_SECONDS", os.getenv("WINDOW_SECONDS", "60")))
VOLATILITY_THRESHOLD_PERCENT = float(
    os.getenv("VOLATILITY_THRESHOLD_PERCENT", os.getenv("PERCENT_CHANGE_THRESHOLD", "1"))
)

# 新增：监控开关
ENABLE_MONITORING = os.getenv("ENABLE_MONITORING", "true").lower() == "true"

COINS_CONFIG_RAW = os.getenv("COINS_CONFIG", "[]")
try:
    COINS_CONFIG = json.loads(COINS_CONFIG_RAW)
except Exception:
    COINS_CONFIG = []
    logger.error("COINS_CONFIG 解析失败，请检查 JSON 格式")

# ==============================================

app = FastAPI(title="liquidation-alert")

# 多币种状态
state = {
    "running": False,
    "started_at": None,
    "loop_count": 0,
    "coins": {},          # 每个币种的最新快照
    "last_alert_time": {}, # 每个币种的最后告警时间
    "last_volatility_alert_time": {}, # 每个币种的最后波动告警时间
    "volatility_alert_armed": {}, # 波动回落到阈值以下后才重新允许告警
    "volatility_stats": {}, # 每个币种的滚动窗口振幅统计
    "last_error": None,
}

price_history: dict[str, deque[dict[str, float]]] = {}


def append_alert_record(record: dict) -> None:
    ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def trigger_phone_alert(symbol: str, price: float, target_price: float, direction: str):
    record = {
        "event": "price_reached",
        "symbol": symbol,
        "price": price,
        "target_price": target_price,
        "direction": direction,
        "timestamp": time.time(),
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
    }

    if not FWALERT_URL:
        record["error"] = "missing_fwalert_url"
        state["last_error"] = "missing_fwalert_url"
        append_alert_record(record)
        logger.error("Missing FWALERT_URL")
        return

    now = time.time()
    last_time = state["last_alert_time"].get(symbol, 0)
    if now - last_time < COOLDOWN_SECONDS:
        record["suppressed"] = True
        record["reason"] = "cooldown"
        append_alert_record(record)
        logger.info(f"[{symbol}] Alert suppressed due to cooldown")
        return

    try:
        resp = requests.get(FWALERT_URL, timeout=15)
        resp.raise_for_status()
        record["status_code"] = resp.status_code
        state["last_alert_time"][symbol] = now
        append_alert_record(record)
        logger.warning(f"[{symbol}] Phone alert triggered! price={price} ({direction})")
    except Exception as e:
        record["error"] = str(e)
        state["last_error"] = str(e)
        append_alert_record(record)
        logger.exception(f"[{symbol}] Failed to trigger phone alert")


def record_price_sample(symbol: str, price: float, timestamp: float | None = None) -> None:
    """记录价格样本，并清理超过波动窗口的旧数据。"""
    now = timestamp or time.time()
    samples = price_history.setdefault(symbol, deque())
    samples.append({"timestamp": now, "price": price})

    cutoff = now - VOLATILITY_WINDOW_SECONDS
    while samples and samples[0]["timestamp"] < cutoff:
        samples.popleft()


def calculate_volatility_stats(symbol: str) -> dict[str, float | int] | None:
    """计算指定币种在窗口内的价格振幅百分比。"""
    samples = price_history.get(symbol)
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
    if window_min <= 0:
        return None

    percent_move = ((window_max - window_min) / window_min) * 100
    return {
        "sample_count": len(prices),
        "window_seconds": VOLATILITY_WINDOW_SECONDS,
        "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
        "window_max": window_max,
        "window_min": window_min,
        "percent_move": percent_move,
    }


def trigger_volatility_alert(symbol: str, price: float, stats: dict[str, float | int]) -> None:
    """当 60 秒窗口振幅达到阈值时触发电话告警。"""
    now = time.time()
    record = {
        "event": "volatility_reached",
        "symbol": symbol,
        "price": price,
        "timestamp": now,
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
        **stats,
    }

    if not FWALERT_URL:
        record["error"] = "missing_fwalert_url"
        state["last_error"] = "missing_fwalert_url"
        append_alert_record(record)
        logger.error("Missing FWALERT_URL")
        return

    last_time = state["last_volatility_alert_time"].get(symbol, 0)
    if now - last_time < COOLDOWN_SECONDS:
        record["suppressed"] = True
        record["reason"] = "cooldown"
        append_alert_record(record)
        logger.info(f"[{symbol}] Volatility alert suppressed due to cooldown")
        return

    try:
        resp = requests.get(FWALERT_URL, timeout=15)
        resp.raise_for_status()
        record["status_code"] = resp.status_code
        state["last_volatility_alert_time"][symbol] = now
        append_alert_record(record)
        logger.warning(f"[{symbol}] Volatility alert triggered! price={price}, stats={stats}")
    except Exception as e:
        record["error"] = str(e)
        state["last_error"] = str(e)
        append_alert_record(record)
        logger.exception(f"[{symbol}] Failed to trigger volatility alert")


def evaluate_volatility_alert(symbol: str, price: float) -> None:
    """评估当前币种是否满足波动告警条件。"""
    record_price_sample(symbol, price)
    stats = calculate_volatility_stats(symbol)
    if stats is None:
        return

    state["volatility_stats"][symbol] = stats
    if symbol in state["coins"]:
        state["coins"][symbol]["volatility"] = stats

    percent_move = stats.get("percent_move")
    if percent_move is None or percent_move < VOLATILITY_THRESHOLD_PERCENT:
        state["volatility_alert_armed"][symbol] = True
        return

    now = time.time()
    last_time = state["last_volatility_alert_time"].get(symbol, 0)
    if last_time and now - last_time < COOLDOWN_SECONDS:
        return

    if not state["volatility_alert_armed"].get(symbol, True):
        return

    logger.warning(
        f"[{symbol}] volatility reached target! percent_move={percent_move}, "
        f"threshold={VOLATILITY_THRESHOLD_PERCENT}"
    )
    trigger_volatility_alert(symbol, price, stats)
    state["volatility_alert_armed"][symbol] = False


def fetch_xyz_xag_price_for_symbol(symbol: str) -> float | None:
    """从 Hyperliquid XYZ 获取指定币种价格，优先使用买卖盘中间价。"""
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {"type": "metaAndAssetCtxs", "dex": "xyz"}
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list) or len(data) < 2:
            logger.error("Hyperliquid 返回结构异常")
            return None

        meta = data[0]
        asset_ctxs = data[1]

        if not isinstance(meta, dict) or not isinstance(asset_ctxs, list):
            logger.error("Hyperliquid 返回字段类型异常")
            return None

        universe = meta.get("universe", [])

        for idx, asset in enumerate(universe):
            if not isinstance(asset, dict):
                continue

            coin = asset.get("name", "")
            normalized = coin.split(":", 1)[1] if ":" in coin else coin
            if normalized.upper() != symbol.upper():
                continue

            if idx >= len(asset_ctxs):
                break

            ctx = asset_ctxs[idx]
            if not isinstance(ctx, dict):
                break

            impact_pxs = ctx.get("impactPxs") or []
            if len(impact_pxs) < 2:
                # 部分资产可能没有 impactPxs，保底读取常见价格字段。
                for field in ("midPx", "markPx", "oraclePx"):
                    if ctx.get(field) is not None:
                        return float(ctx[field])
                break

            # 使用 mid 价格
            bid = float(impact_pxs[0])
            ask = float(impact_pxs[1])
            mid = (bid + ask) / 2
            return mid

        return None
    except Exception as e:
        state["last_error"] = str(e)
        logger.error(f"[{symbol}] Failed to fetch XYZ price: {e}")
        return None


def monitor_loop():
    state["running"] = True
    state["started_at"] = time.time()

    # 初始化币种状态
    for coin in COINS_CONFIG:
        symbol = coin.get("symbol")
        if symbol:
            state["coins"][symbol] = {
                "price": None,
                "targets": coin.get("targets", []),
                "volatility": {
                    "sample_count": 0,
                    "window_seconds": VOLATILITY_WINDOW_SECONDS,
                    "threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
                },
            }
            if symbol not in state["last_alert_time"]:
                state["last_alert_time"][symbol] = 0
            if symbol not in state["last_volatility_alert_time"]:
                state["last_volatility_alert_time"][symbol] = 0
            state["volatility_alert_armed"].setdefault(symbol, True)

    while True:
        try:
            for coin in COINS_CONFIG:
                symbol = coin.get("symbol")
                if not symbol:
                    continue

                price = fetch_xyz_xag_price_for_symbol(symbol)
                if price is None:
                    continue

                # 更新快照
                state["coins"][symbol]["price"] = price
                state["loop_count"] += 1
                evaluate_volatility_alert(symbol, price)

                targets = coin.get("targets", [])
                for target in targets:
                    if isinstance(target, dict):
                        target_price = target.get("price")
                        direction = target.get("direction", "up")
                    else:
                        target_price = target
                        direction = "up"

                    if target_price is None:
                        continue

                    triggered = False
                    if direction == "up" and price >= target_price:
                        triggered = True
                    elif direction == "down" and price <= target_price:
                        triggered = True

                    if triggered:
                        logger.warning(f"[{symbol}] price reached target! price={price}, target={target_price}, direction={direction}")
                        trigger_phone_alert(symbol, price, target_price, direction)

            time.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            state["last_error"] = str(e)
            logger.exception("Monitor loop error")
            time.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event():
    if ENABLE_MONITORING:
        import threading
        threading.Thread(target=monitor_loop, daemon=True).start()
        logger.info("liquidation-alert started (monitoring enabled)")
    else:
        logger.info("liquidation-alert started (monitoring disabled)")


@app.get("/")
def root():
    return {
        "service": "liquidation-alert",
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "fwalert_configured": bool(FWALERT_URL),
        "running": state["running"],
        "coins": state["coins"],
        "last_error": state["last_error"],
        "loop_count": state["loop_count"],
        "cooldown_seconds": COOLDOWN_SECONDS,
        "volatility_window_seconds": VOLATILITY_WINDOW_SECONDS,
        "volatility_threshold_percent": VOLATILITY_THRESHOLD_PERCENT,
        "volatility_stats": state["volatility_stats"],
    }
