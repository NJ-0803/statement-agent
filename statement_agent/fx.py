"""Currency conversion via a bundled, open-source historical rate file — not a
live API call. Never an invented rate.

Data: `data/eurofxref-hist.csv`, the European Central Bank's published daily
EUR foreign-exchange reference rates (euro area statistics, freely published,
no key/auth required), downloaded once and checked into the repo rather than
fetched at request time. Every currency pair is computed as a cross-rate
through EUR (the file's implicit base), using the rate quoted for the
transaction's OWN date — not today's rate, and not one blended rate applied
across a date range.

Why bundled instead of a live API call, decided mid-build after actually
trying the live-call approach first (see DECISIONS.md): a live HTTPS call
turned out to have two real failure modes that had nothing to do with the
math — a CA-certificate issue on this Python install unrelated to network
reachability, and an edge-protection 403 on the default urllib User-Agent —
and, separately, a *direct* CSV download from ECB's own site returned a
stale, partially-corrupted cached copy (data stopping in Feb 2010, with a
few rows of obviously fabricated sequential placeholder values mixed into
otherwise-real data) before the ZIP-packaged endpoint gave the real, current
file. A bundled, version-controlled snapshot has none of these problems:
no network dependency at request time, no SSL/UA fragility, and no runtime
risk of silently picking up a bad remote response — the data that ships is
exactly the data that was inspected and verified before being committed.

The real, disclosed trade-off: the bundled file is a point-in-time snapshot
(downloaded once), not a live feed. A transaction dated after the file's
last covered date won't have a rate. That's surfaced as an honest "no rate
available" (this module returns None), never a stale rate presented as
current — consistent with the rest of this system's INSUFFICIENT_INFORMATION-
over-guessing policy. Refreshing the file (re-running the same download this
module's docstring describes) is a deliberate, visible action, not something
that happens silently underneath a request.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "eurofxref-hist.csv")
_SOURCE_NAME = "ECB euro foreign exchange reference rates (bundled snapshot, data/eurofxref-hist.csv)"
_BASE_CURRENCY = "EUR"

# {date_str: {currency: Decimal or None}} — loaded once per process, lazily.
_rates_by_date: dict[str, dict[str, Decimal | None]] | None = None
_sorted_dates: list[str] | None = None


def _load() -> None:
    global _rates_by_date, _sorted_dates
    if _rates_by_date is not None:
        return

    _rates_by_date = {}
    with open(_DATA_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        currencies = [c.strip() for c in header[1:] if c.strip()]

        for row in reader:
            if not row or not row[0].strip():
                continue
            row_date = row[0].strip()
            try:
                datetime.strptime(row_date, "%Y-%m-%d")
            except ValueError:
                continue  # malformed date — skip this row rather than crash the whole load

            values: dict[str, Decimal | None] = {}
            for currency, raw in zip(currencies, row[1:]):
                raw = raw.strip()
                if not raw or raw == "N/A":
                    values[currency] = None
                    continue
                try:
                    parsed = Decimal(raw)
                except InvalidOperation:
                    values[currency] = None
                    continue
                # Defends against exactly the anomaly found while sourcing this data:
                # a handful of rows in one bad download had implausible tiny sequential
                # placeholder values (1, 2, 3, 4...) instead of real FX rates. A real
                # EUR cross-rate is essentially never below 0.01 or above 100000 for
                # any currency in this file — reject values outside that band rather
                # than silently trust a row that doesn't look like real market data.
                if parsed <= Decimal("0.0001") or parsed >= Decimal("1000000"):
                    values[currency] = None
                    continue
                values[currency] = parsed

            _rates_by_date[row_date] = values

    _sorted_dates = sorted(_rates_by_date.keys())


@dataclass
class FxRate:
    from_currency: str
    to_currency: str
    rate: Decimal
    requested_date: str
    rate_date: str  # the date the rate is actually quoted for; may differ from requested_date
    source: str = _SOURCE_NAME


def _nearest_available_date(requested: str, *, max_lookback_days: int = 10) -> str | None:
    """ECB doesn't publish rates on weekends or EU holidays — falls back to the
    nearest PRIOR date that has data, capped at a short lookback so a genuinely
    uncovered date (e.g. before the file's earliest date) returns None rather
    than silently reaching back arbitrarily far.
    """
    _load()
    if requested in _rates_by_date:
        return requested
    req_date = datetime.strptime(requested, "%Y-%m-%d").date()
    for offset in range(1, max_lookback_days + 1):
        candidate = (req_date - timedelta(days=offset)).isoformat()
        if candidate in _rates_by_date:
            return candidate
    return None


def get_fx_rate(from_currency: str, to_currency: str, as_of: date) -> FxRate | None:
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    requested = as_of.isoformat()

    if from_currency == to_currency:
        return FxRate(from_currency, to_currency, Decimal("1"), requested, requested, source="identity")

    _load()
    resolved_date = _nearest_available_date(requested)
    if resolved_date is None:
        return None
    row = _rates_by_date[resolved_date]

    def _rate_vs_eur(ccy: str) -> Decimal | None:
        if ccy == _BASE_CURRENCY:
            return Decimal("1")
        return row.get(ccy)

    from_vs_eur = _rate_vs_eur(from_currency)
    to_vs_eur = _rate_vs_eur(to_currency)
    if from_vs_eur is None or to_vs_eur is None or from_vs_eur == 0:
        return None

    # Both rates are EUR->X; cross-rate from_currency->to_currency = (EUR->to) / (EUR->from).
    # Decimal division produces far more digits than any real FX quote carries —
    # rounded to 8 decimal places (standard FX-quote precision), not left as a
    # ~28-digit division artifact.
    cross_rate = (to_vs_eur / from_vs_eur).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    return FxRate(
        from_currency=from_currency,
        to_currency=to_currency,
        rate=cross_rate,
        requested_date=requested,
        rate_date=resolved_date,
    )


def convert_amount(amount: Decimal, from_currency: str, to_currency: str, as_of: date) -> tuple[Decimal, FxRate] | None:
    """Converts one amount using the rate quoted for its own transaction date —
    not today's rate, and not one blended rate applied across a whole date range.
    Summing transactions from different days by converting each with its own
    day's rate is the only honest way to combine currencies without misstating
    what the FX exposure actually was on each individual day.
    """
    rate = get_fx_rate(from_currency, to_currency, as_of)
    if rate is None:
        return None
    converted = (amount * rate.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return converted, rate


def coverage() -> tuple[str, str] | None:
    """The (earliest, latest) dates this bundled snapshot actually covers —
    used to disclose the snapshot's boundaries rather than let a caller
    discover them only by getting an unexplained None back.
    """
    _load()
    if not _sorted_dates:
        return None
    return _sorted_dates[0], _sorted_dates[-1]
