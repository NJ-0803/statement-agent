"""CLI entrypoint.

    python -m statement_agent.cli ingest [--folder dataset_public] [--db ledger.db]
    python -m statement_agent.cli ask "What did I spend on dining last quarter?" [--db ledger.db]
    python -m statement_agent.cli ask --interactive [--db ledger.db]

Every subcommand accepts --db PATH directly, or --client NAME to resolve a ledger
path from clients.json instead (see clients.json.example and DECISIONS.md §31) —
each client's data lives in its own separate .db file, never blended into one
shared ledger. `python -m statement_agent.cli clients` lists what's registered.
"""

from __future__ import annotations

import argparse
import os
import sys

from .clients import load_clients, resolve_db_path
from .ingest.pipeline import ingest_folder
from .store import Store


def _resolve_db_or_exit(args: argparse.Namespace) -> str:
    try:
        return resolve_db_path(client=args.client, db=args.db)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_ingest(args: argparse.Namespace) -> None:
    db_path = _resolve_db_or_exit(args)
    if os.path.exists(db_path) and args.fresh:
        os.remove(db_path)
    store = Store(db_path)
    reports = ingest_folder(args.folder, store, attempt_vision=not args.no_vision)

    ingested = [r for r in reports if r.status == "ingested"]
    skipped_dup = [r for r in reports if r.status == "skipped_duplicate"]
    skipped_unsupported = [r for r in reports if r.status == "skipped_unsupported"]
    not_added = [r for r in reports if r.status in ("needs_mapping", "needs_review", "no_transactions")]
    failed = [r for r in reports if r.status == "failed"]

    print(f"Ingested {len(ingested)} file(s), {sum(r.transaction_count for r in ingested)} transaction(s) total.")
    for r in ingested:
        line = f"  [OK] {r.file_path} — {r.transaction_count} txn(s)  (import {r.job_id})"
        print(line)
        for w in r.warnings:
            print(f"        ! {w}")
    if not_added:
        print(f"NOT added — {len(not_added)} file(s) need a person to look (nothing was written to the ledger):")
        labels = {"needs_mapping": "CHECK COLUMNS", "needs_review": "NEEDS REVIEW", "no_transactions": "NO TRANSACTIONS"}
        for r in not_added:
            print(f"  [{labels[r.status]}] {r.file_path}")
            for w in r.warnings:
                print(f"        ! {w}")
        print("  Open `serve` and add these files in the browser to confirm columns or review them.")
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

    print(f"\nLedger: {db_path} ({len(store.all_transactions())} total transactions)")
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
    db_path = _resolve_db_or_exit(args)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Add it to .env or export it before running `ask`.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(db_path):
        print(f"ERROR: no ledger found at {db_path}. Run `ingest` first.", file=sys.stderr)
        sys.exit(1)

    store = Store(db_path)
    ledger = store.all_transactions()
    documents = store.all_documents_as_dicts()
    events = store.list_events()
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
            _ask_one(question, ledger, documents, args.trace, events)
    else:
        _ask_one(args.question, ledger, documents, args.trace, events)


def _ask_one(question: str, ledger, documents, show_trace: bool, events=()) -> None:
    from .agent.loop import run_agent

    try:
        result = run_agent(question, ledger, documents=documents, events=list(events))
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
    from .web import auth, demo

    mode = auth.mode()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is not set — the UI will load, but questions will fail.", file=sys.stderr)

    if mode == "demo":
        # A demo instance never opens a real ledger: each visitor gets a throwaway copy of the
        # synthetic seed. Passing --db here would be silently ignored, so say so instead.
        if args.db or args.client:
            print("In demo mode --db/--client are ignored: visitors get their own sample ledger.", file=sys.stderr)
        db_path = ":demo:"
        if not os.path.exists(demo.seed_path()):
            print(f"WARNING: no demo seed at {demo.seed_path()}. Run `build-demo-seed` first, or visitors "
                  "will start with an empty ledger.", file=sys.stderr)
    else:
        db_path = _resolve_db_or_exit(args)
        if not os.path.exists(db_path):
            print(f"WARNING: no ledger found at {db_path}. Run `ingest` first, or the UI will report it's not ready.",
                  file=sys.stderr)
        if mode == "owner" and not auth.stored_hash():
            print("ERROR: owner mode needs a passphrase. Run `set-passphrase` first.", file=sys.stderr)
            raise SystemExit(2)

    from .web.app import create_app

    app = create_app(db_path=db_path)
    host = args.host or ("127.0.0.1" if mode == "local" else "0.0.0.0")
    where = "a throwaway sample ledger per visitor" if mode == "demo" else db_path
    print(f"Serving on http://{host}:{args.port} (mode: {mode}, ledger: {where})")
    if mode != "local":
        print("Put this behind HTTPS — the session cookie is marked Secure and will not be sent over plain HTTP.")
    app.run(host=host, port=args.port, debug=False)


