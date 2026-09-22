import unittest

from portfolio_alert.config import parse_config
from portfolio_alert.prices import Quote
from portfolio_alert.rules import (
    CRITICAL,
    INFO,
    WARN,
    build_snapshot,
    evaluate_all,
    evaluate_market_guards,
    evaluate_portfolio,
    evaluate_position,
    format_report,
)


def config_with(alerts, amount=100.0, avg_cost=1.0, **extra):
    raw = {
        "positions": [
            {
                "symbol": "TEST",
                "coingecko_id": "test-coin",
                "amount": amount,
                "avg_cost": avg_cost,
                "alerts": alerts,
            }
        ]
    }
    raw.update(extra)
    return parse_config(raw)


def snapshot_at(price, change=None, **kwargs):
    config = config_with(**kwargs)
    quotes = {"test-coin": Quote("test-coin", price, change)}
    return config, build_snapshot(config, quotes), quotes


def keys(alerts):
    return {alert.key for alert in alerts}


class PositionRuleTests(unittest.TestCase):
    def test_below_fires_at_and_under_threshold(self):
        for price in (0.5, 0.5 - 1e-9):
            _, snap, _ = snapshot_at(price, alerts={"below": 0.5})
            alerts = evaluate_position(snap.positions[0], "usd")
            self.assertEqual(len(alerts), 1, price)
            self.assertEqual(alerts[0].severity, WARN)

    def test_below_silent_above_threshold(self):
        _, snap, _ = snapshot_at(0.51, alerts={"below": 0.5})
        self.assertEqual(evaluate_position(snap.positions[0], "usd"), [])

    def test_above_fires_and_is_info(self):
        _, snap, _ = snapshot_at(2.0, alerts={"above": 1.5})
        alerts = evaluate_position(snap.positions[0], "usd")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, INFO)

    def test_stop_loss_is_critical(self):
        _, snap, _ = snapshot_at(0.6, alerts={"stop_loss_pct": -35}, avg_cost=1.0)
        alerts = evaluate_position(snap.positions[0], "usd")
        self.assertEqual([a.severity for a in alerts], [CRITICAL])
        self.assertIn("stop-loss", alerts[0].title)

    def test_stop_loss_silent_above_level(self):
        _, snap, _ = snapshot_at(0.7, alerts={"stop_loss_pct": -35}, avg_cost=1.0)
        self.assertEqual(evaluate_position(snap.positions[0], "usd"), [])

    def test_take_profit_fires(self):
        _, snap, _ = snapshot_at(2.6, alerts={"take_profit_pct": 150}, avg_cost=1.0)
        alerts = evaluate_position(snap.positions[0], "usd")
        self.assertEqual(len(alerts), 1)
        self.assertIn("take-profit", alerts[0].title)

    def test_move_24h_fires_on_drop_as_warning(self):
        _, snap, _ = snapshot_at(1.0, change=-18.0, alerts={"move_24h_pct": 15})
        alerts = evaluate_position(snap.positions[0], "usd")
        self.assertEqual([a.severity for a in alerts], [WARN])
        self.assertIn("down", alerts[0].key)

    def test_move_24h_fires_on_rise_as_info(self):
        _, snap, _ = snapshot_at(1.0, change=18.0, alerts={"move_24h_pct": 15})
        alerts = evaluate_position(snap.positions[0], "usd")
        self.assertEqual([a.severity for a in alerts], [INFO])
        self.assertIn("up", alerts[0].key)

    def test_move_24h_silent_when_change_missing(self):
        _, snap, _ = snapshot_at(1.0, change=None, alerts={"move_24h_pct": 1})
        self.assertEqual(evaluate_position(snap.positions[0], "usd"), [])

    def test_no_rules_means_no_alerts(self):
        _, snap, _ = snapshot_at(1.0, alerts={})
        self.assertEqual(evaluate_position(snap.positions[0], "usd"), [])

    def test_multiple_rules_fire_together(self):
        _, snap, _ = snapshot_at(0.4, change=-30.0, alerts={"below": 0.5, "stop_loss_pct": -35,
                                                            "move_24h_pct": 15})
        self.assertEqual(len(evaluate_position(snap.positions[0], "usd")), 3)


class SnapshotTests(unittest.TestCase):
    def test_values_and_pnl(self):
        _, snap, _ = snapshot_at(1.5, alerts={}, amount=100.0, avg_cost=1.0)
        self.assertAlmostEqual(snap.total_value, 150.0)
        self.assertAlmostEqual(snap.total_cost, 100.0)
        self.assertAlmostEqual(snap.total_pnl_abs, 50.0)
        self.assertAlmostEqual(snap.total_pnl_pct, 50.0)
        self.assertAlmostEqual(snap.weight(snap.positions[0]), 100.0)

    def test_missing_quote_is_reported_not_silently_zero(self):
        config = config_with({})
        snap = build_snapshot(config, {})
        self.assertEqual(snap.positions, ())
        self.assertEqual(snap.missing_ids, ("test-coin",))
        self.assertEqual(snap.total_value, 0.0)

    def test_weight_is_none_for_empty_portfolio(self):
        config = config_with({}, amount=0.0)
        snap = build_snapshot(config, {"test-coin": Quote("test-coin", 1.0, None)})
        self.assertIsNone(snap.weight(snap.positions[0]))


