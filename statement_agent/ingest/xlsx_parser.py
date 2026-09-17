"""XLSX (Excel) statement / expense-sheet parser.

Structurally the same problem as a CSV, with two differences handled in sniff.py: a workbook can
hold several sheets (a cover sheet, one sheet per account, a hidden metadata sheet), so every sheet
is scanned and candidate tables are ranked across all of them rather than trusting the active one;
and Excel stores dates/numbers as typed cell values, converted deliberately (sniff.cell_to_str).
Everything after that is csv_parser.parse_sniffed — one row-normalization path for both formats.
"""

from __future__ import annotations

from ..schema import ExtractionMethod
from .csv_parser import CsvParseResult, parse_sniffed
from .mapping import ColumnMapping
from .sniff import sniff_xlsx
from .tabular import ReviewDecisions


def parse_xlsx(path: str, *, default_currency: str = "INR", sheet_name: str | None = None,
               mapping: ColumnMapping | None = None, decisions: ReviewDecisions | None = None,
               profile: dict | None = None) -> CsvParseResult:
    sniffed = sniff_xlsx(path)
    if sheet_name is not None:
        sniffed.candidates = [c for c in sniffed.candidates if c.sheet == sheet_name]
    return parse_sniffed(
        path, sniffed, extraction_method=ExtractionMethod.XLSX_ROW, mapping=mapping,
        decisions=decisions, profile=profile, default_currency=default_currency,
    )