def _cmd_set_passphrase(args: argparse.Namespace) -> None:
    """Set the passphrase that owner mode asks for. Never echoes it, never stores it in plain text."""
    import getpass

    from .web import auth

    passphrase = os.environ.get("STATEMENT_AGENT_NEW_PASSPHRASE") or getpass.getpass("New passphrase: ")
    if len(passphrase) < 12:
        print("Use at least 12 characters. This is the only thing standing between the internet and "
              "your statements.", file=sys.stderr)
        raise SystemExit(2)
    if not os.environ.get("STATEMENT_AGENT_NEW_PASSPHRASE") and passphrase != getpass.getpass("Again: "):
        print("Those didn't match.", file=sys.stderr)
        raise SystemExit(2)
    path = auth.save_passphrase(passphrase)
    print(f"Saved to {path} (readable only by you). Only the scrypt hash is stored.")
    print("For a host that has no disk to keep it on, set this environment variable instead:")
    print(f"  STATEMENT_AGENT_PASSPHRASE_HASH='{auth.stored_hash()}'")


def _cmd_build_demo_seed(args: argparse.Namespace) -> None:
    """Build the synthetic ledger every demo visitor starts from, out of dataset_public/."""
    from .ingest.pipeline import ingest_folder
    from .web import demo

    source = args.source
    if not os.path.isdir(source):
        print(f"No such folder: {source}", file=sys.stderr)
        raise SystemExit(2)
    target = args.out or demo.seed_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        os.remove(target)
    store = Store(target)
    try:
        reports = ingest_folder(source, store, attempt_vision=bool(os.environ.get("ANTHROPIC_API_KEY")))
        for report in reports:
            note = f" — {report.warnings[0]}" if report.warnings else ""
            print(f"  {report.status:>22}  {os.path.basename(report.file_path)}{note}", file=sys.stderr)
        count = len(store.all_transactions())
        documents = len(store.all_documents_as_dicts())
    finally:
        store.close()
    print(f"Demo seed written to {target}: {count} transactions from {documents} document(s), all synthetic.")


def _cmd_imports(args: argparse.Namespace) -> None:
    db_path = _resolve_db_or_exit(args)
    store = Store(db_path)
    jobs = store.list_jobs(limit=args.limit)
    store.close()
    if not jobs:
        print("No imports recorded in this ledger yet.")
        return
    for job in jobs:
        print(f"{job.job_id}  {job.state.value:<13} {job.transaction_count:>5} txn(s)  {job.created_at}  {job.original_filename}")
        if job.error_summary:
            print(f"    ! {job.error_summary}")


def _cmd_rollback(args: argparse.Namespace) -> None:
    from .ingest.pipeline import ImportConflict, rollback_import

    db_path = _resolve_db_or_exit(args)
    store = Store(db_path)
    try:
        job = rollback_import(store, args.job_id)
        print(f"Removed import {job.job_id} ({job.original_filename}) from {db_path}.")
    except KeyError:
        print(f"ERROR: no import with id {args.job_id}", file=sys.stderr)
        sys.exit(1)
    except ImportConflict as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def _cmd_clients(args: argparse.Namespace) -> None:
    clients = load_clients()
    if not clients:
        print("No clients configured. Create clients.json (see clients.json.example) to register one.")
        return
    print(f"{len(clients)} client(s) configured:")
    for name, db_path in sorted(clients.items()):
        marker = f" (no ledger yet — run `ingest --client {name}`)" if not os.path.exists(db_path) else ""
        print(f"  {name} -> {db_path}{marker}")


def cmd_backup(args: argparse.Namespace) -> None:
    db_path = _resolve_db_or_exit(args)
    if not os.path.exists(db_path):
        sys.exit(f"No ledger at {db_path}.")
    store = Store(db_path)
    try:
        store.backup_to(args.to)
    finally:
        store.close()
    print(f"Backed up {db_path} to {args.to}. It holds your financial data: keep it somewhere safe.")


def cmd_restore(args: argparse.Namespace) -> None:
    import shutil
    import sqlite3

    db_path = _resolve_db_or_exit(args)
    try:
        conn = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.DatabaseError:
        sys.exit(f"{args.source} isn't a ledger backup.")
    if not {"transactions", "documents", "import_jobs"} <= tables:
        sys.exit(f"{args.source} isn't a ledger backup.")
    if os.path.exists(db_path) and not args.force:
        sys.exit(f"{db_path} already exists. Add --force to replace it (make a backup first).")
    shutil.copyfile(args.source, db_path)
    Store(db_path).close()  # brings an older backup's schema up to date
    print(f"Restored {db_path} from {args.source}.")


