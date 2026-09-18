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

The question bank travels with the repo (eval/question_bank.json), so a run needs nothing from anyone's
Downloads folder; an .xlsx path can still be passed to refresh it. Runs are resumable: --resume keeps every
case that already has an answer and re-runs only those blocked by an infrastructure error, which is what
the 21 API-credit failures in the saved run need. Each record keeps the numbers and transaction ids each
tool actually returned, so eval/grade.py can grade amounts and citations independently of the verifier.

Run:    python eval/run_red_team_bank.py [--resume] [--only 3,7] [--limit 10] [path/to/questions.xlsx]
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
from statement_agent.agent.verifier import (  # noqa: E402
    _collect_grounded_numbers, _collect_ledger_transaction_ids,
)
from statement_agent.ingest.pipeline import ingest_folder  # noqa: E402
from statement_agent.store import Store  # noqa: E402

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset_public")
DEFAULT_XLSX = "/Users/navtejsingh/Downloads/Statement_Intelligence_Agent_Evaluation_Questions.xlsx"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "red_team_results.json")
BANK_JSON = os.path.join(HERE, "question_bank.json")


def build_ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store)
    ledger = store.all_transactions()
    documents = store.all_documents_as_dicts()
    events = store.list_events()  # refunds, transfers, card payments, recurring — the agent needs these
    store.close()
    os.remove(path)
    return ledger, documents, events


def load_bank() -> list[dict]:
    """The packaged bank, in the same shape the runner uses."""
    with open(BANK_JSON, encoding="utf-8") as f:
        return json.load(f)["questions"]


def save_bank(questions: list[dict]) -> None:
    payload = {"source": "Statement_Intelligence_Agent_Evaluation_Questions.xlsx (externally supplied)",
               "count": len(questions), "questions": questions}
    with open(BANK_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)


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


def run_one(q: dict, ledger, documents, events) -> dict:
    result = {k: q.get(k) for k in ("id", "category", "question", "expected_response_type", "severity", "must_not_do")}
    result["error"] = None
    try:
        r = run_agent(q["question"], ledger, documents=documents, events=events)
        result["answer_text"] = r.final_answer.answer_text
        result["proposed_status"] = r.final_answer.proposed_status
        result["verification_passed"] = r.verification.passed
        result["verification_failures"] = r.verification.failures
        result["caveats"] = r.final_answer.caveats
        result["cited_transaction_ids"] = r.final_answer.cited_transaction_ids
        result["verified_amounts"] = [
            {"currency": a.currency, "amount": a.amount, "label": a.label} for a in r.final_answer.verified_amounts
        ]
        result["tool_trace"] = [
            {"tool": t.tool_name, "input": t.tool_input, "reasoning": t.reasoning,
             # what the tool actually returned, so grading doesn't have to trust the verifier that ran here
             "result_numbers": sorted(_collect_grounded_numbers([t])),
             "result_transaction_ids": sorted(_collect_ledger_transaction_ids([t]))}
            for t in r.trace
        ]
        result["final_reasoning"] = r.final_reasoning
        result["attempts"] = r.attempts
    except Exception as e:  # noqa: BLE001 - a crash on any single question must not stop the run
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main():
    args = [a for a in sys.argv[1:]]
    resume = "--resume" in args
    only = next((a.split("=", 1)[1] if "=" in a else args[args.index(a) + 1] for a in args if a.startswith("--only")), None)
    limit = next((int(a.split("=", 1)[1] if "=" in a else args[args.index(a) + 1]) for a in args if a.startswith("--limit")), None)
    paths = [a for a in args if not a.startswith("--") and a.endswith(".xlsx")]

    if paths:  # refresh the packaged bank from the supplied spreadsheet
        raw = load_questions(paths[0])
        questions = [{"id": r.get("ID"), "category": r.get("Category"), "question": r.get("Question"),
                      "expected_response_type": r.get("Expected Response Type"), "severity": r.get("Severity"),
                      "must_not_do": r.get("Must Not Do")} for r in raw]
        save_bank(questions)
        print(f"Question bank refreshed from {paths[0]} ({len(questions)} questions).")
    else:
        questions = load_bank()

    previous = {}
    if resume and os.path.exists(OUT_JSON):
        with open(OUT_JSON, encoding="utf-8") as f:
            previous = {str(r["id"]): r for r in json.load(f)}
        keep = [r for r in previous.values() if not r.get("error") or r.get("error") == "None"]
        print(f"Resuming: {len(keep)} case(s) already answered, {len(previous) - len(keep)} to retry.")

    if only:
        wanted = {w.strip() for w in only.split(",")}
        questions = [q for q in questions if str(q["id"]) in wanted]
    todo = [q for q in questions
            if not (resume and str(q["id"]) in previous and previous[str(q["id"])].get("error") in (None, "None"))]
    if limit:
        todo = todo[:limit]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Each question is one live API call and costs money.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting {DATASET} ...")
    ledger, documents, events = build_ledger()
    print(f"Ledger: {len(ledger)} transactions, {len(documents)} documents, {len(events)} linked events.")
    print(f"Running {len(todo)} of {len(questions)} question(s).")

    results = dict(previous)
    start = time.monotonic()
    for i, q in enumerate(todo, 1):
        t0 = time.monotonic()
        r = run_one(q, ledger, documents, events)
        elapsed = time.monotonic() - t0
        status = "ERROR" if r["error"] else r.get("proposed_status", "?")
        print(f"[{i}/{len(todo)}] id={r['id']} ({elapsed:.1f}s) -> {status}")
        if r["error"]:
            print(f"    ERROR: {r['error']}")
        results[str(r["id"])] = r
        ordered = [results[str(q["id"])] for q in questions if str(q["id"]) in results]
        with open(OUT_JSON, "w") as f:  # written after every question: a crash never loses earlier work
            json.dump(ordered, f, indent=2, default=str)

    total = time.monotonic() - start
    n_errors = sum(1 for r in results.values() if r.get("error") not in (None, "None"))
    print(f"\nDone: {len(todo)} question(s) in {total:.0f}s; {n_errors} of {len(results)} still blocked by an error.")
    print(f"Results written to {OUT_JSON}. Grade them with: python eval/grade.py")


if __name__ == "__main__":
    main()
