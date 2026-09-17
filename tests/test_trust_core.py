"""Phase 1, part 1 (DECISIONS.md §34): field-level confidence with reason codes, the review gate that keeps
low-confidence required fields out of the ledger, and reconciliation against a statement's own balances
and totals. Fixtures are synthetic.
"""

import os
from datetime import date
from decimal import Decimal

import pymupdf
import pytest

from statement_agent.ingest import pipeline
from statement_agent.ingest.confidence import gate_issues, low_confidence_fields, set_field
from statement_agent.ingest.csv_parser import parse_csv
from statement_agent.ingest.pdf_native import parse_pdf_native
from statement_agent.ingest.statement_totals import read_figures, signed_balance
from statement_agent.resolve import reconcile_document
from statement_agent.schema import (
    Direction, Document, ExtractionMethod, ImportState, SourceRef, Transaction,
)
from statement_agent.store import Store


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _pdf(tmp_path, name, lines):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((40, 60 + i * 18), line, fontsize=10)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def _txn(amount, direction, currency="INR", **kw):
    return Transaction(
        transaction_id=kw.pop("tid", str(amount)), document_id="d", transaction_date=date(2025, 4, 1), date_raw="",
        amount=Decimal(amount), direction=direction, currency=currency, **kw,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "ledger.db"))
    yield s
    s.close()


def _stage(store, path):
    job = pipeline.create_import(store, path, os.path.basename(path))
    return pipeline.analyze_import(store, job.job_id, attempt_vision=False)


def _issues(store, job):
    return store.get_staging(job.job_id)["issues"]


class TestStatedFigures:
    def test_label_and_figure_on_one_line(self):
        f = read_figures(["Opening Balance: 10,000.00", "Closing Balance 56,350.00 Cr"])
        assert f.values == {"opening": "10,000.00", "closing": "56,350.00 Cr"}

    def test_several_labels_on_one_line(self):
        f = read_figures(["Opening Balance 10,000.00 Total Debits 3,650.00 Total Credits 50,000.00 Closing Balance 56,350.00"])
        assert f.values == {"opening": "10,000.00", "total_debits": "3,650.00",
                            "total_credits": "50,000.00", "closing": "56,350.00"}

    def test_labels_on_one_line_and_figures_on_the_next(self):
        f = read_figures(["Opening Balance    Total Debits    Total Credits    Closing Balance",
                          "10,000.00    3,650.00    50,000.00    56,350.00"])
        assert f.values["total_credits"] == "50,000.00" and f.values["closing"] == "56,350.00"

    def test_a_column_header_alone_is_not_a_figure(self):
        f = read_figures(["Date  Narration  Withdrawal  Deposit  Closing Balance", "01/04/2025 UPI SWIGGY 450.00"])
        assert f.values == {}

    def test_conflicting_values_are_dropped_not_guessed(self):
        f = read_figures(["Balance B/F 1,000.00", "Balance B/F 2,000.00", "Balance B/F 3,000.00"])
        assert "opening" not in f.values
        assert f.warnings() and "opening balance" in f.warnings()[0]

    def test_count_columns_are_not_totals(self):
        assert read_figures(["Total Debits Count 12"]).values == {}

    def test_balance_signs(self):
        assert signed_balance("1,000.00 Dr", credit_card=False) == Decimal("-1000.00")  # overdrawn account
        assert signed_balance("1,000.00 Cr", credit_card=False) == Decimal("1000.00")
        assert signed_balance("1,000.00 Cr", credit_card=True) == Decimal("-1000.00")  # card in credit
        assert signed_balance("(250.00)", credit_card=False) == Decimal("-250.00")


