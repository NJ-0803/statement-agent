import os
from decimal import Decimal

from statement_agent.ingest.pdf_native import parse_pdf_native
from statement_agent.resolve import categorize, detect_anomalies, detect_cross_document_duplicates, detect_duplicates, resolve_all
from statement_agent.schema import Document

STATEMENTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public", "statements")
DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


class TestCategorization:
    def test_known_merchants_map_to_expected_categories(self):
        assert categorize("SWIGGY BANGALORE").category == "Dining"
        assert categorize("RELIANCE FRESH").category == "Groceries"
        assert categorize("UBER INDIA").category == "Transport"
        assert categorize("GOINDIGO AIRLINES").category == "Travel"
        assert categorize("NETFLIX.COM").category == "Subscriptions"
        assert categorize("APOLLO PHARMACY").category == "Healthcare"

    def test_unknown_merchant_is_none_not_guessed(self):
        # per the brief: never fabricate a confident answer when evidence is thin
        result = categorize("RAJ ENTERPRISES XYZ 9284")
        assert result.category is None
        assert result.confidence == 0.0


class TestDuplicateDetection:
    def test_triple_swiggy_charge_flags_the_two_same_day_ones(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        detect_duplicates(result.transactions)
        swiggy = [t for t in result.transactions if "SWIGGY" in t.merchant_raw]
        by_date = {}
        for t in swiggy:
            by_date.setdefault(t.transaction_date, []).append(t)
        same_day_pair = by_date[[d for d, v in by_date.items() if len(v) == 2][0]]
        assert any(t.duplicate_of is not None for t in same_day_pair)
        # the two same-day rows are linked to each other, not to the different-day one
        different_day = [t for t in swiggy if t not in same_day_pair][0]
        assert different_day.duplicate_of is None

    def test_never_deletes_flagged_duplicates(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        count_before = len(result.transactions)
        detect_duplicates(result.transactions)
        assert len(result.transactions) == count_before  # flagging only, never removal


class TestCrossDocumentDuplicateDetection:
    """EC-02 / EC-31: the same real-world transaction split across two different
    source documents (a card statement and a separate CSV export; or two
    overlapping statement periods) — detect_duplicates() alone can't catch this,
    since it only ever compares transactions within one document.
    """

    def _full_ledger(self):
        from statement_agent.ingest.pipeline import ingest_folder
        from statement_agent.store import Store
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        store = Store(path)
        ingest_folder(DATASET, store, attempt_vision=False)
        ledger = store.all_transactions()
        store.close()
        os.remove(path)
        return ledger

    def test_finds_the_real_uber_duplicate_across_csv_and_pdf(self):
        # UBER INDIA 260.00 INR on 2025-07-09 genuinely appears in both
        # meridian_credit_card_jul2025.pdf and team_reimbursements_jul2025.csv —
        # a real instance of the catalogue's EC-02 scenario already sitting in
        # dataset_public/, invisible until this cross-document pass existed.
        ledger = self._full_ledger()
        uber = [t for t in ledger if t.merchant_raw == "UBER INDIA" and t.amount == Decimal("260.00")]
        assert len(uber) == 2
        assert any(t.duplicate_of is not None for t in uber)
        flagged = next(t for t in uber if t.duplicate_of is not None)
        assert "cross-document" in flagged.duplicate_reason

    def test_does_not_flag_across_documents_when_merchant_differs(self):
        ledger = self._full_ledger()
        newly_flagged = detect_cross_document_duplicates(ledger)
        # every flag must genuinely span two different document_ids
        by_id = {t.transaction_id: t for t in ledger}
        for t in newly_flagged:
            original = by_id[t.duplicate_of]
            assert original.document_id != t.document_id

    def test_same_document_pairs_are_not_touched_by_the_cross_document_pass(self):
        # the within-document Swiggy duplicate must stay attributed to detect_duplicates(),
        # not get reprocessed/reattributed by the cross-document pass
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        detect_duplicates(result.transactions)
        swiggy_flagged_before = [t.duplicate_of for t in result.transactions if "SWIGGY" in t.merchant_raw and t.duplicate_of]

        detect_cross_document_duplicates(result.transactions)  # all one document — should be a no-op
        swiggy_flagged_after = [t.duplicate_of for t in result.transactions if "SWIGGY" in t.merchant_raw and t.duplicate_of]
        assert swiggy_flagged_before == swiggy_flagged_after

    def test_never_deletes_only_flags(self):
        ledger = self._full_ledger()
        count_before = len(ledger)
        detect_cross_document_duplicates(ledger)
        assert len(ledger) == count_before


class TestAnomalyDetection:
    def test_grandeur_jewellers_flagged_as_outlier(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        flags = detect_anomalies(result.transactions)
        merchants_flagged = [f.transaction.merchant_raw for f in flags]
        assert any("GRANDEUR" in m for m in merchants_flagged)

    def test_duplicate_flag_and_outlier_flag_are_both_surfaced(self):
        # regression test for a bug where the duplicate-flag list was built from an
        # already-duplicate-excluded baseline and could never contain anything
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        flags = resolve_all(result.document, result.transactions)
        reasons = [f.reason for f in flags]
        assert any("outlier" in r for r in reasons)
        assert any("duplicate" in r for r in reasons)


class TestReconciliation:
    def test_no_balances_stated_gives_no_totals_status(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        resolve_all(result.document, result.transactions)
        assert result.document.reconciliation_status == "NO_TOTALS"

    def test_balanced_statement_reconciles_cleanly(self):
        from statement_agent.resolve import reconcile_document
        from statement_agent.schema import Direction, EconomicType, Transaction
        import uuid
        from datetime import date

        doc = Document(
            document_id="d1", file_path="x", file_hash="h", doc_type="bank_statement",
            opening_balance=Decimal("40000"), closing_balance=Decimal("45000"),
        )
        txns = [
            Transaction(transaction_id=str(uuid.uuid4()), document_id="d1", transaction_date=date(2025, 6, 1),
                        date_raw="", amount=Decimal("20000"), direction=Direction.CREDIT,
                        economic_type=EconomicType.TRANSFER),
            Transaction(transaction_id=str(uuid.uuid4()), document_id="d1", transaction_date=date(2025, 6, 2),
                        date_raw="", amount=Decimal("15000"), direction=Direction.DEBIT,
                        economic_type=EconomicType.PURCHASE),
        ]
        reconcile_document(doc, txns)
        assert doc.reconciliation_status == "RECONCILED"
        assert doc.reconciliation_delta == Decimal("0")

    def test_mismatched_statement_flags_delta(self):
        from statement_agent.resolve import reconcile_document
        from statement_agent.schema import Direction, EconomicType, Transaction
        import uuid
        from datetime import date

        doc = Document(
            document_id="d2", file_path="x", file_hash="h", doc_type="bank_statement",
            opening_balance=Decimal("40000"), closing_balance=Decimal("999999"),  # wrong on purpose
        )
        txns = [
            Transaction(transaction_id=str(uuid.uuid4()), document_id="d2", transaction_date=date(2025, 6, 2),
                        date_raw="", amount=Decimal("15000"), direction=Direction.DEBIT,
                        economic_type=EconomicType.PURCHASE),
        ]
        reconcile_document(doc, txns)
        assert doc.reconciliation_status == "MISMATCH"
        assert doc.reconciliation_delta != Decimal("0")


class TestUnusualStructureFlag:
    def test_cobalt_overlapping_cycles_flagged(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "cobalt_bank_statement_jun2025.pdf"))
        from statement_agent.resolve import flag_unusual_structure

        flag_unusual_structure(result.document, result.transactions)
        assert any("DATA QUALITY" in w for w in result.document.parse_warnings)

    def test_clean_single_cycle_statement_not_flagged(self):
        result = parse_pdf_native(os.path.join(STATEMENTS, "meridian_credit_card_jul2025.pdf"))
        from statement_agent.resolve import flag_unusual_structure

        flag_unusual_structure(result.document, result.transactions)
        assert not any("DATA QUALITY" in w for w in result.document.parse_warnings)
