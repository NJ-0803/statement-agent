"""Tests for the Flask UI routes. No new logic lives in the web layer — it's a
thin wrapper over Store + agent.loop.run_agent — so these tests focus on the
wrapper's own responsibilities: clean error handling, correct status reporting,
and correctly shaping run_agent's result into JSON. run_agent itself is
mocked here (it's already covered live and in tests/test_verifier.py /
tests/test_tools.py) so this suite stays offline, no API key needed.
"""

import io
import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

import pytest

from statement_agent.agent.verifier import ClaimedAmount, FinalAnswer, ToolCallRecord, VerificationResult
from statement_agent.agent.loop import AgentRunResult
from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.store import Store
from tests._dataset import copy_ledger_db
from statement_agent.web.app import create_app

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


def _h(client):
    """Every state-changing API call must carry the page's CSRF token."""
    return {"X-CSRF-Token": client.application.config["CSRF_TOKEN"]}


@pytest.fixture
def empty_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # deliberately does not exist, to test the "no ledger" path
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def populated_db_path(tmp_path):
    return copy_ledger_db(str(tmp_path / "ledger.db"))


class TestIndexPage:
    def test_index_returns_html(self, empty_db_path):
        client = create_app(db_path=empty_db_path).test_client()
        res = client.get("/")
        assert res.status_code == 200
        assert b"Statement Intelligence Agent" in res.data


class TestStatusEndpoint:
    def test_not_ready_when_ledger_missing(self, empty_db_path):
        client = create_app(db_path=empty_db_path).test_client()
        data = client.get("/api/status").get_json()
        assert data["ready"] is False
        assert "ingest" in data["reason"]

    def test_ready_with_correct_counts(self, populated_db_path):
        client = create_app(db_path=populated_db_path).test_client()
        data = client.get("/api/status").get_json()
        assert data["ready"] is True
        assert data["transaction_count"] == 85  # 5 PDFs + 2 CSVs, no-vision ingest
        assert data["document_count"] == 6  # the scanned PDF yields 0 rows without vision, so it isn't committed