class MarketGuardTests(unittest.TestCase):
    def test_guard_below_fires_critical(self):
        config = config_with(
            {}, market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000,
                                "note": "regime."}]
        )
        alerts = evaluate_market_guards(config, {"bitcoin": Quote("bitcoin", 72000.0, None)})
        self.assertEqual([a.severity for a in alerts], [CRITICAL])
        self.assertIn("regime.", alerts[0].body)

    def test_guard_silent_above_level(self):
        config = config_with(
            {}, market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000}]
        )
        self.assertEqual(
            evaluate_market_guards(config, {"bitcoin": Quote("bitcoin", 81000.0, None)}), []
        )

    def test_guard_skipped_when_quote_missing(self):
        config = config_with(
            {}, market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000}]
        )
        self.assertEqual(evaluate_market_guards(config, {}), [])


class PortfolioRuleTests(unittest.TestCase):
    def test_value_below_fires(self):
        config, snap, _ = snapshot_at(
            1.0, alerts={}, amount=100.0, portfolio_alerts={"value_below": 150}
        )
        alerts = evaluate_portfolio(snap, config, None)
        self.assertEqual([a.severity for a in alerts], [CRITICAL])

    def test_drawdown_uses_supplied_peak(self):
        config, snap, _ = snapshot_at(
            0.8, alerts={}, amount=100.0, portfolio_alerts={"drawdown_pct": 15}
        )
        # Value is 80. A peak of 90 is only an 11.1% drawdown, below the 15% rule.
        self.assertEqual(evaluate_portfolio(snap, config, 90.0), [])
        # A peak of 100 is a 20% drawdown, above the rule.
        alerts = evaluate_portfolio(snap, config, 100.0)
        self.assertEqual(len(alerts), 1)
        self.assertIn("drawdown", alerts[0].key)

    def test_drawdown_skipped_without_peak(self):
        config, snap, _ = snapshot_at(
            0.1, alerts={}, portfolio_alerts={"drawdown_pct": 15}
        )
        self.assertEqual(evaluate_portfolio(snap, config, None), [])

    def test_total_pnl_below_fires(self):
        config, snap, _ = snapshot_at(
            0.9, alerts={}, avg_cost=1.0, portfolio_alerts={"total_pnl_below_pct": 0}
        )
        alerts = evaluate_portfolio(snap, config, None)
        self.assertEqual(len(alerts), 1)
        self.assertIn("pnl_below", alerts[0].key)


class EvaluateAllTests(unittest.TestCase):
    def test_sorted_critical_first_and_covers_all_sources(self):
        config = config_with(
            {"below": 0.5, "above": 0.1},
            avg_cost=1.0,
            market_guards=[{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000}],
            portfolio_alerts={"total_pnl_below_pct": 0},
        )
        quotes = {
            "test-coin": Quote("test-coin", 0.4, None),
            "bitcoin": Quote("bitcoin", 70000.0, None),
        }
        snap = build_snapshot(config, quotes)
        alerts = evaluate_all(config, snap, quotes, None)
        self.assertEqual(alerts[0].severity, CRITICAL)
        self.assertEqual([a.severity for a in alerts], sorted(
            [a.severity for a in alerts], key=lambda s: {CRITICAL: 0, WARN: 1, INFO: 2}[s]
        ))
        self.assertTrue(any(k.startswith("guard:") for k in keys(alerts)))
        self.assertTrue(any(k.startswith("portfolio:") for k in keys(alerts)))

    def test_alert_keys_are_unique(self):
        config = config_with({"below": 0.5, "stop_loss_pct": -35, "move_24h_pct": 10})
        quotes = {"test-coin": Quote("test-coin", 0.4, -20.0)}
        snap = build_snapshot(config, quotes)
        alerts = evaluate_all(config, snap, quotes, None)
        self.assertEqual(len(keys(alerts)), len(alerts))


class ReportTests(unittest.TestCase):
    def test_report_contains_symbol_and_total(self):
        config, snap, _ = snapshot_at(1.5, change=3.0, alerts={}, amount=100.0)
        report = format_report(snap, config)
        self.assertIn("TEST", report)
        self.assertIn("TOTAL", report)
        self.assertIn("150.00", report)

    def test_report_flags_missing_prices(self):
        config = config_with({})
        report = format_report(build_snapshot(config, {}), config)
        self.assertIn("WARNING", report)
        self.assertIn("test-coin", report)


if __name__ == "__main__":
    unittest.main()
