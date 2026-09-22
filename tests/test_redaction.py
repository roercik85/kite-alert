"""Redaction must keep monetary amounts out of anything leaving the machine."""

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from portfolio_alert import cli
from portfolio_alert.config import parse_config
from portfolio_alert.prices import Quote
from portfolio_alert.rules import build_snapshot, evaluate_all

# Amounts that must never appear in a redacted alert.
SECRET_VALUE = "10,204"
SECRET_PNL = "1,244"


def config(redact):
    return parse_config(
        {
            "redact_amounts": redact,
            "positions": [
                {
                    "symbol": "AKT",
                    "coingecko_id": "akash-network",
                    "amount": 1234.5678,
                    "avg_cost": 0.6727,
                    "alerts": {"stop_loss_pct": -10, "take_profit_pct": -50},
                }
            ],
            "portfolio_alerts": {
                "value_below": 9999999,
                "value_above": 1,
                "drawdown_pct": 5,
                "total_pnl_below_pct": 100,
            },
        }
    )


def alerts_for(redact):
    cfg = config(redact)
    quotes = {"akash-network": Quote("akash-network", 0.553337, -3.0)}
    snap = build_snapshot(cfg, quotes)
    return evaluate_all(cfg, snap, quotes, peak_value=99999.0), snap


class RedactedContentTests(unittest.TestCase):
    def test_every_rule_still_fires_when_redacting(self):
        plain, _ = alerts_for(False)
        redacted, _ = alerts_for(True)
        self.assertEqual({a.key for a in plain}, {a.key for a in redacted})
        self.assertTrue(len(redacted) >= 5)

    def test_no_currency_amounts_survive_redaction(self):
        redacted, snap = alerts_for(True)
        text = " ".join(f"{a.title} {a.body}" for a in redacted)
        for amount in (
            f"{snap.total_value:,.2f}",
            f"{snap.total_cost:,.2f}",
            f"{snap.positions[0].pnl_abs:,.2f}",
            "9,999,999",
        ):
            self.assertNotIn(amount, text, f"{amount!r} leaked into a redacted alert")

    def test_holding_size_never_appears(self):
        redacted, _ = alerts_for(True)
        text = " ".join(f"{a.title} {a.body}" for a in redacted)
        self.assertNotIn("1111", text)
        self.assertNotIn("1,111", text)

    def test_unredacted_output_does_contain_amounts(self):
        plain, snap = alerts_for(False)
        text = " ".join(f"{a.title} {a.body}" for a in plain)
        self.assertIn(f"{snap.positions[0].pnl_abs:,.2f}", text)

    def test_prices_and_percentages_are_kept(self):
        redacted, _ = alerts_for(True)
        text = " ".join(f"{a.title} {a.body}" for a in redacted)
        self.assertIn("0.553337", text)
        self.assertRegex(text, r"[-+]\d+\.\d%")

    def test_drawdown_keeps_percentage_drops_values(self):
        redacted, _ = alerts_for(True)
        drawdown = next(a for a in redacted if "drawdown" in a.key)
        self.assertIn("%", drawdown.title)
        self.assertNotIn("99,999", drawdown.body)

    def test_portfolio_titles_do_not_reveal_thresholds(self):
        redacted, _ = alerts_for(True)
        for alert in redacted:
            if alert.key.startswith("portfolio:value_"):
                self.assertNotIn("9999999", alert.title)
                self.assertNotIn("9,999,999", alert.title)


class RedactedDispatchTests(unittest.TestCase):
    """End-to-end: what actually reaches a remote sink."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.config_path = self.dir / "portfolio.json"
        self.state_path = self.dir / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, redact):
        raw = {
            "redact_amounts": redact,
            "positions": [
                {
                    "symbol": "AKT",
                    "coingecko_id": "akash-network",
                    "amount": 1234.5678,
                    "avg_cost": 0.6727,
                    "alerts": {"stop_loss_pct": -10},
                }
            ],
            "notifiers": [{"type": "webhook", "url": "https://hooks.example/x"}],
        }
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def captured_payload(self, redact):
        self.write_config(redact)
        sent = []

        class FakeResp:
            def raise_for_status(self):
                return None

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent.append(json)
            return FakeResp()

        import portfolio_alert.notify as notify_module

        original = notify_module.requests.post
        notify_module.requests.post = fake_post
        try:
            with mock.patch.object(
                cli,
                "fetch_quotes",
                lambda *a, **k: {"akash-network": Quote("akash-network", 0.553337, -3.0)},
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    cli.main(
                        [
                            "--config", str(self.config_path),
                            "--state", str(self.state_path),
                        ]
                    )
                return sent, out.getvalue()
        finally:
            notify_module.requests.post = original

    def test_webhook_payload_carries_no_table_when_redacting(self):
        sent, _ = self.captured_payload(redact=True)
        self.assertEqual(len(sent), 1)
        body = sent[0]["text"]
        self.assertNotIn("TOTAL", body)
        self.assertNotIn("WEIGHT", body)
        self.assertNotIn("Cost ", body)

    def test_webhook_payload_has_no_digit_grouped_amounts_when_redacting(self):
        sent, _ = self.captured_payload(redact=True)
        # No "1,234.56"-style figure should survive; prices here are sub-dollar.
        self.assertIsNone(re.search(r"\d,\d{3}\.\d{2}", sent[0]["text"]))

    def test_webhook_payload_does_carry_the_table_without_redaction(self):
        sent, _ = self.captured_payload(redact=False)
        self.assertIn("TOTAL", sent[0]["text"])

    def test_local_stdout_still_shows_full_table_when_redacting(self):
        _, stdout = self.captured_payload(redact=True)
        self.assertIn("TOTAL", stdout)
        self.assertIn("Cost", stdout)


if __name__ == "__main__":
    unittest.main()
