"""CoinGecko price client."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
PRO_SIMPLE_PRICE_URL = "https://pro-api.coingecko.com/api/v3/simple/price"


class PriceFetchError(RuntimeError):
    """Raised when prices could not be retrieved."""


@dataclass(frozen=True)
class Quote:
    coingecko_id: str
    price: float
    change_24h_pct: float | None = None


def _headers(api_key: str | None, plan: str) -> dict[str, str]:
    if not api_key:
        return {}
    # Demo and pro keys look alike, so the plan is configured explicitly rather
    # than sniffed: sending the wrong header is rejected by the API.
    header = "x-cg-pro-api-key" if plan == "pro" else "x-cg-demo-api-key"
    return {header: api_key}


def fetch_quotes(
    coingecko_ids: list[str],
    vs_currency: str = "usd",
    *,
    api_key: str | None = None,
    plan: str = "demo",
    timeout: float = 20.0,
    retries: int = 3,
    session: requests.Session | None = None,
    sleep: "callable" = time.sleep,
) -> dict[str, Quote]:
    """Fetch spot prices and 24h changes, keyed by CoinGecko id.

    Retries transient failures with exponential backoff. Raises PriceFetchError
    if every attempt fails, so a cron run fails loudly instead of reporting
    a silently empty portfolio.
    """
    if not coingecko_ids:
        return {}

    url = PRO_SIMPLE_PRICE_URL if plan == "pro" else SIMPLE_PRICE_URL
    params = {
        "ids": ",".join(coingecko_ids),
        "vs_currencies": vs_currency,
        "include_24hr_change": "true",
    }
    http = session or requests.Session()

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = http.get(
                url, params=params, headers=_headers(api_key, plan), timeout=timeout
            )
            response.raise_for_status()
            return _parse_response(response.json(), coingecko_ids, vs_currency)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries - 1:
                sleep(2**attempt)

    raise PriceFetchError(
        f"could not fetch prices after {retries} attempts: {last_error}"
    ) from last_error


def _parse_response(
    payload: object, coingecko_ids: list[str], vs_currency: str
) -> dict[str, Quote]:
    if not isinstance(payload, dict):
        raise PriceFetchError(f"unexpected response shape: {type(payload).__name__}")

    quotes: dict[str, Quote] = {}
    for coin_id in coingecko_ids:
        entry = payload.get(coin_id)
        if not isinstance(entry, dict) or vs_currency not in entry:
            # An unknown id is a config typo, not a transient failure. Skip it
            # here; the caller reports which ids came back missing.
            continue
        change = entry.get(f"{vs_currency}_24h_change")
        quotes[coin_id] = Quote(
            coingecko_id=coin_id,
            price=float(entry[vs_currency]),
            change_24h_pct=float(change) if change is not None else None,
        )

    if not quotes:
        raise PriceFetchError(
            f"no usable prices returned for {coingecko_ids} in {vs_currency!r}"
        )
    return quotes
