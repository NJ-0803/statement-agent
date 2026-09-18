"""Any financial file, any column (DECISIONS.md §37): every non-core column is kept and typed, headers in
other languages are recognised, and OFX/QIF/MT940/JSON/XML/ODS/DOCX/HTML exports go through the same
staged import as a CSV. All fixtures are synthetic and built here."""

import io
import os
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from statement_agent.agent import tools as T
from statement_agent.ingest import pipeline
from statement_agent.ingest.columns import describe_extra_columns, mask_card_number
from statement_agent.ingest.formats import UnsafeFile, read_xml
from statement_agent.schema import Direction
from statement_agent.store import Store
from statement_agent.web.app import create_app, signature_problem


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "ledger.db"))
    yield s
    s.close()


def _write(tmp_path, name, content):
    p = tmp_path / name
    (p.write_bytes if isinstance(content, bytes) else p.write_text)(content)
    return str(p)


def _add(store, path):
    job = pipeline.create_import(store, path, os.path.basename(path))
    job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
    staging = store.get_staging(job.job_id)
    if job.state.value == "needs_review":
        checks = [i["issue_id"] for i in staging["issues"] if i["severity"] == "check"]
        job = pipeline.update_review(store, job.job_id, {"acknowledge": checks})
    assert job.state.value == "ready", (job.state, job.error_summary, staging.get("mapping"), staging.get("issues"))
    pipeline.commit_import(store, job.job_id)
    return store.get_staging(job.job_id), sorted(store.transactions_for_import(job.job_id), key=lambda t: (t.transaction_date, t.amount))


EXTRAS_CSV = (
    "Date,Time,Description,Amount,Currency,Payment Mode,City,Card Number,Tags,GST\n"
    "2025-04-01,09:15,SWIGGY,450.00,INR,UPI,Bengaluru,4111 1111 1111 1234,food,22.50\n"
    "2025-04-02,18:40,UBER,220.00,INR,Card,Bengaluru,4111 1111 1111 1234,travel,11.00\n"
    "2025-04-03,12:05,DMART,1200.00,INR,UPI,Mysuru,4111 1111 1111 1234,home,60.00\n"
    "2025-04-04,20:30,PVR,600.00,INR,Card,Bengaluru,4111 1111 1111 1234,fun,30.00\n"
)


class TestEveryColumnIsKept:
    def test_extra_columns_are_typed_and_stored(self, store, tmp_path):
        staging, txns = _add(store, _write(tmp_path, "x.csv", EXTRAS_CSV))
        kinds = {c["header"]: (c["kind"], c["meaning"]) for c in staging["extra_columns"]}
        assert kinds["Time"] == ("time", "time")
        assert kinds["Payment Mode"] == ("category", "payment method")
        assert kinds["City"][1] == "location"
        assert kinds["Card Number"] == ("card number", "card number")
        assert kinds["GST"] == ("money", "tax")
        assert txns[0].extra_fields == {"Time": "09:15", "Payment Mode": "UPI", "City": "Bengaluru",
                                        "Card Number": "•••• 1234", "Tags": "food", "GST": "22.50"}

    def test_full_card_numbers_never_reach_storage_or_preview(self, store, tmp_path):
        staging, txns = _add(store, _write(tmp_path, "x.csv", EXTRAS_CSV))
        assert "4111 1111" not in str(staging["sample_rows"]) + str(staging["extra_columns"])
        assert all("4111 1111" not in t.source.raw_text for t in txns)
        assert mask_card_number("12345") == "12345"

    def test_agent_can_filter_and_group_by_an_extra_column(self, store, tmp_path):
        _add(store, _write(tmp_path, "x.csv", EXTRAS_CSV))
        ledger = store.all_transactions()
        upi = T.search_transactions(ledger, field_name="payment mode", field_contains="upi")
        assert sorted(r.description for r in upi.results) == ["DMART", "SWIGGY"]
        assert upi.results[0].extra_fields["City"]
        grouped = T.aggregate_spending(ledger, group_by="field", group_field="City")
        assert grouped.group_breakdown == {"Bengaluru": {"INR": "1270.00"}, "Mysuru": {"INR": "1200.00"}}

    def test_kinds_from_values_alone(self):
        rows = [(i, [v]) for i, v in enumerate(["INV-2291", "INV-2292", "INV-2293"])]
        [col] = describe_extra_columns(["Something"], rows, set())
        assert (col.kind, col.meaning) == ("code", None)


