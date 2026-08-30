import os
import tempfile
from datetime import date
from decimal import Decimal

import pytest

from statement_agent.agent.tools import (
    aggregate_spending,
    compare_periods,
    compute,
    dataset_coverage,
    find_disputable_transactions,
    generate_chart,
    generate_dashboard,
    list_documents,
    search_transactions,
    summarize_statement,
    top_n_per_group,
)
from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.schema import Direction, EconomicType, Transaction
from statement_agent.store import Store

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


def _make_txn(txn_date: date, *, amount: str = "100.00", merchant: str = "TEST MERCHANT", account: str | None = None) -> Transaction:
    import uuid

    return Transaction(
        transaction_id=str(uuid.uuid4()),
        document_id="doc1",
        transaction_date=txn_date,
        date_raw=txn_date.isoformat(),
        description_raw=merchant,
        merchant_raw=merchant,
        amount=Decimal(amount),
        currency="INR",
        direction=Direction.DEBIT,
        economic_type=EconomicType.PURCHASE,
        account_name=account,
    )


def _ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store, attempt_vision=False)
    ledger = store.all_transactions()
    store.close()
    os.remove(path)
    return ledger


def _ledger_and_documents():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store, attempt_vision=False)
    ledger = store.all_transactions()
    documents = store.all_documents_as_dicts()
    store.close()
    os.remove(path)
    return ledger, documents


class TestListDocuments:
    def test_finds_cobalt_by_filename_when_not_a_merchant_string(self):
        # "Cobalt" is a bank name in the filename, never a merchant inside any transaction —
        # this is exactly the discovery gap list_documents exists to close
        ledger, documents = _ledger_and_documents()
        docs = list_documents(ledger, documents)
        cobalt = next(d for d in docs if "cobalt" in d.file_path.lower())
        assert cobalt.transaction_count == 44

    def test_security_warning_surfaced_on_cobalt_document(self):
        ledger, documents = _ledger_and_documents()
        docs = list_documents(ledger, documents)
        cobalt = next(d for d in docs if "cobalt" in d.file_path.lower())
        assert any("SECURITY" in w for w in cobalt.warnings)

    def test_data_quality_warning_surfaced_on_cobalt_document(self):
        # the overlapping-transaction-cycle structural flag must reach the agent via this tool,
        # not stay stranded in the database where it's never actually queried
        ledger, documents = _ledger_and_documents()
        docs = list_documents(ledger, documents)
        cobalt = next(d for d in docs if "cobalt" in d.file_path.lower())
        assert any("DATA QUALITY" in w for w in cobalt.warnings)

    def test_clean_statement_has_no_warnings(self):
        ledger, documents = _ledger_and_documents()
        docs = list_documents(ledger, documents)
        meridian_may = next(d for d in docs if "meridian_credit_card_may2025" in d.file_path)
        assert meridian_may.warnings == []

    def test_transaction_counts_match_actual_ledger(self):
        ledger, documents = _ledger_and_documents()
        docs = list_documents(ledger, documents)
        for d in docs:
            actual = len([t for t in ledger if t.source and t.source.file_path == d.file_path])
            assert d.transaction_count == actual


class TestNonPurchaseEconomicTypesExcludedFromSpend:
    def test_atm_withdrawal_never_counted_as_purchase_spend(self):
        # synthetic: no ATM withdrawal exists in dataset_public/, so this injects one
        # directly into a copy of the real ledger to prove aggregate_spending's default
        # economic_types=("PURCHASE",) correctly excludes it once refine_economic_type
        # has reclassified it as CASH_WITHDRAWAL.
        import uuid
        from statement_agent.resolve import refine_economic_type
        from statement_agent.schema import Direction, EconomicType, Transaction

        ledger = _ledger()
        before_total = aggregate_spending(ledger, category=None)
        before_inr = Decimal(before_total.by_currency["INR"].verified_total) + Decimal(before_total.by_currency["INR"].uncertain_total)

        atm = Transaction(
            transaction_id=str(uuid.uuid4()), document_id="synthetic", transaction_date=date(2025, 6, 15),
            date_raw="2025-06-15", description_raw="ATM CASH WITHDRAWAL", merchant_raw="ATM CASH WITHDRAWAL",
            amount=Decimal("10000.00"), currency="INR", direction=Direction.DEBIT, economic_type=EconomicType.PURCHASE,
        )
        refine_economic_type(atm)
        assert atm.economic_type == EconomicType.CASH_WITHDRAWAL  # sanity: refinement actually happened

        ledger_with_atm = ledger + [atm]
        after_total = aggregate_spending(ledger_with_atm, category=None)  # default economic_types=("PURCHASE",)
        after_inr = Decimal(after_total.by_currency["INR"].verified_total) + Decimal(after_total.by_currency["INR"].uncertain_total)

        assert after_inr == before_inr  # the 10,000 withdrawal must NOT have inflated "spend"


