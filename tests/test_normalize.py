from datetime import date
from decimal import Decimal

from statement_agent.normalize import DocumentDateResolver, is_date_plausible, normalize_amount
from statement_agent.schema import Direction


class TestNormalizeAmount:
    def test_rupee_symbol_with_thousands_separator(self):
        r = normalize_amount('"₹1,340.00"')
        assert r.amount == Decimal("1340.00")
        assert r.currency == "INR"
        assert r.direction == Direction.DEBIT

    def test_rs_prefix_no_decimal(self):
        r = normalize_amount("Rs 860")
        assert r.amount == Decimal("860")
        assert r.currency == "INR"

    def test_rs_dot_prefix(self):
        r = normalize_amount("Rs. 2,494.73")
        assert r.amount == Decimal("2494.73")
        assert r.currency == "INR"

    def test_inr_code_prefix(self):
        r = normalize_amount('"INR 1,120.00"')
        assert r.amount == Decimal("1120.00")
        assert r.currency == "INR"

    def test_usd_code_prefix(self):
        r = normalize_amount("USD 20.00")
        assert r.amount == Decimal("20.00")
        assert r.currency == "USD"
        assert r.currency_inferred is False

    def test_bare_number_infers_default_currency(self):
        r = normalize_amount("540.00", default_currency="INR")
        assert r.amount == Decimal("540.00")
        assert r.currency == "INR"
        assert r.currency_inferred is True

    def test_cr_suffix_is_credit(self):
        r = normalize_amount("8,000.00 CR")
        assert r.amount == Decimal("8000.00")
        assert r.direction == Direction.CREDIT

    def test_parens_is_credit(self):
        r = normalize_amount("(1,250.00)")
        assert r.direction == Direction.CREDIT
        assert r.amount == Decimal("1250.00")

    def test_leading_minus_is_credit(self):
        r = normalize_amount("-1250")
        assert r.direction == Direction.CREDIT
        assert r.amount == Decimal("1250")

    def test_trailing_minus_is_credit(self):
        # EC-08: "Rs 1,250-" is a real accounting negative-amount convention
        r = normalize_amount("Rs 1,250-")
        assert r.direction == Direction.CREDIT
        assert r.amount == Decimal("1250")

    def test_indian_lakh_grouping(self):
        r = normalize_amount("1,25,000.50")
        assert r.amount == Decimal("125000.50")

    def test_extremely_large_amount_no_precision_loss(self):
        # EC-36: Rs 99,99,999.99 (Indian lakh grouping) must stay exact via Decimal
        r = normalize_amount("99,99,999.99")
        assert r.amount == Decimal("9999999.99")
        assert r.amount * 3 == Decimal("29999999.97")  # would drift under float

    def test_none_and_empty_return_none(self):
        assert normalize_amount(None) is None
        assert normalize_amount("") is None
        assert normalize_amount("   ") is None

    def test_uses_exact_decimal_not_float(self):
        # 0.10 is not exactly representable in binary float — Decimal must be used throughout
        r = normalize_amount("0.10")
        assert r.amount == Decimal("0.10")
        assert (r.amount + r.amount + r.amount) == Decimal("0.30")


class TestDateResolver:
    def test_iso_format(self):
        resolver = DocumentDateResolver()
        p = resolver.parse("2025-06-21")
        assert p.value == date(2025, 6, 21)
        assert p.confidence == 1.0

    def test_textual_day_month_year(self):
        resolver = DocumentDateResolver()
        p = resolver.parse("23 Jun 2025")
        assert p.value == date(2025, 6, 23)

    def test_textual_month_day_year(self):
        resolver = DocumentDateResolver()
        p = resolver.parse("July 11, 2025")
        assert p.value == date(2025, 7, 11)

    def test_unambiguous_numeric_pins_convention(self):
        resolver = DocumentDateResolver()
        resolver.observe("14-05-2025")  # 14 > 12 -> unambiguously DMY
        resolver.resolve_convention()
        p = resolver.parse("14-05-2025")
        assert p.value == date(2025, 5, 14)
        assert p.confidence == 1.0

    def test_ambiguous_date_resolved_by_document_wide_evidence(self):
        resolver = DocumentDateResolver()
        for raw in ["03/06/2025", "21/06/2025", "05/07/2025"]:
            resolver.observe(raw)
        resolver.resolve_convention()
        # 21/06/2025 has day=21 > 12, pinning the whole document to DMY
        p = resolver.parse("05/07/2025")
        assert p.value == date(2025, 7, 5)
        assert p.assumption == "" or "document-wide evidence" in p.assumption or p.confidence == 1.0

    def test_fully_ambiguous_date_falls_back_to_locale_default(self):
        resolver = DocumentDateResolver()
        resolver.observe("05/07/2025")  # only date in the document, and it's ambiguous
        resolver.resolve_convention()
        p = resolver.parse("05/07/2025")
        assert p.value == date(2025, 7, 5)  # DMY locale default
        assert p.confidence < 1.0
        assert "locale default" in p.assumption

    def test_invalid_calendar_date_returns_none(self):
        resolver = DocumentDateResolver()
        p = resolver.parse("31/02/2025")  # Feb 31 doesn't exist
        assert p.value is None

    def test_unparseable_text_returns_none(self):
        resolver = DocumentDateResolver()
        p = resolver.parse("not a date")
        assert p.value is None


class TestDatePlausibility:
    def test_within_statement_period_is_plausible(self):
        assert is_date_plausible(
            date(2025, 6, 15), statement_start=date(2025, 6, 1), statement_end=date(2025, 6, 30)
        )

    def test_far_outside_statement_period_is_implausible(self):
        assert not is_date_plausible(
            date(1925, 6, 1), statement_start=date(2025, 6, 1), statement_end=date(2025, 6, 30)
        )

    def test_future_date_beyond_today_is_implausible(self):
        assert not is_date_plausible(date(2099, 1, 1), statement_start=None, statement_end=None, today=date(2025, 6, 1))

    def test_within_tolerance_of_statement_period_is_plausible(self):
        # a transaction dated a few days before/after the printed period (posting delay) shouldn't be rejected
        assert is_date_plausible(
            date(2025, 5, 30), statement_start=date(2025, 6, 1), statement_end=date(2025, 6, 30)
        )
