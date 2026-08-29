"""Deterministic query/aggregation functions over the ledger.

These are the ONLY things that ever compute a financial number in this system.
The LLM decides which of these to call and with what arguments, and narrates
the result — it never does the arithmetic itself. Every function here is a
plain Python function operating on an in-memory transaction list, so it's
directly unit-testable with no LLM/API involvement.

Currency is never silently blended: every aggregate is broken out per currency.
"Verified" total only includes transactions with no open question attached to
them (not flagged as a probable duplicate, not date-implausible); everything
else is reported separately as "uncertain" with a reason, which is how the
Answer Stability range gets its two ends.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..schema import Direction, EconomicType, Transaction


def _is_clean(t: Transaction) -> bool:
    """A transaction with no open question attached — safe to include in a verified total."""
    return t.duplicate_of is None and t.date_plausible


@dataclass
class TxnView:
    """JSON-serializable view of a transaction, for tool results and citations."""

    transaction_id: str
    date: str | None
    merchant: str | None
    description: str
    amount: str
    currency: str
    direction: str
    economic_type: str
    category: str | None
    source_file: str
    source_page: int | None
    source_row: int | None
    notes: str
    is_duplicate_flag: bool
    date_plausible: bool


def _view(t: Transaction) -> TxnView:
    src = t.source
    return TxnView(
        transaction_id=t.transaction_id,
        date=t.transaction_date.isoformat() if t.transaction_date else None,
        merchant=t.merchant_raw,
        description=t.description_raw,
        amount=str(t.amount),
        currency=t.currency,
        direction=t.direction.value,
        economic_type=t.economic_type.value,
        category=t.category,
        source_file=src.file_path if src else "",
        source_page=src.page if src else None,
        source_row=src.row if src else None,
        notes=t.notes,
        is_duplicate_flag=t.duplicate_of is not None,
        date_plausible=t.date_plausible,
    )


def _in_range(t: Transaction, date_from: date | None, date_to: date | None) -> bool:
    if t.transaction_date is None:
        return False
    if date_from and t.transaction_date < date_from:
        return False
    if date_to and t.transaction_date > date_to:
        return False
    return True


def search_transactions(
    ledger: list[Transaction],
    *,
    category: str | None = None,
    economic_types: tuple[str, ...] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    merchant_contains: str | None = None,
    currency: str | None = None,
    include_flagged: bool = True,
    sort_by: str | None = None,  # "amount_desc" | "amount_asc" | "date_desc" | "date_asc"
    limit: int | None = None,
) -> list[TxnView]:
    """`sort_by`/`limit` exist so a question like 'what's my single biggest expense'
    can be answered by sorting deterministically in code (e.g. sort_by="amount_desc",
    limit=1) — never by the model eyeballing a list and picking the largest itself,
    same 'no mental math' principle as every aggregate number.
    """
    results = []
    for t in ledger:
        if category and t.category != category:
            continue
        if economic_types and t.economic_type.value not in economic_types:
            continue
        if date_from or date_to:
            if not _in_range(t, date_from, date_to):
                continue
        if merchant_contains and merchant_contains.lower() not in (t.merchant_raw or "").lower():
            continue
        if currency and t.currency != currency:
            continue
        if not include_flagged and not _is_clean(t):
            continue
        results.append(t)

    if sort_by == "amount_desc":
        results.sort(key=lambda t: t.amount, reverse=True)
    elif sort_by == "amount_asc":
        results.sort(key=lambda t: t.amount)
    elif sort_by == "date_desc":
        results.sort(key=lambda t: t.transaction_date or date.min, reverse=True)
    elif sort_by == "date_asc":
        results.sort(key=lambda t: t.transaction_date or date.min)

    if limit is not None:
        results = results[:limit]

    return [_view(t) for t in results]


@dataclass
class CurrencyTotal:
    verified_total: str
    uncertain_total: str
    verified_count: int
    uncertain_count: int
    uncertain_reasons: list[str] = field(default_factory=list)


@dataclass
class AggregateResult:
    by_currency: dict[str, CurrencyTotal]
    verified_transaction_ids: list[str]
    uncertain_transaction_ids: list[str]
    group_breakdown: dict[str, dict[str, str]] | None = None  # {group_key: {currency: total}}
    # Completeness signal for a category-filtered query: this system filters the whole
    # ledger deterministically rather than doing lossy semantic retrieval, so the
    # realistic way a category total under-counts is a categorization gap — a real
    # dining transaction whose merchant isn't in the keyword list stays category=None
    # and is silently excluded. These fields surface that possibility instead of
    # letting a category total look complete when it might not be.
    possibly_missing_uncategorized_count: int = 0
    possibly_missing_uncategorized_ids: list[str] = field(default_factory=list)
    # Set only when convert_to is passed. The per-currency breakdown above is NEVER
    # replaced by this — a converted combined total is reported ALONGSIDE the honest
    # per-currency figures, never instead of them.
    converted: "ConvertedTotal | None" = None
    conversion_details: list["ConversionDetail"] = field(default_factory=list)


@dataclass
class ConversionDetail:
    transaction_id: str
    original_amount: str
    original_currency: str
    converted_amount: str
    rate: str
    rate_date: str  # the date the rate is actually quoted for (may differ from the transaction's own date on a weekend/holiday)
    source: str


@dataclass
class ConvertedTotal:
    currency: str
    verified_total: str
    uncertain_total: str
    conversion_count: int
    failed_conversion_count: int
    failed_conversion_ids: list[str] = field(default_factory=list)


def aggregate_spending(
    ledger: list[Transaction],
    *,
    category: str | None = None,
    economic_types: tuple[str, ...] = ("PURCHASE",),
    date_from: date | None = None,
    date_to: date | None = None,
    currency: str | None = None,
    group_by: str | None = None,  # "month" | "category" | "merchant" | None
    convert_to: str | None = None,
) -> AggregateResult:
    matched = [
        t
        for t in ledger
        if (category is None or t.category == category)
        and t.economic_type.value in economic_types
        and (date_from is None and date_to is None or _in_range(t, date_from, date_to))
        and (currency is None or t.currency == currency)
    ]

    by_currency: dict[str, CurrencyTotal] = {}
    verified_ids, uncertain_ids = [], []

    for ccy in sorted({t.currency for t in matched}):
        clean = [t for t in matched if t.currency == ccy and _is_clean(t)]
        unclean = [t for t in matched if t.currency == ccy and not _is_clean(t)]
        verified_total = sum((t.amount for t in clean), Decimal("0"))
        uncertain_total = sum((t.amount for t in unclean), Decimal("0"))
        reasons = []
        for t in unclean:
            if t.duplicate_of is not None:
                reasons.append(f"{t.transaction_id[:8]}: possible duplicate ({t.amount} {t.currency}, {t.merchant_raw})")
            if not t.date_plausible:
                reasons.append(f"{t.transaction_id[:8]}: implausible date ({t.amount} {t.currency}, {t.merchant_raw})")

        by_currency[ccy] = CurrencyTotal(
            verified_total=str(verified_total),
            uncertain_total=str(uncertain_total),
            verified_count=len(clean),
            uncertain_count=len(unclean),
            uncertain_reasons=reasons,
        )
        verified_ids.extend(t.transaction_id for t in clean)
        uncertain_ids.extend(t.transaction_id for t in unclean)

    group_breakdown = None
    if group_by:
        group_breakdown = {}
        for t in matched:
            if group_by == "month" and t.transaction_date:
                key = t.transaction_date.strftime("%Y-%m")
            elif group_by == "category":
                key = t.category or "UNCATEGORIZED"
            elif group_by == "merchant":
                key = t.merchant_raw or "UNKNOWN"
            else:
                continue
            group_breakdown.setdefault(key, {})
            group_breakdown[key][t.currency] = str(
                Decimal(group_breakdown[key].get(t.currency, "0")) + t.amount
            )

    possibly_missing_ids: list[str] = []
    if category is not None:
        same_scope_uncategorized = [
            t
            for t in ledger
            if t.category is None
            and t.economic_type.value in economic_types
            and (date_from is None and date_to is None or _in_range(t, date_from, date_to))
            and (currency is None or t.currency == currency)
        ]
        possibly_missing_ids = [t.transaction_id for t in same_scope_uncategorized]

    converted = None
    conversion_details: list[ConversionDetail] = []
    if convert_to:
        from ..fx import convert_amount

        convert_to = convert_to.upper()
        conv_verified = Decimal("0")
        conv_uncertain = Decimal("0")
        conv_count = 0
        failed_ids: list[str] = []

        for t in matched:
            if t.transaction_date is None:
                failed_ids.append(t.transaction_id)
                continue
            result = convert_amount(t.amount, t.currency, convert_to, t.transaction_date)
            if result is None:
                failed_ids.append(t.transaction_id)
                continue
            converted_amount, rate = result
            conversion_details.append(ConversionDetail(
                transaction_id=t.transaction_id,
                original_amount=str(t.amount),
                original_currency=t.currency,
                converted_amount=str(converted_amount),
                rate=str(rate.rate),
                rate_date=rate.rate_date,
                source=rate.source,
            ))
            if _is_clean(t):
                conv_verified += converted_amount
            else:
                conv_uncertain += converted_amount
            conv_count += 1

        converted = ConvertedTotal(
            currency=convert_to,
            verified_total=str(conv_verified),
            uncertain_total=str(conv_uncertain),
            conversion_count=conv_count,
            failed_conversion_count=len(failed_ids),
            failed_conversion_ids=failed_ids,
        )

    return AggregateResult(
        by_currency, verified_ids, uncertain_ids, group_breakdown,
        possibly_missing_uncategorized_count=len(possibly_missing_ids),
        possibly_missing_uncategorized_ids=possibly_missing_ids,
        converted=converted,
        conversion_details=conversion_details,
    )


def compare_periods(
    ledger: list[Transaction],
    *,
    period_a: tuple[date, date],
    period_b: tuple[date, date],
    category: str | None = None,
    currency: str | None = None,
) -> dict[str, AggregateResult]:
    return {
        "period_a": aggregate_spending(ledger, category=category, date_from=period_a[0], date_to=period_a[1], currency=currency),
        "period_b": aggregate_spending(ledger, category=category, date_from=period_b[0], date_to=period_b[1], currency=currency),
    }


@dataclass
class DocumentView:
    file_path: str
    doc_type: str
    account_label: str | None
    currency_declared: str | None
    statement_start: str | None
    statement_end: str | None
    reconciliation_status: str
    transaction_count: int
    warnings: list[str] = field(default_factory=list)


def list_documents(ledger: list[Transaction], documents: list[dict]) -> list[DocumentView]:
    """Discover which source documents/statements exist — filename, bank/account label,
    period, transaction count, and any parse-time warnings (security flags, data-quality
    flags like an unusual overlapping-cycle structure, extraction issues). Without this,
    the agent has no way to answer 'what does the Cobalt statement say' unless 'Cobalt'
    happens to appear as a merchant string inside a transaction — a bank/institution name
    in a filename or header is not something search_transactions can find. And without the
    `warnings` field specifically, real ingest-time signals (like a document flagged for
    looking like two statements merged into one file) never reach the conversation at all —
    they'd otherwise sit unused in the database.
    """
    counts: dict[str, int] = {}
    for t in ledger:
        if t.source and t.source.file_path:
            counts[t.source.file_path] = counts.get(t.source.file_path, 0) + 1

    return [
        DocumentView(
            file_path=d["file_path"],
            doc_type=d["doc_type"],
            account_label=d.get("account_label"),
            currency_declared=d.get("currency_declared"),
            statement_start=d.get("statement_start"),
            statement_end=d.get("statement_end"),
            reconciliation_status=d.get("reconciliation_status", "NOT_CHECKED"),
            transaction_count=counts.get(d["file_path"], 0),
            warnings=[w for w in (d.get("parse_warnings") or "").split("\n") if w],
        )
        for d in documents
    ]


def find_disputable_transactions(ledger: list[Transaction]) -> list[TxnView]:
    """Duplicates and anomaly-flagged rows — anything with FLAGGED: or duplicate_of set."""
    return [
        _view(t)
        for t in ledger
        if t.duplicate_of is not None or "FLAGGED:" in t.notes or not t.date_plausible
    ]


def summarize_statement(ledger: list[Transaction], *, source_file: str) -> dict:
    rows = [t for t in ledger if t.source and t.source.file_path == source_file]
    if not rows:
        return {"found": False, "source_file": source_file}

    by_currency: dict[str, dict[str, str]] = {}
    for t in rows:
        bucket = by_currency.setdefault(t.currency, {"debits": "0", "credits": "0"})
        key = "debits" if t.direction == Direction.DEBIT else "credits"
        bucket[key] = str(Decimal(bucket[key]) + t.amount)

    by_category: dict[str, str] = {}
    for t in rows:
        if t.economic_type == EconomicType.PURCHASE and t.category:
            by_category[t.category] = str(Decimal(by_category.get(t.category, "0")) + t.amount)

    flagged = [t for t in rows if t.duplicate_of is not None or "FLAGGED:" in t.notes]

    return {
        "found": True,
        "source_file": source_file,
        "transaction_count": len(rows),
        "by_currency": by_currency,
        "by_category": by_category,
        "flagged_count": len(flagged),
        "flagged_transaction_ids": [t.transaction_id for t in flagged],
    }


def get_sources(ledger: list[Transaction], transaction_ids: list[str]) -> list[TxnView]:
    by_id = {t.transaction_id: t for t in ledger}
    return [_view(by_id[tid]) for tid in transaction_ids if tid in by_id]


def dataset_coverage(ledger: list[Transaction]) -> dict:
    """What date range and currencies the ledger actually covers — used to answer
    'insufficient information' honestly instead of guessing about a period with no data.
    """
    dated = [t.transaction_date for t in ledger if t.transaction_date is not None]
    if not dated:
        return {"min_date": None, "max_date": None, "currencies": []}
    return {
        "min_date": min(dated).isoformat(),
        "max_date": max(dated).isoformat(),
        "currencies": sorted({t.currency for t in ledger}),
        "transaction_count": len(ledger),
    }


_EXPLICIT_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_EXPLICIT_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_EXPLICIT_YEAR_RE = re.compile(r"^(\d{4})$")


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    end_day = calendar.monthrange(year, end_month)[1]
    return date(year, start_month, 1), date(year, end_month, end_day)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def resolve_period(period: str, *, as_of: date | None = None) -> dict:
    """Deterministically resolves a named or explicit period into (start, end) ISO dates,
    so the agent never has to compute date arithmetic itself — including the easy-to-get-
    wrong year-boundary case: 'last quarter' asked in Q1 must resolve to Q4 of the
    PREVIOUS year, not Q0 or the current year's Q4.

    Accepts: 'this_month', 'last_month', 'this_quarter', 'last_quarter', 'this_year',
    'last_year', 'last_7_days', 'last_30_days', 'last_90_days', or an explicit
    'YYYY-MM', 'YYYY-QN', or 'YYYY'.
    """
    today = as_of or date.today()

    m = _EXPLICIT_MONTH_RE.match(period)
    if m:
        start, end = _month_bounds(int(m.group(1)), int(m.group(2)))
        return {"start": start.isoformat(), "end": end.isoformat(), "resolved_as": period}

    m = _EXPLICIT_QUARTER_RE.match(period)
    if m:
        start, end = _quarter_bounds(int(m.group(1)), int(m.group(2)))
        return {"start": start.isoformat(), "end": end.isoformat(), "resolved_as": period}

    m = _EXPLICIT_YEAR_RE.match(period)
    if m:
        year = int(m.group(1))
        return {"start": date(year, 1, 1).isoformat(), "end": date(year, 12, 31).isoformat(), "resolved_as": period}

    key = period.strip().lower()
    current_quarter = (today.month - 1) // 3 + 1

    if key == "this_month":
        start, end = _month_bounds(today.year, today.month)
    elif key == "last_month":
        first_of_this_month = date(today.year, today.month, 1)
        last_month_end = first_of_this_month - timedelta(days=1)
        start, end = _month_bounds(last_month_end.year, last_month_end.month)
    elif key == "this_quarter":
        start, end = _quarter_bounds(today.year, current_quarter)
    elif key == "last_quarter":
        if current_quarter == 1:
            start, end = _quarter_bounds(today.year - 1, 4)  # year-boundary case
        else:
            start, end = _quarter_bounds(today.year, current_quarter - 1)
    elif key == "this_year":
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    elif key == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif key == "last_7_days":
        start, end = today - timedelta(days=7), today
    elif key == "last_30_days":
        start, end = today - timedelta(days=30), today
    elif key == "last_90_days":
        start, end = today - timedelta(days=90), today
    else:
        return {"start": None, "end": None, "resolved_as": None, "error": f"unrecognized period: {period!r}"}

    return {"start": start.isoformat(), "end": end.isoformat(), "resolved_as": key}
