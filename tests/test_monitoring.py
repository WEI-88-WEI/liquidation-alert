import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app


class MonitoringTests(unittest.TestCase):
    @staticmethod
    def catalog_snapshot(dex: str):
        symbols = {
            "xyz": ["CL", "NBIS"],
            "io": ["OAI", "NBIS"],
        }
        return {}, [
            {"symbol": symbol, "name": f"{dex}:{symbol}", "is_delisted": False}
            for symbol in symbols[dex]
        ]

    def setUp(self) -> None:
        self.key = app.market_key("xyz", "CL")
        with app.state_lock:
            app.price_history.pop(self.key, None)
            app.state["coins"][self.key] = {
                "dex": "xyz",
                "symbol": "CL",
                "price": None,
                "targets": [],
                "enabled": True,
                "volatility_enabled": True,
            }
            app.state["last_volatility_alert_time"][self.key] = 0
            app.state["volatility_alert_armed"][self.key] = True

    def tearDown(self) -> None:
        with app.state_lock:
            app.price_history.pop(self.key, None)
            app.state["coins"].pop(self.key, None)
            app.state["last_volatility_alert_time"].pop(self.key, None)
            app.state["volatility_alert_armed"].pop(self.key, None)
            app.state["volatility_stats"].pop(self.key, None)

    def test_dex_snapshot_extracts_prices_and_assets(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {
                "universe": [
                    {"name": "io:OAI"},
                    {"name": "io:OLD", "isDelisted": True},
                ]
            },
            [
                {"impactPxs": ["99", "101"]},
                {"markPx": "4.2"},
            ],
        ]

        with patch.object(app.requests, "post", return_value=response) as request:
            prices, assets = app.request_dex_snapshot("io")

        self.assertEqual(prices, {"OAI": 100.0, "OLD": 4.2})
        self.assertEqual(assets[0]["symbol"], "OAI")
        self.assertTrue(assets[1]["is_delisted"])
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["json"]["dex"], "io")

    def test_invalid_impact_prices_fall_back_to_mark_price(self) -> None:
        self.assertEqual(
            app._price_from_context(
                {"impactPxs": [None, None], "midPx": None, "markPx": "42.5"}
            ),
            42.5,
        )

    def test_config_is_persisted_and_same_symbol_isolated_by_dex(self) -> None:
        original_config = app.get_coins_config_snapshot()
        original_source = app.state["config_source"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "coins.json"
            try:
                with (
                    patch.object(app, "COINS_CONFIG_PATH", config_path),
                    patch.object(
                        app,
                        "request_dex_snapshot",
                        side_effect=self.catalog_snapshot,
                    ),
                ):
                    updated = app.replace_coins_config(
                        {
                            "coins": [
                                {"dex": "xyz", "symbol": "NBIS"},
                                {"dex": "io", "symbol": "NBIS"},
                            ]
                        }
                    )

                self.assertEqual(len(updated), 2)
                self.assertIn("xyz:NBIS", app.state["coins"])
                self.assertIn("io:NBIS", app.state["coins"])
                persisted = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(len(persisted["coins"]), 2)
            finally:
                with app.config_lock:
                    app.coins_config = original_config
                    app.sync_state_with_config(original_config)
                    app.state["config_source"] = original_source
                app.monitor_wakeup_event.clear()

    def test_unknown_enabled_symbol_is_rejected_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "coins.json"
            with (
                patch.object(app, "COINS_CONFIG_PATH", config_path),
                patch.object(
                    app,
                    "request_dex_snapshot",
                    side_effect=self.catalog_snapshot,
                ),
                self.assertRaisesRegex(app.ConfigValidationError, "xyz:NOTREAL"),
            ):
                app.replace_coins_config(
                    {"coins": [{"dex": "xyz", "symbol": "NOTREAL"}]}
                )

            self.assertFalse(config_path.exists())

    def test_monitor_loop_retries_after_unexpected_cycle_error(self) -> None:
        original_loop_count = app.state["loop_count"]
        original_last_error = app.state["last_error"]
        attempts = 0

        def run_cycle() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary cycle failure")
            app.monitor_stop_event.set()

        app.monitor_stop_event.clear()
        try:
            with (
                patch.object(app, "run_monitor_cycle", side_effect=run_cycle),
                patch.object(app.monitor_wakeup_event, "wait", return_value=True),
                self.assertLogs("liquidation-alert", level="ERROR"),
            ):
                app.monitor_loop()
        finally:
            app.monitor_stop_event.clear()
            app.monitor_wakeup_event.clear()
            with app.state_lock:
                app.state["loop_count"] = original_loop_count
                app.state["last_error"] = original_last_error

        self.assertEqual(attempts, 2)
        self.assertFalse(app.state["running"])

    def test_failed_volatility_webhook_does_not_disarm_alert(self) -> None:
        now = time.time()
        app.record_price_sample(self.key, 100, now - 1)

        with (
            patch.object(app, "VOLATILITY_THRESHOLD_PERCENT", 1),
            patch.object(app, "trigger_volatility_alert", return_value=False) as trigger,
        ):
            app.evaluate_volatility_alert("xyz", "CL", 102)

        trigger.assert_called_once()
        self.assertTrue(app.state["volatility_alert_armed"][self.key])

    def test_successful_volatility_webhook_disarms_alert(self) -> None:
        now = time.time()
        app.record_price_sample(self.key, 100, now - 1)

        with (
            patch.object(app, "VOLATILITY_THRESHOLD_PERCENT", 1),
            patch.object(app, "trigger_volatility_alert", return_value=True),
        ):
            app.evaluate_volatility_alert("xyz", "CL", 102)

        self.assertFalse(app.state["volatility_alert_armed"][self.key])


if __name__ == "__main__":
    unittest.main()
