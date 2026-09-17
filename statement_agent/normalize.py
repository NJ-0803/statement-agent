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
    ambiguous_separator: bool = False  # the ',' or '.' in this cell could group thousands or be the decimal
    separator_assumption: str = ""  # plain language: how the separators were read


_AMOUNT_CELL_RE = re.compile(
    r"""^\s*
    (?P<prefix_ccy>₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?\s*
    (?P<sign>[-+])?\s*
    (?P<paren_open>\()?\s*
    (?P<prefix_ccy2>₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?\s*
    (?P<sign2>[-+])?\s*
    (?P<digits>\d[\d.,'\u00a0\u202f ]*)
    \s*(?P<paren_close>\))?
    \s*(?P<suffix>CR|DR)?\.?
    \s*(?P<trailing_minus>-)?
    \s*(?P<suffix_ccy>₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?
    \s*(?P<suffix2>CR|DR)?\.?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)
_GROUPED_RE = re.compile(r"^\d{1,3}(?:[,. '\u00a0\u202f]\d{2,3})*$")


def split_amount_digits(digits: str, decimal_separator: str | None) -> tuple[str, bool, str]:
    """Turn the digit part of a cell into a plain number string.

    Returns (plain digits, ambiguous, how it was read). Which character is the decimal point depends on the
    file, not on this string: "1.234,50" is 1234.50 in Europe and "1,234.50" is the same number elsewhere.
    Rules, in order:
      * both ',' and '.' present -> whichever comes last is the decimal point
      * one kind, more than once -> grouping ("1.234.567")
      * one kind, once, with 1, 2 or 4+ digits after it -> decimal point ("1234,50", "1.5")
      * one kind, once, with exactly 3 digits after it -> genuinely ambiguous ("1.234"): the file's own
        convention decides, and when nothing has settled it the number is read as grouped AND flagged, so
        the import asks instead of guessing silently.
    """
    cleaned = digits.strip().replace("'", "").replace("\u00a0", " ").replace("\u202f", " ")
    if " " in cleaned.strip():  # space is only ever a thousands separator
        cleaned = cleaned.replace(" ", "")
    commas, dots = cleaned.count(","), cleaned.count(".")

    def strip_all(text: str) -> str:
        return text.replace(",", "").replace(".", "")

    if commas and dots:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        whole, _, fraction = cleaned.rpartition(decimal)
        return f"{strip_all(whole)}.{strip_all(fraction)}", False, f"'{decimal}' is the decimal point"
    sep = "," if commas else "." if dots else ""
    if not sep:
        return cleaned, False, "whole number"
    count = commas or dots
    whole, _, fraction = cleaned.rpartition(sep)
    if count > 1:
        return strip_all(cleaned), False, f"'{sep}' repeats, so it groups thousands"
    if len(fraction) != 3:
        return f"{strip_all(whole)}.{fraction}", False, f"'{sep}' is the decimal point"
    if decimal_separator == sep:
        return f"{strip_all(whole)}.{fraction}", False, f"this file uses '{sep}' as the decimal point"
    if decimal_separator and decimal_separator != sep:
        return strip_all(cleaned), False, f"this file uses '{decimal_separator}' as the decimal point, so '{sep}' groups thousands"
    return strip_all(cleaned), True, f"'{sep}' could group thousands or be the decimal point"


def infer_decimal_separator(samples: list[str]) -> str | None:
    """Which character this document uses as its decimal point, from its own amounts: a separator followed
    by exactly two digits at the end of a value is a decimal point ("1.234,50" / "1,234.50"). None when the
    document's amounts never settle it."""
    comma = sum(1 for v in samples if re.search(r",\d{2}\b(?!\d)", v or ""))
    dot = sum(1 for v in samples if re.search(r"\.\d{2}\b(?!\d)", v or ""))
    if comma and not dot:
        return ","
    if dot and not comma:
        return "."
    return None


def normalize_amount(raw: str, *, default_currency: str = "INR",
                     decimal_separator: str | None = None) -> ParsedAmount | None:
    """Parse one amount cell into a signed-aware (amount, currency, direction).

    The whole cell must be an amount: a cell with other text in it is not a number, and reading one out of
    it (the old behaviour) invented values from things like "ref 12.34 fee". `decimal_separator` is the
    file's confirmed convention when one is known; without it, a cell whose separator could mean either
    thing is marked ambiguous for the import to ask about.

    Handles: ₹1,340.00 / Rs 860 / Rs. 2,494.73 / INR 1,120.00 / USD 20.00 / 540.00 / 8,000.00 CR /
    (1,250.00) / -1250 / 1,25,000.50 (Indian grouping) / EUR 1.234,50 / 1 234,50 (French spacing).
    """
    if raw is None:
        return None
    text = raw.strip().strip('"').strip("'").strip()  # a value still carrying its CSV quotes is still a number
    if not text:
        return None

    m = _AMOUNT_CELL_RE.match(text)
    if not m or not m.group("digits"):
        return None

    plain, ambiguous, assumption = split_amount_digits(m.group("digits"), decimal_separator)
    try:
        value = Decimal(plain)
    except InvalidOperation:
        return None

    suffix = (m.group("suffix") or m.group("suffix2") or "").upper()
    is_negative = (m.group("sign") == "-" or m.group("sign2") == "-"
                   or bool(m.group("paren_open")) or bool(m.group("trailing_minus")))
    direction = Direction.CREDIT if (suffix == "CR" or is_negative) else Direction.DEBIT

    ccy_token = (m.group("prefix_ccy") or m.group("prefix_ccy2") or m.group("suffix_ccy") or "").upper().rstrip(".")
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
        ambiguous_separator=ambiguous,
        separator_assumption=assumption,
    )


