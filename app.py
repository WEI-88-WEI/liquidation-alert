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
TARGET_PRICE = float(os.getenv("TARGET_PRICE", "63.2"))
SYMBOL = "XAG"
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))

# ==============================================

app = FastAPI(title="liquidation-alert")

state = {
    "running": False,
    "last_snapshot": None,
    "last_error": None,
    "last_alert": None,
    "started_at": None,
    "loop_count": 0,
    "last_alert_time": 0,
}


def append_alert_record(record: dict) -> None:
    ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def trigger_phone_alert(price: float, event: str = "price_reached"):
    record = {
        "event": event,
        "symbol": SYMBOL,
        "price": price,
        "target_price": TARGET_PRICE,
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
    if now - state["last_alert_time"] < COOLDOWN_SECONDS:
        record["suppressed"] = True
        record["reason"] = "cooldown"
        append_alert_record(record)
        logger.info("Alert suppressed due to cooldown")
        return

    try:
        resp = requests.get(FWALERT_URL, timeout=15)
        resp.raise_for_status()
        record["status_code"] = resp.status_code
        state["last_alert"] = record
        state["last_alert_time"] = now
        append_alert_record(record)
        logger.warning(f"Phone alert triggered! price={price}")
    except Exception as e:
        record["error"] = str(e)
        state["last_error"] = str(e)
        append_alert_record(record)
        logger.exception("Failed to trigger phone alert")


def fetch_xyz_price() -> float | None:
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {"type": "allMids"}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        for item in data:
            if item.get("coin") == "xyz:XAG":
                return float(item["px"])
        return None
    except Exception as e:
        logger.error(f"Failed to fetch XAG price: {e}")
        return None


def monitor_loop():
    state["running"] = True
    state["started_at"] = time.time()

    while True:
        try:
            price = fetch_xyz_price()
            if price is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            snapshot = {
                "price": price,
                "target": TARGET_PRICE,
                "timestamp": time.time(),
            }
            state["last_snapshot"] = snapshot
            state["loop_count"] += 1

            if price >= TARGET_PRICE:
                logger.warning(f"XAG price reached target! price={price}")
                trigger_phone_alert(price)

            time.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            state["last_error"] = str(e)
            logger.exception("Monitor loop error")
            time.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event():
    import threading
    threading.Thread(target=monitor_loop, daemon=True).start()
    logger.info("liquidation-alert started")


@app.get("/")
def root():
    return {
        "service": "liquidation-alert",
        "symbol": SYMBOL,
        "target_price": TARGET_PRICE,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "fwalert_configured": bool(FWALERT_URL),
        "running": state["running"],
        "last_snapshot": state["last_snapshot"],
        "last_error": state["last_error"],
        "last_alert": state["last_alert"],
        "loop_count": state["loop_count"],
    }