class TestCurrencyConversion:
    """convert_to on aggregate_spending — the exact scenario named directly: 'what if
    someone wants sum of transactions and one of the transactions is in another currency'.
    """

    def test_july_total_converted_to_inr_matches_hand_computed_figure(self):
        ledger = _ledger()
        result = aggregate_spending(
            ledger, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), convert_to="INR"
        )
        # hand-computed: 102978.00 (INR-native) + 1727.85 + 29179.53 + 10336.58 (the three USD legs,
        # each converted at its OWN transaction date's real ECB rate) = 144221.96
        assert result.converted.currency == "INR"
        assert Decimal(result.converted.verified_total) == Decimal("144221.96")
        assert result.converted.failed_conversion_count == 0

    def test_original_per_currency_breakdown_is_never_replaced_by_conversion(self):
        ledger = _ledger()
        result = aggregate_spending(
            ledger, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), convert_to="INR"
        )
        # the honest per-currency figures must still be there, untouched, alongside the converted total
        assert "INR" in result.by_currency and "USD" in result.by_currency
        assert Decimal(result.by_currency["INR"].verified_total) == Decimal("102978.00")
        assert Decimal(result.by_currency["USD"].verified_total) == Decimal("480.00")

    def test_uncertain_split_preserved_through_conversion(self):
        ledger = _ledger()
        result = aggregate_spending(
            ledger, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), convert_to="INR"
        )
        # the two duplicate-flagged July transactions are INR-native (identity conversion) and
        # must stay in the uncertain bucket of the converted total too, not silently verified
        assert Decimal(result.converted.uncertain_total) == Decimal("1110.00")

    def test_different_dates_use_different_rates_not_one_blended_rate(self):
        ledger = _ledger()
        result = aggregate_spending(
            ledger, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), convert_to="INR"
        )
        usd_details = [d for d in result.conversion_details if d.original_currency == "USD"]
        assert len(usd_details) == 3
        rates_used = {d.rate for d in usd_details}
        assert len(rates_used) == 3  # three different transaction dates, three different real rates

    def test_no_convert_to_means_no_conversion_fields_populated(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        assert result.converted is None
        assert result.conversion_details == []

    def test_unconvertible_currency_disclosed_not_silently_dropped(self):
        import uuid

        from statement_agent.resolve import refine_economic_type
        from statement_agent.schema import Direction, EconomicType, Transaction

        ledger = _ledger()
        fake = Transaction(
            transaction_id=str(uuid.uuid4()), document_id="synthetic", transaction_date=date(2025, 7, 15),
            date_raw="2025-07-15", description_raw="MYSTERY MERCHANT", merchant_raw="MYSTERY MERCHANT",
            amount=Decimal("100.00"), currency="XXX", direction=Direction.DEBIT, economic_type=EconomicType.PURCHASE,
        )
        ledger_with_fake = ledger + [fake]
        result = aggregate_spending(
            ledger_with_fake, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), convert_to="INR"
        )
        assert fake.transaction_id in result.converted.failed_conversion_ids
        assert result.converted.failed_conversion_count >= 1


