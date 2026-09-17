"""A thin browser front end over the same tested pipeline and agent used by the CLI.

Imports are jobs (see ingest/pipeline.py), exposed as a small JSON API:

    POST   /api/imports                  upload one or more files; each becomes an ImportJob (202)
    GET    /api/imports                  recent imports
    GET    /api/imports/<id>             state + plain-language message + summary
    GET    /api/imports/<id>/preview     detected table, proposed column mapping, sample rows, issues
    PUT    /api/imports/<id>/mapping     confirm/correct column choices (re-runs normalization)
    PUT    /api/imports/<id>/review      leave rows out, confirm assumptions, set money in/out
    POST   /api/imports/<id>/commit      add to the ledger atomically (409 unless ready)
    POST   /api/imports/<id>/rollback    undo one committed import
    POST   /api/imports/<id>/retry       re-read a failed import
    DELETE /api/imports/<id>             cancel an import that wasn't added

Reading a file (OCR in particular) runs on a small background worker, not in the request thread;
the browser polls the job. Every /api/ error is JSON, including 404/405/413. State-changing API
calls must carry the page's CSRF token in an X-CSRF-Token header: this server listens on
localhost, and without it any website open in the same browser could post files or questions to
it (a multipart form POST needs no CORS preflight).

This is still a local, single-user tool: no accounts or authentication. See NOT_IMPLEMENTED.md §I.
"""

from __future__ import annotations

import os
import secrets
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from ..ingest.mapping import MappingError
from ..ingest.pipeline import (
    SUPPORTED_EXTENSIONS, ImportConflict, analyze_import, cancel_import, commit_import, create_import, job_view,
    rollback_import, update_mapping, update_review,
)
from ..ingest.sniff import detect_encoding
from ..ingest.vocab import KNOWN_CURRENCIES, ROLE_LABELS, ROLES
from ..schema import ImportState
from ..store import Store

# Where browser uploads are kept, one server-generated directory per import — separate from
# dataset_public/ since this holds people's real financial documents. Gitignored.
UPLOAD_DIR = "uploaded_documents"

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # per file
MAX_FILES_PER_REQUEST = 20
_MAX_XLSX_UNCOMPRESSED = 200 * 1024 * 1024
_MAX_XLSX_RATIO = 200
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_FRIENDLY_HTTP = {
    404: "Not found.",
    405: "That action isn't allowed here.",
    413: f"That upload is too large. Each file can be up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
}


def signature_problem(ext: str, head: bytes) -> str | None:
    """File contents must match the extension — a renamed executable is never opened."""
    if ext == ".pdf":
        ok = b"%PDF-" in head[:1024]
    elif ext == ".xlsx":
        ok = head.startswith(b"PK\x03\x04")
    elif ext == ".png":
        ok = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in (".jpg", ".jpeg"):
        ok = head.startswith(b"\xff\xd8\xff")
    elif ext == ".csv":
        binary_magic = (b"%PDF", b"PK\x03\x04", b"MZ", b"\x7fELF", b"\x89PNG", b"\xff\xd8\xff", b"\xd0\xcf\x11\xe0")
        ok = not head.startswith(binary_magic) and (b"\x00" not in head or detect_encoding(head)[0].startswith("utf-16"))
    else:
        ok = False
    return None if ok else f"This file's contents don't match its {ext} name, so I didn't open it."


