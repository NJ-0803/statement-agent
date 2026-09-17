"""Tests for the generalized, staged import path (DECISIONS.md §33): structure discovery,
column-role inference, deterministic row normalization with no silent row loss, and
atomic commit / isolated rollback.

Bank-export layouts here are SYNTHETIC — modeled on common Indian export shapes (preamble
rows, separate withdrawal/deposit columns, "(INR )" in headers, DR/CR columns, newest-first
running balances). They are not real bank files and don't prove compatibility with any
specific bank; a consented real-export corpus is still listed in NOT_IMPLEMENTED.md §I.
"""

import os
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from statement_agent.ingest import pipeline
from statement_agent.ingest.csv_parser import parse_csv
from statement_agent.ingest.mapping import MappingError, apply_user_mapping, header_fingerprint, infer_mapping
from statement_agent.ingest.sniff import detect_encoding, sniff_csv, sniff_xlsx
from statement_agent.ingest.xlsx_parser import parse_xlsx
from statement_agent.normalize import DocumentDateResolver
from statement_agent.schema import AmountModel, Direction, ImportState
from statement_agent.store import Store

HDFC_LIKE = (
    "Sample Bank Ltd\n"
    "Account No: XXXXXXXX1234,,,,,,\n"
    "Statement From : 13/04/2025 To : 30/04/2025,,,,,,\n"
    "\n"
    "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    '13/04/2025,Opening Balance,,,,,"10,000.00"\n'
    '14/04/2025,UPI-SWIGGY-BANGALORE,UPI123,14/04/2025,450.00,,"9,550.00"\n'
    '15/04/2025,NEFT SALARY ACME,N12345,15/04/2025,,"50,000.00","59,550.00"\n'
    '16/04/2025,POS AMAZON,,16/04/2025,"1,200.00",,"58,350.00"\n'
    ",MKTPLACE ORDER 55,,,,,\n"
    "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    '17/04/2025,ATM WDL,,17/04/2025,"2,000.00",,"56,350.00"\n'
)


def _write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return str(p)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "ledger.db"))
    yield s
    s.close()


class TestStructureDiscovery:
    def test_preamble_rows_are_skipped_and_the_real_header_found(self, tmp_path):
        sniffed = sniff_csv(_write(tmp_path, "b.csv", HDFC_LIKE))
        assert sniffed.best.header_row == 5
        assert sniffed.best.headers[4] == "Withdrawal Amt."
        assert len(sniffed.best.preamble) == 3

    def test_utf16_without_bom_is_detected(self, tmp_path):
        raw = "Date,Description,Amount\n2025-01-01,TEA,10.00\n".encode("utf-16-le")
        assert detect_encoding(raw)[0] == "utf-16-le"
        path = tmp_path / "u16.csv"
        path.write_bytes(raw)
        assert len(parse_csv(str(path)).transactions) == 1

    def test_semicolon_delimiter_and_quoted_newline(self, tmp_path):
        text = 'Date;Description;Amount\n2025-01-01;"TWO\nLINES";10.00\n2025-01-02;TEA;5.00\n'
        r = parse_csv(_write(tmp_path, "s.csv", text))
        assert r.sniff.delimiter == ";"
        assert [t.description_raw for t in r.transactions] == ["TWO\nLINES", "TEA"]

    def test_duplicate_headers_are_renamed_and_reported(self, tmp_path):
        r = parse_csv(_write(tmp_path, "d.csv", "Date,Amount,Amount\n2025-01-01,10.00,99\n"))
        assert r.table.headers == ["Date", "Amount", "Amount (2)"]
        assert any(i.rule == "duplicate_headers" for i in r.issues)

    def test_workbook_picks_the_transactions_sheet_not_the_active_cover_sheet(self, tmp_path):
        wb = openpyxl.Workbook()
        cover = wb.active
        cover.title = "Cover"
        cover.append(["Account summary for April"])
        cover.append(["Prepared by the bank"])
        tx = wb.create_sheet("Transactions")
        tx.append(["Statement of account"])
        tx.append([])
        tx.append(["Txn Date", "Description", "Debit", "Credit", "Balance"])
        tx.append([date(2025, 4, 1), "RENT", 2000, None, 8000])
        tx.append([date(2025, 4, 2), "SALARY", None, 5000, 13000])
        wb.active = 0
        path = str(tmp_path / "multi.xlsx")
        wb.save(path)

        sniffed = sniff_xlsx(path)
        assert sniffed.best.sheet == "Transactions" and sniffed.best.header_row == 3
        r = parse_xlsx(path)
        assert [(t.description_raw, t.direction) for t in r.transactions] == [("RENT", Direction.DEBIT), ("SALARY", Direction.CREDIT)]