class TestAskEndpointErrorPaths:
    def test_missing_question_returns_400(self, populated_db_path):
        client = create_app(db_path=populated_db_path).test_client()
        res = client.post("/api/ask", headers=_h(client), json={})
        assert res.status_code == 400
        assert "question" in res.get_json()["error"]

    def test_missing_ledger_returns_400_not_500(self, empty_db_path):
        client = create_app(db_path=empty_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            res = client.post("/api/ask", headers=_h(client), json={"question": "anything"})
        assert res.status_code == 400
        assert "ingest" in res.get_json()["error"]

    def test_missing_api_key_returns_clean_500_not_crash(self, populated_db_path):
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            res = client.post("/api/ask", headers=_h(client), json={"question": "What did I spend on dining?"})
        assert res.status_code == 500
        assert "ANTHROPIC_API_KEY" in res.get_json()["error"]


class TestAskEndpointShapesRunAgentResultCorrectly:
    def test_successful_answer_shaped_into_expected_json(self, populated_db_path):
        fake_result = AgentRunResult(
            final_answer=FinalAnswer(
                answer_text="You spent 9805.00 INR on dining.",
                proposed_status="VERIFIED_WITH_CAVEATS",
                verified_amounts=[ClaimedAmount(currency="INR", amount="9805.00", label="Dining Q2")],
                cited_transaction_ids=["a", "b", "c"],
                caveats=["one caveat"],
            ),
            verification=VerificationResult(status="VERIFIED_WITH_CAVEATS", passed=True, failures=[]),
            trace=[ToolCallRecord("aggregate_spending", {"category": "Dining"}, None)],
            attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                res = client.post("/api/ask", headers=_h(client), json={"question": "dining spend?"})

        assert res.status_code == 200
        data = res.get_json()
        assert data["status"] == "VERIFIED_WITH_CAVEATS"
        assert data["amounts"] == [{"currency": "INR", "amount": "9805.00", "label": "Dining Q2"}]
        assert data["caveats"] == ["one caveat"]
        assert data["cited_count"] == 3
        assert data["trace"] == [{"tool": "aggregate_spending", "input": {"category": "Dining"}}]

    def test_anthropic_api_error_returns_502_not_stack_trace(self, populated_db_path):
        import anthropic

        client = create_app(db_path=populated_db_path).test_client()

        def _raise(*args, **kwargs):
            raise anthropic.APIStatusError("boom", response=_FakeResponse(), body=None)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", side_effect=_raise):
                res = client.post("/api/ask", headers=_h(client), json={"question": "anything"})

        assert res.status_code == 502
        assert "error" in res.get_json()


class TestChartImageEmbedding:
    def test_generate_chart_in_trace_is_embedded_as_a_data_uri(self, populated_db_path, tmp_path, monkeypatch):
        # use the real generate_chart tool to produce a real PNG, rather than fabricate
        # bytes — confirms the endpoint reads and encodes an actual chart file correctly
        import statement_agent.agent.tools as tools_module
        from statement_agent.agent.tools import generate_chart

        monkeypatch.setattr(tools_module, "_CHARTS_DIR", str(tmp_path / "charts"))
        store = Store(populated_db_path)
        real_ledger = store.all_transactions()
        store.close()
        chart_result = generate_chart(real_ledger, chart_type="bar", group_by="category", currency="INR")
        assert "error" not in chart_result

        fake_result = AgentRunResult(
            final_answer=FinalAnswer(answer_text="Here's your category breakdown.", proposed_status="VERIFIED"),
            verification=VerificationResult(status="VERIFIED", passed=True, failures=[]),
            trace=[ToolCallRecord("generate_chart", {"chart_type": "bar", "group_by": "category"}, chart_result)],
            attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                res = client.post("/api/ask", headers=_h(client), json={"question": "show my spending by category"})

        data = res.get_json()
        assert data["chart_image"] is not None
        assert data["chart_image"].startswith("data:image/png;base64,")

    def test_no_chart_call_means_no_chart_image(self, populated_db_path):
        fake_result = AgentRunResult(
            final_answer=FinalAnswer(answer_text="You spent 100 INR.", proposed_status="VERIFIED"),
            verification=VerificationResult(status="VERIFIED", passed=True, failures=[]),
            trace=[],
            attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                res = client.post("/api/ask", headers=_h(client), json={"question": "how much did I spend"})

        assert res.get_json()["chart_image"] is None

    def test_generate_dashboard_passes_through_both_chart_and_table(self, populated_db_path, tmp_path, monkeypatch):
        import statement_agent.agent.tools as tools_module
        from statement_agent.agent.tools import generate_dashboard

        monkeypatch.setattr(tools_module, "_CHARTS_DIR", str(tmp_path / "charts"))
        store = Store(populated_db_path)
        real_ledger = store.all_transactions()
        store.close()
        dashboard_result = generate_dashboard(real_ledger, group_by="category", top_n=2, currency="INR")
        assert "error" not in dashboard_result

        fake_result = AgentRunResult(
            final_answer=FinalAnswer(answer_text="Here's your dashboard.", proposed_status="VERIFIED"),
            verification=VerificationResult(status="VERIFIED", passed=True, failures=[]),
            trace=[ToolCallRecord("generate_dashboard", {"group_by": "category", "top_n": 2}, dashboard_result)],
            attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                res = client.post("/api/ask", headers=_h(client), json={"question": "show me a dashboard of top transactions per category"})

        data = res.get_json()
        assert data["chart_image"] is not None
        assert data["dashboard_table"] is not None
        assert data["dashboard_table"]["rows"] == dashboard_result["table_rows"]
        assert data["dashboard_table"]["total_rows"] == dashboard_result["table_total_rows"]
        assert data["dashboard_table"]["currency"] == "INR"

    def test_generate_chart_alone_never_populates_dashboard_table(self, populated_db_path, tmp_path, monkeypatch):
        # a plain chart request must not accidentally trigger dashboard-table rendering
        import statement_agent.agent.tools as tools_module
        from statement_agent.agent.tools import generate_chart

        monkeypatch.setattr(tools_module, "_CHARTS_DIR", str(tmp_path / "charts"))
        store = Store(populated_db_path)
        real_ledger = store.all_transactions()
        store.close()
        chart_result = generate_chart(real_ledger, chart_type="bar", group_by="category", currency="INR")

        fake_result = AgentRunResult(
            final_answer=FinalAnswer(answer_text="Here's your chart.", proposed_status="VERIFIED"),
            verification=VerificationResult(status="VERIFIED", passed=True, failures=[]),
            trace=[ToolCallRecord("generate_chart", {"chart_type": "bar", "group_by": "category"}, chart_result)],
            attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                res = client.post("/api/ask", headers=_h(client), json={"question": "chart my spending"})

        assert res.get_json()["dashboard_table"] is None


def _upload(client, payload: bytes, name: str):
    return client.post(
        "/api/imports", headers=_h(client),
        data={"files": (io.BytesIO(payload), name)}, content_type="multipart/form-data",
    )


def _dataset_csv() -> bytes:
    with open(os.path.join(DATASET, "expenses", "personal_expenses_q2_2025.csv"), "rb") as f:
        return f.read()


BANK_EXPORT = (
    b"Sample Bank Ltd\nAccount No: XXXXXXXX1234\n\n"
    b"Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    b'13/04/2025,Opening Balance,,,,,"10,000.00"\n'
    b'14/04/2025,UPI-SWIGGY,UPI1,14/04/2025,450.00,,"9,550.00"\n'
    b'15/04/2025,NEFT SALARY,N1,15/04/2025,,"50,000.00","59,550.00"\n'
)


@pytest.fixture
def inline_client(empty_db_path, tmp_path):
    app = create_app(db_path=empty_db_path, upload_dir=str(tmp_path / "uploads"), run_imports_inline=True)
    return app.test_client()


class TestImportsEndpoint:
    def test_no_files_returns_400(self, inline_client):
        res = inline_client.post("/api/imports", headers=_h(inline_client), data={})
        assert res.status_code == 400
        assert "error" in res.get_json()

    def test_state_changing_call_without_csrf_token_is_refused(self, inline_client):
        res = inline_client.post(
            "/api/imports", data={"files": (io.BytesIO(_dataset_csv()), "x.csv")}, content_type="multipart/form-data"
        )
        assert res.status_code == 403
        assert "token" in res.get_json()["error"]
        assert inline_client.post("/api/ask", json={"question": "hi"}).status_code == 403

    def test_upload_stages_but_does_not_commit_until_asked(self, inline_client):
        data = _upload(inline_client, _dataset_csv(), "personal_expenses_q2_2025.csv").get_json()
        job = data["imports"][0]
        assert job["state"] in ("needs_review", "ready")
        assert job["transaction_count"] == 6
        assert inline_client.get("/api/status").get_json()["transaction_count"] == 0  # staged, not in the ledger

    def test_review_then_commit_then_rollback_round_trip(self, inline_client):
        job = _upload(inline_client, _dataset_csv(), "personal_expenses_q2_2025.csv").get_json()["imports"][0]
        preview = inline_client.get(f"/api/imports/{job['id']}/preview").get_json()
        assert preview["mapping"]["amount_model"] == "signed"
        open_checks = [i["issue_id"] for i in preview["issues"] if i["severity"] == "check" and not i["resolution"]]

        early = inline_client.post(f"/api/imports/{job['id']}/commit", headers=_h(inline_client))
        if open_checks:
            assert early.status_code == 409  # nothing unresolved is ever committed
            res = inline_client.put(f"/api/imports/{job['id']}/review", headers=_h(inline_client), json={"acknowledge": open_checks})
            assert res.get_json()["state"] == "ready"
            assert inline_client.post(f"/api/imports/{job['id']}/commit", headers=_h(inline_client)).status_code == 200

        assert inline_client.get("/api/status").get_json()["transaction_count"] == 6
        res = inline_client.post(f"/api/imports/{job['id']}/rollback", headers=_h(inline_client))
        assert res.get_json()["state"] == "rolled_back"
        assert inline_client.get("/api/status").get_json()["transaction_count"] == 0

    def test_bank_export_with_preamble_and_debit_credit_columns(self, inline_client):
        job = _upload(inline_client, BANK_EXPORT, "bank.csv").get_json()["imports"][0]
        preview = inline_client.get(f"/api/imports/{job['id']}/preview").get_json()
        roles = {r: preview["headers"][c] for r, c in preview["mapping"]["roles"].items()}
        assert roles["debit"] == "Withdrawal Amt." and roles["credit"] == "Deposit Amt."
        assert preview["counts"]["transactions"] == 2
        assert preview["summary"]["money_out"] == {"INR": "450.00"}

    def test_user_mapping_correction_is_validated_and_reapplied(self, inline_client):
        job = _upload(inline_client, b"2025-06-21,TRUFFLES,1340.00\n2025-06-22,ZOMATO,540.00\n", "noheader.csv").get_json()["imports"][0]
        assert job["state"] == "needs_mapping"
        bad = inline_client.put(f"/api/imports/{job['id']}/mapping", headers=_h(inline_client), json={"roles": {"description": 1}})
        assert bad.status_code == 400 and "date" in bad.get_json()["error"]
        good = inline_client.put(
            f"/api/imports/{job['id']}/mapping", headers=_h(inline_client),
            json={"roles": {"date": 0, "description": 1, "amount": 2}, "currency": "INR"},
        ).get_json()
        assert good["state"] == "ready"
        assert good["transaction_count"] == 2

    def test_uploading_the_same_file_after_commit_is_reported_as_duplicate(self, inline_client):
        body = BANK_EXPORT
        job = _upload(inline_client, body, "bank.csv").get_json()["imports"][0]
        preview = inline_client.get(f"/api/imports/{job['id']}/preview").get_json()
        acks = [i["issue_id"] for i in preview["issues"] if i["severity"] == "check"]
        inline_client.put(f"/api/imports/{job['id']}/review", headers=_h(inline_client), json={"acknowledge": acks})
        assert inline_client.post(f"/api/imports/{job['id']}/commit", headers=_h(inline_client)).status_code == 200
        again = _upload(inline_client, body, "bank_copy.csv").get_json()["imports"][0]
        assert again["state"] == "duplicate"
        assert inline_client.get("/api/status").get_json()["transaction_count"] == 2

    def test_unsupported_file_type_is_reported_not_silently_dropped(self, inline_client):
        res = _upload(inline_client, b"not a real statement", "notes.rtf")  # .txt is a supported table format now
        assert res.status_code == 400
        assert "isn't supported" in res.get_json()["rejected"][0]["error"]

    def test_renamed_executable_is_rejected_by_signature(self, inline_client):
        res = _upload(inline_client, b"MZ\x90\x00\x03" + b"\x00" * 100, "statement.pdf")
        assert "don't match" in res.get_json()["rejected"][0]["error"]

    def test_xlsx_with_macros_is_rejected(self, inline_client, tmp_path):
        import zipfile

        p = tmp_path / "m.xlsx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", "<x/>")
            z.writestr("xl/workbook.xml", "<x/>")
            z.writestr("xl/vbaProject.bin", b"\x00" * 10)
        res = _upload(inline_client, p.read_bytes(), "m.xlsx")
        assert "macros" in res.get_json()["rejected"][0]["error"]

    def test_per_file_size_limit_is_enforced(self, inline_client, monkeypatch):
        import statement_agent.web.app as web_app

        monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 100)
        res = _upload(inline_client, _dataset_csv(), "big.csv")
        assert "too large" in res.get_json()["rejected"][0]["error"]

    def test_unknown_import_is_json_404(self, inline_client):
        res = inline_client.get("/api/imports/does-not-exist")
        assert res.status_code == 404 and res.get_json()["error"]
        assert inline_client.get("/api/nope").get_json()["error"]

    def test_path_traversal_filename_is_sanitized(self, empty_db_path, tmp_path):
        upload_dir = tmp_path / "uploads"
        client = create_app(db_path=empty_db_path, upload_dir=str(upload_dir), run_imports_inline=True).test_client()
        _upload(client, b"date,merchant,amount\n2025-01-01,X,1.00\n", "../../etc/evil.csv")
        assert not (tmp_path.parent.parent / "etc" / "evil.csv").exists()
        for _, _, files in os.walk(upload_dir):
            for fn in files:
                assert ".." not in fn

    def test_cancel_removes_the_uploaded_file(self, inline_client, tmp_path):
        job = _upload(inline_client, b"2025-06-21,TRUFFLES,1340.00\n", "noheader.csv").get_json()["imports"][0]
        res = inline_client.delete(f"/api/imports/{job['id']}", headers=_h(inline_client))
        assert res.get_json()["state"] == "cancelled"
        assert not (tmp_path / "uploads" / job["id"]).exists()


class TestAskSources:
    def test_cited_transactions_come_back_as_checkable_sources(self, populated_db_path):
        store = Store(populated_db_path)
        some = store.all_transactions()[:2]
        store.close()
        fake_result = AgentRunResult(
            final_answer=FinalAnswer(answer_text="x", proposed_status="VERIFIED", cited_transaction_ids=[t.transaction_id for t in some]),
            verification=VerificationResult(status="VERIFIED", passed=True, failures=[]),
            trace=[], attempts=1,
        )
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            with patch("statement_agent.agent.loop.run_agent", return_value=fake_result):
                data = client.post("/api/ask", headers=_h(client), json={"question": "q"}).get_json()
        assert len(data["sources"]) == 2
        assert data["sources"][0]["file"] and data["sources"][0]["amount"]


class _FakeResponse:
    status_code = 400
    headers = {}
    request = None
