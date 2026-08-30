"""CLI entrypoint.

    python -m statement_agent.cli ingest [--folder dataset_public] [--db ledger.db]
    python -m statement_agent.cli ask "What did I spend on dining last quarter?" [--db ledger.db]
    python -m statement_agent.cli ask --interactive [--db ledger.db]
"""

from __future__ import annotations

import argparse
import os
import sys

from .ingest.pipeline import ingest_folder
from .store import Store


def _cmd_ingest(args: argparse.Namespace) -> None:
    if os.path.exists(args.db) and args.fresh:
        os.remove(args.db)
    store = Store(args.db)
    reports = ingest_folder(args.folder, store, attempt_vision=not args.no_vision)

    ingested = [r for r in reports if r.status == "ingested"]
    skipped_dup = [r for r in reports if r.status == "skipped_duplicate"]
    skipped_unsupported = [r for r in reports if r.status == "skipped_unsupported"]
    failed = [r for r in reports if r.status == "failed"]

    print(f"Ingested {len(ingested)} file(s), {sum(r.transaction_count for r in ingested)} transaction(s) total.")
    for r in ingested:
        line = f"  [OK] {r.file_path} — {r.transaction_count} txn(s)"
        print(line)
        for w in r.warnings:
            print(f"        ! {w}")
    if skipped_dup:
        print(f"Skipped {len(skipped_dup)} already-ingested file(s) (unchanged since last run).")
    if skipped_unsupported:
        print(f"Skipped {len(skipped_unsupported)} unsupported file(s):")
        for r in skipped_unsupported:
            print(f"  - {r.file_path}")
    if failed:
        print(f"FAILED on {len(failed)} file(s):")
        for r in failed:
            print(f"  [FAIL] {r.file_path}: {r.warnings}")

    print(f"\nLedger: {args.db} ({len(store.all_transactions())} total transactions)")
    store.close()


def _print_answer(result) -> None:
    fa = result.final_answer
    print(f"\n[{result.verification.status}]")
    print(fa.answer_text)
    if fa.verified_amounts:
        print("\nAmounts:")
        for a in fa.verified_amounts:
            print(f"  {a.amount} {a.currency}" + (f" — {a.label}" if a.label else ""))
    if fa.caveats:
        print("\nCaveats:")
        for c in fa.caveats:
            print(f"  - {c}")
    if fa.cited_transaction_ids:
        print(f"\nSources: {len(fa.cited_transaction_ids)} transaction(s) cited (see --trace for detail)")
    for record in result.trace:
        if record.tool_name in ("generate_chart", "generate_dashboard") and isinstance(record.tool_result, dict) and record.tool_result.get("chart_path"):
            print(f"\nChart saved to: {record.tool_result['chart_path']}")
            # The dashboard table itself renders richly in the web UI only (per explicit
            # direction) — the CLI just points to the chart file rather than reformatting
            # a multi-group table as ASCII art.
    if not result.verification.passed:
        print("\nVerification failures:")
        for f in result.verification.failures:
            print(f"  - {f}")


def _cmd_ask(args: argparse.Namespace) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Add it to .env or export it before running `ask`.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.db):
        print(f"ERROR: no ledger found at {args.db}. Run `ingest` first.", file=sys.stderr)
        sys.exit(1)

    store = Store(args.db)
    ledger = store.all_transactions()
    documents = store.all_documents_as_dicts()
    store.close()

    if not ledger:
        print("ERROR: ledger is empty. Run `ingest` first.", file=sys.stderr)
        sys.exit(1)

    if args.interactive:
        print("Statement Intelligence Agent — interactive mode. Ctrl-D to quit.\n")
        while True:
            try:
                question = input("> ").strip()
            except EOFError:
                print()
                break
            if not question:
                continue
            _ask_one(question, ledger, documents, args.trace)
    else:
        _ask_one(args.question, ledger, documents, args.trace)


def _ask_one(question: str, ledger, documents, show_trace: bool) -> None:
    from .agent.loop import run_agent

    try:
        result = run_agent(question, ledger, documents=documents)
    except Exception as e:  # noqa: BLE001 - an API/network failure must produce a clean message, not a stack trace
        import anthropic

        if isinstance(e, anthropic.APIStatusError):
            print(f"\n[AGENT UNAVAILABLE] The Anthropic API returned an error: {e.message if hasattr(e, 'message') else e}")
            print("No answer was generated — this is reported honestly rather than falling back to a guess.")
        else:
            print(f"\n[AGENT UNAVAILABLE] Unexpected error calling the agent: {type(e).__name__}: {e}")
        return
    _print_answer(result)
    if show_trace:
        _print_trace(result)


def _print_trace(result) -> None:
    print("\n--- Execution trace ---")
    for i, record in enumerate(result.trace, 1):
        if record.reasoning:
            print(f"   reasoning: {record.reasoning}")
        print(f"{i}. {record.tool_name}({record.tool_input})")
    if result.final_reasoning:
        print(f"\nfinal reasoning: {result.final_reasoning}")


def _cmd_serve(args: argparse.Namespace) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is not set — the UI will load, but questions will fail.", file=sys.stderr)
    if not os.path.exists(args.db):
        print(f"WARNING: no ledger found at {args.db}. Run `ingest` first, or the UI will report it's not ready.", file=sys.stderr)

    from .web.app import create_app

    app = create_app(db_path=args.db)
    print(f"Serving on http://127.0.0.1:{args.port} (ledger: {args.db})")
    app.run(host="127.0.0.1", port=args.port, debug=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="statement-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="parse dataset_public/ into a local ledger")
    p_ingest.add_argument("--folder", default="dataset_public")
    p_ingest.add_argument("--db", default="ledger.db")
    p_ingest.add_argument("--fresh", action="store_true", help="delete any existing ledger DB before ingesting")
    p_ingest.add_argument("--no-vision", action="store_true", help="skip vision-OCR fallback (useful without API credits)")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_ask = sub.add_parser("ask", help="ask a natural-language question against the ledger")
    p_ask.add_argument("question", nargs="?", help="the question to ask (omit with --interactive)")
    p_ask.add_argument("--db", default="ledger.db")
    p_ask.add_argument("--interactive", action="store_true")
    p_ask.add_argument("--trace", action="store_true", help="print the tool-call execution trace")
    p_ask.set_defaults(func=_cmd_ask)

    p_serve = sub.add_parser("serve", help="run a small local web UI for asking questions in a browser")
    p_serve.add_argument("--db", default="ledger.db")
    p_serve.add_argument("--port", type=int, default=5050)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args()
    if args.command == "ask" and not args.interactive and not args.question:
        parser.error("ask requires a question, or pass --interactive")
    args.func(args)


if __name__ == "__main__":
    main()