class TestColumnRoleInference:
    def test_bank_export_roles_and_amount_model(self, tmp_path):
        m = infer_mapping(sniff_csv(_write(tmp_path, "b.csv", HDFC_LIKE)).best)
        named = {role: m.headers[col] for role, col in m.roles.items()}
        assert named["date"] == "Date" and named["value_date"] == "Value Dt"
        assert named["debit"] == "Withdrawal Amt." and named["credit"] == "Deposit Amt."
        assert named["balance"] == "Closing Balance" and named["reference"] == "Chq./Ref.No."
        assert m.amount_model == AmountModel.DEBIT_CREDIT.value
        assert not m.needs_confirmation

    def test_currency_inside_a_header_is_evidence(self, tmp_path):
        text = (
            "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )\n"
            "1,01/04/2025,01/04/2025,,UPI/ZOMATO,250.00,0.00,750.00\n"
        )
        m = infer_mapping(sniff_csv(_write(tmp_path, "i.csv", text)).best)
        assert m.currency == "INR" and m.currency_source == "header"
        assert m.headers[m.roles["date"]] == "Transaction Date"

    def test_terse_dr_cr_bal_headers(self, tmp_path):
        text = "Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL\n01-04-2025,,ATM,500.00,,4500.00,123\n02-04-2025,,NEFT,,1000.00,5500.00,123\n"
        r = parse_csv(_write(tmp_path, "a.csv", text))
        assert r.mapping.amount_model == AmountModel.DEBIT_CREDIT.value
        assert [t.direction for t in r.transactions] == [Direction.DEBIT, Direction.CREDIT]
        assert not any(i.rule == "balance_break" for i in r.issues)

    def test_amount_with_dr_cr_marker(self, tmp_path):
        text = "Date,Details,Amount,Dr/Cr\n2025-04-01,SALARY,5000.00,CR\n2025-04-02,RENT,2000.00,DR\n"
        r = parse_csv(_write(tmp_path, "m.csv", text))
        assert r.mapping.amount_model == AmountModel.AMOUNT_WITH_MARKER.value
        assert [t.direction for t in r.transactions] == [Direction.CREDIT, Direction.DEBIT]

    def test_two_equally_plausible_date_columns_need_confirmation(self, tmp_path):
        text = "Date,Posting Date,Description,Amount\n2025-04-01,2025-04-02,TEA,10.00\n"
        r = parse_csv(_write(tmp_path, "t.csv", text))
        assert r.mapping.needs_confirmation
        assert r.transactions == []  # never a silent guess on the non-interactive path

    def test_headerless_file_is_only_ever_a_proposal(self, tmp_path):
        m = infer_mapping(sniff_csv(_write(tmp_path, "n.csv", "2025-06-21,TRUFFLES,1340.00\n")).best)
        assert m.needs_confirmation and m.roles.get("date") == 0 and m.roles.get("amount") == 2

    def test_user_mapping_is_validated(self, tmp_path):
        table = sniff_csv(_write(tmp_path, "n.csv", "2025-06-21,TRUFFLES,1340.00\n")).best
        base = infer_mapping(table)
        with pytest.raises(MappingError):
            apply_user_mapping(table, base, {"roles": {"date": 0, "amount": 0}})
        with pytest.raises(MappingError):
            apply_user_mapping(table, base, {"roles": {"description": 1}})
        confirmed = apply_user_mapping(table, base, {"roles": {"date": 0, "description": 1, "amount": 2}, "currency": "usd"})
        assert confirmed.source == "user" and confirmed.currency == "USD" and not confirmed.needs_confirmation

    def test_fingerprint_depends_on_layout_not_rows(self, tmp_path):
        a = sniff_csv(_write(tmp_path, "a.csv", "Date,Details,Amount\n2025-01-01,A,1.00\n")).best
        b = sniff_csv(_write(tmp_path, "b.csv", "date , DETAILS,amount\n2026-02-02,B,2.00\n2026-02-03,C,3.00\n")).best
        c = sniff_csv(_write(tmp_path, "c.csv", "Date,Amount,Details\n2025-01-01,1.00,A\n")).best
        assert header_fingerprint(a) == header_fingerprint(b) != header_fingerprint(c)