class TestReconciliation:
    def _doc(self, **kw):
        return Document("d", "p", "h", kw.pop("doc_type", "bank_statement"), currency_declared="INR", **kw)

    def test_bank_balances_reconcile(self):
        doc = self._doc(opening_balance=Decimal("1000"), closing_balance=Decimal("1300"))
        reconcile_document(doc, [_txn("200", Direction.DEBIT), _txn("500", Direction.CREDIT)])
        assert doc.reconciliation_status == "RECONCILED" and doc.reconciliation_delta == 0
        assert "matches the closing balance" in doc.reconciliation_detail

    def test_card_balance_runs_the_other_way(self):
        doc = self._doc(doc_type="credit_card_statement", opening_balance=Decimal("1000"), closing_balance=Decimal("700"))
        reconcile_document(doc, [_txn("200", Direction.DEBIT), _txn("500", Direction.CREDIT)])
        assert doc.reconciliation_status == "RECONCILED"

    def test_balance_mismatch_is_explained(self):
        doc = self._doc(opening_balance=Decimal("1000"), closing_balance=Decimal("900"))
        reconcile_document(doc, [_txn("200", Direction.DEBIT)])
        assert doc.reconciliation_status == "MISMATCH" and doc.reconciliation_delta == Decimal("-100")
        assert "off by 100.00" in doc.reconciliation_detail

    def test_totals_alone_are_checked(self):
        doc = self._doc(stated_total_debits=Decimal("200"), stated_total_credits=Decimal("400"))
        reconcile_document(doc, [_txn("200", Direction.DEBIT), _txn("500", Direction.CREDIT)])
        assert doc.reconciliation_status == "MISMATCH"
        assert "Money out adds up to 200.00, matching" in doc.reconciliation_detail
        assert "statement's total is 400.00" in doc.reconciliation_detail

    def test_foreign_currency_rows_make_it_uncheckable_not_a_mismatch(self):
        doc = self._doc(opening_balance=Decimal("0"), closing_balance=Decimal("200"))
        reconcile_document(doc, [_txn("200", Direction.DEBIT, tid="a"), _txn("20", Direction.DEBIT, "USD", tid="b")])
        assert doc.reconciliation_status == "CANNOT_CHECK" and "USD" in doc.reconciliation_detail

    def test_nothing_stated(self):
        doc = self._doc()
        reconcile_document(doc, [_txn("200", Direction.DEBIT)])
        assert doc.reconciliation_status == "NO_TOTALS"


BANK_PDF = [
    "Sample Bank Account Statement",
    "Currency: INR",
    "01/04/2025 Opening Balance 10,000.00",
    "02/04/2025 UPI SWIGGY 450.00",
    "15/04/2025 NEFT SALARY ACME 50,000.00 CR",
    "20/04/2025 POS AMAZON 1,200.00",
    "Total Debits 1,650.00 Total Credits 50,000.00",
    "Closing Balance 58,350.00",
]


class TestPdfStatementFigures:
    def test_dated_opening_balance_line_is_not_a_transaction(self, tmp_path):
        r = parse_pdf_native(_pdf(tmp_path, "s.pdf", BANK_PDF))
        assert [t.description_raw for t in r.transactions] == ["UPI SWIGGY", "NEFT SALARY ACME", "POS AMAZON"]
        assert any("summary line" in s["reason"] for s in r.skipped_lines)
        assert r.document.opening_balance == Decimal("10000.00")
        assert r.document.stated_total_debits == Decimal("1650.00")

    def test_matching_pdf_reconciles_and_is_ready(self, store, tmp_path):
        job = _stage(store, _pdf(tmp_path, "s.pdf", BANK_PDF))
        assert job.state == ImportState.READY
        summary = store.get_staging(job.job_id)["summary"]
        assert summary["reconciliation"] == "RECONCILED"
        assert "matches the closing balance" in summary["reconciliation_detail"]

    def test_a_missing_row_is_caught_by_the_totals(self, store, tmp_path):
        lines = [l for l in BANK_PDF if "AMAZON" not in l]
        job = _stage(store, _pdf(tmp_path, "s.pdf", lines))
        assert job.state == ImportState.NEEDS_REVIEW
        issue = next(i for i in _issues(store, job) if i["rule"] == "reconciliation_mismatch")
        assert "off by 1,200.00" in issue["message"]

    def test_leaving_a_row_out_is_named_as_a_likely_cause(self, store, tmp_path):
        job = _stage(store, _pdf(tmp_path, "s.pdf", BANK_PDF))
        txn = next(t for t in store.get_staging(job.job_id)["preview_transactions"] if t["description"] == "POS AMAZON")
        job = pipeline.update_review(store, job.job_id, {"exclude": [txn["key"]]})
        issue = next(i for i in _issues(store, job) if i["rule"] == "reconciliation_mismatch")
        assert "You left out 1 row" in issue["message"]

    def test_a_merchant_named_like_a_label_is_still_a_transaction(self, tmp_path):
        lines = BANK_PDF[:4] + ["03/04/2025 NEW BALANCE ATHLETICS 4,999.00"] + BANK_PDF[4:]
        r = parse_pdf_native(_pdf(tmp_path, "s.pdf", lines))
        assert "NEW BALANCE ATHLETICS" in [t.description_raw for t in r.transactions]
        assert r.document.closing_balance == Decimal("58350.00")

    def test_pdf_rows_carry_reason_codes(self, tmp_path):
        r = parse_pdf_native(_pdf(tmp_path, "s.pdf", BANK_PDF))
        t = r.transactions[1]
        assert t.field_reasons == {"date": "date_unambiguous", "amount": "amount_read",
                                   "direction": "direction_sign", "currency": "currency_file"}