class TestCurrencyIsNeverBlended:
    def test_usd_and_inr_reported_separately(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category=None)
        assert "INR" in result.by_currency
        assert "USD" in result.by_currency
        # sanity: no currency key like "MIXED" or a single blended total exists
        assert set(result.by_currency.keys()) <= {"INR", "USD", "EUR", "GBP"}

    def test_usd_total_matches_known_usd_transactions(self):
        # OpenAI $20 + AWS $120 + Grand Hyatt $340 = $480, all distinct transaction types
        ledger = _ledger()
        result = aggregate_spending(ledger, category=None, economic_types=("PURCHASE",))
        usd = result.by_currency.get("USD")
        assert usd is not None
        assert Decimal(usd.verified_total) + Decimal(usd.uncertain_total) == Decimal("480.00")


class TestVerifiedVsUncertainSplit:
    def test_duplicate_swiggy_charge_excluded_from_verified_dining_total(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        inr = result.by_currency["INR"]
        assert Decimal(inr.uncertain_total) == Decimal("850.00")
        assert inr.uncertain_count == 1
        assert "duplicate" in inr.uncertain_reasons[0]

    def test_verified_plus_uncertain_equals_naive_sum(self):
        # the split must partition the data, not lose or double-count any of it
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        dining_txns = [t for t in ledger if t.category == "Dining"]
        naive_total = sum((t.amount for t in dining_txns), Decimal("0"))
        inr = result.by_currency["INR"]
        assert Decimal(inr.verified_total) + Decimal(inr.uncertain_total) == naive_total


class TestCompareperiods:
    def test_june_vs_july_dining_are_independent_and_correct(self):
        ledger = _ledger()
        cmp = compare_periods(
            ledger, category="Dining",
            period_a=(date(2025, 6, 1), date(2025, 6, 30)),
            period_b=(date(2025, 7, 1), date(2025, 7, 31)),
        )
        june_total = Decimal(cmp["period_a"].by_currency["INR"].verified_total)
        july_total = Decimal(cmp["period_b"].by_currency["INR"].verified_total)
        assert june_total > 0
        assert july_total > 0
        assert june_total != july_total  # sanity: not accidentally comparing the same slice twice


class TestRetrievalCompletenessSignal:
    """EC-26: correct arithmetic on an incomplete retrieval must not look like a
    complete answer. Two real transactions in this dataset are genuinely
    uncategorized (GRAND HYATT and GRANDEUR JEWELLERS PVT, both July 2025) — a
    category-filtered total for that period must disclose that they exist and
    weren't checked, rather than silently presenting the categorized total as final.
    """

    def test_category_filtered_july_query_flags_the_two_uncategorized_transactions(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining", date_from=date(2025, 7, 1), date_to=date(2025, 7, 31))
        assert result.possibly_missing_uncategorized_count == 2
        assert len(result.possibly_missing_uncategorized_ids) == 2

    def test_completeness_signal_respects_currency_filter(self):
        ledger = _ledger()
        # GRAND HYATT is USD; only GRANDEUR JEWELLERS (INR) should count when currency=INR
        result = aggregate_spending(
            ledger, category="Dining", date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), currency="INR"
        )
        assert result.possibly_missing_uncategorized_count == 1

    def test_no_category_filter_means_no_completeness_concern(self):
        # category=None already includes every purchase regardless of category, so
        # there's nothing that could have been silently excluded by categorization
        ledger = _ledger()
        result = aggregate_spending(ledger, category=None, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31))
        assert result.possibly_missing_uncategorized_count == 0

    def test_period_with_no_uncategorized_transactions_reports_zero(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining", date_from=date(2025, 5, 1), date_to=date(2025, 5, 31))
        assert result.possibly_missing_uncategorized_count == 0


class TestDisputableTransactions:
    def test_finds_the_known_duplicate_and_the_known_outlier(self):
        ledger = _ledger()
        disputable = find_disputable_transactions(ledger)
        merchants = [t.merchant for t in disputable]
        assert any("SWIGGY" in (m or "") for m in merchants)
        assert any("GRANDEUR" in (m or "") for m in merchants)


class TestSearchTransactions:
    def test_merchant_filter_is_case_insensitive(self):
        ledger = _ledger()
        results = search_transactions(ledger, merchant_contains="swiggy").results
        # 3 in the July statement (incl. the same-day repeat) + 1 each in May and June
        assert len(results) == 5

    def test_date_range_filter_excludes_outside_range(self):
        ledger = _ledger()
        results = search_transactions(ledger, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31)).results
        assert all(date.fromisoformat(t.date) >= date(2025, 7, 1) for t in results if t.date)
        assert all(date.fromisoformat(t.date) <= date(2025, 7, 31) for t in results if t.date)