def xlsx_container_problem(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
            if "xl/workbook.xml" not in names:
                return "This isn't a valid Excel workbook."
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                return "This workbook contains macros, so I didn't open it. Save it as a plain .xlsx and try again."
            total = sum(i.file_size for i in infos)
            packed = sum(i.compress_size for i in infos) or 1
            if total > _MAX_XLSX_UNCOMPRESSED or total / packed > _MAX_XLSX_RATIO:
                return "This workbook expands to an unsafe size, so I didn't open it."
    except zipfile.BadZipFile:
        return "This isn't a valid Excel workbook."
    return None


def create_app(db_path: str = "ledger.db", *, upload_dir: str = UPLOAD_DIR, run_imports_inline: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["UPLOAD_DIR"] = upload_dir
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES * MAX_FILES_PER_REQUEST + 1024 * 1024
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    executor = None if run_imports_inline else ThreadPoolExecutor(max_workers=2, thread_name_prefix="import")

    if os.path.exists(db_path):
        store = Store(db_path)
        store.fail_interrupted_jobs()
        store.close()

    def _store() -> Store:
        return Store(app.config["DB_PATH"])

    def _analyze(job_id: str) -> None:
        store = _store()
        try:
            analyze_import(store, job_id, attempt_vision=True)
        finally:
            store.close()

    def _schedule(job_id: str) -> None:
        if executor is None:
            _analyze(job_id)
        else:
            executor.submit(_analyze, job_id)

    def _discard_upload(stored_path: str) -> None:
        path = os.path.realpath(stored_path)
        root = os.path.realpath(app.config["UPLOAD_DIR"])
        if path.startswith(root + os.sep):  # never delete a file the CLI imported from the user's own folder
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def _error(message: str, code: int, **extra):
        return jsonify({"error": message, **extra}), code

    @app.before_request
    def _csrf():
        if request.path.startswith("/api/") and request.method in _STATE_CHANGING:
            token = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, app.config["CSRF_TOKEN"]):
                return _error("Missing or invalid security token. Reload the page and try again.", 403)
        return None

    @app.errorhandler(HTTPException)
    def _http_error(e: HTTPException):
        if request.path.startswith("/api/"):
            return _error(_FRIENDLY_HTTP.get(e.code, e.description or e.name), e.code or 500)
        return e

    @app.errorhandler(Exception)
    def _unhandled(e: Exception):
        if isinstance(e, HTTPException):
            return _http_error(e)
        app.logger.exception("unhandled error")
        if request.path.startswith("/api/"):
            return _error("Something went wrong on our side. Please try again.", 500)
        return "Internal Server Error", 500

    @app.route("/")
    def index():
        return render_template("index.html", csrf_token=app.config["CSRF_TOKEN"])

    @app.route("/api/status")
    def status():
        path = app.config["DB_PATH"]
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not os.path.exists(path):
            return jsonify({
                "ready": False,
                "reason": "No statements added yet — add one below, or run "
                          "`python -m statement_agent.cli ingest` from the command line.",
                "transaction_count": 0, "document_count": 0, "has_api_key": has_key,
            })
        store = _store()
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()
        return jsonify({
            "ready": bool(ledger),
            "reason": None if ledger else "No transactions yet — add a statement below.",
            "transaction_count": len(ledger), "document_count": len(documents), "has_api_key": has_key,
        })

    # -- imports -----------------------------------------------------------------------

    @app.route("/api/imports", methods=["POST"])
    def create_imports():
        files = request.files.getlist("files")
        if not files:
            return _error("no files provided", 400)
        if len(files) > MAX_FILES_PER_REQUEST:
            return _error(f"Please add at most {MAX_FILES_PER_REQUEST} files at a time.", 400)

        created, rejected = [], []
        store = _store()
        try:
            for f in files:
                original = f.filename or "(unnamed)"
                safe = secure_filename(f.filename or "")
                ext = os.path.splitext(safe)[1].lower()
                if not safe or ext not in SUPPORTED_EXTENSIONS:
                    rejected.append({"file": original, "error": "This type of file isn't supported. Use PDF, CSV, Excel (.xlsx), JPG or PNG."})
                    continue
                f.stream.seek(0, os.SEEK_END)
                size = f.stream.tell()
                f.stream.seek(0)
                if size == 0:
                    rejected.append({"file": original, "error": "This file is empty."})
                    continue
                if size > MAX_UPLOAD_BYTES:
                    rejected.append({"file": original, "error": _FRIENDLY_HTTP[413]})
                    continue
                problem = signature_problem(ext, f.stream.read(8192))
                f.stream.seek(0)
                if problem:
                    rejected.append({"file": original, "error": problem})
                    continue

                job_id = str(uuid.uuid4())
                job_dir = os.path.join(app.config["UPLOAD_DIR"], job_id)  # server-generated, one per import
                os.makedirs(job_dir, exist_ok=True)
                dest = os.path.join(job_dir, safe)
                f.save(dest)
                if ext == ".xlsx":
                    problem = xlsx_container_problem(dest)
                    if problem:
                        shutil.rmtree(job_dir, ignore_errors=True)
                        rejected.append({"file": original, "error": problem})
                        continue
                create_import(store, dest, original, job_id=job_id)
                created.append(job_id)
        finally:
            store.close()

        for job_id in created:
            _schedule(job_id)

        store = _store()
        try:
            views = [job_view(store.get_job(j), store.get_staging(j)) for j in created]
        finally:
            store.close()
        return jsonify({"imports": views, "rejected": rejected}), 202 if created else 400

    @app.route("/api/imports", methods=["GET"])
    def list_imports():
        if not os.path.exists(app.config["DB_PATH"]):
            return jsonify({"imports": []})
        store = _store()
        try:
            return jsonify({"imports": [job_view(j, store.get_staging(j.job_id)) for j in store.list_jobs()]})
        finally:
            store.close()

    def _with_job(job_id: str, fn):
        store = _store()
        try:
            job = store.get_job(job_id)
            if job is None:
                return _error("Not found.", 404)
            return fn(store, job)
        except MappingError as e:
            return _error(str(e), 400)
        except ImportConflict as e:
            job = store.get_job(job_id)
            return _error(str(e), 409, **{"import": job_view(job, store.get_staging(job_id))})
        finally:
            store.close()

    def _preview(store: Store, job) -> dict:
        staging = store.get_staging(job.job_id) or {}
        view = job_view(job, staging)
        view.update({
            "headers": staging.get("headers", []),
            "sample_rows": staging.get("sample_rows", []),
            "preamble": staging.get("preamble", []),
            "mapping": staging.get("mapping"),
            "sniff": staging.get("sniff"),
            "issues": staging.get("issues", []),
            "ignored_rows": staging.get("ignored_rows", [])[:50],
            "transactions": staging.get("preview_transactions", []),
            "decisions": staging.get("decisions"),
            "warnings": staging.get("warnings", []),
            "role_options": [{"value": r, "label": ROLE_LABELS[r]} for r in ROLES],
            "currencies": sorted(KNOWN_CURRENCIES),
        })
        return view

    @app.route("/api/imports/<job_id>", methods=["GET"])
    def get_import(job_id):
        return _with_job(job_id, lambda s, j: jsonify(job_view(j, s.get_staging(job_id))))

    @app.route("/api/imports/<job_id>/preview", methods=["GET"])
    def preview_import(job_id):
        return _with_job(job_id, lambda s, j: jsonify(_preview(s, j)))

    @app.route("/api/imports/<job_id>/mapping", methods=["PUT"])
    def put_mapping(job_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error("Expected a JSON object.", 400)
        return _with_job(job_id, lambda s, j: jsonify(_preview(s, update_mapping(s, job_id, data))))

    @app.route("/api/imports/<job_id>/review", methods=["PUT"])
    def put_review(job_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error("Expected a JSON object.", 400)
        return _with_job(job_id, lambda s, j: jsonify(_preview(s, update_review(s, job_id, data))))

    @app.route("/api/imports/<job_id>/commit", methods=["POST"])
    def post_commit(job_id):
        return _with_job(job_id, lambda s, j: jsonify(job_view(commit_import(s, job_id), s.get_staging(job_id))))

    @app.route("/api/imports/<job_id>/rollback", methods=["POST"])
    def post_rollback(job_id):
        def run(store, job):
            job = rollback_import(store, job_id)
            _discard_upload(job.stored_path)
            return jsonify(job_view(job, store.get_staging(job_id)))
        return _with_job(job_id, run)

    @app.route("/api/imports/<job_id>/retry", methods=["POST"])
    def post_retry(job_id):
        def run(store, job):
            if job.state != ImportState.FAILED or not os.path.exists(job.stored_path):
                raise ImportConflict("Only a failed import whose file is still here can be retried.")
            job.state, job.error_summary = ImportState.ANALYZING, None
            store.save_job(job)
            _schedule(job_id)
            return jsonify(job_view(store.get_job(job_id), store.get_staging(job_id))), 202
        return _with_job(job_id, run)

    @app.route("/api/imports/<job_id>", methods=["DELETE"])
    def delete_import(job_id):
        def run(store, job):
            job = cancel_import(store, job_id)
            _discard_upload(job.stored_path)
            return jsonify(job_view(job, store.get_staging(job_id)))
        return _with_job(job_id, run)

    # -- questions ---------------------------------------------------------------------------

    @app.route("/api/ask", methods=["POST"])
    def ask():
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or "").strip()
        if not question:
            return _error("question is required", 400)

        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _error("ANTHROPIC_API_KEY is not set on the server.", 500)

        path = app.config["DB_PATH"]
        if not os.path.exists(path):
            return _error(f"No ledger found at '{path}'. Add a statement (or run ingest) first.", 400)

        store = _store()
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()

        if not ledger:
            return _error("Ledger is empty. Add a statement (or run ingest) first.", 400)

        from ..agent.loop import run_agent

        try:
            result = run_agent(question, ledger, documents=documents)
        except Exception as e:  # noqa: BLE001 - surface a clean API error, never a stack trace to the browser
            import anthropic

            if isinstance(e, anthropic.APIStatusError):
                message = getattr(e, "message", str(e))
                return _error(f"Anthropic API error: {message}", 502)
            return _error(f"{type(e).__name__}: {e}", 500)

        fa = result.final_answer

        # A generate_chart or generate_dashboard call leaves a real PNG on disk (see agent/tools.py) —
        # embedded as a data URI so the browser renders it inline; the dashboard's table_rows render in
        # the same answer card, never a separate page.
        chart_image = None
        dashboard_table = None
        for record in result.trace:
            if record.tool_name in ("generate_chart", "generate_dashboard") and isinstance(record.tool_result, dict):
                chart_path = record.tool_result.get("chart_path")
                if chart_path and os.path.exists(chart_path):
                    try:
                        import base64

                        with open(chart_path, "rb") as f:
                            chart_image = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
                    except OSError:
                        chart_image = None
                if record.tool_name == "generate_dashboard" and "table_rows" in record.tool_result:
                    dashboard_table = {
                        "rows": record.tool_result["table_rows"],
                        "total_rows": record.tool_result["table_total_rows"],
                        "truncated": record.tool_result["table_truncated"],
                        "currency": record.tool_result.get("currency"),
                    }

        # "Why?" — the cited source rows themselves, so an answer can be checked against the statement.
        by_id = {t.transaction_id: t for t in ledger}
        sources = []
        for tid in fa.cited_transaction_ids[:25]:
            t = by_id.get(tid)
            if t is None:
                continue
            src = t.source
            sources.append({
                "date": t.transaction_date.isoformat() if t.transaction_date else None,
                "description": t.description_raw, "amount": str(t.amount), "currency": t.currency,
                "direction": t.direction.value,
                "file": os.path.basename(src.file_path) if src and src.file_path else None,
                "page": src.page if src else None, "row": src.row if src else None,
                "text": (src.raw_text if src else "")[:240],
            })

        return jsonify({
            "status": result.verification.status,
            "answer_text": fa.answer_text,
            "amounts": [{"currency": a.currency, "amount": a.amount, "label": a.label} for a in fa.verified_amounts],
            "caveats": fa.caveats,
            "cited_count": len(fa.cited_transaction_ids),
            "sources": sources,
            "verification_passed": result.verification.passed,
            "verification_failures": result.verification.failures,
            "dashboard_table": dashboard_table,
            "trace": [{"tool": r.tool_name, "input": r.tool_input} for r in result.trace],
            "chart_image": chart_image,
        })

    return app
