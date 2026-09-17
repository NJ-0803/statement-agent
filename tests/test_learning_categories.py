"""Categories that grow with each file (DECISIONS.md §39): a file's own labels, merchant memory, wider word
lists, wallet top-ups, budget-style sheets, and optional Groq suggestions. Groq is always faked here — no
test touches the network."""

import io
import json
import os
import urllib.error
from datetime import date
from decimal import Decimal

import pytest

from statement_agent import groq_categorize, ledger_edits
from statement_agent.categories import canonical
from statement_agent.corrections import CorrectionError
from statement_agent.env import load_dotenv
from statement_agent.ingest import pipeline
from statement_agent.schema import Direction, EconomicType, ImportState
from statement_agent.store import Store
from statement_agent.web.app import create_app

TRACKING = (
    "Date,Expense Type,Description,Amount (USD)\n"
    "2021-01-01,Food,Lunch at restaurant,$15.00\n"
    "2021-01-02,Grocery Shopping,Vegetables and fruits,$25.60\n"
    "2021-01-04,Fitness,Gym membership fee,$50.00\n"
    "2021-01-07,Rent,Monthly rent payment,$800.00\n"
    "2021-01-08,Pet Care,Pawsome Grooming,$30.00\n"
    "2021-01-10,Miscellaneous,Stationery purchase,$7.80\n"
)
NO_LABELS = (
    "Date,Description,Amount,Currency\n"
    "2021-02-08,Pawsome Grooming,$32.00,USD\n"
    "2021-02-09,Amazon Pay balance top-up,$20.00,USD\n"
    "2021-02-10,Anthropic subscription,$20.00,USD\n"
    "2021-02-11,eBay order,$35.00,USD\n"
    "2021-02-12,ZZ Gadget Hut,$99.00,USD\n"
    "2021-02-13,Quiet Owl Emporium,$12.00,USD\n"
)
BUDGET = (
    "Personal Income and Expense Statement,,,,,\n"
    "For the Month of: December 2025,,,,,\n"
    "Income,,,,,\n"
    "Category,Subcategory,Description,Amount ($),Frequency,Notes\n"
    "Employment,Salary,Monthly Salary,4500,Monthly,Net pay\n"
    "Other,Side Gig,Uber driving,400,Monthly,Part-time\n"
    "Total Income,,,4900,,\n"
    ",,,,,\n"
    "Expenses,,,,,\n"
    "Category,Subcategory,Description,Amount ($),Frequency,Notes\n"
    "Food,Groceries,Grocery shopping,400,Monthly,Food and household\n"
    "Other,Subscriptions,Streaming services,50,Monthly,Netflix etc.\n"
    "Total Expenses,,,450,,\n"
)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "ledger.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_groq(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def _add(store, tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    job = pipeline.create_import(store, str(p), name)
    job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
    staging = store.get_staging(job.job_id)
    if job.state == ImportState.NEEDS_REVIEW:
        job = pipeline.update_review(store, job.job_id, {"acknowledge": [i["issue_id"] for i in staging["issues"] if i["severity"] == "check"]})
    assert job.state == ImportState.READY, staging["issues"]
    pipeline.commit_import(store, job.job_id)
    return {t.description_raw: t for t in store.transactions_for_import(job.job_id)}, store.get_staging(job.job_id)


class FakeGroq:
    def __init__(self, answer=None, error=None):
        self.calls = []
        self.answer = answer
        self.error = error

    def __call__(self, path, body=None):
        self.calls.append((path, body))
        if path == "/models":
            return {"data": [{"id": "whisper-large-v3"}, {"id": "llama-3.9-99b-versatile", "active": True}]}
        if self.error:
            err, self.error = self.error, None
            raise err
        merchants = json.loads(body["messages"][1]["content"])["merchants"]
        content = self.answer(merchants) if callable(self.answer) else self.answer
        return {"choices": [{"message": {"content": content}}]}


def _groq(monkeypatch, fake):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_categorize, "_request", fake)
    return fake


def _http_error(code, body=b""):
    return urllib.error.HTTPError("https://api.groq.com", code, "err", {}, io.BytesIO(body))


