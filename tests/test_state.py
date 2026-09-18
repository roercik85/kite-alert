import json
import tempfile
import unittest
from pathlib import Path

from portfolio_alert.rules import Alert
from portfolio_alert.state import AlertState

A = Alert(key="AKT:below:0.45", severity="warn", title="t", body="b")
B = Alert(key="guard:BTC:below:73000", severity="critical", title="t", body="b")


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_occurrence_is_sent(self):
        state = AlertState(self.path)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=0), [A])

    def test_repeat_is_suppressed_within_cooldown(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=3600, now=0)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=60), [])

    def test_resends_after_cooldown_lapses(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=3600, now=0)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=3600), [A])

    def test_rearms_after_condition_clears(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=3600, now=0)
        state.select_new([], cooldown_seconds=3600, now=10)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=20), [A])

    def test_independent_keys_do_not_mask_each_other(self):
        state = AlertState(self.path)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=0), [A])
        self.assertEqual(state.select_new([A, B], cooldown_seconds=3600, now=10), [B])

    def test_state_survives_reload(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=3600, now=0)
        state.save()
        self.assertEqual(AlertState(self.path).select_new([A], cooldown_seconds=3600, now=60), [])

    def test_corrupt_state_file_does_not_crash(self):
        self.path.write_text("}{ not json", encoding="utf-8")
        state = AlertState(self.path)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=0), [A])

    def test_save_writes_readable_json(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=3600, now=0)
        state.observe_value(1000.0)
        state.save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn(A.key, raw["active"])
        self.assertEqual(raw["peak_value"], 1000.0)

    def test_save_leaves_no_temp_files_behind(self):
        state = AlertState(self.path)
        state.observe_value(10.0)
        state.save()
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class PeakTrackingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_peak_starts_empty_and_rises(self):
        state = AlertState(self.path)
        self.assertIsNone(state.peak_value)
        state.observe_value(100.0)
        state.observe_value(150.0)
        self.assertEqual(state.peak_value, 150.0)

    def test_peak_does_not_fall(self):
        state = AlertState(self.path)
        state.observe_value(150.0)
        state.observe_value(90.0)
        self.assertEqual(state.peak_value, 150.0)

    def test_non_positive_value_is_ignored(self):
        state = AlertState(self.path)
        state.observe_value(0.0)
        self.assertIsNone(state.peak_value)

    def test_reset_peak(self):
        state = AlertState(self.path)
        state.observe_value(150.0)
        state.reset_peak(80.0)
        self.assertEqual(state.peak_value, 80.0)

    def test_peak_persists_across_reload(self):
        state = AlertState(self.path)
        state.observe_value(1234.0)
        state.save()
        self.assertEqual(AlertState(self.path).peak_value, 1234.0)


if __name__ == "__main__":
    unittest.main()


class RollbackTests(unittest.TestCase):
    """A failed delivery must not count as sent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_rollback_lets_a_first_alert_retry(self):
        state = AlertState(self.path)
        sent = state.select_new([A], cooldown_seconds=3600, now=0)
        self.assertEqual(sent, [A])
        state.rollback(sent)
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=1), [A])

    def test_rollback_restores_an_earlier_fired_timestamp(self):
        state = AlertState(self.path)
        state.select_new([A], cooldown_seconds=100, now=0)
        # Cooldown lapses, so it sends again - then delivery fails.
        resent = state.select_new([A], cooldown_seconds=100, now=200)
        self.assertEqual(resent, [A])
        state.rollback(resent)
        # The original timestamp is back, so the cooldown is measured from it.
        self.assertEqual(state.select_new([A], cooldown_seconds=100, now=250), [A])
        self.assertEqual(state.select_new([A], cooldown_seconds=100, now=260), [])

    def test_rollback_of_one_alert_leaves_the_other_marked(self):
        state = AlertState(self.path)
        state.select_new([A, B], cooldown_seconds=3600, now=0)
        state.rollback([A])
        self.assertEqual(state.select_new([A, B], cooldown_seconds=3600, now=1), [A])

    def test_rollback_survives_save_and_reload(self):
        state = AlertState(self.path)
        sent = state.select_new([A], cooldown_seconds=3600, now=0)
        state.rollback(sent)
        state.save()
        self.assertEqual(AlertState(self.path).select_new([A], cooldown_seconds=3600, now=1), [A])

    def test_rollback_is_safe_for_unknown_alerts(self):
        state = AlertState(self.path)
        state.rollback([A])
        self.assertEqual(state.select_new([A], cooldown_seconds=3600, now=0), [A])