_MONTH_NAMES = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)

_ISO_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$")
# Bank exports routinely use dotted dates (31.03.2025) and two-digit years (31/03/25), not just a
# 4-digit year with '/' or '-' — both were outright rejected before the generalized import work.
_NUMERIC_SLASH_RE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\s*$")
_TEXTUAL_RE = re.compile(
    rf"^\s*(?:(\d{{1,2}})\s+)?({_MONTH_NAMES})\.?,?\s+(?:(\d{{1,2}}),?\s+)?(\d{{4}})\s*$",
    re.IGNORECASE,
)
# "01-Apr-2025" / "01-Apr-25" / "01/Apr/2025" — the most common Indian bank-export date shape
_TEXTUAL_DASH_RE = re.compile(rf"^\s*(\d{{1,2}})[-/ ]({_MONTH_NAMES})\.?[-/ ,]\s*(\d{{4}}|\d{{2}})\s*$", re.IGNORECASE)


def _expand_year(year: str) -> int:
    """Two-digit years pivot on the current year: up to next year's two digits -> 20xx, else 19xx.
    A statement dated '31/03/25' in 2026 is 2025, never 1925."""
    if len(year) == 4:
        return int(year)
    yy = int(year)
    return 2000 + yy if yy <= (datetime.now().year % 100) + 1 else 1900 + yy

# A trailing time-of-day (e.g. "01-01-2023 08:00" or an ISO "2023-01-01 08:00:00") — found
# necessary against a real downloaded dataset whose date column was a full timestamp, not a
# bare date. None of the three patterns above need the time part (the schema is date-only),
# and _NUMERIC_SLASH_RE already accepts both '/' and '-' as separators — the trailing time was
# the only thing breaking the full-string match, not the date portion itself.
_TRAILING_TIME_RE = re.compile(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*$")


def _strip_trailing_time(raw: str) -> str:
    return _TRAILING_TIME_RE.sub("", raw) if raw else raw

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
        m = _NUMERIC_SLASH_RE.match(_strip_trailing_time(raw or ""))
        if m:
            a, b, year = int(m.group(1)), int(m.group(2)), _expand_year(m.group(3))
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

        # Matched against a time-stripped copy only — date_raw (below, via `text`) still
        # preserves the original full string (e.g. "01-01-2023 08:00") for citation, since
        # the schema itself is date-only and none of these three patterns need the time part.
        match_text = _strip_trailing_time(text)

        m = _ISO_RE.match(match_text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _safe_date(y, mo, d, text, confidence=1.0)

        m = _TEXTUAL_RE.match(match_text)
        if m:
            day_before, month_name, day_after, year = m.groups()
            day = day_before or day_after
            month = _MONTH_LOOKUP.get(month_name.lower())
            if day and month:
                return _safe_date(int(year), month, int(day), text, confidence=1.0)

        m = _TEXTUAL_DASH_RE.match(match_text)
        if m:
            day, month_name, year = m.groups()
            month = _MONTH_LOOKUP.get(month_name.lower())
            if month:
                return _safe_date(_expand_year(year), month, int(day), text, confidence=1.0)

        m = _NUMERIC_SLASH_RE.match(match_text)
        if m:
            a, b, year = int(m.group(1)), int(m.group(2)), _expand_year(m.group(3))
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