class TestRowNormalization:
    def test_every_source_row_gets_exactly_one_outcome(self, tmp_path):
        r = parse_csv(_write(tmp_path, "b.csv", HDFC_LIKE))
        assert len(r.candidates) == len(r.table.rows)  # nothing silently lost
        reasons = {c.source_row: (c.outcome, c.reason) for c in r.candidates}
        assert reasons[6][1].startswith("summary line")
        assert reasons[10] == ("ignored", "continuation of row 9's description")
        assert reasons[11] == ("ignored", "repeated header row")
        assert [c.outcome for c in r.candidates].count("transaction") == len(r.transactions) == 4

    def test_opening_balance_captured_and_balance_continuity_holds(self, tmp_path):
        r = parse_csv(_write(tmp_path, "b.csv", HDFC_LIKE))
        assert r.document.opening_balance == Decimal("10000.00")
        assert r.document.closing_balance == Decimal("56350.00")
        assert not any(i.rule == "balance_break" for i in r.issues)
        amazon = next(t for t in r.transactions if t.description_raw.startswith("POS AMAZON"))
        assert amazon.description_raw == "POS AMAZON MKTPLACE ORDER 55"
        assert amazon.value_date == date(2025, 4, 16) and amazon.balance_after == Decimal("58350.00")

    def test_a_missing_row_shows_up_as_a_balance_break(self, tmp_path):
        broken = HDFC_LIKE.replace('15/04/2025,NEFT SALARY ACME,N12345,15/04/2025,,"50,000.00","59,550.00"\n', "")
        r = parse_csv(_write(tmp_path, "b.csv", broken))
        assert any(i.rule == "balance_break" and i.severity.value == "check" for i in r.issues)

    def test_both_debit_and_credit_filled_is_blocking(self, tmp_path):
        r = parse_csv(_write(tmp_path, "x.csv", "Date,Description,Debit,Credit\n2025-04-01,ODD,10.00,20.00\n2025-04-02,OK,5.00,\n"))
        assert [i.rule for i in r.issues if i.severity.value == "blocking"] == ["both_debit_and_credit"]
        assert len(r.transactions) == 1

    def test_direction_derived_from_running_balance_newest_first(self, tmp_path):
        text = (
            "Opening Balance: 6000.00\n"
            "Txn Date,Description,Amount,Balance\n"
            "05-Apr-25,ATM,500.00,4500.00\n"
            "03-Apr-25,SALARY,1000.00,5000.00\n"
            "02-Apr-25,RENT,2000.00,4000.00\n"
        )
        r = parse_csv(_write(tmp_path, "nb.csv", text))
        assert r.mapping.amount_model == AmountModel.AMOUNT_WITH_BALANCE.value
        dirs = {t.description_raw: t.direction for t in r.transactions}
        assert dirs == {"RENT": Direction.DEBIT, "SALARY": Direction.CREDIT, "ATM": Direction.DEBIT}
        assert r.transactions[0].transaction_date == date(2025, 4, 5)

    def test_without_an_opening_balance_the_first_direction_is_asked_not_guessed(self, tmp_path):
        text = "Txn Date,Description,Amount,Balance\n02-Apr-2025,RENT,2000.00,4000.00\n03-Apr-2025,SALARY,1000.00,5000.00\n"
        r = parse_csv(_write(tmp_path, "nb.csv", text))
        assert [i.rule for i in r.issues if i.severity.value == "blocking"] == ["direction_unknown"]
        assert [t.description_raw for t in r.transactions] == ["SALARY"]

    def test_decimal_comma_amounts(self, tmp_path):
        r = parse_csv(_write(tmp_path, "eu.csv", "Date;Description;Amount;Currency\n01.04.2025;REWE;-12,50;EUR\n02.04.2025;SALARY;2.500,00;EUR\n"))
        assert r.mapping.decimal_separator == ","
        assert [(t.amount, t.direction) for t in r.transactions] == [(Decimal("12.50"), Direction.CREDIT), (Decimal("2500.00"), Direction.DEBIT)]

    def test_excel_serial_dates(self, tmp_path):
        r = parse_csv(_write(tmp_path, "serial.csv", "Date,Description,Amount\n45748,TEA,10.00\n45749,COFFEE,12.00\n"))
        assert r.transactions[0].transaction_date == date(2025, 4, 1)

    def test_missing_currency_is_a_check_not_a_silent_default(self, tmp_path):
        r = parse_csv(_write(tmp_path, "c.csv", "Date,Description,Amount\n2025-04-01,TEA,10.00\n"))
        issue = next(i for i in r.issues if i.rule == "currency_assumed")
        assert issue.severity.value == "check"
        assert r.transactions[0].field_confidence["currency"] == 0.5


