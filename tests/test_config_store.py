import json
import tempfile
import unittest
from pathlib import Path

from config_store import (
    ConfigValidationError,
    read_coins_config,
    validate_coins_config,
    write_coins_config,
)


class ConfigStoreTests(unittest.TestCase):
    def test_legacy_config_defaults_to_xyz(self) -> None:
        config = validate_coins_config(
            [{"symbol": "cl", "targets": [{"price": 98, "direction": "up"}]}]
        )

        self.assertEqual(config[0]["dex"], "xyz")
        self.assertEqual(config[0]["symbol"], "CL")
        self.assertTrue(config[0]["enabled"])
        self.assertEqual(config[0]["targets"][0]["price"], 98.0)

    def test_same_symbol_is_allowed_on_different_dexes(self) -> None:
        config = validate_coins_config(
            [
                {"dex": "xyz", "symbol": "NBIS"},
                {"dex": "io", "symbol": "NBIS"},
            ]
        )
        self.assertEqual([coin["dex"] for coin in config], ["xyz", "io"])

    def test_duplicate_market_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "重复"):
            validate_coins_config(
                [
                    {"dex": "xyz", "symbol": "CL"},
                    {"dex": "xyz", "symbol": "cl"},
                ]
            )

    def test_invalid_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "大于 0"):
            validate_coins_config(
                [{"dex": "io", "symbol": "OAI", "targets": [{"price": 0}]}]
            )

    def test_boolean_strings_are_normalised(self) -> None:
        config = validate_coins_config(
            [{"symbol": "CL", "enabled": "false", "volatility_enabled": "true"}]
        )
        self.assertFalse(config[0]["enabled"])
        self.assertTrue(config[0]["volatility_enabled"])

    def test_atomic_file_round_trip(self) -> None:
        config = validate_coins_config([{"dex": "io", "symbol": "OAI"}])
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "coins.json"
            write_coins_config(path, config)
            loaded, source = read_coins_config(path, "[]")

            self.assertEqual(source, "file")
            self.assertEqual(loaded, config)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)


if __name__ == "__main__":
    unittest.main()
