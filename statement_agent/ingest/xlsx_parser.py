"""XLSX (Excel) expense/reimbursement sheet parser.

Structurally, an Excel sheet is the same problem as a CSV — tabular rows with
a header row whose column names vary sheet to sheet. Rather than duplicate
csv_parser.py's header-alias matching and row->Transaction logic, this reads
the sheet into the same (headers, rows-as-dicts) shape CSV parsing produces
and hands off to csv_parser.parse_tabular_rows — the only genuinely different
code here is getting cell values out of openpyxl in the first place.

One real wrinkle CSV never has: Excel stores dates and numbers as native
typed cell values, not text. A date cell comes back from openpyxl as a
`datetime.date`/`datetime.datetime` object — converting it with a naive
`str()` would produce "2025-06-21 00:00:00", which the ISO date regex in
normalize.py rejects (it requires nothing after the day). Cell values are
converted to strings deliberately here, not left to a generic str() call.
"""

from __future__ import annotations

import datetime as _dt

import openpyxl

from .csv_parser import CsvParseResult, file_hash, parse_tabular_rows
from ..schema import ExtractionMethod


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d") if value.time() == _dt.time() else value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        return str(value)  # Python's shortest round-trip repr — no scientific notation for normal amounts
    return str(value).strip()


def parse_xlsx(path: str, *, default_currency: str = "INR", sheet_name: str | None = None) -> CsvParseResult:
    fhash = file_hash(path)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = wb[sheet_name] if sheet_name else wb.active
        rows_iter = sheet.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        headers = [_cell_to_str(h) for h in (header_row or [])]

        rows: list[dict] = []
        for raw_row in rows_iter:
            if raw_row is None or all(v is None for v in raw_row):
                continue  # a genuinely blank row — not a transaction with a blank description
            row_dict = {}
            for header, value in zip(headers, raw_row):
                if not header:
                    continue
                row_dict[header] = _cell_to_str(value)
            rows.append(row_dict)
    finally:
        wb.close()

    return parse_tabular_rows(
        headers, rows, path=path, fhash=fhash, default_currency=default_currency,
        extraction_method=ExtractionMethod.XLSX_ROW,
    )