class TestFileLabels:
    def test_expense_type_column_becomes_the_category(self, store, tmp_path):
        rows, staging = _add(store, tmp_path, "t.csv", TRACKING)
        assert staging["mapping"]["roles"]["category"] == 1
        got = {d: (t.category, t.category_source) for d, t in rows.items()}
        assert got["Lunch at restaurant"] == ("Food", "file")  # "Food" is ambiguous, so it stays as written
        assert got["Vegetables and fruits"] == ("Groceries", "file")
        assert got["Gym membership fee"] == ("Fitness", "file")  # the label beats the gym keyword
        assert got["Monthly rent payment"] == ("Housing", "file")
        assert got["Pawsome Grooming"] == ("Pet Care", "file")  # a brand-new category
        assert got["Stationery purchase"] == ("Other", "file")

    def test_label_mapping(self):
        assert [canonical(x) for x in ("Dining Out", "Bills & Utilities", "Uncategorized", "  Pet  Care ")] == [
            "Dining", "Utilities", None, "Pet Care"]

    def test_labels_teach_the_next_file_without_labels(self, store, tmp_path):
        _add(store, tmp_path, "t.csv", TRACKING)
        rows, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        assert (rows["Pawsome Grooming"].category, rows["Pawsome Grooming"].category_source) == ("Pet Care", "learned")
        assert store.merchant_knowledge()["PAWSOME GROOMING"] == ("Pet Care", "file")


class TestWordsAndWallets:
    def test_brands_and_wallet_top_ups(self, store, tmp_path):
        rows, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        assert rows["Anthropic subscription"].category == "Subscriptions"
        assert rows["eBay order"].category == "Shopping"
        top_up = rows["Amazon Pay balance top-up"]
        assert (top_up.economic_type, top_up.category) == (EconomicType.TRANSFER, None)
        assert rows["ZZ Gadget Hut"].category is None  # nothing knows it, and Groq is off


class TestGroq:
    def test_unknown_merchants_are_asked_once_and_remembered(self, store, tmp_path, monkeypatch):
        fake = _groq(monkeypatch, FakeGroq(lambda ms: json.dumps({"results": [
            {"merchant": m, "category": {"ZZ GADGET HUT": "Electronics", "QUIET OWL EMPORIUM": "Dining"}[m]} for m in ms]})))
        rows, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        assert (rows["ZZ Gadget Hut"].category, rows["ZZ Gadget Hut"].category_source) == ("Electronics", "groq")
        assert rows["Quiet Owl Emporium"].category == "Dining"
        [(_, body)] = fake.calls
        sent = body["messages"][1]["content"]
        assert json.loads(sent) == {"merchants": ["QUIET OWL EMPORIUM", "ZZ GADGET HUT"]}
        assert not any(ch.isdigit() for ch in sent)  # no amounts, dates or numbers leave the machine
        assert body["temperature"] == 0 and "Subscriptions" in body["messages"][0]["content"]

        again, _ = _add(store, tmp_path, "m.csv", NO_LABELS.replace("2021-02", "2021-03"))
        assert again["ZZ Gadget Hut"].category == "Electronics" and len(fake.calls) == 1

    def test_answers_that_dont_fit_are_thrown_away(self, store, tmp_path, monkeypatch):
        _groq(monkeypatch, FakeGroq(json.dumps({"results": [
            {"merchant": "ZZ GADGET HUT", "category": "<script>alert(1)</script>"},
            {"merchant": "SOMETHING I NEVER SENT", "category": "Shopping"},
            {"merchant": "QUIET OWL EMPORIUM", "category": "x" * 60},
        ]})))
        rows, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        assert rows["ZZ Gadget Hut"].category is None and rows["Quiet Owl Emporium"].category is None
        assert store.merchant_knowledge() == {}

    def test_rate_limit_never_blocks_an_import(self, store, tmp_path, monkeypatch):
        _groq(monkeypatch, FakeGroq(error=_http_error(429)))
        rows, staging = _add(store, tmp_path, "n.csv", NO_LABELS)
        assert rows["ZZ Gadget Hut"].category is None
        assert any("free-tier limit" in w for w in staging["warnings"])

    def test_a_retired_model_falls_back_to_an_available_one(self, store, tmp_path, monkeypatch):
        fake = _groq(monkeypatch, FakeGroq(
            lambda ms: json.dumps({"results": [{"merchant": m, "category": "Other"} for m in ms]}),
            error=_http_error(400, b'{"error": {"message": "The model has been decommissioned"}}')))
        _add(store, tmp_path, "n.csv", NO_LABELS)
        models = [body["model"] for path, body in fake.calls if path == "/chat/completions"]
        assert models == ["llama-3.3-70b-versatile", "llama-3.9-99b-versatile"]  # retired one first, then the fallback
        assert store.merchant_knowledge()["ZZ GADGET HUT"] == ("Other", "groq")

    def test_correcting_a_groq_guess_teaches_the_other_rows(self, store, tmp_path, monkeypatch):
        _groq(monkeypatch, FakeGroq(lambda ms: json.dumps({"results": [{"merchant": m, "category": "Other"} for m in ms]})))
        first, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        second, _ = _add(store, tmp_path, "m.csv", NO_LABELS.replace("2021-02", "2021-03"))
        ledger_edits.correct_one(store, first["ZZ Gadget Hut"].transaction_id, category="Electronics")
        follow = store.get_transaction(second["ZZ Gadget Hut"].transaction_id)
        assert (follow.category, follow.category_source) == ("Electronics", "learned")
        assert store.merchant_knowledge()["ZZ GADGET HUT"] == ("Electronics", "you")

    def test_correcting_a_keyword_row_changes_only_that_row(self, store, tmp_path):
        first, _ = _add(store, tmp_path, "n.csv", NO_LABELS)
        second, _ = _add(store, tmp_path, "m.csv", NO_LABELS.replace("2021-02", "2021-03"))
        ledger_edits.correct_one(store, first["eBay order"].transaction_id, category="Gifts & Donations")
        assert store.get_transaction(second["eBay order"].transaction_id).category == "Shopping"

    def test_ask_groq_about_what_is_already_added(self, store, tmp_path, monkeypatch):
        _add(store, tmp_path, "n.csv", NO_LABELS)
        with pytest.raises(CorrectionError, match="GROQ_API_KEY"):
            ledger_edits.categorize_with_groq(store)
        _groq(monkeypatch, FakeGroq(lambda ms: json.dumps({"results": [{"merchant": m, "category": "Hobbies"} for m in ms]})))
        result = ledger_edits.categorize_with_groq(store)
        assert (result["asked"], result["answered"], result["changed"]) == (2, 2, 2)
        assert ledger_edits.categorize_with_groq(store)["asked"] == 0

    def test_web_endpoint_and_status(self, tmp_path, monkeypatch):
        s = Store(str(tmp_path / "l.db"))
        _add(s, tmp_path, "n.csv", NO_LABELS)
        s.close()
        client = create_app(db_path=str(tmp_path / "l.db"), upload_dir=str(tmp_path / "up")).test_client()
        headers = {"X-CSRF-Token": client.application.config["CSRF_TOKEN"]}
        assert client.get("/api/status").get_json()["groq"] is False
        assert client.post("/api/categorize/groq", headers=headers).status_code == 400
        _groq(monkeypatch, FakeGroq(lambda ms: json.dumps({"results": [{"merchant": m, "category": "Hobbies"} for m in ms]})))
        assert client.get("/api/status").get_json()["groq"] is True
        assert client.post("/api/categorize/groq", headers=headers).get_json()["answered"] == 2
        row = client.get("/api/transactions?q=gadget").get_json()["transactions"][0]
        assert row["category"] == "Hobbies" and row["why"]["category"].startswith("suggested by Groq")


