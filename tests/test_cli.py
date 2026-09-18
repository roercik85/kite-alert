import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from portfolio_alert import cli
from portfolio_alert.prices import PriceFetchError, Quote

CONFIG = {
    "positions": [
        {
            "symbol": "AKT",
            "coingecko_id": "akash-network",
            "amount": 100,
            "avg_cost": 1.0,
            "alerts": {"below": 0.6},
        }
    ],
    "market_guards": [{"symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000}],
    "portfolio_alerts": {"drawdown_pct": 15},
    "notifiers": [{"type": "console"}],
}


def quotes(akt=0.55, btc=81000.0):
    return {
        "akash-network": Quote("akash-network", akt, -3.0),
        "bitcoin": Quote("bitcoin", btc, 6.0),
    }


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.config_path = self.dir / "portfolio.json"
        self.state_path = self.dir / "state.json"
        self.config_path.write_text(json.dumps(CONFIG), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *extra, fetch=None):
        argv = ["--config", str(self.config_path), "--state", str(self.state_path), *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "fetch_quotes", fetch or (lambda *a, **k: quotes())):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class CheckConfigTests(CliTestCase):
    def test_valid_config_reports_ok(self):
        code, out, _ = self.run_cli("--check-config")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("config OK", out)
        self.assertIn("1 positions", out)

    def test_missing_config_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--config", str(self.dir / "nope.json"), "--check-config"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("not found", err.getvalue())

    def test_bad_notifier_is_caught_without_network(self):
        bad = dict(CONFIG, notifiers=[{"type": "smoke-signal"}])
        self.config_path.write_text(json.dumps(bad), encoding="utf-8")
        code, _, err = self.run_cli("--check-config")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("smoke-signal", err)

    def test_check_config_makes_no_price_call(self):
        def explode(*a, **k):
            raise AssertionError("network must not be touched")

        code, _, _ = self.run_cli("--check-config", fetch=explode)
        self.assertEqual(code, cli.EXIT_OK)


class ReportTests(CliTestCase):
    def test_report_prints_table_and_skips_alerts(self):
        code, out, _ = self.run_cli("--report")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("AKT", out)
        self.assertIn("TOTAL", out)
        self.assertNotIn("[WARN]", out)

    def test_report_writes_no_state_file(self):
        self.run_cli("--report")
        self.assertFalse(self.state_path.exists())

    def test_quiet_suppresses_the_table(self):
        code, out, _ = self.run_cli("--quiet")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("TOTAL", out)


class AlertFlowTests(CliTestCase):
    def test_threshold_breach_notifies_once_then_is_quiet(self):
        code, out, _ = self.run_cli("--quiet")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("[WARN] AKT below", out)

        _, out2, _ = self.run_cli("--quiet")
        self.assertNotIn("AKT below", out2)

    def test_alert_rearms_after_price_recovers(self):
        self.run_cli("--quiet")
        self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.9))
        _, out, _ = self.run_cli("--quiet")
        self.assertIn("[WARN] AKT below", out)

    def test_market_guard_fires_when_btc_breaks_level(self):
        _, out, _ = self.run_cli("--quiet", fetch=lambda *a, **k: quotes(btc=70000.0))
        self.assertIn("[CRITICAL] Regime guard: BTC below", out)

    def test_fail_on_alert_changes_exit_code(self):
        code, _, _ = self.run_cli("--quiet", "--fail-on-alert")
        self.assertEqual(code, cli.EXIT_ALERTS)

    def test_no_alerts_exits_zero(self):
        code, out, _ = self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.9))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out, "")

    def test_state_file_is_written(self):
        self.run_cli("--quiet")
        self.assertTrue(self.state_path.exists())
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("peak_value", raw)


class DrawdownTests(CliTestCase):
    def test_drawdown_fires_on_the_run_that_breaches_it(self):
        # First run at 0.90 sets the peak at 90.
        self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.9))
        # Second run at 0.70 is a 22% drawdown from that peak.
        _, out, _ = self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.7))
        self.assertIn("Portfolio drawdown", out)

    def test_new_high_does_not_trigger_drawdown(self):
        self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.9))
        _, out, _ = self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=1.5))
        self.assertNotIn("drawdown", out)

    def test_reset_peak_rebaselines(self):
        self.run_cli("--quiet", fetch=lambda *a, **k: quotes(akt=0.9))
        self.run_cli("--quiet", "--reset-peak", fetch=lambda *a, **k: quotes(akt=0.7))
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(raw["peak_value"], 70.0)


class FailureTests(CliTestCase):
    def test_price_failure_is_reported_as_error(self):
        def boom(*a, **k):
            raise PriceFetchError("upstream down")

        code, _, err = self.run_cli("--quiet", fetch=boom)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("upstream down", err)

    def test_missing_price_warns_on_stderr(self):
        code, _, err = self.run_cli(
            "--quiet", fetch=lambda *a, **k: {"bitcoin": Quote("bitcoin", 81000.0, 1.0)}
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("akash-network", err)

    def test_non_positive_interval_is_rejected(self):
        code, _, err = self.run_cli("--watch", "--interval", "0")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("interval", err)


if __name__ == "__main__":
    unittest.main()
