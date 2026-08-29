import os
import tempfile
from datetime import date
from decimal import Decimal

from statement_agent.agent.tools import (
    aggregate_spending,
    compare_periods,
    dataset_coverage,
    find_disputable_transactions,
    list_documents,
    search_transactions,
    summarize_statement,
)
from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.store import Store

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


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
        results = search_transactions(ledger, merchant_contains="swiggy")
        # 3 in the July statement (incl. the same-day repeat) + 1 each in May and June
        assert len(results) == 5

    def test_date_range_filter_excludes_outside_range(self):
        ledger = _ledger()
        results = search_transactions(ledger, date_from=date(2025, 7, 1), date_to=date(2025, 7, 31))
        assert all(date.fromisoformat(t.date) >= date(2025, 7, 1) for t in results if t.date)
        assert all(date.fromisoformat(t.date) <= date(2025, 7, 31) for t in results if t.date)


class TestSortAndLimit:
    """EC-22: 'biggest expense' must be answerable deterministically (sorted in code),
    never by the model eyeballing a list and picking the largest itself."""

    def test_amount_desc_returns_largest_single_transaction_first(self):
        ledger = _ledger()
        results = search_transactions(ledger, sort_by="amount_desc", limit=1)
        assert len(results) == 1
        assert results[0].merchant == "GRANDEUR JEWELLERS PVT"  # the known ₹80,000 outlier
        assert results[0].amount == "80000.00"

    def test_amount_asc_is_the_reverse_order(self):
        # compare by amount value, not exact id sequence — Python's stable sort keeps
        # tied amounts (e.g. the two same-day ₹850 Swiggy charges) in original relative
        # order in BOTH directions, so reversing desc-by-id isn't exactly asc-by-id
        ledger = _ledger()
        desc = search_transactions(ledger, sort_by="amount_desc")
        asc = search_transactions(ledger, sort_by="amount_asc")
        assert [t.amount for t in desc] == [t.amount for t in asc][::-1]

    def test_limit_without_sort_still_truncates(self):
        ledger = _ledger()
        results = search_transactions(ledger, limit=3)
        assert len(results) == 3

    def test_no_sort_or_limit_returns_everything_unmodified(self):
        ledger = _ledger()
        all_results = search_transactions(ledger)
        assert len(all_results) == len(ledger)


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
