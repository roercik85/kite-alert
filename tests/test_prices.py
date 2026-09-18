import unittest

import requests

from portfolio_alert.prices import (
    PRO_SIMPLE_PRICE_URL,
    SIMPLE_PRICE_URL,
    PriceFetchError,
    Quote,
    _headers,
    _parse_response,
    fetch_quotes,
)

PAYLOAD = {
    "akash-network": {"usd": 0.553437, "usd_24h_change": 7.52},
    "bitcoin": {"usd": 81196.0, "usd_24h_change": 6.2},
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"status {self.status}")

    def json(self):
        return self._payload


class FakeSession:
    """Replays a scripted list of responses or exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ParseResponseTests(unittest.TestCase):
    def test_parses_price_and_change(self):
        quotes = _parse_response(PAYLOAD, ["akash-network"], "usd")
        self.assertEqual(quotes["akash-network"].price, 0.553437)
        self.assertAlmostEqual(quotes["akash-network"].change_24h_pct, 7.52)

    def test_missing_change_becomes_none(self):
        quotes = _parse_response({"x": {"usd": 1.0}}, ["x"], "usd")
        self.assertIsNone(quotes["x"].change_24h_pct)

    def test_unknown_id_is_dropped_not_fatal(self):
        quotes = _parse_response(PAYLOAD, ["akash-network", "typo-coin"], "usd")
        self.assertEqual(set(quotes), {"akash-network"})

    def test_all_ids_missing_raises(self):
        with self.assertRaises(PriceFetchError):
            _parse_response(PAYLOAD, ["typo-coin"], "usd")

    def test_wrong_currency_raises(self):
        with self.assertRaises(PriceFetchError):
            _parse_response(PAYLOAD, ["bitcoin"], "eur")

    def test_non_dict_payload_raises(self):
        with self.assertRaises(PriceFetchError):
            _parse_response([1, 2, 3], ["bitcoin"], "usd")


class FetchQuotesTests(unittest.TestCase):
    def test_empty_ids_short_circuits(self):
        session = FakeSession()
        self.assertEqual(fetch_quotes([], session=session), {})
        self.assertEqual(session.calls, [])

    def test_successful_fetch_sends_expected_params(self):
        session = FakeSession(FakeResponse(PAYLOAD))
        quotes = fetch_quotes(["akash-network", "bitcoin"], session=session)
        self.assertEqual(set(quotes), {"akash-network", "bitcoin"})
        params = session.calls[0]["params"]
        self.assertEqual(params["ids"], "akash-network,bitcoin")
        self.assertEqual(params["vs_currencies"], "usd")
        self.assertEqual(params["include_24hr_change"], "true")

    def test_retries_then_succeeds(self):
        session = FakeSession(
            requests.ConnectionError("boom"), FakeResponse(PAYLOAD)
        )
        slept = []
        quotes = fetch_quotes(["bitcoin"], session=session, sleep=slept.append)
        self.assertIn("bitcoin", quotes)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(slept, [1])

    def test_raises_after_exhausting_retries(self):
        session = FakeSession(*[requests.ConnectionError("boom")] * 3)
        with self.assertRaises(PriceFetchError):
            fetch_quotes(["bitcoin"], session=session, retries=3, sleep=lambda _: None)
        self.assertEqual(len(session.calls), 3)

    def test_http_error_is_retried(self):
        session = FakeSession(FakeResponse({}, status=429), FakeResponse(PAYLOAD))
        quotes = fetch_quotes(["bitcoin"], session=session, sleep=lambda _: None)
        self.assertIn("bitcoin", quotes)

    def test_backoff_is_exponential(self):
        session = FakeSession(*[requests.ConnectionError("boom")] * 4)
        slept = []
        with self.assertRaises(PriceFetchError):
            fetch_quotes(["bitcoin"], session=session, retries=4, sleep=slept.append)
        self.assertEqual(slept, [1, 2, 4])

    def test_demo_plan_uses_free_url_and_demo_header(self):
        session = FakeSession(FakeResponse(PAYLOAD))
        fetch_quotes(["bitcoin"], api_key="key", plan="demo", session=session)
        self.assertEqual(session.calls[0]["url"], SIMPLE_PRICE_URL)
        self.assertIn("x-cg-demo-api-key", session.calls[0]["headers"])

    def test_pro_plan_uses_pro_url_and_pro_header(self):
        session = FakeSession(FakeResponse(PAYLOAD))
        fetch_quotes(["bitcoin"], api_key="key", plan="pro", session=session)
        self.assertEqual(session.calls[0]["url"], PRO_SIMPLE_PRICE_URL)
        self.assertIn("x-cg-pro-api-key", session.calls[0]["headers"])

    def test_no_key_sends_no_auth_header(self):
        self.assertEqual(_headers(None, "demo"), {})


class QuoteTests(unittest.TestCase):
    def test_quote_is_hashable_and_frozen(self):
        quote = Quote("bitcoin", 1.0, 2.0)
        self.assertEqual(hash(quote), hash(Quote("bitcoin", 1.0, 2.0)))


if __name__ == "__main__":
    unittest.main()
