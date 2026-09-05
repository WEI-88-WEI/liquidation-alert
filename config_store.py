from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_DEXES = {
    "xyz": "XYZ",
    "io": "EntropyIO",
}

MAX_COINS = 200
MAX_TARGETS_PER_COIN = 50
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._-]{1,64}$")


class ConfigValidationError(ValueError):
    """Raised when a monitoring configuration is structurally invalid."""


def _normalise_boolean(value: Any, location: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ConfigValidationError(f"{location} 必须是 true 或 false")


def _normalise_price(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise ConfigValidationError(f"{location} 必须是大于 0 的数字")

    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{location} 必须是大于 0 的数字") from exc

    if not math.isfinite(price) or price <= 0:
        raise ConfigValidationError(f"{location} 必须是大于 0 的有限数字")
    return price


def validate_coins_config(raw_config: Any) -> list[dict[str, Any]]:
    """Validate and normalise both the legacy list and persisted wrapper format."""
    if isinstance(raw_config, dict) and "coins" in raw_config:
        raw_config = raw_config["coins"]

    if not isinstance(raw_config, list):
        raise ConfigValidationError("配置必须是币种数组")
    if len(raw_config) > MAX_COINS:
        raise ConfigValidationError(f"最多只能配置 {MAX_COINS} 个监控项")

    normalised: list[dict[str, Any]] = []
    seen_markets: set[str] = set()

    for coin_index, raw_coin in enumerate(raw_config):
        location = f"第 {coin_index + 1} 个监控项"
        if not isinstance(raw_coin, dict):
            raise ConfigValidationError(f"{location} 必须是对象")

        dex = str(raw_coin.get("dex", "xyz")).strip().lower()
        if dex not in SUPPORTED_DEXES:
            supported = "、".join(SUPPORTED_DEXES)
            raise ConfigValidationError(f"{location} 的 dex 只能是 {supported}")

        raw_symbol = raw_coin.get("symbol", "")
        if not isinstance(raw_symbol, str):
            raise ConfigValidationError(f"{location} 的 symbol 必须是字符串")
        symbol = raw_symbol.strip()
        if ":" in symbol:
            prefix, symbol = symbol.split(":", 1)
            if prefix.lower() != dex:
                raise ConfigValidationError(f"{location} 的 dex 与 symbol 前缀不一致")
        symbol = symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ConfigValidationError(
                f"{location} 的 symbol 只能包含字母、数字、点、下划线或连字符"
            )

        market_id = f"{dex}:{symbol}"
        if market_id in seen_markets:
            raise ConfigValidationError(f"监控项 {market_id} 重复")
        seen_markets.add(market_id)

        raw_targets = raw_coin.get("targets", [])
        if not isinstance(raw_targets, list):
            raise ConfigValidationError(f"{location} 的 targets 必须是数组")
        if len(raw_targets) > MAX_TARGETS_PER_COIN:
            raise ConfigValidationError(
                f"{location} 最多只能配置 {MAX_TARGETS_PER_COIN} 个目标价"
            )

        targets: list[dict[str, Any]] = []
        for target_index, raw_target in enumerate(raw_targets):
            target_location = f"{location}的第 {target_index + 1} 个目标价"
            if isinstance(raw_target, dict):
                price_value = raw_target.get("price")
                direction = str(raw_target.get("direction", "up")).strip().lower()
            else:
                # Backwards compatibility with the original numeric target format.
                price_value = raw_target
                direction = "up"

            if direction not in {"up", "down"}:
                raise ConfigValidationError(f"{target_location}的方向只能是 up 或 down")

            targets.append(
                {
                    "price": _normalise_price(price_value, f"{target_location}的价格"),
                    "direction": direction,
                }
            )

        normalised.append(
            {
                "dex": dex,
                "symbol": symbol,
                "enabled": _normalise_boolean(
                    raw_coin.get("enabled", True),
                    f"{location}的 enabled",
                ),
                "volatility_enabled": _normalise_boolean(
                    raw_coin.get("volatility_enabled", True),
                    f"{location}的 volatility_enabled",
                ),
                "targets": targets,
            }
        )

    return normalised


def read_coins_config(config_path: Path, env_value: str) -> tuple[list[dict[str, Any]], str]:
    """Read persisted config first, falling back to the legacy COINS_CONFIG env value."""
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return validate_coins_config(payload), "file"

    payload = json.loads(env_value or "[]")
    return validate_coins_config(payload), "environment"


def write_coins_config(config_path: Path, coins: list[dict[str, Any]]) -> None:
    """Atomically persist a validated configuration in a forward-compatible wrapper."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "coins": coins,
    }

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_name, config_path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
