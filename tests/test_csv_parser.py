import os
from decimal import Decimal

from statement_agent.ingest.csv_parser import parse_csv
from statement_agent.schema import Direction, EconomicType

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public", "expenses")


class TestRealDataset:
    def test_personal_expenses_parses_all_rows(self):
        r = parse_csv(os.path.join(DATASET, "personal_expenses_q2_2025.csv"))
        assert len(r.transactions) == 6
        assert len(r.rejected_rows) == 0

    def test_usd_row_keeps_usd_currency_not_inr(self):
        r = parse_csv(os.path.join(DATASET, "personal_expenses_q2_2025.csv"))
        chatgpt = next(t for t in r.transactions if "CHATGPT" in (t.merchant_raw or ""))
        assert chatgpt.currency == "USD"
        assert chatgpt.amount == Decimal("20.00")

    def test_ambiguous_slash_date_resolved_to_july_not_may(self):
        # "05/07/2025" sits among ISO/textual June-July dates in this file with no
        # other numeric-slash date to disambiguate -> falls back to DMY locale default -> 5 July
        r = parse_csv(os.path.join(DATASET, "personal_expenses_q2_2025.csv"))
        zomato = next(t for t in r.transactions if t.merchant_raw == "ZOMATO ONLINE ORDER")
        assert zomato.transaction_date.month == 7
        assert zomato.transaction_date.day == 5

    def test_team_reimbursements_currency_column_respected(self):
        r = parse_csv(os.path.join(DATASET, "team_reimbursements_jul2025.csv"))
        hyatt = next(t for t in r.transactions if t.merchant_raw == "GRAND HYATT")
        assert hyatt.currency == "USD"
        assert hyatt.amount == Decimal("340.00")
        assert "currency inferred" not in hyatt.notes  # came from an explicit column, not a guess


class TestMalformedCsv:
    def test_missing_headers_rejects_whole_file_gracefully(self):
        r = parse_csv(os.path.join(FIXTURES, "malformed_missing_headers.csv"))
        assert len(r.transactions) == 0
        assert any("missing required column" in w for w in r.document.parse_warnings)

    def test_partial_bad_rows_dont_block_good_rows(self):
        r = parse_csv(os.path.join(FIXTURES, "mixed_valid_and_bad_rows.csv"))
        assert len(r.transactions) == 2  # TRUFFLES and DMART parse fine
        assert len(r.rejected_rows) == 2  # bad amount, missing date
        reasons = [row["reason"] for row in r.rejected_rows]
        assert any("amount" in r_ for r_ in reasons)
        assert any("missing" in r_ for r_ in reasons)

    def test_missing_file_raises_not_silently_empty(self):
        import pytest

        with pytest.raises(FileNotFoundError):
            parse_csv(os.path.join(FIXTURES, "does_not_exist.csv"))


class TestTimestampColumnWithTrailingTime:
    """A real downloaded dataset (Kaggle's Financial Anomaly Data) uses a "Timestamp"
    header with "DD-MM-YYYY HH:MM" values — neither the column name nor the trailing
    time-of-day were previously recognized, so every row would be rejected outright."""

    def test_timestamp_header_is_recognized_as_the_date_column(self):
        r = parse_csv(os.path.join(FIXTURES, "timestamp_column.csv"))
        assert len(r.rejected_rows) == 0
        assert len(r.transactions) == 3

    def test_trailing_time_of_day_does_not_block_parsing(self):
        r = parse_csv(os.path.join(FIXTURES, "timestamp_column.csv"))
        first = r.transactions[0]
        assert first.transaction_date.year == 2023
        assert first.transaction_date.month == 1
        assert first.transaction_date.day == 1

    def test_merchant_column_still_recognized_alongside_timestamp(self):
        r = parse_csv(os.path.join(FIXTURES, "timestamp_column.csv"))
        assert r.transactions[0].merchant_raw == "MerchantH"
        assert r.transactions[0].amount == Decimal("95071.92")


