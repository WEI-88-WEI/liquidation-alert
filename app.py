from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("liquidation-alert")

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ALERTS_LOG_PATH = Path(__file__).with_name("alerts_log.jsonl")


def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

# ==================== 配置 ====================
FWALERT_URL = os.getenv("LIQUIDATION_FWALERT_URL", os.getenv("FWALERT_URL", ""))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))

# 新增：监控开关
ENABLE_MONITORING = os.getenv("ENABLE_MONITORING", "true").lower() == "true"

# 新增多币种配置
import json
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
    "last_error": None,
}


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


def fetch_xyz_xag_price() -> float | None:
    """使用 price-alerts 的方式获取 XYZ XAG 价格"""
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {"type": "metaAndAssetCtxs", "dex": "xyz"}
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if len(data) < 2:
            return None

        meta = data[0]
        asset_ctxs = data[1]

        universe = meta.get("universe", [])

        for idx, asset in enumerate(universe):
            coin = asset.get("name", "")
            normalized = coin.split(":", 1)[1] if ":" in coin else coin
            if normalized.upper() != SYMBOL.upper():
                continue

            if idx >= len(asset_ctxs):
                break

            ctx = asset_ctxs[idx]
            impact_pxs = ctx.get("impactPxs") or []
            if len(impact_pxs) < 2:
                break

            # 使用 mid 价格
            bid = float(impact_pxs[0])
            ask = float(impact_pxs[1])
            mid = (bid + ask) / 2
            return mid

        return None
    except Exception as e:
        logger.error(f"Failed to fetch XAG price: {e}")
        return None


def monitor_loop():
    state["running"] = True
    state["started_at"] = time.time()

    # 初始化币种状态
    for coin in COINS_CONFIG:
        symbol = coin.get("symbol")
        if symbol:
            state["coins"][symbol] = {"price": None, "targets": coin.get("targets", [])}
            if symbol not in state["last_alert_time"]:
                state["last_alert_time"][symbol] = 0

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
    }