class TestOtherLanguages:
    def test_german_export_with_decimal_commas(self, store, tmp_path):
        text = ("Buchungstag;Verwendungszweck;Betrag;Währung\n"
                "13.04.2025;REWE MARKT;-1.234,50;EUR\n14.04.2025;GEHALT;2.500,00;EUR\n")
        staging, txns = _add(store, _write(tmp_path, "de.csv", text))
        assert staging["mapping"]["decimal_separator"] == ","
        assert [(t.amount, t.direction, t.currency) for t in txns] == [
            (Decimal("1234.50"), Direction.DEBIT, "EUR"), (Decimal("2500.00"), Direction.CREDIT, "EUR")]

    def test_hindi_headers(self, store, tmp_path):
        text = "तारीख,विवरण,नामे,जमा,मुद्रा\n13/04/2025,किराना,500.00,,INR\n14/04/2025,वेतन,,30000.00,INR\n"
        _, txns = _add(store, _write(tmp_path, "hi.csv", text))
        assert [(t.description_raw, t.direction) for t in txns] == [("किराना", Direction.DEBIT), ("वेतन", Direction.CREDIT)]

    def test_tab_separated_text(self, store, tmp_path):
        _, txns = _add(store, _write(tmp_path, "t.tsv", "Date\tDetails\tAmount\tCurrency\n2025-04-01\tTEA\t10.00\tINR\n"))
        assert txns[0].amount == Decimal("10.00")


OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKACCTFROM><ACCTID>XXXX9876</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20250401120000[-5:EST]<TRNAMT>-45.20<FITID>A1<NAME>WHOLE FOODS<MEMO>groceries</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20250402<TRNAMT>1500.00<FITID>A2<NAME>ACME PAYROLL</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>2454.80<DTASOF>20250402</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

QIF = """!Type:Bank
D4/15'25
T-45.00
PCORNER CAFE
LDining
^
D4/16'25
T1,200.00
PCLIENT PAYMENT
MInvoice 7
^
"""

MT940 = """:20:STMT001
:25:DE12345678/0001
:28C:1/1
:60F:C250401EUR1000,00
:61:2504020402D45,20NTRFNONREF//B1
:86:?00SEPA?20EDEKA MARKT?21BERLIN
:61:2504030403C250,00NTRFREF77
:86:REFUND ONLINE SHOP
:62F:C250403EUR1204,80
-}
"""

JSON_EXPORT = """{"account": {"id": "wallet-1", "currency": "INR"},
 "transactions": [
   {"date": "2025-04-01", "amount": -250.0, "description": "PAYTM MOVIE", "currency": "INR", "merchant": {"city": "Pune"}, "status": "SUCCESS"},
   {"date": "2025-04-03", "amount": 1000.0, "description": "WALLET TOPUP", "currency": "INR", "merchant": {"city": "Pune"}, "status": "SUCCESS"}
 ]}"""

CAMT = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">100.00</Amt></Bal>
<Ntry><Amt Ccy="EUR">12.50</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2025-04-01</Dt></BookgDt><ValDt><Dt>2025-04-02</Dt></ValDt>
 <NtryDtls><TxDtls><RmtInf><Ustrd>BAKERY MUELLER</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="EUR">40.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2025-04-03</Dt></BookgDt><ValDt><Dt>2025-04-03</Dt></ValDt>
 <NtryDtls><TxDtls><RmtInf><Ustrd>REFUND SHOE STORE</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="EUR">5.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2025-04-05</Dt></BookgDt><ValDt><Dt>2025-04-05</Dt></ValDt>
 <NtryDtls><TxDtls><RmtInf><Ustrd>BANK FEE</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


ODS_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:spreadsheet><table:table table:name="Spends">
<table:table-row><table:table-cell><text:p>Date</text:p></table:table-cell><table:table-cell><text:p>Description</text:p></table:table-cell><table:table-cell><text:p>Amount</text:p></table:table-cell><table:table-cell><text:p>Currency</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell office:value-type="date" office:date-value="2025-04-01"><text:p>01/04/25</text:p></table:table-cell><table:table-cell><text:p>BOOKSTORE</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="320.5"><text:p>320.50</text:p></table:table-cell><table:table-cell><text:p>INR</text:p></table:table-cell></table:table-row>
<table:table-row table:number-rows-repeated="1000"><table:table-cell table:number-columns-repeated="4"/></table:table-row>
</table:table></office:spreadsheet></office:body></office:document-content>"""

DOCX_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Statement for April</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>Date</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Particulars</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Debit</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Credit</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Currency</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>15/04/2025</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>ELECTRICITY</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>900.00</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc><w:tc><w:p><w:r><w:t>INR</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl></w:body></w:document>"""

