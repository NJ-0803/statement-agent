"""CSV expense/reimbursement sheet parser.

Column names vary sheet to sheet (`Txn Date` vs `date`, `Details` vs `merchant`),
so headers are matched against alias lists rather than assumed fixed. Rows that
can't be parsed are rejected individually with a reason, never silently dropped
or allowed to crash the whole file (EC: malformed CSV must fail gracefully).
"""

from __future__ import annotations

import csv
import hashlib
import uuid
from dataclasses import dataclass, field

from ..normalize import DocumentDateResolver, normalize_amount
from ..schema import Direction, Document, EconomicType, ExtractionMethod, SourceRef, Transaction

_DATE_ALIASES = {"date", "txn date", "transaction date", "posted date", "timestamp", "transaction date and time"}
_DESC_ALIASES = {"details", "merchant", "description", "narration", "particulars", "merchant name"}
_AMOUNT_ALIASES = {"amount", "amt", "value", "transaction amount"}
_CURRENCY_ALIASES = {"currency", "ccy", "transaction currency"}
_NOTES_ALIASES = {"notes", "note", "remarks", "transaction notes"}
# Kept separate from _NOTES_ALIASES: a column literally named "Category" is a declared
# spend-category label, not freeform text — folding it into `notes` would make it
# unreadable to assign_categories()'s declared-category fallback (see resolve.py).
_CATEGORY_ALIASES = {"category"}
# A source file can span more than one of the account holder's own accounts/cards in a
# single sheet (e.g. "Platinum Card", "Checking") — captured so it stays queryable/
# groupable instead of silently discarded (see DECISIONS.md §32).
_ACCOUNT_ALIASES = {"account", "account name"}
# Some expense sheets state debit/credit explicitly in its own column rather than via a
# sign or CR/DR suffix on the amount itself (normalize_amount only ever looks at the
# amount string — it has no way to see this column). Without reading it, a row like
# amount="2000", "Transaction Type"="credit" silently defaults to DEBIT/PURCHASE — a
# real bug found ingesting income rows ("Biweekly Paycheck") that were being counted as
# spend (see DECISIONS.md §32). An explicit value here always overrides the amount
# string's own inferred sign, the same way an explicit currency column already does.
_DIRECTION_ALIASES = {"transaction type", "type"}
_CREDIT_VALUES = {"credit", "cr"}
_DEBIT_VALUES = {"debit", "dr"}


@dataclass
class CsvParseResult:
    document: Document
    transactions: list[Transaction]
    rejected_rows: list[dict] = field(default_factory=list)  # {"row": int, "raw": dict, "reason": str}


def _match_column(headers: list[str], aliases: set[str]) -> str | None:
    lowered = {h: h.strip().lower() for h in headers}
    for header, low in lowered.items():
        if low in aliases:
            return header
    return None


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_csv(path: str, *, default_currency: str = "INR") -> CsvParseResult:
    fhash = file_hash(path)

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
    except UnicodeDecodeError:
        with open(path, newline="", encoding="latin-1") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)

    return parse_tabular_rows(
        headers, rows, path=path, fhash=fhash, default_currency=default_currency,
        extraction_method=ExtractionMethod.CSV_ROW,
    )


