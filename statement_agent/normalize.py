"""Currency and date normalization.

Amount parsing is a pure per-string function: no cross-row context needed.

Date parsing is NOT pure per-string, because a bare numeric date like "05/07/2025"
is genuinely ambiguous (DD/MM vs MM/DD) in isolation. We resolve that ambiguity at
the document level: scan every date in a document first, and if any date's first
or second slash-group is >12, that pins the whole document's convention. Only when
no disambiguating date exists in the document do we fall back to a locale default
(DD/MM, since every statement in this dataset states amounts in Rs/INR/₹) — and we
mark that fallback as an assumption rather than a certainty, because a defaulted
guess and a proven convention are not the same thing to a verifier reading it later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .schema import Direction

CURRENCY_SYMBOLS = {
    "₹": "INR",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}
CURRENCY_CODES = {"INR", "USD", "EUR", "GBP", "RS", "RS."}

_AMOUNT_TOKEN_RE = re.compile(
    r"""
    (?P<prefix_ccy>₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?
    \s*
    (?P<paren_open>\()?
    \s*-?\s*
    (?P<digits>[\d,]+(?:\.\d+)?)
    \s*
    (?P<paren_close>\))?
    \s*
    (?P<suffix>CR|DR|Cr|Dr|cr|dr)?
    \s*
    (?P<trailing_minus>-)?
    \s*
    (?P<suffix_ccy>INR|USD|EUR|GBP)?
    """,
    re.VERBOSE,
)


@dataclass
class ParsedAmount:
    amount: Decimal
    currency: str
    direction: Direction
    raw: str
    currency_inferred: bool  # True if currency came from the document default, not the row itself


def normalize_amount(raw: str, *, default_currency: str = "INR") -> ParsedAmount | None:
    """Parse one amount cell into a signed-aware (amount, currency, direction).

    Handles: ₹1,340.00 / Rs 860 / Rs. 2,494.73 / INR 1,120.00 / USD 20.00 /
    540.00 / 8,000.00 CR / (1,250.00) / -1250 / 1,25,000.50 (Indian grouping).
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    m = _AMOUNT_TOKEN_RE.search(text)
    if not m or not m.group("digits"):
        return None

    digits = m.group("digits").replace(",", "")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None

    is_negative = text.strip().startswith("-") or bool(m.group("paren_open")) or bool(m.group("trailing_minus"))
    suffix = (m.group("suffix") or "").upper()
    direction = Direction.DEBIT
    if suffix == "CR" or is_negative:
        direction = Direction.CREDIT

    ccy_token = (m.group("prefix_ccy") or m.group("suffix_ccy") or "").upper().rstrip(".")
    currency_inferred = False
    if ccy_token in CURRENCY_SYMBOLS:
        currency = CURRENCY_SYMBOLS[ccy_token]
    elif ccy_token in ("RS", "RS."):
        currency = "INR"
    elif ccy_token in CURRENCY_CODES:
        currency = ccy_token
    else:
        currency = default_currency
        currency_inferred = True

    return ParsedAmount(
        amount=value,
        currency=currency,
        direction=direction,
        raw=raw,
        currency_inferred=currency_inferred,
    )


_MONTH_NAMES = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)

_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_NUMERIC_SLASH_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$")
_TEXTUAL_RE = re.compile(
    rf"^\s*(?:(\d{{1,2}})\s+)?({_MONTH_NAMES})\.?,?\s+(?:(\d{{1,2}}),?\s+)?(\d{{4}})\s*$",
    re.IGNORECASE,
)

_MONTH_LOOKUP = {
    name: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for name in names
}


@dataclass
class ParsedDate:
    value: date | None
    raw: str
    confidence: float  # 1.0 = unambiguous format; <1.0 = ambiguous, resolved by document-level inference or locale default
    assumption: str = ""  # non-empty if a fallback/default was used to resolve ambiguity


