"""Portfolio-wide 24h move bands."""

import unittest

from portfolio_alert.config import ConfigError, parse_config
from portfolio_alert.prices import Quote
from portfolio_alert.rules import INFO, WARN, build_snapshot, evaluate_portfolio


def snapshot_for(positions, bands=(2, 5)):
    """positions: list of (symbol, amount, price, change_24h or None)."""
    raw = {
        "positions": [
            {"symbol": s, "coingecko_id": s.lower(), "amount": a, "avg_cost": 1}
            for s, a, _, _ in positions
        ],
        "portfolio_alerts": {"move_24h_pct": list(bands)},
    }
    config = parse_config(raw)
    quotes = {s.lower(): Quote(s.lower(), p, c) for s, _, p, c in positions}
    return config, build_snapshot(config, quotes)


class ChangeComputationTests(unittest.TestCase):
    def test_single_position_matches_its_own_change(self):
        _, snap = snapshot_for([("A", 10, 11.0, 10.0)])
        change, covered = snap.change_24h()
        self.assertAlmostEqual(change, 10.0)
        self.assertAlmostEqual(covered, 100.0)

    def test_weighted_by_position_size_not_averaged(self):
        # 90% of value moved +10%, 10% moved -10%. A naive mean would say 0%.
        _, snap = snapshot_for([("BIG", 900, 1.0, 10.0), ("SMALL", 100, 1.0, -10.0)])
        change, _ = snap.change_24h()
        self.assertGreater(change, 7.0)
        self.assertLess(change, 9.0)

    def test_flat_portfolio_is_zero(self):
        _, snap = snapshot_for([("A", 10, 1.0, 0.0), ("B", 5, 2.0, 0.0)])
        change, _ = snap.change_24h()
        self.assertAlmostEqual(change, 0.0)

    def test_decline_is_negative(self):
        _, snap = snapshot_for([("A", 10, 0.9, -10.0)])
        change, _ = snap.change_24h()
        self.assertAlmostEqual(change, -10.0)

    def test_position_without_change_is_excluded_and_coverage_reported(self):
        _, snap = snapshot_for([("A", 100, 1.0, 10.0), ("B", 100, 1.0, None)])
        change, covered = snap.change_24h()
        self.assertAlmostEqual(change, 10.0, places=6)
        self.assertAlmostEqual(covered, 50.0)

    def test_missing_change_is_not_treated_as_flat(self):
        # If B counted as 0%, the result would be diluted to about +5%.
        _, snap = snapshot_for([("A", 100, 1.0, 10.0), ("B", 100, 1.0, None)])
        change, _ = snap.change_24h()
        self.assertAlmostEqual(change, 10.0, places=6)

    def test_total_wipeout_is_skipped_not_a_division_by_zero(self):
        _, snap = snapshot_for([("A", 100, 1.0, 5.0), ("DEAD", 100, 1.0, -100.0)])
        change, covered = snap.change_24h()
        self.assertAlmostEqual(change, 5.0)
        self.assertAlmostEqual(covered, 50.0)

    def test_returns_none_when_nothing_is_computable(self):
        _, snap = snapshot_for([("A", 10, 1.0, None)])
        self.assertIsNone(snap.change_24h())

    def test_returns_none_for_an_empty_portfolio(self):
        config = parse_config(
            {
                "positions": [{"symbol": "A", "coingecko_id": "a", "amount": 1, "avg_cost": 1}],
                "portfolio_alerts": {"move_24h_pct": [2]},
            }
        )
        self.assertIsNone(build_snapshot(config, {}).change_24h())


class BandTests(unittest.TestCase):
    def keys(self, change_pct, bands=(2, 5)):
        config, snap = snapshot_for([("A", 100, 1.0, change_pct)], bands)
        return {a.key: a for a in evaluate_portfolio(snap, config, None)}

    def test_quiet_below_every_band(self):
        self.assertEqual(self.keys(1.5), {})

    def test_small_move_fires_only_the_narrow_band(self):
        keys = self.keys(3.0)
        self.assertEqual(set(keys), {"portfolio:move24h:up:2.0"})

    def test_large_move_fires_both_bands(self):
        keys = self.keys(6.0)
        self.assertEqual(
            set(keys), {"portfolio:move24h:up:2.0", "portfolio:move24h:up:5.0"}
        )

    def test_band_fires_exactly_at_the_threshold(self):
        self.assertIn("portfolio:move24h:up:2.0", self.keys(2.0))

    def test_direction_is_part_of_the_key(self):
        self.assertIn("portfolio:move24h:down:2.0", self.keys(-3.0))
        self.assertIn("portfolio:move24h:up:2.0", self.keys(3.0))

    def test_widest_downward_band_is_a_warning(self):
        keys = self.keys(-6.0)
        self.assertEqual(keys["portfolio:move24h:down:5.0"].severity, WARN)
        self.assertEqual(keys["portfolio:move24h:down:2.0"].severity, INFO)

    def test_upward_moves_stay_informational(self):
        for alert in self.keys(6.0).values():
            self.assertEqual(alert.severity, INFO)

    def test_no_bands_configured_means_no_alerts(self):
        config, snap = snapshot_for([("A", 100, 1.0, 30.0)], bands=())
        self.assertEqual(evaluate_portfolio(snap, config, None), [])

    def test_body_notes_partial_coverage(self):
        config, snap = snapshot_for(
            [("A", 100, 1.0, 10.0), ("B", 100, 1.0, None)], bands=(2,)
        )
        alert = evaluate_portfolio(snap, config, None)[0]
        self.assertIn("50% of portfolio value", alert.body)

    def test_body_omits_the_note_at_full_coverage(self):
        config, snap = snapshot_for([("A", 100, 1.0, 10.0)], bands=(2,))
        alert = evaluate_portfolio(snap, config, None)[0]
        self.assertNotIn("portfolio value.", alert.body.split("crossing")[0])
        self.assertNotIn("Based on", alert.body)


class BandConfigTests(unittest.TestCase):
    def parse(self, value):
        return parse_config(
            {
                "positions": [{"symbol": "A", "coingecko_id": "a", "amount": 1, "avg_cost": 1}],
                "portfolio_alerts": {"move_24h_pct": value},
            }
        ).portfolio.move_24h_pct

    def test_list_is_sorted_widest_first(self):
        self.assertEqual(self.parse([2, 5]), (5.0, 2.0))

    def test_single_number_is_accepted(self):
        self.assertEqual(self.parse(5), (5.0,))

    def test_duplicates_collapse(self):
        self.assertEqual(self.parse([5, 2, 5, 2]), (5.0, 2.0))

    def test_absent_means_empty(self):
        self.assertEqual(
            parse_config(
                {"positions": [{"symbol": "A", "coingecko_id": "a", "amount": 1, "avg_cost": 1}]}
            ).portfolio.move_24h_pct,
            (),
        )

    def test_zero_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.parse([0])

    def test_negative_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.parse([-2])

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.parse(["a lot"])


if __name__ == "__main__":
    unittest.main()