class TestAccountFilterAndGrouping:
    """A real uploaded file (DECISIONS.md §32) spans 3 of the account holder's own
    accounts/cards in one sheet (Platinum Card, Silver Card, Checking) — previously
    silently discarded, now captured as account_name and queryable/groupable the same
    way category already is."""

    def _three_account_ledger(self):
        return [
            _make_txn(date(2025, 1, 1), amount="100.00", merchant="AMAZON", account="Platinum Card"),
            _make_txn(date(2025, 1, 2), amount="50.00", merchant="NETFLIX", account="Platinum Card"),
            _make_txn(date(2025, 1, 3), amount="200.00", merchant="RENT", account="Checking"),
            _make_txn(date(2025, 1, 4), amount="30.00", merchant="ZOMATO", account="Silver Card"),
            _make_txn(date(2025, 1, 5), amount="20.00", merchant="UNLABELED"),  # no declared account
        ]

    def test_search_transactions_account_filter(self):
        ledger = self._three_account_ledger()
        results = search_transactions(ledger, account="Platinum Card").results
        assert {r.merchant for r in results} == {"AMAZON", "NETFLIX"}

    def test_search_transactions_account_filter_excludes_rows_with_no_declared_account(self):
        ledger = self._three_account_ledger()
        results = search_transactions(ledger, account="Checking").results
        assert len(results) == 1
        assert results[0].merchant == "RENT"

    def test_aggregate_spending_account_filter_scopes_the_total(self):
        ledger = self._three_account_ledger()
        result = aggregate_spending(ledger, account="Platinum Card")
        assert result.by_currency["INR"].verified_total == "150.00"

    def test_aggregate_spending_group_by_account(self):
        ledger = self._three_account_ledger()
        result = aggregate_spending(ledger, group_by="account")
        assert result.group_breakdown["Platinum Card"]["INR"] == "150.00"
        assert result.group_breakdown["Checking"]["INR"] == "200.00"
        assert result.group_breakdown["Silver Card"]["INR"] == "30.00"
        assert result.group_breakdown["UNKNOWN ACCOUNT"]["INR"] == "20.00"

    def test_top_n_per_group_group_by_account(self):
        ledger = self._three_account_ledger()
        result = top_n_per_group(ledger, group_by="account", n=5, currency="INR")
        assert set(result["groups"].keys()) == {"Platinum Card", "Checking", "Silver Card", "UNKNOWN ACCOUNT"}
        assert result["groups"]["Platinum Card"][0]["merchant"] == "AMAZON"  # higher amount ranks first

    def test_generate_chart_group_by_account(self, tmp_path, monkeypatch):
        monkeypatch.setattr("statement_agent.agent.tools._CHARTS_DIR", str(tmp_path))
        ledger = self._three_account_ledger()
        result = generate_chart(ledger, chart_type="bar", group_by="account", currency="INR")
        assert "error" not in result
        assert result["data"]["Platinum Card"] == "150.00"


