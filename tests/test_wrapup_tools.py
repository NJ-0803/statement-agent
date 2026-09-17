"""Export, explain, currency conversion, delete-everything, backup/restore, rate limits and Groq column
suggestions (DECISIONS.md §40). Groq is faked; nothing touches the network."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date

import pytest

from statement_agent import groq_categorize
from statement_agent.agent import tools as T
from statement_agent.ingest import pipeline
from statement_agent.store import Store
from statement_agent.web.app import create_app

CSV = ("Date,Description,Amount,Currency,Payment Mode\n"
       "2025-06-01,SWIGGY,450.00,INR,UPI\n2025-06-02,=HYPERLINK(\"x\"),10.00,INR,Card\n2025-06-03,UBER,120.00,INR,UPI\n")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    path = str(tmp_path / "l.db")
    s = Store(path)
    p = tmp_path / "a.csv"
    p.write_text(CSV)
    job = pipeline.create_import(s, str(p), "a.csv")
    job = pipeline.analyze_import(s, job.job_id, attempt_vision=False)
    pipeline.commit_import(s, job.job_id)
    s.close()
    return path


def _client(db, tmp_path):
    c = create_app(db_path=db, upload_dir=str(tmp_path / "up")).test_client()
    return c, {"X-CSRF-Token": c.application.config["CSRF_TOKEN"]}


def test_explain_and_convert(db):
    s = Store(db)
    ledger = s.all_transactions()
    swiggy = next(t for t in ledger if t.description_raw == "SWIGGY")
    info = T.explain_transaction(ledger, s.list_events(), swiggy.transaction_id)
    assert info["found"] and info["how_fields_were_read"]["amount"].startswith("the amount was read")
    assert info["category_and_names"]["category"] == "worked out from the merchant name"
    assert len(info["neighbours"]) == 2
    assert T.explain_transaction(ledger, [], "nope") == {"found": False, "transaction_id": "nope"}
    assert T.convert_currency("abc", "USD", "INR", date(2025, 6, 2))["ok"] is False
    s.close()


def test_export_csv_neutralises_formulas_and_keeps_extras(db, tmp_path):
    client, _ = _client(db, tmp_path)
    res = client.get("/api/export.csv")
    assert res.status_code == 200 and "attachment" in res.headers["Content-Disposition"]
    text = res.get_data(as_text=True)
    assert "'=HYPERLINK" in text and "extra: Payment Mode" in text and "SWIGGY" in text
    assert client.get("/api/export.csv?from=nope").status_code == 400
    assert client.get("/api/export.csv?from=2025-06-03").get_data(as_text=True).count("\n") == 2


def test_delete_everything_needs_the_phrase(db, tmp_path):
    client, h = _client(db, tmp_path)
    (tmp_path / "up" / "job1").mkdir(parents=True)
    assert client.delete("/api/everything", json={"confirm": "yes"}, headers=h).status_code == 400
    assert client.delete("/api/everything", json={"confirm": "DELETE EVERYTHING"}).status_code == 403
    assert client.delete("/api/everything", json={"confirm": "DELETE EVERYTHING"}, headers=h).get_json() == {"deleted": True}
    assert Store(db).all_transactions() == [] and os.listdir(tmp_path / "up") == []


def test_rate_limit_on_ask(db, tmp_path):
    client, h = _client(db, tmp_path)
    codes = [client.post("/api/ask", json={}, headers=h).status_code for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[10:] == [429, 429]


def test_groq_column_suggestions_send_no_values(db, tmp_path, monkeypatch):
    s = Store(db)
    p = tmp_path / "odd.csv"
    p.write_text("Posted,Memo Line,Value In Rs\n2025-01-01,TEA SHOP 4411,10.00\n")
    job = pipeline.create_import(s, str(p), "odd.csv")
    pipeline.analyze_import(s, job.job_id, attempt_vision=False)
    s.close()
    sent = {}

    def fake(path, body=None):
        sent["body"] = body
        return {"choices": [{"message": {"content": json.dumps({"roles": {"date": 0, "description": 1, "amount": 2, "bogus": 1, "notes": 9}})}}]}

    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(groq_categorize, "_request", fake)
    client, h = _client(db, tmp_path)
    r = client.post(f"/api/imports/{job.job_id}/suggest-columns", headers=h).get_json()
    assert r["roles"] == {"date": 0, "description": 1, "amount": 2}
    content = sent["body"]["messages"][1]["content"]
    assert "TEA SHOP" not in content and "10.00" not in content and "Memo Line" in content


def test_backup_restore_and_wipe_cli(db, tmp_path):
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    env.pop("GROQ_API_KEY", None)
    # cwd is the temp dir, so `wipe` can only ever clear a temporary uploaded_documents/
    run = lambda *a: subprocess.run([sys.executable, "-m", "statement_agent.cli", *a], capture_output=True,
                                    text=True, cwd=tmp_path, env=env)
    backup = str(tmp_path / "b.db")
    assert run("backup", "--db", db, "--to", backup).returncode == 0
    assert sqlite3.connect(backup).execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    assert run("wipe", "--db", db).returncode != 0
    assert run("wipe", "--db", db, "--yes").returncode == 0
    assert Store(db).all_transactions() == []
    assert run("restore", "--db", db, "--from", backup).returncode != 0  # exists: needs --force
    (tmp_path / "junk.db").write_text("not a database")
    assert run("restore", "--db", db, "--from", str(tmp_path / "junk.db"), "--force").returncode != 0
    assert run("restore", "--db", db, "--from", backup, "--force").returncode == 0
    assert len(Store(db).all_transactions()) == 3