class TestTabularFigures:
    def test_total_row_under_debit_credit_columns(self, store, tmp_path):
        text = ("Date,Narration,Withdrawal,Deposit,Currency\n"
                "13/04/2025,UPI SWIGGY,450.00,,INR\n14/04/2025,SALARY,,5000.00,INR\n"
                "Total,,450.00,5000.00,\n")
        r = parse_csv(_write(tmp_path, "t.csv", text))
        assert len(r.transactions) == 2
        from statement_agent.resolve import resolve_all
        resolve_all(r.document, r.transactions)
        assert (r.document.stated_total_debits, r.document.stated_total_credits) == (Decimal("450.00"), Decimal("5000.00"))
        assert r.document.reconciliation_status == "RECONCILED"

    def test_wrong_total_row_needs_review(self, store, tmp_path):
        text = ("Date,Narration,Withdrawal,Deposit,Currency\n"
                "13/04/2025,UPI SWIGGY,450.00,,INR\n14/04/2025,SALARY,,5000.00,INR\n"
                "Total,,950.00,5000.00,\n")
        job = _stage(store, _write(tmp_path, "t.csv", text))
        assert job.state == ImportState.NEEDS_REVIEW
        assert [i["rule"] for i in _issues(store, job)] == ["reconciliation_mismatch"]

    def test_stated_opening_without_a_balance_column(self, tmp_path):
        text = ("Opening Balance: 1000.00\nClosing Balance: 5550.00\n"
                "Date,Narration,Withdrawal,Deposit,Currency\n"
                "13/04/2025,UPI SWIGGY,450.00,,INR\n14/04/2025,SALARY,,5000.00,INR\n")
        r = parse_csv(_write(tmp_path, "t.csv", text))
        from statement_agent.resolve import resolve_all
        resolve_all(r.document, r.transactions)
        assert r.document.opening_balance == Decimal("1000.00")
        assert r.document.reconciliation_status == "RECONCILED"
        assert not r.document.opening_balance_derived

    def test_derived_opening_is_disclosed(self, tmp_path):
        text = ("Date,Narration,Withdrawal,Deposit,Balance,Currency\n"
                "13/04/2025,UPI SWIGGY,450.00,,550.00,INR\n14/04/2025,SALARY,,5000.00,5550.00,INR\n")
        r = parse_csv(_write(tmp_path, "t.csv", text))
        from statement_agent.resolve import resolve_all
        resolve_all(r.document, r.transactions)
        assert r.document.reconciliation_status == "RECONCILED"
        assert "worked out from the first row" in r.document.reconciliation_detail