class TestSortAndLimit:
    """EC-22: 'biggest expense' must be answerable deterministically (sorted in code),
    never by the model eyeballing a list and picking the largest itself."""

    def test_amount_desc_returns_largest_single_transaction_first(self):
        ledger = _ledger()
        results = search_transactions(ledger, sort_by="amount_desc", limit=1).results
        assert len(results) == 1
        assert results[0].merchant == "GRANDEUR JEWELLERS PVT"  # the known ₹80,000 outlier
        assert results[0].amount == "80000.00"

    def test_amount_asc_is_the_reverse_order(self):
        # compare by amount value, not exact id sequence — Python's stable sort keeps
        # tied amounts (e.g. the two same-day ₹850 Swiggy charges) in original relative
        # order in BOTH directions, so reversing desc-by-id isn't exactly asc-by-id
        ledger = _ledger()
        desc = search_transactions(ledger, sort_by="amount_desc").results
        asc = search_transactions(ledger, sort_by="amount_asc").results
        assert [t.amount for t in desc] == [t.amount for t in asc][::-1]

    def test_limit_without_sort_still_truncates(self):
        ledger = _ledger()
        results = search_transactions(ledger, limit=3).results
        assert len(results) == 3

    def test_no_sort_or_limit_returns_everything_unmodified(self):
        ledger = _ledger()
        result = search_transactions(ledger)
        assert len(result.results) == len(ledger)
        assert result.total_matched == len(ledger)
        assert result.truncated is False
        assert result.limit_applied is None

    def test_extraction_order_reveals_the_real_meridian_july_statement_is_not_date_sorted(self):
        # the real case this sort_by option exists for: a live question ("is this
        # statement sorted by date?") got a false-positive "yes" from sorting by date
        # and observing the (trivially, circularly) sorted result. Checked honestly
        # here via extraction_order, scoped to July the same way the agent does —
        # see DECISIONS.md for the incident this fixes.
        ledger = _ledger()
        by_extraction = search_transactions(
            ledger, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), sort_by="extraction_order"
        ).results
        by_date = search_transactions(
            ledger, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31), sort_by="date_asc"
        ).results

        # date_asc is trivially always sorted by date — that's the whole bug. The real
        # question is whether extraction_order matches it, and here it must not.
        assert [t.date for t in by_extraction] != [t.date for t in by_date]

    def test_closest_to_amount_finds_the_nearest_transaction(self):
        # real case this exists for: "which transaction is closest to the average of my
        # highest and lowest" — sort_by=amount_desc/asc find the extremes, compute()
        # finds the midpoint, and this finds the transaction nearest that midpoint
        ledger = _ledger()
        highest = search_transactions(ledger, sort_by="amount_desc", limit=1).results[0]
        lowest = search_transactions(ledger, sort_by="amount_asc", limit=1).results[0]
        midpoint = compute("average", [highest.amount, lowest.amount])["result"]

        closest = search_transactions(ledger, sort_by="closest_to_amount", target_amount=midpoint, limit=1).results
        assert len(closest) == 1
        # the closest transaction's distance from the midpoint must be <= every other
        # transaction's distance — verified independently, not just "some result came back"
        from decimal import Decimal

        target = Decimal(midpoint)
        closest_distance = abs(Decimal(closest[0].amount) - target)
        all_distances = [abs(Decimal(t.amount) - target) for t in search_transactions(ledger).results]
        assert closest_distance == min(all_distances)

    def test_closest_to_amount_without_a_target_is_a_lenient_noop(self):
        # missing/invalid target_amount must not crash — same lenient no-op as any
        # other inapplicable sort_by
        ledger = _ledger()
        result = search_transactions(ledger, sort_by="closest_to_amount")
        assert result.total_matched == len(ledger)


class TestCompute:
    """New tool: deterministic arithmetic over numbers the model already has, so a
    simple derived value (an average, a difference) never has to be either mental
    math (forbidden) or an unanswerable question."""

    def test_average_of_two_values(self):
        assert compute("average", ["10", "20"])["result"] == "15"

    def test_average_of_several_values(self):
        # Decimal division preserves the inputs' precision rather than normalizing it
        # away — "20.00", not "20", consistent with how money is represented everywhere
        # else in this system (never silently reformatted)
        result = compute("average", ["10.00", "20.00", "30.00"])
        assert result["result"] == "20.00"

    def test_sum(self):
        assert compute("sum", ["10.50", "20.25"])["result"] == "30.75"

    def test_difference_is_ordered_not_absolute(self):
        assert compute("difference", ["30", "10"])["result"] == "20"
        assert compute("difference", ["10", "30"])["result"] == "-20"

    def test_difference_requires_exactly_two_values(self):
        result = compute("difference", ["10", "20", "30"])
        assert "error" in result

    def test_min_and_max(self):
        assert compute("min", ["30", "10", "20"])["result"] == "10"
        assert compute("max", ["30", "10", "20"])["result"] == "30"

    def test_invalid_number_returns_error_not_a_crash(self):
        result = compute("average", ["10", "not a number"])
        assert "error" in result

    def test_unrecognized_operation_returns_error(self):
        result = compute("multiply", ["10", "20"])
        assert "error" in result

    def test_no_values_returns_error(self):
        result = compute("average", [])
        assert "error" in result

    def test_input_values_are_echoed_back_for_traceability(self):
        result = compute("average", ["10", "20"])
        assert result["input_values"] == ["10", "20"]


