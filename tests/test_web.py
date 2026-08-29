"""Tests for the Flask UI routes. No new logic lives in the web layer — it's a
thin wrapper over Store + agent.loop.run_agent — so these tests focus on the
wrapper's own responsibilities: clean error handling, correct status reporting,
and correctly shaping run_agent's result into JSON. run_agent itself is
mocked here (it's already covered live and in tests/test_verifier.py /
tests/test_tools.py) so this suite stays offline, no API key needed.
"""

import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

import pytest

from statement_agent.agent.verifier import ClaimedAmount, FinalAnswer, ToolCallRecord, VerificationResult
from statement_agent.agent.loop import AgentRunResult
from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.store import Store
from statement_agent.web.app import create_app

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


@pytest.fixture
def empty_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # deliberately does not exist, to test the "no ledger" path
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def populated_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store, attempt_vision=False)
    store.close()
    yield path
    os.remove(path)


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
        assert data["document_count"] == 7


class TestAskEndpointErrorPaths:
    def test_missing_question_returns_400(self, populated_db_path):
        client = create_app(db_path=populated_db_path).test_client()
        res = client.post("/api/ask", json={})
        assert res.status_code == 400
        assert "question" in res.get_json()["error"]

    def test_missing_ledger_returns_400_not_500(self, empty_db_path):
        client = create_app(db_path=empty_db_path).test_client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key-for-this-test"}):
            res = client.post("/api/ask", json={"question": "anything"})
        assert res.status_code == 400
        assert "ingest" in res.get_json()["error"]

    def test_missing_api_key_returns_clean_500_not_crash(self, populated_db_path):
        client = create_app(db_path=populated_db_path).test_client()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            res = client.post("/api/ask", json={"question": "What did I spend on dining?"})
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
                res = client.post("/api/ask", json={"question": "dining spend?"})

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
                res = client.post("/api/ask", json={"question": "anything"})

        assert res.status_code == 502
        assert "error" in res.get_json()


class _FakeResponse:
    status_code = 400
    headers = {}
    request = None