def parse_tabular_rows(
    headers: list[str],
    rows: list[dict],
    *,
    path: str,
    fhash: str,
    default_currency: str = "INR",
    extraction_method: ExtractionMethod = ExtractionMethod.CSV_ROW,
) -> CsvParseResult:
    """The shared row->Transaction logic behind both parse_csv (CSV rows) and
    xlsx_parser.parse_xlsx (Excel rows) — a tabular expense sheet is a tabular
    expense sheet regardless of container format; only how `headers`/`rows` were
    read from disk differs between the two callers.
    """
    document = Document(
        document_id=str(uuid.uuid4()),
        file_path=path,
        file_hash=fhash,
        doc_type="expense_sheet",
        currency_declared=None,
        reconciliation_status="NO_TOTALS",
    )

    if not headers:
        document.parse_warnings.append("no header row detected; file rejected")
        return CsvParseResult(document=document, transactions=[], rejected_rows=[])

    date_col = _match_column(headers, _DATE_ALIASES)
    desc_col = _match_column(headers, _DESC_ALIASES)
    amount_col = _match_column(headers, _AMOUNT_ALIASES)
    currency_col = _match_column(headers, _CURRENCY_ALIASES)
    notes_col = _match_column(headers, _NOTES_ALIASES)
    category_col = _match_column(headers, _CATEGORY_ALIASES)
    account_col = _match_column(headers, _ACCOUNT_ALIASES)
    direction_col = _match_column(headers, _DIRECTION_ALIASES)

    missing = [name for name, col in [("date", date_col), ("amount", amount_col)] if col is None]
    if missing:
        document.parse_warnings.append(f"missing required column(s): {missing}; headers seen: {headers}")
        return CsvParseResult(document=document, transactions=[], rejected_rows=[])

    date_resolver = DocumentDateResolver()
    for row in rows:
        if date_col in row and row[date_col]:
            date_resolver.observe(row[date_col])
    date_resolver.resolve_convention()

    transactions: list[Transaction] = []
    rejected: list[dict] = []

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        raw_date = (row.get(date_col) or "").strip()
        raw_amount = (row.get(amount_col) or "").strip()
        row_currency_hint = (row.get(currency_col) or "").strip().upper() if currency_col else ""

        if not raw_date or not raw_amount:
            rejected.append({"row": i, "raw": row, "reason": "missing date or amount"})
            continue

        parsed_date = date_resolver.parse(raw_date)
        if parsed_date.value is None:
            rejected.append({"row": i, "raw": row, "reason": f"unparseable date: {raw_date!r}"})
            continue

        row_currency_explicit = row_currency_hint in ("INR", "USD", "EUR", "GBP")
        row_default_currency = row_currency_hint if row_currency_explicit else default_currency
        parsed_amount = normalize_amount(raw_amount, default_currency=row_default_currency)
        if parsed_amount is None:
            rejected.append({"row": i, "raw": row, "reason": f"unparseable amount: {raw_amount!r}"})
            continue
        # normalize_amount only knows the amount string, not that a CSV currency column
        # supplied the fallback — a value taken from an explicit column isn't "inferred".
        if row_currency_explicit:
            parsed_amount.currency_inferred = False

        description = (row.get(desc_col) or "").strip() if desc_col else ""
        notes = (row.get(notes_col) or "").strip() if notes_col else ""
        category_declared = (row.get(category_col) or "").strip() if category_col else ""
        account_name = (row.get(account_col) or "").strip() if account_col else ""

        direction = parsed_amount.direction
        if direction_col:
            direction_hint = (row.get(direction_col) or "").strip().lower()
            if direction_hint in _CREDIT_VALUES:
                direction = Direction.CREDIT
            elif direction_hint in _DEBIT_VALUES:
                direction = Direction.DEBIT
            # any other value (blank, or something unrecognized) — same lenient no-op as
            # an unrecognized sort_by elsewhere: fall back to the amount string's own sign

        txn = Transaction(
            transaction_id=str(uuid.uuid4()),
            document_id=document.document_id,
            transaction_date=parsed_date.value,
            date_raw=raw_date,
            date_plausible=True,  # bounded later once we know statement period; expense sheets have none
            description_raw=description or "(blank description)",
            merchant_raw=description or None,
            amount=parsed_amount.amount,
            currency=parsed_amount.currency,
            amount_raw=raw_amount,
            direction=direction,
            economic_type=EconomicType.PURCHASE if direction == Direction.DEBIT else EconomicType.REFUND,
            notes=notes,
            category_declared=category_declared or None,
            account_name=account_name or None,
            source=SourceRef(
                file_path=path,
                file_hash=fhash,
                row=i,
                raw_text=str(row),
                extraction_method=extraction_method,
                extraction_confidence=1.0,
            ),
        )
        if parsed_date.confidence < 1.0:
            txn.notes = (txn.notes + f" | date assumption: {parsed_date.assumption}").strip(" |")
        if parsed_amount.currency_inferred:
            txn.notes = (txn.notes + " | currency inferred (not stated on row)").strip(" |")
        transactions.append(txn)

    if rejected:
        document.parse_warnings.append(f"{len(rejected)} row(s) rejected during parse — see rejected_rows")

    return CsvParseResult(document=document, transactions=transactions, rejected_rows=rejected)
