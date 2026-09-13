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

    def test_read_recent_alerts_returns_newest_first_and_skips_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "alerts_log.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"event": "volatility_reached", "market": "io:OAI", "timestamp": 100.0}),
                        "not-json",
                        json.dumps({"event": "price_reached", "market": "xyz:CL", "timestamp": 200.0}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(app, "ALERTS_LOG_PATH", log_path):
                records = app.read_recent_alerts(10)
                self.assertEqual([record["timestamp"] for record in records], [200.0, 100.0])
                self.assertEqual(len(app.read_recent_alerts(1)), 1)
                self.assertEqual(app.read_recent_alerts(1)[0]["event"], "price_reached")

            with patch.object(app, "ALERTS_LOG_PATH", Path(tmp_dir) / "missing.jsonl"):
                self.assertEqual(app.read_recent_alerts(5), [])

    def test_alert_page_escapes_record_fields_and_renders_both_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "alerts_log.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "price_reached",
                                "market": "xyz:CL",
                                "price": 98.5,
                                "target_price": 98,
                                "direction": "up",
                                "timestamp": 1_789_200_000.0,
                                "beijing_time": "2026-09-12T18:00:00.000000+08:00",
                                "error": "ConnectionError",
                            }
                        ),
                        json.dumps(
                            {
                                "event": "volatility_reached",
                                "market": "<img src=x onerror=alert(1)>",
                                "price": 1510.1709999999998,
                                "window_seconds": 60,
                                "threshold_percent": 1.0,
                                "window_min": 1510.171,
                                "window_max": 1531.807,
                                "percent_move": 1.4326854376093963,
                                "timestamp": time.time(),
                                "beijing_time": "2026-09-13T15:39:17.305630+08:00",
                                "status_code": 200,
                            }
                        ),
                        # 旧记录：没有 market/dex 字段，且是冷却期被抑制
                        json.dumps(
                            {
                                "event": "price_reached",
                                "symbol": "BRENTOIL",
                                "price": 83.2999,
                                "target_price": 84,
                                "direction": "down",
                                "timestamp": 1_785_229_005.0,
                                "beijing_time": "2026-07-28T16:56:45.155060+08:00",
                                "suppressed": True,
                                "reason": "cooldown",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(app, "ALERTS_LOG_PATH", log_path):
                page = app.render_alert_html(100)

            # 记录里的字符串必须转义，页面不允许出现可执行的原始标签
            self.assertNotIn("<img src=x", page)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
            # 两种事件都渲染，数字裁掉浮点尾巴，结果区分成功/失败
            self.assertIn("价格到达", page)
            self.assertIn("目标价 98（向上到达）", page)
            self.assertIn("失败（ConnectionError）", page)
            self.assertIn("波动告警", page)
            self.assertIn("1510.171", page)
            self.assertIn("已拨出（200）", page)
            self.assertIn("近 24 小时 1 条", page)
            # 旧记录没有 market 字段 → 回退显示 symbol；冷却期抑制单独标出
            self.assertIn("<b>BRENTOIL</b>", page)
            self.assertIn("冷却期抑制（cooldown）", page)
            self.assertNotIn("<b>-</b>", page)

            with patch.object(app, "ALERTS_LOG_PATH", Path(tmp_dir) / "missing.jsonl"):
                empty_page = app.render_alert_html(50)
            self.assertIn("暂无电话告警记录", empty_page)
            self.assertIn("共 0 条", empty_page)


if __name__ == "__main__":
    unittest.main()
