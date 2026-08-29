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

from ..store import Store


def create_app(db_path: str = "ledger.db") -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def status():
        path = app.config["DB_PATH"]
        if not os.path.exists(path):
            return jsonify({
                "ready": False,
                "reason": f"No ledger found at '{path}'. Run `python -m statement_agent.cli ingest` first.",
            })
        store = Store(path)
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()
        return jsonify({
            "ready": True,
            "transaction_count": len(ledger),
            "document_count": len(documents),
            "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        })

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
        return jsonify({
            "status": result.verification.status,
            "answer_text": fa.answer_text,
            "amounts": [{"currency": a.currency, "amount": a.amount, "label": a.label} for a in fa.verified_amounts],
            "caveats": fa.caveats,
            "cited_count": len(fa.cited_transaction_ids),
            "verification_passed": result.verification.passed,
            "verification_failures": result.verification.failures,
            "trace": [{"tool": r.tool_name, "input": r.tool_input} for r in result.trace],
        })

    return app
