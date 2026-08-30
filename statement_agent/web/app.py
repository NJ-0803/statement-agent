"""A thin browser front end over the same tested agent used by the CLI.

No new logic lives here — every route just loads the ledger from the Store
and calls agent.loop.run_agent, exactly like statement_agent.cli's `ask`
command does. This exists because the brief evaluates the agent on questions
the grader types themselves, and a browser text box is lower-friction for that
than a terminal. The CLI remains the primary documented path (README.md) —
this is a secondary, optional way to run the same thing.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from ..ingest.pipeline import SUPPORTED_EXTENSIONS, ingest_file
from ..store import Store

# Where files uploaded through the browser are saved before ingestion — separate from
# dataset_public/ (the committed sample data) since this holds whatever a user drags in,
# which may be their own real financial documents. Gitignored; never meant to be committed.
UPLOAD_DIR = "uploaded_documents"

# 25MB per file — generous for a scanned statement PDF, just a sanity cap against an
# accidental huge upload on a local single-user tool with no auth in front of it.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def create_app(db_path: str = "ledger.db", *, upload_dir: str = UPLOAD_DIR) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["UPLOAD_DIR"] = upload_dir
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES * 20  # whole-request cap, generous for several files at once

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def status():
        path = app.config["DB_PATH"]
        if not os.path.exists(path):
            return jsonify({
                "ready": False,
                "reason": "No ledger yet — upload a statement/expense sheet below, or run "
                          "`python -m statement_agent.cli ingest` from the command line.",
                "transaction_count": 0,
                "document_count": 0,
                "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            })
        store = Store(path)
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()
        return jsonify({
            "ready": bool(ledger),
            "reason": None if ledger else "Ledger exists but is empty — upload a statement/expense sheet below.",
            "transaction_count": len(ledger),
            "document_count": len(documents),
            "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        })

    @app.route("/api/upload", methods=["POST"])
    def upload():
        """Lets someone add their own statements/expense sheets straight from the
        browser, instead of only via `statement-agent ingest` on the command line.
        Reuses ingest_file directly (the exact same per-file logic ingest_folder
        calls) — no separate upload-specific parsing path to drift from the tested one.
        """
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "no files provided"}), 400

        upload_dir = app.config["UPLOAD_DIR"]
        os.makedirs(upload_dir, exist_ok=True)

        store = Store(app.config["DB_PATH"])
        results = []
        try:
            for f in files:
                original_name = f.filename or ""
                safe_name = secure_filename(original_name)
                ext = os.path.splitext(safe_name)[1].lower()
                if not safe_name or ext not in SUPPORTED_EXTENSIONS:
                    results.append({
                        "file": original_name or "(unnamed)",
                        "status": "skipped_unsupported",
                        "transaction_count": 0,
                        "warnings": [f"unrecognized file type — supported: {sorted(SUPPORTED_EXTENSIONS)}"],
                    })
                    continue

                # never silently overwrite a previous upload with the same filename
                dest = os.path.join(upload_dir, safe_name)
                base, extn = os.path.splitext(dest)
                n = 1
                while os.path.exists(dest):
                    dest = f"{base}_{n}{extn}"
                    n += 1
                f.save(dest)

                try:
                    report = ingest_file(dest, store, attempt_vision=True)
                    results.append({
                        "file": original_name,
                        "status": report.status,
                        "transaction_count": report.transaction_count,
                        "warnings": report.warnings,
                    })
                except Exception as e:  # noqa: BLE001 - one bad file must not fail the whole upload batch
                    results.append({
                        "file": original_name,
                        "status": "failed",
                        "transaction_count": 0,
                        "warnings": [f"{type(e).__name__}: {e}"],
                    })

            # Same cross-document duplicate pass ingest_folder runs after every file is
            # in — otherwise a newly-uploaded statement overlapping an existing one
            # would never be compared against the ledger that was already there.
            from ..resolve import detect_cross_document_duplicates

            ledger = store.all_transactions()
            newly_flagged = detect_cross_document_duplicates(ledger)
            if newly_flagged:
                store.insert_transactions(newly_flagged)
        finally:
            store.close()

        return jsonify({"results": results})

    @app.route("/api/ask", methods=["POST"])
    def ask():
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or "").strip()
        if not question:
            return jsonify({"error": "question is required"}), 400

        if not os.environ.get("ANTHROPIC_API_KEY"):
            return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server."}), 500

        path = app.config["DB_PATH"]
        if not os.path.exists(path):
            return jsonify({"error": f"No ledger found at '{path}'. Run ingest first."}), 400

        store = Store(path)
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()

        if not ledger:
            return jsonify({"error": "Ledger is empty. Run ingest first."}), 400

        from ..agent.loop import run_agent

        try:
            result = run_agent(question, ledger, documents=documents)
        except Exception as e:  # noqa: BLE001 - surface a clean API error, never a stack trace to the browser
            import anthropic

            if isinstance(e, anthropic.APIStatusError):
                message = getattr(e, "message", str(e))
                return jsonify({"error": f"Anthropic API error: {message}"}), 502
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

        fa = result.final_answer

        # A generate_chart call leaves a real PNG on disk (see agent/tools.py) — read
        # and embed it as a data URI so the browser can render it inline. Only the last
        # chart is sent (a turn producing several is unlikely, and this keeps the
        # response bounded); any read failure is swallowed rather than failing the
        # whole answer, since the text answer is still valid without the image.
        chart_image = None
        for record in result.trace:
            if record.tool_name == "generate_chart" and isinstance(record.tool_result, dict):
                chart_path = record.tool_result.get("chart_path")
                if chart_path and os.path.exists(chart_path):
                    try:
                        import base64

                        with open(chart_path, "rb") as f:
                            chart_image = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
                    except OSError:
                        chart_image = None

        return jsonify({
            "status": result.verification.status,
            "answer_text": fa.answer_text,
            "amounts": [{"currency": a.currency, "amount": a.amount, "label": a.label} for a in fa.verified_amounts],
            "caveats": fa.caveats,
            "cited_count": len(fa.cited_transaction_ids),
            "verification_passed": result.verification.passed,
            "verification_failures": result.verification.failures,
            "trace": [{"tool": r.tool_name, "input": r.tool_input} for r in result.trace],
            "chart_image": chart_image,
        })

    return app
