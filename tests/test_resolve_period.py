from datetime import date

from statement_agent.agent.tools import resolve_period


class TestExplicitPeriods:
    def test_explicit_month(self):
        r = resolve_period("2025-06")
        assert r["start"] == "2025-06-01"
        assert r["end"] == "2025-06-30"

    def test_explicit_quarter(self):
        r = resolve_period("2025-Q2")
        assert r["start"] == "2025-04-01"
        assert r["end"] == "2025-06-30"

    def test_explicit_year(self):
        r = resolve_period("2025")
        assert r["start"] == "2025-01-01"
        assert r["end"] == "2025-12-31"

    def test_february_leap_year_end_date(self):
        r = resolve_period("2024-02")
        assert r["end"] == "2024-02-29"

    def test_february_non_leap_year_end_date(self):
        r = resolve_period("2025-02")
        assert r["end"] == "2025-02-28"


class TestRelativePeriods:
    def test_this_month(self):
        r = resolve_period("this_month", as_of=date(2025, 7, 15))
        assert r["start"] == "2025-07-01" and r["end"] == "2025-07-31"

    def test_last_month_normal_case(self):
        r = resolve_period("last_month", as_of=date(2025, 7, 15))
        assert r["start"] == "2025-06-01" and r["end"] == "2025-06-30"

    def test_last_month_year_boundary(self):
        # "last month" asked in January must resolve to December of the PREVIOUS year
        r = resolve_period("last_month", as_of=date(2025, 1, 15))
        assert r["start"] == "2024-12-01" and r["end"] == "2024-12-31"

    def test_this_quarter(self):
        r = resolve_period("this_quarter", as_of=date(2025, 5, 1))  # May -> Q2
        assert r["start"] == "2025-04-01" and r["end"] == "2025-06-30"

    def test_last_quarter_normal_case(self):
        r = resolve_period("last_quarter", as_of=date(2025, 7, 1))  # July -> Q3, last = Q2
        assert r["start"] == "2025-04-01" and r["end"] == "2025-06-30"

    def test_last_quarter_year_boundary(self):
        # EC-43: "last quarter" asked while currently in Q1 must resolve to Q4 of the PREVIOUS year
        r = resolve_period("last_quarter", as_of=date(2025, 2, 10))  # Feb -> Q1
        assert r["start"] == "2024-10-01" and r["end"] == "2024-12-31"

    def test_this_year(self):
        r = resolve_period("this_year", as_of=date(2025, 6, 1))
        assert r["start"] == "2025-01-01" and r["end"] == "2025-12-31"

    def test_last_year(self):
        r = resolve_period("last_year", as_of=date(2025, 6, 1))
        assert r["start"] == "2024-01-01" and r["end"] == "2024-12-31"

    def test_last_30_days(self):
        r = resolve_period("last_30_days", as_of=date(2025, 6, 30))
        assert r["start"] == "2025-05-31" and r["end"] == "2025-06-30"


class TestUnrecognizedPeriod:
    def test_garbage_input_returns_error_not_a_guess(self):
        r = resolve_period("sometime last spring")
        assert r["start"] is None and r["end"] is None
        assert "error" in r