class TestGenerateChart:
    """generate_chart renders the SAME grouped totals aggregate_spending computes —
    it must never introduce a second aggregation path, and must never blend
    currencies into one chart any more than aggregate_spending does."""

    @pytest.fixture(autouse=True)
    def _use_tmp_charts_dir(self, tmp_path, monkeypatch):
        import statement_agent.agent.tools as tools_module

        monkeypatch.setattr(tools_module, "_CHARTS_DIR", str(tmp_path / "charts"))

    def test_bar_chart_creates_a_real_png_file(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="bar", group_by="category", currency="INR")
        assert "error" not in result
        assert os.path.exists(result["chart_path"])
        assert os.path.getsize(result["chart_path"]) > 0

    def test_line_chart_grouped_by_month(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="line", group_by="month", currency="INR")
        assert "error" not in result
        assert os.path.exists(result["chart_path"])

    def test_pie_chart_grouped_by_merchant(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="pie", group_by="merchant", currency="INR")
        assert "error" not in result
        assert os.path.exists(result["chart_path"])

    def test_data_matches_aggregate_spending_exactly(self):
        # the chart must never compute its own numbers — cross-check against the
        # same aggregate_spending call it's supposed to be reusing internally
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="bar", group_by="category", currency="INR")
        expected = aggregate_spending(ledger, currency="INR", group_by="category").group_breakdown
        for label, total in result["data"].items():
            assert total == expected[label]["INR"]

    def test_multi_currency_without_scoping_returns_an_error_not_a_blended_chart(self):
        ledger = _ledger()  # real dataset has both INR and USD
        result = generate_chart(ledger, chart_type="bar", group_by="category")
        assert "error" in result
        assert "currenc" in result["error"].lower()

    def test_scoping_to_one_currency_resolves_the_multi_currency_case(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="bar", group_by="category", currency="USD")
        assert "error" not in result
        assert result["currency"] == "USD"

    def test_unrecognized_chart_type_returns_error(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="scatter", group_by="category", currency="INR")
        assert "error" in result

    def test_unrecognized_group_by_returns_error(self):
        ledger = _ledger()
        result = generate_chart(ledger, chart_type="bar", group_by="bogus_dimension", currency="INR")
        assert "error" in result

    def test_no_matching_transactions_returns_error_not_an_empty_chart(self):
        ledger = _ledger()
        result = generate_chart(
            ledger, chart_type="bar", group_by="category", currency="INR",
            date_from=date(2099, 1, 1), date_to=date(2099, 12, 31),
        )
        assert "error" in result

    def test_pie_chart_rejects_negative_values(self, monkeypatch):
        # amounts are always positive magnitudes throughout this system in practice, so
        # a negative group total can't actually arise from real aggregate_spending output
        # today — this guard is defensive. Test it directly by monkeypatching
        # aggregate_spending's return value, rather than skip testing dead-in-practice
        # code or leave the guard unverified.
        import statement_agent.agent.tools as tools_module
        from statement_agent.agent.tools import AggregateResult, CurrencyTotal

        fake_result = AggregateResult(
            by_currency={"INR": CurrencyTotal(verified_total="50.00", uncertain_total="0", verified_count=2, uncertain_count=0)},
            verified_transaction_ids=[],
            uncertain_transaction_ids=[],
            group_breakdown={"Dining": {"INR": "-50.00"}, "Groceries": {"INR": "100.00"}},
        )
        monkeypatch.setattr(tools_module, "aggregate_spending", lambda *a, **kw: fake_result)

        result = generate_chart([], chart_type="pie", group_by="category", currency="INR")
        assert "error" in result
        assert "negative" in result["error"].lower()


