"""Tests for fx.py against the actual bundled ECB rate file — no mocking, since
there's no network call to mock anymore. These are real assertions against the
real shipped data, which is a stronger test than mocking a fabricated response.
"""

from datetime import date
from decimal import Decimal

from statement_agent import fx


class TestCoverage:
    def test_file_covers_the_full_dataset_date_range(self):
        earliest, latest = fx.coverage()
        assert earliest <= "2025-05-01"
        assert latest >= "2025-07-31"


class TestGetFxRate:
    def test_identity_conversion(self):
        rate = fx.get_fx_rate("INR", "INR", date(2025, 7, 19))
        assert rate.rate == Decimal("1")
        assert rate.source == "identity"

    def test_real_dataset_transaction_aws_dollar_120(self):
        # AMAZON WEB SERVICES USD 120.00, 2025-07-19 (a Saturday — ECB doesn't
        # publish weekend rates, so this must fall back to the prior trading day)
        rate = fx.get_fx_rate("USD", "INR", date(2025, 7, 19))
        assert rate is not None
        assert rate.rate_date == "2025-07-18"
        assert rate.requested_date == "2025-07-19"
        assert Decimal("85") < rate.rate < Decimal("87")  # sanity band around the known real rate (~86.14)

    def test_weekday_date_needs_no_fallback(self):
        rate = fx.get_fx_rate("USD", "INR", date(2025, 7, 24))  # a Thursday
        assert rate.rate_date == rate.requested_date == "2025-07-24"

    def test_unsupported_currency_returns_none(self):
        assert fx.get_fx_rate("XXX", "INR", date(2025, 7, 19)) is None

    def test_date_before_file_coverage_returns_none_not_a_guess(self):
        assert fx.get_fx_rate("USD", "INR", date(1990, 1, 1)) is None

    def test_cross_rate_is_internally_consistent(self):
        # USD->INR should equal 1 / (INR->USD) for the same date, since both are
        # derived from the same EUR-based row
        usd_to_inr = fx.get_fx_rate("USD", "INR", date(2025, 7, 24))
        inr_to_usd = fx.get_fx_rate("INR", "USD", date(2025, 7, 24))
        product = usd_to_inr.rate * inr_to_usd.rate
        assert abs(product - Decimal("1")) < Decimal("0.0001")

    def test_rate_precision_is_bounded_not_a_raw_division_artifact(self):
        rate = fx.get_fx_rate("USD", "INR", date(2025, 7, 24))
        # Decimal division of two 8-sig-fig quotes can produce ~28 digits if left
        # unrounded — this checks it was actually rounded to a sane FX-quote precision
        assert rate.rate.as_tuple().exponent >= -8


class TestConvertAmount:
    def test_converts_real_transaction_using_its_own_date_rate(self):
        # AMAZON WEB SERVICES, USD 120.00, 2025-07-19 — a real row in dataset_public/
        result = fx.convert_amount(Decimal("120.00"), "USD", "INR", date(2025, 7, 19))
        converted, rate = result
        assert converted == (Decimal("120.00") * rate.rate).quantize(Decimal("0.01"))
        assert Decimal("10000") < converted < Decimal("10700")  # sanity band, not an exact pin to one day's rate

    def test_failed_lookup_returns_none_never_a_fallback_number(self):
        assert fx.convert_amount(Decimal("120.00"), "XXX", "INR", date(2025, 7, 19)) is None

    def test_identity_currency_conversion_is_exact(self):
        result = fx.convert_amount(Decimal("500.00"), "INR", "INR", date(2025, 7, 19))
        converted, rate = result
        assert converted == Decimal("500.00")
        assert rate.rate == Decimal("1")

    def test_different_days_use_different_rates_not_one_blended_rate(self):
        # the whole point of per-transaction conversion: two USD charges on
        # different real dataset dates must not silently use the same rate
        r1 = fx.convert_amount(Decimal("100"), "USD", "INR", date(2025, 7, 12))
        r2 = fx.convert_amount(Decimal("100"), "USD", "INR", date(2025, 7, 24))
        assert r1[1].rate != r2[1].rate
