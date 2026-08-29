"""Runs the externally-supplied 95-question evaluation/red-team bank
(Statement_Intelligence_Agent_Evaluation_Questions.xlsx) against the live
agent loop and records what actually happened for each question.

This is NOT the gold_qa.py harness (that one hand-verifies the deterministic
aggregation layer against independently-computed numbers). This one exercises
the FULL agent loop — natural language in, a live Claude API call choosing
tools, the verifier, the final answer out — against a question bank designed
to probe correctness, dataset traps, privacy, prompt injection, OCR, and
uncertainty calibration. It does not auto-grade Pass/Fail (that requires
judging free-text answers against qualitative "Expected Behavior" criteria,
which is not a mechanical check) — it captures the raw result (answer text,
status, caveats, citations, tool trace, or any error/crash) for every
question so each can be graded afterward.

Run: python eval/run_red_team_bank.py [path/to/questions.xlsx]
Writes: eval/red_team_results.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl  # noqa: E402

from statement_agent.agent.loop import run_agent  # noqa: E402
from statement_agent.ingest.pipeline import ingest_folder  # noqa: E402
from statement_agent.store import Store  # noqa: E402

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset_public")
DEFAULT_XLSX = "/Users/navtejsingh/Downloads/Statement_Intelligence_Agent_Evaluation_Questions.xlsx"
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "red_team_results.json")


def build_ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store)
    ledger = store.all_transactions()
    documents = store.all_documents_as_dicts()
    store.close()
    os.remove(path)
    return ledger, documents


def load_questions(xlsx_path: str) -> list[dict]:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Evaluation Questions"]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    header_idx = next(i for i, r in enumerate(rows) if r[0] == "ID")
    header = list(rows[header_idx])
    questions = []
    for row in rows[header_idx + 1:]:
        if row[0] is None:
            continue
        record = dict(zip(header, row))
        questions.append(record)
    return questions


def run_one(q: dict, ledger, documents) -> dict:
    result = {
        "id": q["ID"],
        "category": q["Category"],
        "question": q["Question"],
        "expected_response_type": q["Expected Response Type"],
        "severity": q["Severity"],
        "must_not_do": q["Must Not Do"],
        "error": None,
    }
    try:
        r = run_agent(q["Question"], ledger, documents=documents)
        result["answer_text"] = r.final_answer.answer_text
        result["proposed_status"] = r.final_answer.proposed_status
        result["verification_passed"] = r.verification.passed
        result["verification_failures"] = r.verification.failures
        result["caveats"] = r.final_answer.caveats
        result["cited_transaction_ids"] = r.final_answer.cited_transaction_ids
        result["verified_amounts"] = [
            {"currency": a.currency, "amount": a.amount, "label": a.label} for a in r.final_answer.verified_amounts
        ]
        result["tool_trace"] = [{"tool": t.tool_name, "input": t.tool_input} for t in r.trace]
        result["attempts"] = r.attempts
    except Exception as e:  # noqa: BLE001 - a crash on any single question must not stop the run
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main():
    xlsx_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting {DATASET} ...")
    ledger, documents = build_ledger()
    print(f"Ledger: {len(ledger)} transactions, {len(documents)} documents.")

    questions = load_questions(xlsx_path)
    print(f"Loaded {len(questions)} questions from {xlsx_path}.")

    results = []
    start = time.monotonic()
    for i, q in enumerate(questions, 1):
        t0 = time.monotonic()
        r = run_one(q, ledger, documents)
        elapsed = time.monotonic() - t0
        status = "ERROR" if r["error"] else r.get("proposed_status", "?")
        print(f"[{i}/{len(questions)}] id={r['id']} ({elapsed:.1f}s) -> {status}")
        if r["error"]:
            print(f"    ERROR: {r['error']}")
        results.append(r)
        # persist incrementally so a crash mid-run doesn't lose prior results
        with open(OUT_JSON, "w") as f:
            json.dump(results, f, indent=2, default=str)

    total = time.monotonic() - start
    n_errors = sum(1 for r in results if r["error"])
    print(f"\nDone: {len(results)} questions in {total:.0f}s, {n_errors} raised an exception.")
    print(f"Results written to {OUT_JSON}")


if __name__ == "__main__":
    main()
