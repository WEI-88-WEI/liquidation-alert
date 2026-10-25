import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests

import wallet_monitor as wm
from config_store import ConfigValidationError


ADDRESS = "0x" + "a" * 40
OTHER_ADDRESS = "0x" + "b" * 40


def fill(trade_id, timestamp, *, start="0", side="B", size="1", coin="BTC"):
    return {
        "coin": coin,
        "px": "60000",
        "sz": size,
        "side": side,
        "time": timestamp,
        "startPosition": start,
        "dir": "Open Long",
        "closedPnl": "12.5" if start != "0" else "0",
        "hash": "0x" + str(trade_id).zfill(64),
        "oid": trade_id,
        "tid": trade_id,
        "crossed": True,
        "fee": "1.2",
        "feeToken": "USDC",
    }


def raw_position(coin, size):
    return {
        "position": {
            "coin": coin,
            "szi": size,
            "entryPx": "100",
            "positionValue": "200",
            "unrealizedPnl": "5",
            "liquidationPx": "70",
            "leverage": {"type": "cross", "value": 3},
        },
        "type": "oneWay",
    }


class WalletMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.clock = 1_800_000_000.0
        self.time_patch = patch.object(wm.time, "time", side_effect=lambda: self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.monitor = self.new_monitor()
        self.monitor.replace_config(self.config())
        self.fills = []
        self.positions = []
        self.fill_patch = patch.object(
            self.monitor,
            "_request_fills",
            side_effect=lambda address, start_ms, end_ms: copy.deepcopy(self.fills),
        )
        self.position_patch = patch.object(
            self.monitor,
            "_request_positions",
            side_effect=lambda address: copy.deepcopy(self.positions),
        )
        self.fetch_fills = self.fill_patch.start()
        self.fetch_positions = self.position_patch.start()
        self.addCleanup(self.fill_patch.stop)
        self.addCleanup(self.position_patch.stop)

    def new_monitor(self):
        return wm.WalletMonitor(self.base, "https://example.invalid/info")

    @staticmethod
    def config(wallets=None, interval=15):
        return {
            "poll_interval_seconds": interval,
            "wallets": wallets if wallets is not None else [
                {"address": ADDRESS, "label": "主账户", "enabled": True}
            ],
        }

    def tick(self, seconds=15):
        self.clock += seconds
        return int(self.clock * 1000)

    def events(self):
        return self.monitor.get_events()

    def test_config_is_normalized_persisted_and_independent_of_coin_config(self):
        coins = self.base / "coins_config.json"
        coins.write_text('{"coins": []}', encoding="utf-8")
        result = self.monitor.replace_config(self.config([
            {"address": "0x" + "A" * 40, "label": "  鲸鱼  ", "enabled": False}
        ], interval=30))
        self.assertEqual(result["wallets"][0]["address"], ADDRESS)
        self.assertEqual(result["poll_interval_seconds"], 30)
        reloaded = self.new_monitor().get_config()
        self.assertEqual(reloaded["wallets"], result["wallets"])
        self.assertEqual(reloaded["poll_interval_seconds"], 30)
        self.assertEqual(json.loads(coins.read_text(encoding="utf-8")), {"coins": []})

    def test_invalid_config_does_not_replace_saved_config(self):
        original = self.monitor.get_config()
        cases = [
            self.config(interval=0),
            self.config(interval=True),
            self.config(interval=301),
            self.config([{"address": "0x123"}]),
            self.config([{"address": ADDRESS}, {"address": ADDRESS.upper()}]),
            self.config([{"address": ADDRESS, "enabled": "yes"}]),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigValidationError):
                    self.monitor.replace_config(payload)
                self.assertEqual(self.monitor.get_config()["wallets"], original["wallets"])
                self.assertEqual(self.monitor.get_config()["poll_interval_seconds"], 15)

    def test_conditional_save_returns_new_version_and_preserves_it_after_restart(self):
        original_version = self.monitor.get_config()["config"]["version"]
        result = self.monitor.replace_config({
            **self.config(interval=30), "expected_version": original_version,
        })
        self.assertNotEqual(result["config"]["version"], original_version)
        self.assertEqual(self.monitor.snapshot()["config"]["version"], result["config"]["version"])
        self.assertEqual(self.new_monitor().get_config()["config"]["version"], result["config"]["version"])

    def test_stale_conditional_save_does_not_overwrite_config_or_checkpoint(self):
        original_version = self.monitor.get_config()["config"]["version"]
        latest = self.monitor.replace_config(self.config([
            {"address": ADDRESS, "label": "主账户", "enabled": True},
            {"address": OTHER_ADDRESS, "label": "新增账户", "enabled": True},
        ]))
        saved_file = self.monitor.config_path.read_bytes()
        checkpoint = copy.deepcopy(self.monitor._checkpoints)
        revision = latest["config"]["revision"]
        with self.assertRaises(wm.ConfigConflictError):
            self.monitor.replace_config({
                **self.config(interval=30), "expected_version": original_version,
            })
        self.assertEqual(self.monitor.get_config(), latest)
        self.assertEqual(self.monitor.config_path.read_bytes(), saved_file)
        self.assertEqual(self.monitor._checkpoints, checkpoint)
        self.assertEqual(self.monitor.snapshot()["config"]["revision"], revision)

    def test_config_version_tracks_normalized_public_content_only(self):
        original = self.monitor.get_config()
        same_content = self.monitor.replace_config({
            "poll_interval_seconds": 15,
            "wallets": [{"address": "0x" + "A" * 40, "label": " 主账户 "}],
            "expected_version": original["config"]["version"],
        })
        self.assertEqual(same_content["config"]["version"], original["config"]["version"])
        self.assertGreater(same_content["config"]["revision"], original["config"]["revision"])

    def test_invalid_expected_version_does_not_change_configuration(self):
        original = self.monitor.get_config()
        for value in (None, True, 1, [], {}):
            with self.subTest(expected_version=value):
                with self.assertRaises(ConfigValidationError):
                    self.monitor.replace_config({**self.config(interval=30), "expected_version": value})
                self.assertEqual(self.monitor.get_config(), original)

    def test_first_poll_ignores_history_but_keeps_trades_since_configuration(self):
        start_ms = int(self.clock * 1000)
        self.fills = [fill(1, start_ms - 1000), fill(2, start_ms + 1000)]
        self.tick()
        self.monitor.run_cycle()
        self.assertEqual([event["trade_id"] for event in self.events()], [2])
        self.assertEqual(self.events()[0]["label"], "主账户")

    def test_roundtrip_between_polls_generates_both_open_and_close(self):
        self.monitor.run_cycle()
        timestamp = self.tick()
        self.fills = [
            fill(1, timestamp - 2000),
            fill(2, timestamp - 1000, start="1", side="A"),
        ]
        self.monitor.run_cycle()
        self.assertEqual([event["action"] for event in self.events()], ["close", "open"])
        self.assertTrue(all(event["coin"] == "BTC" for event in self.events()))
        self.assertEqual(self.events()[0]["side"], "long")
        self.assertEqual(Decimal(str(self.events()[0]["closed_pnl"])), Decimal("12.5"))

    def test_increase_reduce_and_reverse_derive_from_signed_position(self):
        timestamp = self.tick()
        self.fills = [
            fill(1, timestamp - 3000, start="1", side="B", size="1"),
            fill(2, timestamp - 2000, start="2", side="A", size="0.5"),
            fill(3, timestamp - 1000, start="1.5", side="A", size="2"),
        ]
        self.monitor.run_cycle()
        events = list(reversed(self.events()))
        self.assertEqual([event["action"] for event in events], ["increase", "reduce", "reverse"])
        self.assertEqual([event["side"] for event in events], ["long", "long", "short"])

    def test_two_trades_at_same_millisecond_are_not_collapsed_or_repeated(self):
        timestamp = self.tick() - 1
        self.fills = [fill(1, timestamp), fill(2, timestamp, coin="xyz:CL")]
        self.monitor.run_cycle()
        self.tick()
        self.monitor.run_cycle()
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual({event["trade_id"] for event in events}, {1, 2})
        self.assertEqual({event["coin"] for event in events}, {"BTC", "xyz:CL"})
        self.assertEqual(next(event for event in events if event["coin"] == "xyz:CL")["dex"], "xyz")

    def test_restart_preserves_events_and_deduplicates_overlap(self):
        timestamp = self.tick() - 1000
        self.fills = [fill(1, timestamp)]
        self.monitor.run_cycle()
        resumed = self.new_monitor()
        self.tick()
        with (
            patch.object(resumed, "_request_fills", return_value=self.fills),
            patch.object(resumed, "_request_positions", return_value=[]),
        ):
            resumed.run_cycle()
        self.assertEqual(len(resumed.get_events()), 1)
        self.assertEqual(resumed.get_events()[0]["trade_id"], 1)

    def test_first_network_failure_does_not_move_monitoring_start(self):
        start_ms = int(self.clock * 1000)
        self.fetch_fills.side_effect = requests.ConnectionError("offline")
        self.tick()
        self.monitor.run_cycle()
        self.fetch_fills.side_effect = None
        self.fetch_fills.return_value = [fill(1, start_ms + 1000)]
        self.tick()
        self.monitor.run_cycle()
        self.assertEqual([event["trade_id"] for event in self.events()], [1])

    def test_restart_after_first_network_failure_keeps_original_start(self):
        start_ms = int(self.clock * 1000)
        self.fetch_fills.side_effect = requests.ConnectionError("offline")
        self.tick()
        self.monitor.run_cycle()
        self.tick()
        resumed = self.new_monitor()
        with (
            patch.object(resumed, "_request_fills", return_value=[fill(1, start_ms + 1000)]),
            patch.object(resumed, "_request_positions", return_value=[]),
        ):
            resumed.run_cycle()
        self.assertEqual([event["trade_id"] for event in resumed.get_events()], [1])

    def test_failed_state_write_does_not_lose_fill_on_next_poll(self):
        timestamp = self.tick() - 1000
        self.fills = [fill(1, timestamp)]
        with patch.object(wm, "_atomic_write", side_effect=OSError("disk full")):
            self.monitor.run_cycle()
        self.tick()
        self.monitor.run_cycle()
        resumed = self.new_monitor()
        self.assertEqual([event["trade_id"] for event in self.events()], [1])
        self.assertEqual([event["trade_id"] for event in resumed.get_events()], [1])

    def test_failed_wallet_does_not_prevent_other_wallet_notifications(self):
        self.monitor.replace_config(self.config([
            {"address": ADDRESS}, {"address": OTHER_ADDRESS}
        ]))
        timestamp = self.tick() - 1000

        def request_fills(address, start_ms, end_ms):
            if address == ADDRESS:
                raise requests.ConnectionError("offline")
            return [fill(1, timestamp)]

        self.fetch_fills.side_effect = request_fills
        self.monitor.run_cycle()
        self.assertEqual([event["address"] for event in self.events()], [OTHER_ADDRESS])

    def test_removal_during_fetch_does_not_restore_wallet_or_create_alert(self):
        timestamp = self.tick() - 1000

        def request_fills(address, start_ms, end_ms):
            self.monitor.replace_config(self.config([]))
            return [fill(1, timestamp)]

        self.fetch_fills.side_effect = request_fills
        self.monitor.run_cycle()
        self.assertEqual(self.events(), [])
        self.assertNotIn(ADDRESS, self.monitor.snapshot()["wallets"])

    def test_reenabling_does_not_replay_activity_while_paused(self):
        self.monitor.run_cycle()
        self.monitor.replace_config(self.config([{"address": ADDRESS, "enabled": False}]))
        paused_trade_time = self.tick() - 1000
        self.tick()
        self.monitor.replace_config(self.config())
        active_trade_time = self.tick() - 1000
        self.fills = [fill(1, paused_trade_time), fill(2, active_trade_time)]
        self.monitor.run_cycle()
        self.assertEqual([event["trade_id"] for event in self.events()], [2])

    def test_restart_immediately_after_reenabling_ignores_paused_activity(self):
        self.monitor.run_cycle()
        self.monitor.replace_config(self.config([{"address": ADDRESS, "enabled": False}]))
        paused_trade_time = self.tick() - 1000
        self.tick()
        self.monitor.replace_config(self.config())
        active_trade_time = self.tick() - 1000
        resumed = self.new_monitor()
        with (
            patch.object(resumed, "_request_fills", return_value=[
                fill(1, paused_trade_time), fill(2, active_trade_time)
            ]),
            patch.object(resumed, "_request_positions", return_value=[]),
        ):
            resumed.run_cycle()
        self.assertEqual([event["trade_id"] for event in resumed.get_events()], [2])

    def test_pagination_keeps_same_millisecond_boundary_trades(self):
        start_ms = int(self.clock * 1000)
        first = [fill(index, start_ms + index) for index in range(1, 2001)]
        boundary_time = start_ms + 2000
        second = [first[-1], fill(2001, boundary_time), fill(2002, boundary_time + 1)]
        with patch.object(self.monitor, "_request", side_effect=[first, second]) as request:
            fills = type(self.monitor)._request_fills(self.monitor, ADDRESS, start_ms, start_ms + 5000)
        self.assertEqual({record["tid"] for record in fills}, set(range(1, 2003)))
        self.assertEqual(request.call_args_list[1].args[0]["startTime"], boundary_time)

    def test_spot_trades_do_not_generate_perpetual_open_position_alerts(self):
        timestamp = self.tick() - 1000
        self.fills = [fill(1, timestamp, coin="@107"), fill(2, timestamp, coin="PURR/USDC"), fill(3, timestamp)]
        self.monitor.run_cycle()
        self.assertEqual([event["trade_id"] for event in self.events()], [3])

    def test_positions_cover_native_and_discovered_dexes_and_exclude_zero_size(self):
        requests_seen = []

        def request(payload):
            requests_seen.append(payload)
            if payload["type"] == "perpDexs":
                return [None, {"name": "xyz"}, {"name": "io"}]
            self.assertEqual(payload["type"], "clearinghouseState")
            return {
                "assetPositions": {
                    "": [raw_position("BTC", "2"), raw_position("ETH", "0")],
                    "xyz": [raw_position("xyz:CL", "-3")],
                    "io": [raw_position("io:OAI", "4")],
                }[payload.get("dex", "")]
            }

        with patch.object(self.monitor, "_request", side_effect=request):
            positions = type(self.monitor)._request_positions(self.monitor, ADDRESS)
        self.assertEqual({position["coin"] for position in positions}, {"BTC", "xyz:CL", "io:OAI"})
        self.assertEqual(next(position for position in positions if position["coin"] == "xyz:CL")["side"], "short")
        self.assertEqual({payload.get("dex", "") for payload in requests_seen if payload["type"] == "clearinghouseState"}, {"", "xyz", "io"})

    def test_position_failure_preserves_cache_and_still_reports_new_fills(self):
        self.positions = [{"coin": "BTC", "side": "long", "size": "1"}]
        self.monitor.run_cycle()
        original = self.monitor.snapshot()["wallets"][ADDRESS]["positions"]
        self.fetch_positions.side_effect = requests.ConnectionError("HIP-3 offline")
        timestamp = self.tick() - 1000
        self.fills = [fill(1, timestamp, coin="xyz:CL")]
        self.monitor.run_cycle()
        wallet = self.monitor.snapshot()["wallets"][ADDRESS]
        self.assertEqual(wallet["positions"], original)
        self.assertTrue(wallet["positions_error"])
        self.assertEqual([event["coin"] for event in self.events()], ["xyz:CL"])

    def test_get_positions_rejects_unconfigured_address(self):
        with self.assertRaises(ConfigValidationError):
            self.monitor.get_positions(OTHER_ADDRESS)


if __name__ == "__main__":
    unittest.main()