class TestDateShapes:
    @pytest.mark.parametrize("raw,expected", [
        ("31.03.2025", date(2025, 3, 31)), ("31/03/25", date(2025, 3, 31)),
        ("01-Apr-2025", date(2025, 4, 1)), ("01-Apr-25", date(2025, 4, 1)), ("2025/04/01", date(2025, 4, 1)),
    ])
    def test_common_bank_export_date_shapes(self, raw, expected):
        assert DocumentDateResolver().parse(raw).value == expected


class TestStagedImports:
    def _stage(self, store, path):
        job = pipeline.create_import(store, path, os.path.basename(path))
        return pipeline.analyze_import(store, job.job_id, attempt_vision=False)

    def _ready(self, store, job):
        staging = store.get_staging(job.job_id)
        acks = [i["issue_id"] for i in staging["issues"] if i["severity"] == "check"]
        return pipeline.update_review(store, job.job_id, {"acknowledge": acks})

    def test_nothing_reaches_the_ledger_before_commit(self, store, tmp_path):
        job = self._stage(store, _write(tmp_path, "b.csv", HDFC_LIKE))
        assert job.state == ImportState.NEEDS_REVIEW  # currency and date format are unconfirmed
        with pytest.raises(pipeline.ImportConflict):
            pipeline.commit_import(store, job.job_id)
        job = self._ready(store, job)
        assert job.state == ImportState.READY
        assert store.all_transactions() == [] and store.all_documents_as_dicts() == []
        job = pipeline.commit_import(store, job.job_id)
        assert job.state == ImportState.COMMITTED and len(store.all_transactions()) == 4

    def test_zero_rows_can_never_be_committed(self, store, tmp_path):
        job = self._stage(store, _write(tmp_path, "z.csv", "Date,Description,Amount\n,,\n"))
        assert job.state == ImportState.NEEDS_MAPPING
        with pytest.raises(pipeline.ImportConflict):
            pipeline.commit_import(store, job.job_id)
        with pytest.raises(ValueError):
            from statement_agent.schema import Document
            store.commit_import(job, Document("d", "p", "h", "expense_sheet"), [])

    def test_commit_is_atomic(self, store, tmp_path, monkeypatch):
        job = self._ready(store, self._stage(store, _write(tmp_path, "b.csv", HDFC_LIKE)))
        assert job.state == ImportState.READY

        def boom(txns):
            raise RuntimeError("disk full")

        monkeypatch.setattr(store, "_write_transactions", boom)
        with pytest.raises(RuntimeError):
            pipeline.commit_import(store, job.job_id)
        assert store.all_documents_as_dicts() == []  # the document insert was rolled back too
        assert store.get_job(job.job_id).state == ImportState.READY

    def test_rollback_removes_only_that_import_and_clears_dangling_duplicate_flags(self, store, tmp_path):
        first = self._ready(store, self._stage(store, _write(tmp_path, "one.csv", "Date,Description,Amount,Currency\n2025-04-01,SWIGGY,450.00,INR\n2025-04-03,UBER,120.00,INR\n")))
        pipeline.commit_import(store, first.job_id)
        second = self._ready(store, self._stage(store, _write(tmp_path, "two.csv", "Date,Description,Amount,Currency\n2025-04-02,SWIGGY,450.00,INR\n2025-04-09,DMART,900.00,INR\n")))
        pipeline.commit_import(store, second.job_id)
        assert sum(1 for t in store.all_transactions() if t.duplicate_of) == 1

        pipeline.rollback_import(store, first.job_id)
        remaining = store.all_transactions()
        assert sorted(t.description_raw for t in remaining) == ["DMART", "SWIGGY"]
        assert all(t.duplicate_of is None for t in remaining)
        assert all(t.import_job_id == second.job_id for t in remaining)
        with pytest.raises(pipeline.ImportConflict):
            pipeline.rollback_import(store, first.job_id)

    def test_confirmed_mapping_is_remembered_for_the_same_layout(self, store, tmp_path):
        job = self._stage(store, _write(tmp_path, "jan.csv", "Posted,Memo Line,Value In Rs\n2025-01-01,TEA,10.00\n"))
        assert job.state == ImportState.NEEDS_MAPPING
        pipeline.update_mapping(store, job.job_id, {"roles": {"date": 0, "description": 1, "amount": 2}, "currency": "INR"})
        pipeline.commit_import(store, self._ready(store, store.get_job(job.job_id)).job_id)

        feb = self._stage(store, _write(tmp_path, "feb.csv", "Posted,Memo Line,Value In Rs\n2025-02-01,COFFEE,12.00\n"))
        assert feb.state == ImportState.READY
        assert store.get_staging(feb.job_id)["mapping"]["source"] == "profile"

    def test_excluded_rows_are_reported_as_ignored_not_lost(self, store, tmp_path):
        job = self._stage(store, _write(tmp_path, "x.csv", "Date,Description,Amount,Currency\n2025-04-01,TEA,10.00,INR\n2025-04-02,BAD,abc,INR\n"))
        assert job.state == ImportState.NEEDS_REVIEW
        job = pipeline.update_review(store, job.job_id, {"exclude": ["row:3"]})
        assert job.state == ImportState.READY
        staging = store.get_staging(job.job_id)
        assert {"key": "row:3", "row": 3, "reason": "left out by you"} in staging["ignored_rows"]
        assert staging["counts"] == {"rows": 2, "transactions": 1, "ignored": 1, "issues": 0}

    def test_same_bytes_after_commit_is_a_duplicate_job(self, store, tmp_path):
        path = _write(tmp_path, "b.csv", HDFC_LIKE)
        pipeline.commit_import(store, self._ready(store, self._stage(store, path)).job_id)
        assert self._stage(store, path).state == ImportState.DUPLICATE

    def test_cli_path_refuses_an_uncertain_mapping(self, store, tmp_path):
        report = pipeline.ingest_file(_write(tmp_path, "t.csv", "Date,Posting Date,Description,Amount\n2025-04-01,2025-04-02,TEA,10.00\n"), store, attempt_vision=False)
        assert report.status == "needs_mapping"
        assert store.all_transactions() == []