class DocumentDateResolver:
    """Resolves ambiguous DD/MM vs MM/DD dates using every date in one document as context.

    Usage: feed it every raw date string seen in a document via `observe`, call
    `resolve_convention()` once, then `parse` each string.
    """

    def __init__(self, *, locale_default: str = "DMY"):
        self.locale_default = locale_default  # "DMY" or "MDY"
        self._numeric_dates: list[tuple[int, int, int]] = []  # (a, b, year) as written
        self._convention: str | None = None  # "DMY" or "MDY", once resolved

    def observe(self, raw: str) -> None:
        m = _NUMERIC_SLASH_RE.match(raw or "")
        if m:
            a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            self._numeric_dates.append((a, b, year))

    def resolve_convention(self) -> None:
        for a, b, _ in self._numeric_dates:
            if a > 12 and b <= 12:
                self._convention = "DMY"
                return
            if b > 12 and a <= 12:
                self._convention = "MDY"
                return
        self._convention = None  # genuinely ambiguous across the whole document

    def parse(self, raw: str) -> ParsedDate:
        if raw is None:
            return ParsedDate(None, "", 0.0, "empty date")
        text = raw.strip()
        if not text:
            return ParsedDate(None, raw, 0.0, "empty date")

        m = _ISO_RE.match(text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _safe_date(y, mo, d, text, confidence=1.0)

        m = _TEXTUAL_RE.match(text)
        if m:
            day_before, month_name, day_after, year = m.groups()
            day = day_before or day_after
            month = _MONTH_LOOKUP.get(month_name.lower())
            if day and month:
                return _safe_date(int(year), month, int(day), text, confidence=1.0)

        m = _NUMERIC_SLASH_RE.match(text)
        if m:
            a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 and b <= 12:
                return _safe_date(year, b, a, text, confidence=1.0)
            if b > 12 and a <= 12:
                return _safe_date(year, a, b, text, confidence=1.0)
            # genuinely ambiguous single date — use document-wide convention if one was found
            convention = self._convention or self.locale_default
            assumption = (
                f"ambiguous DD/MM-vs-MM/DD date resolved via "
                f"{'document-wide evidence' if self._convention else 'locale default (' + self.locale_default + ')'}"
            )
            month, day = (b, a) if convention == "DMY" else (a, b)
            parsed = _safe_date(year, month, day, text, confidence=0.6)
            parsed.assumption = assumption
            return parsed

        return ParsedDate(None, text, 0.0, "unrecognized date format")


def _safe_date(year: int, month: int, day: int, raw: str, *, confidence: float) -> ParsedDate:
    try:
        return ParsedDate(date(year, month, day), raw, confidence)
    except ValueError:
        return ParsedDate(None, raw, 0.0, f"invalid calendar date: {year}-{month}-{day}")


def is_date_plausible(d: date, *, statement_start: date | None, statement_end: date | None, today: date | None = None) -> bool:
    """Bound a parsed date against reality, per the temporal-corruption edge case:
    a date that's decades off the statement period is extraction corruption
    (OCR digit error, bad century default), not a real transaction — flag it,
    never silently trust or "correct" it.
    """
    today = today or datetime.now().date()
    if d.year < 1990 or d > today:
        return False
    if statement_start and statement_end:
        tolerance_days = 45
        lo = statement_start.toordinal() - tolerance_days
        hi = statement_end.toordinal() + tolerance_days
        return lo <= d.toordinal() <= hi
    return True


_TRAILING_CORP_SUFFIX_RE = re.compile(
    r"\s+(PVT\.?\s*LTD\.?|PVT\.?|LTD\.?|LIMITED|LLC|INC\.?|CO\.?)\s*$", re.IGNORECASE
)


def normalize_merchant(raw: str | None) -> str | None:
    """Canonicalizes a merchant string for grouping/dedup purposes. Never replaces
    merchant_raw — that stays the citation/provenance value seen in the source
    document; this is an additional field for consolidating what's obviously the
    same merchant.

    Deliberately conservative: only strips noise that's unambiguous regardless of
    which specific merchant it is — collapsed whitespace, case, and a trailing
    corporate-entity suffix (PVT, PVT LTD, LTD, LIMITED, INC, LLC, CO) that
    different banks/processors inconsistently append for the exact same company
    ("GRANDEUR JEWELLERS PVT" and a hypothetical "GRANDEUR JEWELLERS" on another
    statement are almost certainly the same merchant; stripping the suffix lets
    them group together instead of fragmenting).

    Deliberately does NOT attempt to resolve asterisk-separated payment-processor
    prefixes/suffixes (e.g. "SQ *Blue Bottle Coffee" vs "OPENAI *ChatGPT") — which
    side of the "*" is the real merchant varies by processor with no reliable
    general rule, and guessing wrong would actively corrupt grouping rather than
    just fail to help it. A curated alias table for known processor/brand
    patterns is the natural next step once real fixture data with actual
    collisions exists to build and verify one against (see NOT_IMPLEMENTED.md) —
    not guessed at here, for the same reason the cross-transaction linking layer
    in that document isn't guessed at either.
    """
    if not raw:
        return None
    text = " ".join(raw.strip().upper().split())
    text = _TRAILING_CORP_SUFFIX_RE.sub("", text).strip()
    return text or None