class TestTopNPerGroup:
    """Closes a real gap: "top N in every category" needed one search_transactions call
    per category before this existed — close to the tool-call budget on a ledger with
    many categories. One deterministic call instead."""

    def test_top_2_per_category_is_correctly_ranked_within_each_group(self):
        ledger = _ledger()
        result = top_n_per_group(ledger, group_by="category", n=2, currency="INR")
        assert "error" not in result
        for group, rows in result["groups"].items():
            amounts = [Decimal(r["amount"]) for r in rows]
            assert amounts == sorted(amounts, reverse=True)  # each group's own rows are ranked
            assert len(rows) <= 2
            assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))

    def test_cross_checked_against_search_transactions_for_one_group(self):
        # the top-2 Dining rows here must match what search_transactions itself would
        # return for that same filter — never a second, independently-computed ranking
        ledger = _ledger()
        result = top_n_per_group(ledger, group_by="category", n=2, currency="INR")
        expected = search_transactions(
            ledger, category="Dining", currency="INR", sort_by="amount_desc", limit=2
        ).results
        actual = result["groups"]["Dining"]
        assert [r["amount"] for r in actual] == [e.amount for e in expected]
        assert [r["transaction_id"] for r in actual] == [e.transaction_id for e in expected]

    def test_multi_currency_without_scoping_returns_an_error(self):
        ledger = _ledger()
        result = top_n_per_group(ledger, group_by="category", n=5)
        assert "error" in result

    def test_unrecognized_group_by_returns_error(self):
        ledger = _ledger()
        result = top_n_per_group(ledger, group_by="bogus_dimension", n=5, currency="INR")
        assert "error" in result

    def test_n_less_than_one_returns_error(self):
        ledger = _ledger()
        result = top_n_per_group(ledger, group_by="category", n=0, currency="INR")
        assert "error" in result

    def test_no_matching_transactions_returns_error(self):
        ledger = _ledger()
        result = top_n_per_group(
            ledger, group_by="category", n=5, currency="INR",
            date_from=date(2099, 1, 1), date_to=date(2099, 12, 31),
        )
        assert "error" in result


class TestGenerateDashboard:
    """generate_dashboard combines generate_chart + top_n_per_group — never a third,
    separately-computed source of numbers."""

    @pytest.fixture(autouse=True)
    def _use_tmp_charts_dir(self, tmp_path, monkeypatch):
        import statement_agent.agent.tools as tools_module

        monkeypatch.setattr(tools_module, "_CHARTS_DIR", str(tmp_path / "charts"))

    def test_returns_both_a_real_chart_and_a_table(self):
        ledger = _ledger()
        result = generate_dashboard(ledger, group_by="category", top_n=3, currency="INR")
        assert "error" not in result
        assert os.path.exists(result["chart_path"])
        assert result["table_rows"]
        assert result["table_truncated"] is False

    def test_chart_data_matches_generate_chart_exactly(self):
        ledger = _ledger()
        dashboard = generate_dashboard(ledger, group_by="category", top_n=3, currency="INR")
        chart = generate_chart(ledger, chart_type="bar", group_by="category", currency="INR")
        assert dashboard["chart_data"] == chart["data"]

    def test_table_rows_match_top_n_per_group_exactly(self):
        ledger = _ledger()
        dashboard = generate_dashboard(ledger, group_by="category", top_n=3, currency="INR")
        table = top_n_per_group(ledger, group_by="category", n=3, currency="INR")
        expected_rows = [
            {"group": group, **row} for group, rows in table["groups"].items() for row in rows
        ]
        # order-independent comparison — both are built from the same dict iteration,
        # but the point being verified is content equality, not incidental ordering
        key = lambda r: (r["group"], r["rank"])
        assert sorted(dashboard["table_rows"], key=key) == sorted(expected_rows, key=key)

    def test_multi_currency_without_scoping_returns_an_error(self):
        ledger = _ledger()
        result = generate_dashboard(ledger, group_by="category", top_n=3)
        assert "error" in result


