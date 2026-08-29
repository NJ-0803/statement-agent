import os
from decimal import Decimal

from statement_agent.ingest.xlsx_parser import parse_xlsx

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestXlsxParsing:
    def test_all_real_rows_parsed_and_blank_row_skipped(self):
        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        assert len(r.transactions) == 4  # 5 data rows in the sheet, one is fully blank

    def test_native_excel_date_cell_converted_correctly(self):
        # the critical case this parser exists to get right: openpyxl returns a real
        # datetime.date object for a date cell, not a string — must not become "2025-06-21 00:00:00"
        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        truffles = next(t for t in r.transactions if t.merchant_raw == "TRUFFLES RESTAURANT BLR")
        assert truffles.transaction_date.isoformat() == "2025-06-21"

    def test_datetime_cell_with_time_component_still_resolves_to_correct_date(self):
        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        dmart = next(t for t in r.transactions if t.merchant_raw == "DMART AVENUE")
        assert dmart.transaction_date.isoformat() == "2025-07-11"

    def test_amounts_parsed_as_exact_decimal(self):
        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        dmart = next(t for t in r.transactions if t.merchant_raw == "DMART AVENUE")
        assert dmart.amount == Decimal("1120.50")
        truffles = next(t for t in r.transactions if t.merchant_raw == "TRUFFLES RESTAURANT BLR")
        assert truffles.amount == Decimal("1340.00")

    def test_extraction_method_is_xlsx_row(self):
        from statement_agent.schema import ExtractionMethod

        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        assert all(t.source.extraction_method == ExtractionMethod.XLSX_ROW for t in r.transactions)

    def test_header_alias_matching_works_same_as_csv(self):
        # "Txn Date" / "Details" — same alias set csv_parser.py already handles
        r = parse_xlsx(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"))
        assert r.document.parse_warnings == []
        assert all(t.merchant_raw for t in r.transactions)


class TestXlsxRobustness:
    def test_missing_required_columns_rejected_gracefully_not_crashed(self):
        r = parse_xlsx(os.path.join(FIXTURES, "missing_columns_sample.xlsx"))
        assert len(r.transactions) == 0
        assert any("missing required column" in w for w in r.document.parse_warnings)
