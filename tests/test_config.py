import json
import tempfile
import unittest
from pathlib import Path

from portfolio_alert.config import ConfigError, load_config, parse_config


def minimal(**overrides):
    raw = {
        "positions": [
            {"symbol": "akt", "coingecko_id": "akash-network", "amount": 10, "avg_cost": 0.5}
        ]
    }
    raw.update(overrides)
    return raw


class ParseConfigTests(unittest.TestCase):
    def test_parses_minimal_config(self):
        config = parse_config(minimal())
        self.assertEqual(len(config.positions), 1)
        self.assertEqual(config.positions[0].symbol, "AKT")
        self.assertEqual(config.vs_currency, "usd")
        self.assertEqual(config.cooldown_minutes, 720)
        self.assertEqual(config.notifiers, ({"type": "console"},))

    def test_symbol_is_upper_cased(self):
        config = parse_config(minimal())
        self.assertEqual(config.positions[0].symbol, "AKT")

    def test_cost_basis_and_pnl(self):
        position = parse_config(minimal()).positions[0]
        self.assertAlmostEqual(position.cost_basis, 5.0)
        self.assertAlmostEqual(position.value(1.0), 10.0)
        self.assertAlmostEqual(position.pnl_pct(1.0), 100.0)

    def test_pnl_is_none_without_cost_basis(self):
        raw = minimal()
        raw["positions"][0]["avg_cost"] = 0
        position = parse_config(raw).positions[0]
        self.assertIsNone(position.pnl_pct(1.0))

    def test_coingecko_ids_deduplicates_and_keeps_order(self):
        raw = minimal(
            market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 1}],
        )
        raw["positions"].append(
            {"symbol": "BTC2", "coingecko_id": "bitcoin", "amount": 1, "avg_cost": 1}
        )
        config = parse_config(raw)
        self.assertEqual(config.coingecko_ids, ["akash-network", "bitcoin"])

    def test_rejects_missing_positions(self):
        with self.assertRaises(ConfigError):
            parse_config({})

    def test_rejects_empty_positions(self):
        with self.assertRaises(ConfigError):
            parse_config({"positions": []})

    def test_rejects_missing_coingecko_id(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config({"positions": [{"symbol": "AKT", "amount": 1}]})
        self.assertIn("coingecko_id", str(ctx.exception))

    def test_rejects_negative_amount(self):
        raw = minimal()
        raw["positions"][0]["amount"] = -1
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_rejects_non_numeric_amount(self):
        raw = minimal()
        raw["positions"][0]["amount"] = "many"
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_rejects_duplicate_symbols(self):
        raw = minimal()
        raw["positions"].append(
            {"symbol": "AKT", "coingecko_id": "akash-network", "amount": 1, "avg_cost": 1}
        )
        with self.assertRaises(ConfigError) as ctx:
            parse_config(raw)
        self.assertIn("duplicate", str(ctx.exception))

    def test_rejects_guard_without_threshold(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin"}]))

    def test_rejects_notifier_without_type(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(notifiers=[{"topic": "x"}]))

    def test_rejects_unknown_plan(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(coingecko_plan="enterprise"))

    def test_rejects_negative_cooldown(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(cooldown_minutes=-1))


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_raises_config_error(self):
        with self.assertRaises(ConfigError):
            load_config("definitely-not-here.json")

    def test_invalid_json_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_loads_shipped_example(self):
        example = Path(__file__).resolve().parents[1] / "portfolio.example.json"
        config = load_config(example)
        self.assertEqual(len(config.positions), 7)
        self.assertEqual(len(config.market_guards), 2)
        symbols = {p.symbol for p in config.positions}
        self.assertEqual(symbols, {"AKT", "LINK", "ONDO", "TAO", "ZEC", "GRASS", "GRT"})

    def test_example_json_round_trips(self):
        example = Path(__file__).resolve().parents[1] / "portfolio.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(parse_config(raw).positions[0].symbol, "AKT")


if __name__ == "__main__":
    unittest.main()