HTML = """<html><body><script>var t = "<table>";</script><h1>Account statement</h1>
<table><tr><th>Txn Date</th><th>Narration</th><th>Withdrawal</th><th>Deposit</th><th>Balance</th><th>Branch</th></tr>
<tr><td>13/04/2025</td><td>ATM&nbsp;WDL</td><td>2,000.00</td><td></td><td>8,000.00</td><td>MG Road</td></tr>
<tr><td>14/04/2025</td><td>NEFT SALARY</td><td></td><td>50,000.00</td><td>58,000.00</td><td>MG Road</td></tr>
</table></body></html>"""


class TestFormats:
    def test_ofx(self, store, tmp_path):
        staging, txns = _add(store, _write(tmp_path, "s.ofx", OFX))
        assert [(t.transaction_date, t.amount, t.direction, t.currency, t.description_raw) for t in txns] == [
            (date(2025, 4, 1), Decimal("45.20"), Direction.DEBIT, "USD", "WHOLE FOODS"),
            (date(2025, 4, 2), Decimal("1500.00"), Direction.CREDIT, "USD", "ACME PAYROLL"),
        ]
        assert txns[0].reference_id == "A1" and txns[0].extra_fields["Transaction kind"] == "DEBIT"
        assert txns[0].source.extraction_method.value == "RECORD"

    def test_qif(self, store, tmp_path):
        _, txns = _add(store, _write(tmp_path, "s.qif", QIF))
        assert [(t.transaction_date, t.amount, t.direction, t.category_declared) for t in txns] == [
            (date(2025, 4, 15), Decimal("45.00"), Direction.DEBIT, "Dining"),
            (date(2025, 4, 16), Decimal("1200.00"), Direction.CREDIT, None),
        ]

    def test_mt940_with_balances_that_reconcile(self, store, tmp_path):
        staging, txns = _add(store, _write(tmp_path, "s.sta", MT940))
        assert [(t.amount, t.direction, t.currency) for t in txns] == [
            (Decimal("45.20"), Direction.DEBIT, "EUR"), (Decimal("250.00"), Direction.CREDIT, "EUR")]
        assert "EDEKA MARKT" in txns[0].description_raw
        assert staging["summary"]["reconciliation"] == "RECONCILED"

    def test_json_asks_what_a_minus_sign_means(self, store, tmp_path):
        path = _write(tmp_path, "w.json", JSON_EXPORT)
        job = pipeline.create_import(store, path, "w.json")
        job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
        rules = [i["rule"] for i in store.get_staging(job.job_id)["issues"]]
        assert rules == ["sign_convention_assumed"]  # one spend, one top-up: the file doesn't settle it
        job = pipeline.update_review(store, job.job_id, {"negative_means": "DEBIT", "acknowledge": ["sign_convention_assumed:file"]})
        assert job.state.value == "ready"
        pipeline.commit_import(store, job.job_id)
        txns = sorted(store.transactions_for_import(job.job_id), key=lambda t: t.transaction_date)
        assert txns[0].field_reasons["direction"] == "direction_sign"
        assert [(t.amount, t.direction) for t in txns] == [(Decimal("250.0"), Direction.DEBIT), (Decimal("1000.0"), Direction.CREDIT)]
        assert txns[0].extra_fields == {"city": "Pune", "status": "SUCCESS"}

    def test_iso20022_xml(self, store, tmp_path):
        _, txns = _add(store, _write(tmp_path, "camt.xml", CAMT))
        assert [(t.transaction_date, t.amount, t.direction, t.currency, t.description_raw) for t in txns] == [
            (date(2025, 4, 1), Decimal("12.50"), Direction.DEBIT, "EUR", "BAKERY MUELLER"),
            (date(2025, 4, 3), Decimal("40.00"), Direction.CREDIT, "EUR", "REFUND SHOE STORE"),
            (date(2025, 4, 5), Decimal("5.00"), Direction.DEBIT, "EUR", "BANK FEE"),
        ]
        assert txns[0].value_date == date(2025, 4, 2)

    def test_xml_with_entities_is_refused(self, tmp_path):
        path = _write(tmp_path, "bomb.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
        with pytest.raises(UnsafeFile):
            read_xml(path)

    def test_ods(self, store, tmp_path):
        data = _zip({"mimetype": "application/vnd.oasis.opendocument.spreadsheet", "content.xml": ODS_CONTENT})
        _, txns = _add(store, _write(tmp_path, "s.ods", data))
        assert [(t.transaction_date, t.amount) for t in txns] == [(date(2025, 4, 1), Decimal("320.5"))]

    def test_docx_table(self, store, tmp_path):
        _, txns = _add(store, _write(tmp_path, "s.docx", _zip({"word/document.xml": DOCX_CONTENT})))
        assert [(t.description_raw, t.amount, t.direction) for t in txns] == [("ELECTRICITY", Decimal("900.00"), Direction.DEBIT)]

    def test_html_table_ignores_scripts(self, store, tmp_path):
        _, txns = _add(store, _write(tmp_path, "s.html", HTML))
        assert [(t.description_raw, t.direction, t.balance_after) for t in txns] == [
            ("ATM WDL", Direction.DEBIT, Decimal("8000.00")), ("NEFT SALARY", Direction.CREDIT, Decimal("58000.00"))]
        assert txns[0].extra_fields == {"Branch": "MG Road"}

    def test_upload_checks_for_new_formats(self, tmp_path):
        assert signature_problem(".xls", b"PK\x03\x04rest") is not None
        assert signature_problem(".xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest") is None
        assert signature_problem(".json", b"MZ\x90\x00") is not None
        assert signature_problem(".ofx", OFX.encode()) is None
        client = create_app(db_path=str(tmp_path / "l.db"), upload_dir=str(tmp_path / "up"), run_imports_inline=True).test_client()
        headers = {"X-CSRF-Token": client.application.config["CSRF_TOKEN"]}
        res = client.post("/api/imports", headers=headers, content_type="multipart/form-data", data={
            "files": [(io.BytesIO(JSON_EXPORT.encode()), "w.json"), (io.BytesIO(b"not a zip"), "fake.docx")]})
        body = res.get_json()
        assert [j["file"] for j in body["imports"]] == ["w.json"]
        assert body["rejected"][0]["file"] == "fake.docx"


class TestEuropeanAmountsEndToEnd:
    """The brief's locale case, all the way through an import rather than at the parser alone."""

    def test_a_german_statement_imports_the_right_numbers(self, store, tmp_path):
        text = ("Buchungstag;Verwendungszweck;Betrag;Währung\n"
                "13.04.2025;REWE MARKT;-1.234,50;EUR\n"
                "14.04.2025;GEHALT APRIL;2.500,00;EUR\n"
                "15.04.2025;SPOTIFY;-9,99;EUR\n")
        _, txns = _add(store, _write(tmp_path, "de.csv", text))
        assert [(str(t.transaction_date), str(t.amount), t.direction.value) for t in sorted(txns, key=lambda t: t.transaction_date)] == [
            ("2025-04-13", "1234.50", "DEBIT"), ("2025-04-14", "2500.00", "CREDIT"), ("2025-04-15", "9.99", "DEBIT")]

    def test_a_file_whose_amounts_never_settle_the_format_asks(self, store, tmp_path):
        # every amount is "<digits><separator><3 digits>", which means either thousands or a decimal point
        text = "Date,Description,Amount,Currency\n2025-04-01,EDEKA,1.234,EUR\n2025-04-02,BAECKEREI,2.500,EUR\n"
        path = _write(tmp_path, "unsettled.csv", text)
        job = pipeline.create_import(store, path, "unsettled.csv")
        job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
        issues = {i["rule"]: i for i in store.get_staging(job.job_id)["issues"]}
        assert "amount_format_assumed" in issues and job.state.value == "needs_review"
        assert "could group thousands" in issues["amount_format_assumed"]["message"]

        # answering changes the numbers: with '.' as the decimal point these are 1.234 and 2.500,
        # not the 1234 and 2500 the unsettled reading assumed
        job = pipeline.update_review(store, job.job_id, {"decimal_separator": "."})
        assert not any(i["rule"] == "amount_format_assumed" for i in store.get_staging(job.job_id)["issues"])
        pipeline.commit_import(store, job.job_id)
        assert sorted(str(t.amount) for t in store.all_transactions()) == ["1.234", "2.500"]


class TestSpreadsheetFormulasWithoutResults:
    """A spreadsheet written by a script often stores formulas with no saved result, and every such cell
    then reads as empty. The rows are already reported as problems; the cause has to be sayable too."""

    def _book(self, tmp_path, amount):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["Date", "Description", "Amount", "Currency"])
        ws.append(["2025-06-01", "TEA", amount, "INR"])
        ws.append(["2025-06-02", "COFFEE", 30, "INR"])
        path = str(tmp_path / "f.xlsx")
        wb.save(path)
        return path

    def test_the_cause_is_named_and_the_row_is_not_lost(self, store, tmp_path):
        path = self._book(tmp_path, "=10*2")
        job = pipeline.create_import(store, path, "f.xlsx")
        job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
        staging = store.get_staging(job.job_id)
        rules = {i["rule"]: i for i in staging["issues"]}
        assert "formulas_without_results" in rules and "C2" in rules["formulas_without_results"]["evidence"]
        assert "missing_amount" in rules  # the affected row is still reported, never silently dropped
        assert staging["counts"]["rows"] == 2

    def test_an_ordinary_workbook_says_nothing_about_formulas(self, store, tmp_path):
        _, txns = _add(store, self._book(tmp_path, 20))
        assert len(txns) == 2