class TestVerboseHeaderNames:
    """A real user-uploaded fraud-detection-style CSV ("Transaction Date and Time",
    "Transaction Amount", "Merchant Name", "Transaction Currency") was rejected outright
    with "missing required column(s): ['date', 'amount']" — none of these longer,
    more descriptive header names were in the alias lists."""

    def test_all_rows_parse_with_no_rejections(self):
        r = parse_csv(os.path.join(FIXTURES, "verbose_header_names.csv"))
        assert len(r.document.parse_warnings) == 0
        assert len(r.rejected_rows) == 0
        assert len(r.transactions) == 3

    def test_trailing_time_of_day_does_not_block_parsing(self):
        r = parse_csv(os.path.join(FIXTURES, "verbose_header_names.csv"))
        first = r.transactions[0]
        assert (first.transaction_date.year, first.transaction_date.month, first.transaction_date.day) == (2022, 9, 24)

    def test_merchant_name_column_recognized(self):
        r = parse_csv(os.path.join(FIXTURES, "verbose_header_names.csv"))
        assert r.transactions[0].merchant_raw == "Rajagopalan Ghose and Kant"

    def test_transaction_currency_column_respected_per_row(self):
        r = parse_csv(os.path.join(FIXTURES, "verbose_header_names.csv"))
        currencies = {t.merchant_raw: t.currency for t in r.transactions}
        assert currencies["Rajagopalan Ghose and Kant"] == "INR"
        assert currencies["Konda-Sodhi"] == "USD"
        assert currencies["Sule PLC"] == "EUR"


class TestCategoryAndAccountColumns:
    """A real uploaded file (DECISIONS.md §32) has its own "Category" and "Account Name"
    columns spanning 3 accounts (Platinum Card, Silver Card, Checking) — both were
    previously silently discarded (Category got smuggled into freeform notes; Account Name
    wasn't captured anywhere at all)."""

    def test_category_declared_captured_separately_from_notes(self):
        r = parse_csv(os.path.join(FIXTURES, "category_and_account_columns.csv"))
        amazon = next(t for t in r.transactions if t.merchant_raw == "Amazon")
        assert amazon.category_declared == "Shopping"
        assert "Shopping" not in amazon.notes  # no longer smuggled into freeform notes

    def test_account_name_captured_per_row(self):
        r = parse_csv(os.path.join(FIXTURES, "category_and_account_columns.csv"))
        accounts = {t.merchant_raw: t.account_name for t in r.transactions}
        assert accounts["Amazon"] == "Platinum Card"
        assert accounts["Thai Restaurant"] == "Silver Card"
        assert accounts["Biweekly Paycheck"] == "Checking"


class TestExplicitTransactionTypeColumn:
    """The same real file also has a "Transaction Type" (debit/credit) column that
    normalize_amount can never see, since it only ever looks at the amount string itself
    (no minus sign or CR/DR suffix on any row here). Without reading this column, income
    rows like "Biweekly Paycheck" (amount="2298.09", Transaction Type="credit") were
    silently defaulting to DEBIT/PURCHASE — counted as spend rather than excluded from it,
    a real bug found live (DECISIONS.md §32)."""

    def test_explicit_credit_overrides_the_amount_strings_inferred_debit(self):
        r = parse_csv(os.path.join(FIXTURES, "category_and_account_columns.csv"))
        paycheck = next(t for t in r.transactions if t.merchant_raw == "Biweekly Paycheck")
        assert paycheck.direction == Direction.CREDIT
        assert paycheck.economic_type == EconomicType.REFUND  # excluded from aggregate_spending's default PURCHASE-only total

    def test_explicit_debit_rows_unaffected(self):
        r = parse_csv(os.path.join(FIXTURES, "category_and_account_columns.csv"))
        amazon = next(t for t in r.transactions if t.merchant_raw == "Amazon")
        assert amazon.direction == Direction.DEBIT
        assert amazon.economic_type == EconomicType.PURCHASE

    def test_no_transaction_type_column_falls_back_to_amount_strings_own_sign(self):
        # personal_expenses_q2_2025.csv has no such column — must be unaffected
        r = parse_csv(os.path.join(DATASET, "personal_expenses_q2_2025.csv"))
        assert len(r.transactions) == 6
        assert len(r.rejected_rows) == 0
