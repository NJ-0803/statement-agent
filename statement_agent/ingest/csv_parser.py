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

_DATE_ALIASES = {"date", "txn date", "transaction date", "posted date"}
_DESC_ALIASES = {"details", "merchant", "description", "narration", "particulars"}
_AMOUNT_ALIASES = {"amount", "amt", "value"}
_CURRENCY_ALIASES = {"currency", "ccy"}
_NOTES_ALIASES = {"notes", "note", "category", "remarks"}


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
    document = Document(
        document_id=str(uuid.uuid4()),
        file_path=path,
        file_hash=fhash,
        doc_type="expense_sheet",
        currency_declared=None,
        reconciliation_status="NO_TOTALS",
    )

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

    if not headers:
        document.parse_warnings.append("no header row detected; file rejected")
        return CsvParseResult(document=document, transactions=[], rejected_rows=[])

    date_col = _match_column(headers, _DATE_ALIASES)
    desc_col = _match_column(headers, _DESC_ALIASES)
    amount_col = _match_column(headers, _AMOUNT_ALIASES)
    currency_col = _match_column(headers, _CURRENCY_ALIASES)
    notes_col = _match_column(headers, _NOTES_ALIASES)

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
            direction=parsed_amount.direction,
            economic_type=EconomicType.PURCHASE if parsed_amount.direction == Direction.DEBIT else EconomicType.REFUND,
            notes=notes,
            source=SourceRef(
                file_path=path,
                file_hash=fhash,
                row=i,
                raw_text=str(row),
                extraction_method=ExtractionMethod.CSV_ROW,
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
