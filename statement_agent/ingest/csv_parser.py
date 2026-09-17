"""CSV (and, via xlsx_parser, Excel) statement / expense-sheet parsing entry point.

This module no longer owns header semantics. Structure comes from sniff.py (encoding, delimiter,
header row, candidate tables), column meaning from mapping.py (scored role inference with
evidence, or a mapping the user confirmed), and row normalization from tabular.py. What stays
here is the stable result shape callers already depend on — including `rejected_rows`, which is
now simply the rows whose outcome is a blocking validation issue.

Called without a mapping (the non-interactive CLI path), an inferred mapping that needs a person
to confirm it produces no transactions at all rather than a guess.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from ..schema import Document, ExtractionMethod, RawTransactionCandidate, Transaction, ValidationIssue
from .mapping import ColumnMapping, infer_mapping
from .sniff import SniffResult, TableCandidate, sniff_csv
from .tabular import ReviewDecisions, normalize_table


@dataclass
class CsvParseResult:
    document: Document
    transactions: list[Transaction]
    rejected_rows: list[dict] = field(default_factory=list)  # {"row": int, "raw": dict, "reason": str}
    issues: list[ValidationIssue] = field(default_factory=list)
    candidates: list[RawTransactionCandidate] = field(default_factory=list)
    mapping: ColumnMapping | None = None
    table: TableCandidate | None = None
    sniff: SniffResult | None = None
    extra_columns: list = field(default_factory=list)


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sniffed(
    path: str,
    sniffed: SniffResult,
    *,
    extraction_method: ExtractionMethod,
    mapping: ColumnMapping | None = None,
    decisions: ReviewDecisions | None = None,
    profile: dict | None = None,
    default_currency: str = "INR",
) -> CsvParseResult:
    fhash = file_hash(path)
    table = sniffed.best
    if mapping is not None and (mapping.sheet is not None or mapping.header_row is not None):
        table = sniffed.select(mapping.sheet, mapping.header_row) or table

    if table is None:
        document = Document(str(uuid.uuid4()), path, fhash, "expense_sheet", reconciliation_status="NO_TOTALS")
        document.parse_warnings.append("missing required column(s): ['date', 'amount']; no table with dates and amounts found")
        return CsvParseResult(document=document, transactions=[], sniff=sniffed)

    mapping = mapping or infer_mapping(table, profile=profile)
    if mapping.needs_confirmation and mapping.source != "user":
        document = Document(str(uuid.uuid4()), path, fhash, "expense_sheet", reconciliation_status="NO_TOTALS")
        if mapping.missing_required:
            document.parse_warnings.append(
                f"missing required column(s): {mapping.missing_required}; headers seen: {table.headers}"
            )
        for a in mapping.ambiguities:
            document.parse_warnings.append(f"column mapping needs confirmation: {a}")
        return CsvParseResult(document=document, transactions=[], mapping=mapping, table=table, sniff=sniffed)

    result = normalize_table(
        table, mapping, path=path, fhash=fhash, extraction_method=extraction_method,
        default_currency=default_currency, decisions=decisions,
    )
    rejected = [
        {"row": c.source_row, "raw": c.cells, "reason": c.reason} for c in result.candidates if c.outcome == "issue"
    ]
    if rejected:
        result.document.parse_warnings.append(f"{len(rejected)} row(s) rejected during parse — see rejected_rows")
    return CsvParseResult(
        document=result.document, transactions=result.transactions, rejected_rows=rejected, issues=result.issues,
        candidates=result.candidates, mapping=mapping, table=table, sniff=sniffed,
        extra_columns=result.extra_columns,
    )


def parse_csv(path: str, *, default_currency: str = "INR", mapping: ColumnMapping | None = None,
              decisions: ReviewDecisions | None = None, profile: dict | None = None) -> CsvParseResult:
    return parse_sniffed(
        path, sniff_csv(path), extraction_method=ExtractionMethod.CSV_ROW, mapping=mapping,
        decisions=decisions, profile=profile, default_currency=default_currency,
    )