class TestSearchTransactionsTruncation:
    """New behavior for fix #1: search_transactions must never silently drop rows —
    when the match set exceeds the default cap, it must say so via total_matched/
    truncated/limit_applied rather than returning a partial list that looks complete."""

    @staticmethod
    def _big_ledger(n: int):
        import uuid
        from decimal import Decimal

        from statement_agent.schema import Direction, EconomicType, Transaction

        return [
            Transaction(
                transaction_id=str(uuid.uuid4()),
                document_id="doc_big",
                transaction_date=date(2025, 7, 1),
                date_raw="", description_raw="BULK MERCHANT", merchant_raw="BULK MERCHANT",
                amount=Decimal("10.00"), currency="INR",
                direction=Direction.DEBIT, economic_type=EconomicType.PURCHASE,
            )
            for _ in range(n)
        ]

    def test_default_limit_truncates_and_discloses_it(self):
        from statement_agent.agent.tools import DEFAULT_SEARCH_LIMIT

        ledger = self._big_ledger(DEFAULT_SEARCH_LIMIT + 50)
        result = search_transactions(ledger)
        assert result.total_matched == DEFAULT_SEARCH_LIMIT + 50
        assert len(result.results) == DEFAULT_SEARCH_LIMIT
        assert result.truncated is True
        assert result.limit_applied == DEFAULT_SEARCH_LIMIT

    def test_under_the_default_limit_is_not_truncated(self):
        from statement_agent.agent.tools import DEFAULT_SEARCH_LIMIT

        ledger = self._big_ledger(DEFAULT_SEARCH_LIMIT - 10)
        result = search_transactions(ledger)
        assert result.total_matched == DEFAULT_SEARCH_LIMIT - 10
        assert len(result.results) == DEFAULT_SEARCH_LIMIT - 10
        assert result.truncated is False
        assert result.limit_applied is None

    def test_explicit_limit_below_default_is_still_disclosed(self):
        ledger = self._big_ledger(10)
        result = search_transactions(ledger, limit=3)
        assert result.total_matched == 10
        assert len(result.results) == 3
        assert result.truncated is True
        assert result.limit_applied == 3


class TestSummarizeStatement:
    def test_unknown_file_returns_not_found_not_empty_success(self):
        ledger = _ledger()
        result = summarize_statement(ledger, source_file="does_not_exist.pdf")
        assert result["found"] is False

    def test_known_statement_summarizes_correctly(self):
        ledger = _ledger()
        path = os.path.join(DATASET, "statements", "meridian_credit_card_jul2025.pdf")
        result = summarize_statement(ledger, source_file=path)
        assert result["found"] is True
        assert result["transaction_count"] == 12
        assert result["flagged_count"] >= 1  # the Swiggy duplicate + Grandeur outlier


class TestDatasetCoverage:
    def test_reports_actual_min_and_max_dates(self):
        ledger = _ledger()
        coverage = dataset_coverage(ledger)
        assert coverage["min_date"] == "2025-05-14"
        assert coverage["max_date"] == "2025-07-28"

    def test_december_is_outside_coverage(self):
        # this is what the agent should check before answering a December question
        ledger = _ledger()
        coverage = dataset_coverage(ledger)
        assert coverage["max_date"] < "2025-12-01"

    def test_real_dataset_has_no_internal_gaps(self):
        # May-July are all present in this fixture (attempt_vision=False skips the
        # scanned April Axis statement entirely, so April is outside coverage, not a gap)
        ledger = _ledger()
        coverage = dataset_coverage(ledger)
        assert coverage["coverage_gaps"] == []

    def test_a_missing_quarter_is_detected_as_a_gap(self):
        # the real case this exists for: min/max alone would make Jan-Dec look like full
        # coverage even with Apr-Jun never uploaded
        txns = [
            _make_txn(date(2025, 1, 15)),
            _make_txn(date(2025, 2, 10)),
            _make_txn(date(2025, 7, 20)),
            _make_txn(date(2025, 12, 5)),
        ]
        coverage = dataset_coverage(txns)
        assert coverage["min_date"] == "2025-01-15"
        assert coverage["max_date"] == "2025-12-05"
        assert coverage["coverage_gaps"] == [
            {"start": "2025-03", "end": "2025-06"},
            {"start": "2025-08", "end": "2025-11"},
        ]

    def test_no_gap_reported_for_dates_outside_min_max(self):
        # a gap before the first or after the last transaction isn't a "gap" —
        # that's just outside the ledger's coverage, already disclosed via min/max
        txns = [_make_txn(date(2025, 6, 1)), _make_txn(date(2025, 6, 15))]
        coverage = dataset_coverage(txns)
        assert coverage["coverage_gaps"] == []
