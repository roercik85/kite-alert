import io
import unittest

from portfolio_alert.notify import (
    ConsoleNotifier,
    Notifier,
    NotifierError,
    NtfyNotifier,
    WebhookNotifier,
    build_notifier,
    build_notifiers,
    dispatch,
)
from portfolio_alert.rules import Alert

ALERTS = [
    Alert("k1", "critical", "Stop-loss hit", "AKT is down 35%."),
    Alert("k2", "info", "Take-profit hit", "ZEC is up 180%."),
]


class BuildNotifierTests(unittest.TestCase):
    def test_console(self):
        self.assertIsInstance(build_notifier({"type": "console"}), ConsoleNotifier)

    def test_type_is_case_insensitive(self):
        self.assertIsInstance(build_notifier({"type": "CONSOLE"}), ConsoleNotifier)

    def test_ntfy_requires_topic(self):
        with self.assertRaises(NotifierError):
            build_notifier({"type": "ntfy"})

    def test_webhook_requires_url(self):
        with self.assertRaises(NotifierError):
            build_notifier({"type": "webhook"})

    def test_unknown_type_raises(self):
        with self.assertRaises(NotifierError) as ctx:
            build_notifier({"type": "carrier-pigeon"})
        self.assertIn("carrier-pigeon", str(ctx.exception))

    def test_build_many(self):
        built = build_notifiers([{"type": "console"}, {"type": "ntfy", "topic": "t"}])
        self.assertEqual(len(built), 2)

    def test_ntfy_strips_trailing_slash_from_server(self):
        notifier = build_notifier({"type": "ntfy", "topic": "t", "server": "https://x.dev/"})
        self.assertEqual(notifier.server, "https://x.dev")


class ConsoleNotifierTests(unittest.TestCase):
    def test_prints_each_alert_with_severity_prefix(self):
        stream = io.StringIO()
        ConsoleNotifier(stream=stream).send(ALERTS, "REPORT")
        out = stream.getvalue()
        self.assertIn("[CRITICAL] Stop-loss hit", out)
        self.assertIn("[INFO] Take-profit hit", out)
        self.assertIn("AKT is down 35%.", out)

    def test_report_omitted_by_default(self):
        stream = io.StringIO()
        ConsoleNotifier(stream=stream).send(ALERTS, "REPORT")
        self.assertNotIn("REPORT", stream.getvalue())

    def test_report_included_when_requested(self):
        stream = io.StringIO()
        ConsoleNotifier(stream=stream, show_report=True).send(ALERTS, "REPORT")
        self.assertIn("REPORT", stream.getvalue())


class DispatchTests(unittest.TestCase):
    def test_one_failing_notifier_does_not_silence_the_others(self):
        class Boom(Notifier):
            def send(self, alerts, report):
                raise RuntimeError("network down")

        class Recording(Notifier):
            def __init__(self):
                self.received = None

            def send(self, alerts, report):
                self.received = list(alerts)

        recording = Recording()
        errors = dispatch([Boom(), recording], ALERTS, "REPORT")
        self.assertEqual(len(errors), 1)
        self.assertEqual(recording.received, ALERTS)

    def test_no_errors_returns_empty_list(self):
        self.assertEqual(dispatch([ConsoleNotifier(stream=io.StringIO())], ALERTS, ""), [])


class HttpNotifierPayloadTests(unittest.TestCase):
    def test_ntfy_posts_one_request_per_alert_with_priority(self):
        calls = []

        class FakeResp:
            def raise_for_status(self):
                return None

        def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
            calls.append({"url": url, "data": data, "headers": headers})
            return FakeResp()

        notifier = NtfyNotifier(topic="my-topic", token="secret")
        import portfolio_alert.notify as notify_module

        original = notify_module.requests.post
        notify_module.requests.post = fake_post
        try:
            notifier.send(ALERTS, "REPORT")
        finally:
            notify_module.requests.post = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["url"], "https://ntfy.sh/my-topic")
        self.assertEqual(calls[0]["headers"]["Priority"], "urgent")
        self.assertEqual(calls[1]["headers"]["Priority"], "default")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer secret")

    def test_webhook_posts_once_with_all_alerts(self):
        calls = []

        class FakeResp:
            def raise_for_status(self):
                return None

        def fake_post(url, json=None, timeout=None, **kwargs):
            calls.append({"url": url, "json": json})
            return FakeResp()

        import portfolio_alert.notify as notify_module

        original = notify_module.requests.post
        notify_module.requests.post = fake_post
        try:
            WebhookNotifier(url="https://hooks.example/x").send(ALERTS, "REPORT")
        finally:
            notify_module.requests.post = original

        self.assertEqual(len(calls), 1)
        body = calls[0]["json"]["text"]
        self.assertIn("Stop-loss hit", body)
        self.assertIn("Take-profit hit", body)
        self.assertIn("REPORT", body)

    def test_webhook_sends_nothing_without_alerts(self):
        calls = []
        import portfolio_alert.notify as notify_module

        original = notify_module.requests.post
        notify_module.requests.post = lambda *a, **k: calls.append(a)
        try:
            WebhookNotifier(url="https://hooks.example/x").send([], "REPORT")
        finally:
            notify_module.requests.post = original
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