class TestBudgetSheets:
    def test_stated_month_and_income_expense_sections(self, store, tmp_path):
        p = tmp_path / "budget.csv"
        p.write_text(BUDGET)
        job = pipeline.create_import(store, str(p), "budget.csv")
        job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
        staging = store.get_staging(job.job_id)
        assert [i["rule"] for i in staging["issues"]] == ["date_from_period"]
        job = pipeline.update_review(store, job.job_id, {"acknowledge": ["date_from_period:file"]})
        pipeline.commit_import(store, job.job_id)
        rows = {t.description_raw: t for t in store.all_transactions()}
        assert set(rows) == {"Monthly Salary", "Uber driving", "Grocery shopping", "Streaming services"}
        assert all(t.transaction_date == date(2025, 12, 1) for t in rows.values())
        assert (rows["Uber driving"].direction, rows["Uber driving"].economic_type) == (Direction.CREDIT, EconomicType.INCOME)
        assert (rows["Grocery shopping"].direction, rows["Grocery shopping"].category) == (Direction.DEBIT, "Groceries")
        assert rows["Streaming services"].extra_fields["Category"] == "Other"
        assert rows["Monthly Salary"].amount == Decimal("4500")


def test_env_file_never_overrides_what_is_already_set(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nGROQ_API_KEY='from-file'\nexport OTHER_TEST_VAR=x\nEMPTY=\n")
    monkeypatch.setenv("OTHER_TEST_VAR", "already")
    load_dotenv(str(env))
    assert os.environ["GROQ_API_KEY"] == "from-file" and os.environ["OTHER_TEST_VAR"] == "already"
    assert "EMPTY" not in os.environ
    monkeypatch.delenv("GROQ_API_KEY")


def test_groq_requests_use_certifis_ca_bundle(monkeypatch):
    # a python.org macOS build without 'Install Certificates' fails every HTTPS call; found on the first live call
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout, context):
        seen["context"] = context
        seen["auth"] = req.headers["Authorization"]
        return Resp(b'{"data": []}')

    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(groq_categorize.urllib.request, "urlopen", fake_urlopen)
    groq_categorize._request("/models")
    import certifi
    assert seen["context"].get_ca_certs() or certifi.where()
    assert seen["auth"] == "Bearer k"