def cmd_wipe(args: argparse.Namespace) -> None:
    import shutil

    db_path = _resolve_db_or_exit(args)
    if not args.yes:
        sys.exit("This deletes every statement, transaction, rule and uploaded file. Re-run with --yes to confirm.")
    if os.path.exists(db_path):
        store = Store(db_path)
        try:
            store.wipe()
        finally:
            store.close()
    from .web.app import UPLOAD_DIR

    if os.path.isdir(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            shutil.rmtree(os.path.join(UPLOAD_DIR, name), ignore_errors=True)
    print("Everything was deleted.")


def main() -> None:
    from .env import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="statement-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="parse dataset_public/ into a local ledger")
    p_ingest.add_argument("--folder", default="dataset_public")
    p_ingest.add_argument("--db", default=None, help="ledger DB path (default: ledger.db)")
    p_ingest.add_argument("--client", default=None, help="named client from clients.json, instead of --db")
    p_ingest.add_argument("--fresh", action="store_true", help="delete any existing ledger DB before ingesting")
    p_ingest.add_argument("--no-vision", action="store_true", help="skip vision-OCR fallback (useful without API credits)")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_ask = sub.add_parser("ask", help="ask a natural-language question against the ledger")
    p_ask.add_argument("question", nargs="?", help="the question to ask (omit with --interactive)")
    p_ask.add_argument("--db", default=None, help="ledger DB path (default: ledger.db)")
    p_ask.add_argument("--client", default=None, help="named client from clients.json, instead of --db")
    p_ask.add_argument("--interactive", action="store_true")
    p_ask.add_argument("--trace", action="store_true", help="print the tool-call execution trace")
    p_ask.set_defaults(func=_cmd_ask)

    p_backup = sub.add_parser("backup", help="copy the ledger to a file (safe while the app runs)")
    p_backup.add_argument("--db", default=None)
    p_backup.add_argument("--client", default=None)
    p_backup.add_argument("--to", required=True)
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="replace the ledger with a backup file")
    p_restore.add_argument("--db", default=None)
    p_restore.add_argument("--client", default=None)
    p_restore.add_argument("--from", dest="source", required=True)
    p_restore.add_argument("--force", action="store_true", help="overwrite an existing ledger")
    p_restore.set_defaults(func=cmd_restore)

    p_wipe = sub.add_parser("wipe", help="delete every statement, transaction, rule and upload")
    p_wipe.add_argument("--db", default=None)
    p_wipe.add_argument("--client", default=None)
    p_wipe.add_argument("--yes", action="store_true", help="required: confirms you mean it")
    p_wipe.set_defaults(func=cmd_wipe)

    p_serve = sub.add_parser("serve", help="run a small local web UI for asking questions in a browser")
    p_serve.add_argument("--db", default=None, help="ledger DB path (default: ledger.db)")
    p_serve.add_argument("--client", default=None, help="named client from clients.json, instead of --db")
    p_serve.add_argument("--port", type=int, default=5050)
    p_serve.add_argument("--host", default=None,
                         help="address to bind (default: 127.0.0.1 locally, 0.0.0.0 in owner/demo mode)")
    p_serve.set_defaults(func=_cmd_serve)

    p_pass = sub.add_parser("set-passphrase", help="set the passphrase owner mode asks for")
    p_pass.set_defaults(func=_cmd_set_passphrase)

    p_seed = sub.add_parser("build-demo-seed", help="build the synthetic ledger demo visitors start from")
    p_seed.add_argument("--source", default="dataset_public", help="folder of synthetic statements")
    p_seed.add_argument("--out", default=None, help="where to write the seed (default: the demo root)")
    p_seed.set_defaults(func=_cmd_build_demo_seed)

    p_imports = sub.add_parser("imports", help="list import jobs recorded in the ledger")
    p_imports.add_argument("--db", default=None, help="ledger DB path (default: ledger.db)")
    p_imports.add_argument("--client", default=None, help="named client from clients.json, instead of --db")
    p_imports.add_argument("--limit", type=int, default=50)
    p_imports.set_defaults(func=_cmd_imports)

    p_rollback = sub.add_parser("rollback", help="undo one committed import, leaving every other import untouched")
    p_rollback.add_argument("job_id")
    p_rollback.add_argument("--db", default=None, help="ledger DB path (default: ledger.db)")
    p_rollback.add_argument("--client", default=None, help="named client from clients.json, instead of --db")
    p_rollback.set_defaults(func=_cmd_rollback)

    p_clients = sub.add_parser("clients", help="list clients registered in clients.json")
    p_clients.set_defaults(func=_cmd_clients)

    args = parser.parse_args()
    if args.command == "ask" and not args.interactive and not args.question:
        parser.error("ask requires a question, or pass --interactive")
    if getattr(args, "client", None) and getattr(args, "db", None):
        parser.error("pass either --client or --db, not both")
    args.func(args)


if __name__ == "__main__":
    main()
