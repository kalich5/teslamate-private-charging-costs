import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)

_cache: dict[tuple, float] = {}

FRANKFURTER_BASE_URL = "https://api.frankfurter.app"
REQUEST_TIMEOUT = 10  # seconds


def _get_eur_rate(date: datetime, currency: str) -> float:
    """
    Return how many units of *currency* equal 1 EUR on *date*.

    Results are cached in-process to avoid redundant API calls.

    Raises:
        ValueError: if the currency is not found in the API response.
        requests.RequestException: on network or HTTP errors.
    """
    date_str = date.strftime("%Y-%m-%d")
    cache_key = (date_str, currency)

    if cache_key in _cache:
        return _cache[cache_key]

    url = f"{FRANKFURTER_BASE_URL}/{date_str}"
    params = {"from": "EUR", "to": currency}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise requests.RequestException(
            f"Failed to fetch FX rate for {currency} on {date_str}: {e}"
        ) from e

    data = resp.json()
    rates = data.get("rates", {})

    if currency not in rates:
        raise ValueError(
            f"Currency '{currency}' not found in Frankfurter response for {date_str}. "
            f"Available: {list(rates.keys())}"
        )

    rate = float(rates[currency])
    _cache[cache_key] = rate
    log.debug("FX rate: 1 EUR = %.6f %s (on %s)", rate, currency, date_str)
    return rate


def convert(amount: float, from_cur: str, to_cur: str, date: datetime) -> float:
    """
    Convert *amount* from *from_cur* to *to_cur* using historical rates on *date*.

    Uses EUR as the pivot currency (via the Frankfurter API).

    Args:
        amount:   The value to convert.
        from_cur: ISO 4217 source currency code (e.g. "CZK").
        to_cur:   ISO 4217 target currency code (e.g. "CHF").
        date:     Date for which the historical rate should be fetched.

    Returns:
        Converted amount rounded to 4 decimal places.

    Raises:
        ValueError: if a currency code is not supported by the API.
        requests.RequestException: on network errors.
    """
    from_cur = from_cur.upper()
    to_cur = to_cur.upper()

    if from_cur == to_cur:
        return round(amount, 4)

    # Convert source → EUR
    if from_cur == "EUR":
        eur_amount = amount
    else:
        rate_from = _get_eur_rate(date, from_cur)
        eur_amount = amount / rate_from

    # Convert EUR → target
    if to_cur == "EUR":
        result = eur_amount
    else:
        rate_to = _get_eur_rate(date, to_cur)
        result = eur_amount * rate_to

    return round(result, 4)