class TestConfidenceGate:
    def test_unmarked_unsigned_amount_is_asked_not_assumed(self, store, tmp_path):
        text = "Date,Details,Amount,Dr/Cr,Currency\n2025-04-01,SALARY,5000.00,CR,INR\n2025-04-02,RENT,2000.00,,INR\n"
        job = _stage(store, _write(tmp_path, "m.csv", text))
        assert job.state == ImportState.NEEDS_REVIEW
        [issue] = _issues(store, job)
        assert (issue["rule"], issue["target"], issue["field"]) == ("low_confidence", "row:3", "direction")
        assert "Dr/Cr column was empty" in issue["message"]
        preview = store.get_staging(job.job_id)["preview_transactions"]
        assert preview[1]["unsure"][0]["field"] == "direction" and not preview[0]["unsure"]

        job = pipeline.update_review(store, job.job_id, {"directions": {"row:3": "DEBIT"}})
        assert job.state == ImportState.READY
        pipeline.commit_import(store, job.job_id)
        rent = next(t for t in store.all_transactions() if t.description_raw == "RENT")
        assert rent.field_reasons["direction"] == "direction_confirmed" and rent.field_confidence["direction"] == 1.0

    def test_unmarked_but_signed_amount_is_fine(self, tmp_path):
        text = "Date,Details,Amount,Dr/Cr,Currency\n2025-04-01,SALARY,5000.00,CR,INR\n2025-04-02,RENT,-2000.00,,INR\n"
        r = parse_csv(_write(tmp_path, "m.csv", text))
        assert r.transactions[1].field_reasons["direction"] == "direction_sign"
        assert not low_confidence_fields(r.transactions[1])

    def test_date_order_settled_by_the_file_needs_no_review(self, store, tmp_path):
        text = "Date,Details,Amount,Currency\n13/04/2025,TEA,10.00,INR\n05/04/2025,COFFEE,12.00,INR\n"
        job = _stage(store, _write(tmp_path, "d.csv", text))
        assert job.state == ImportState.READY
        pipeline.commit_import(store, job.job_id)
        coffee = next(t for t in store.all_transactions() if t.description_raw == "COFFEE")
        assert coffee.transaction_date == date(2025, 4, 5)
        assert coffee.field_reasons["date"] == "date_order_from_document"

    def test_unsettled_date_order_is_still_asked_and_confirming_it_records_why(self, store, tmp_path):
        job = _stage(store, _write(tmp_path, "d.csv", "Date,Details,Amount,Currency\n05/04/2025,COFFEE,12.00,INR\n"))
        assert [i["rule"] for i in _issues(store, job)] == ["date_order_assumed"]
        job = pipeline.update_review(store, job.job_id, {"date_order": "MDY", "acknowledge": ["date_order_assumed:file"]})
        assert job.state == ImportState.READY
        pipeline.commit_import(store, job.job_id)
        [t] = store.all_transactions()
        assert t.transaction_date == date(2025, 5, 4) and t.field_reasons["date"] == "date_order_confirmed"

    def test_image_rows_get_one_file_level_check(self):
        rows = []
        for i in range(3):
            t = _txn("10", Direction.DEBIT, tid=str(i),
                     source=SourceRef("p", "h", page=1, extraction_method=ExtractionMethod.VISION_OCR, extraction_confidence=0.75))
            if i:  # rows staged before reason codes existed still count, through extraction confidence
                for f in ("date", "amount", "direction", "currency"):
                    set_field(t, f, "read_from_image", 0.75)
            rows.append(t)
        [issue] = gate_issues(rows, [], row_key=lambda t: f"txn:{t.transaction_id}")
        assert issue.rule == "read_from_image" and issue.target is None and "3 transactions" in issue.message

    def test_existing_row_issue_counts_as_coverage(self):
        from statement_agent.schema import IssueSeverity, ValidationIssue
        t = _txn("10", Direction.DEBIT, source=SourceRef("p", "h", row=4))
        set_field(t, "direction", "direction_unmarked")
        existing = [ValidationIssue("x", "sign_marker_disagree", IssueSeverity.CHECK, "", "", target="row:4", field="direction")]
        assert gate_issues([t], existing, row_key=lambda t: "row:4") == []
        assert len(gate_issues([t], [], row_key=lambda t: "row:4")) == 1


class TestPersistence:
    def test_reasons_and_detail_survive_the_ledger(self, store, tmp_path):
        job = _stage(store, _pdf(tmp_path, "s.pdf", BANK_PDF))
        pipeline.commit_import(store, job.job_id)
        [doc] = store.all_documents_as_dicts()
        assert doc["reconciliation_status"] == "RECONCILED" and "matches" in doc["reconciliation_detail"]
        assert all(t.field_reasons.get("amount") == "amount_read" for t in store.all_transactions())

    def test_old_ledger_gets_the_new_columns(self, tmp_path):
        import sqlite3
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, file_path TEXT, file_hash TEXT UNIQUE, "
                     "doc_type TEXT, reconciliation_status TEXT)")
        conn.commit()
        conn.close()
        s = Store(path)
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(documents)")}
        tcols = {r[1] for r in s.conn.execute("PRAGMA table_info(transactions)")}
        s.close()
        assert "reconciliation_detail" in cols and "field_reasons" in tcols
