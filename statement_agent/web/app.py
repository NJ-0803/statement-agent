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

    GET    /api/transactions             added transactions (search, filter), with why each has its category
    POST   /api/transactions/<id>/correction   change one row, or make a rule for matching rows
    GET    /api/rules                    your correction rules
    POST   /api/rules/preview            which rows a rule with these words would cover
    DELETE /api/rules/<id>               remove a rule; affected rows go back to automatic
    GET    /api/links                    linked transactions (refunds, transfers, …) and regular payments
    POST   /api/links                    link a money-out row and a money-in row yourself
    POST   /api/links/<id>/decision      yes / not related / undo your decision

Reading a file (OCR in particular) runs on a small background worker, not in the request thread;
the browser polls the job. Every /api/ error is JSON, including 404/405/413. State-changing API
calls must carry the page's CSRF token in an X-CSRF-Token header: this server listens on
localhost, and without it any website open in the same browser could post files or questions to
it (a multipart form POST needs no CORS preflight).

Where this runs is decided by STATEMENT_AGENT_MODE (see auth.py): `local` is the original
single-user tool with no login, `owner` puts one passphrase in front of your own ledger, and `demo`
serves a throwaway copy of the synthetic sample data to each visitor and cannot open a real ledger
at all (see demo.py).
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from .. import ledger_edits
from ..corrections import CorrectionError, describe_source, rule_matches, suggested_pattern
from ..ingest.mapping import MappingError
from ..ingest.pipeline import (
    SUPPORTED_EXTENSIONS, ImportConflict, analyze_import, cancel_import, commit_import, create_import, job_view,
    rollback_import, unlock_import, update_mapping, update_review,
)
from ..ingest.sniff import detect_encoding
from ..ingest.vocab import KNOWN_CURRENCIES, ROLE_LABELS, ROLES
from ..schema import EconomicType, ImportState
from ..store import Store
from . import auth, demo, redact

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
    elif ext in (".ods", ".docx"):
        ok = head.startswith(b"PK\x03\x04")
    elif ext == ".xls":
        ok = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    elif ext in _TEXT_EXTENSIONS:
        binary_magic = (b"%PDF", b"PK\x03\x04", b"MZ", b"\x7fELF", b"\x89PNG", b"\xff\xd8\xff", b"\xd0\xcf\x11\xe0")
        ok = not head.startswith(binary_magic) and (b"\x00" not in head or detect_encoding(head)[0].startswith("utf-16"))
    else:
        ok = False
    return None if ok else f"This file's contents don't match its {ext} name, so I didn't open it."


_TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".tab", ".psv", ".dat", ".ofx", ".qfx", ".qif", ".sta", ".mt940",
                    ".940", ".json", ".xml", ".html", ".htm"}
_CONTAINER_MAIN_PART = {".xlsx": ("xl/workbook.xml", "Excel workbook"), ".ods": ("content.xml", "OpenDocument spreadsheet"),
                        ".docx": ("word/document.xml", "Word document")}


def xlsx_container_problem(path: str, ext: str = ".xlsx") -> str | None:
    part, what = _CONTAINER_MAIN_PART[ext]
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
            if part not in names:
                return f"This isn't a valid {what}."
            if any(n.lower().endswith("vbaproject.bin") or n.lower().startswith("basic/") for n in names):
                return f"This file contains macros, so I didn't open it. Save it as a plain {ext} and try again."
            total = sum(i.file_size for i in infos)
            packed = sum(i.compress_size for i in infos) or 1
            if total > _MAX_XLSX_UNCOMPRESSED or total / packed > _MAX_XLSX_RATIO:
                return "This file expands to an unsafe size, so I didn't open it."
    except zipfile.BadZipFile:
        return f"This isn't a valid {what}."
    return None


def create_app(db_path: str = "ledger.db", *, upload_dir: str = UPLOAD_DIR, run_imports_inline: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["UPLOAD_DIR"] = upload_dir
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES * MAX_FILES_PER_REQUEST + 1024 * 1024
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    app.config["MODE"] = auth.mode()
    executor = None if run_imports_inline else ThreadPoolExecutor(max_workers=2, thread_name_prefix="import")

    key, durable = auth.secret_key()
    app.secret_key = key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Only send the session cookie over HTTPS once this is on a network. A browser drops a
        # Secure cookie on a plain-HTTP origin, so trying demo/owner mode locally without TLS needs
        # STATEMENT_AGENT_INSECURE_COOKIES=1 — never set that on anything reachable from outside.
        SESSION_COOKIE_SECURE=(app.config["MODE"] != "local"
                               and os.environ.get("STATEMENT_AGENT_INSECURE_COOKIES") != "1"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )
    if app.config["MODE"] != "local" and not durable:
        app.logger.warning("STATEMENT_AGENT_SECRET_KEY is not set: everyone is signed out on restart.")

    login_throttle = auth.LoginThrottle()

    if app.config["MODE"] != "local":
        # once this is on a network the logs are no longer only yours to read
        redact.install()
        app.logger.addFilter(redact.RedactingFilter())

    def _db_path() -> str:
        """The ledger this request may use.

        In demo mode this never consults configuration: it is derived from the visitor's own session
        and checked to be inside the demo area, so a demo instance cannot serve a real ledger.
        """
        if app.config["MODE"] == "demo":
            return demo.sandbox_paths(_sandbox_id())[0]
        return app.config["DB_PATH"]

    def _upload_dir() -> str:
        if app.config["MODE"] == "demo":
            return demo.sandbox_paths(_sandbox_id())[1]
        return app.config["UPLOAD_DIR"]

    def _sandbox_id() -> str:
        sandbox_id = session.get(demo.SESSION_KEY)
        if not sandbox_id:
            sandbox_id = uuid.uuid4().hex
            session[demo.SESSION_KEY] = sandbox_id
            session.permanent = True
        return sandbox_id

    if app.config["MODE"] == "demo":
        demo.sweep()  # clear anything a previous run left behind before taking any traffic
    elif os.path.exists(db_path):
        store = Store(db_path)
        store.fail_interrupted_jobs()
        store.close()

    def _store() -> Store:
        return Store(_db_path())

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
        root = os.path.realpath(_upload_dir())
        if path.startswith(root + os.sep):  # never delete a file the CLI imported from the user's own folder
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def _error(message: str, code: int, **extra):
        return jsonify({"error": message, **extra}), code

    hits: dict[tuple[str, str], list[float]] = {}
    limits = {"ask": (10, 60), "groq": (6, 60), "upload": (30, 60), "write": (240, 60)}

    @app.before_request
    def _rate_limit():
        """Per-client request limits, in memory: enough to stop a runaway page or script from burning API credits
        or hammering the ledger. Localhost-only app, so this is a safety valve, not DDoS protection."""
        if not request.path.startswith("/api/") or request.method not in _STATE_CHANGING:
            return None
        bucket = ("ask" if request.path == "/api/ask" else "groq" if "groq" in request.path or "suggest-columns" in request.path
                  else "upload" if request.path == "/api/imports" else "write")
        limit, window = limits[bucket]
        now = time.monotonic()
        key = (request.remote_addr or "?", bucket)
        recent = [t for t in hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            hits[key] = recent
            return _error("Too many requests in a short time. Please wait a minute and try again.", 429)
        recent.append(now)
        hits[key] = recent
        return None

    @app.before_request
    def _require_login():
        """Deny by default. A route added later is protected without anyone remembering to do it.

        Only auth.PUBLIC_PATHS are open. In local mode there is nothing to sign in to and this lets
        everything through, so running it on your own machine is unchanged.
        """
        if not auth.needs_login() or auth.is_public_path(request.path):
            return None
        if session.get("signed_in") is True:
            return None
        if request.path.startswith("/api/"):
            return _error("Please sign in again.", 401)
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        """For a host's health check. Says nothing about the data — only that the process is up."""
        return jsonify({"ok": True, "mode": app.config["MODE"]})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.needs_login():
            return redirect(url_for("index"))
        expected = auth.stored_hash()
        client = request.remote_addr or "?"
        if request.method == "GET":
            return render_template("login.html", error=None, configured=bool(expected)), 200

        if not expected:
            # no passphrase set: say so plainly rather than letting anyone in
            return render_template("login.html", configured=False,
                                   error="No passphrase has been set on this server yet."), 503
        if login_throttle.blocked(client):
            wait = login_throttle.seconds_until_retry(client)
            return render_template("login.html", configured=True,
                                   error=f"Too many attempts. Try again in about {wait // 60 + 1} minute(s)."), 429
        if not auth.verify_passphrase(request.form.get("passphrase", ""), expected):
            login_throttle.record_failure(client)
            return render_template("login.html", configured=True, error="That passphrase didn't match."), 401

        login_throttle.clear(client)
        session.clear()  # new session id on sign-in, so a fixed cookie cannot be reused
        session["signed_in"] = True
        session.permanent = True
        return redirect(url_for("index"))

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login") if auth.needs_login() else url_for("index"))

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
        if app.config["MODE"] == "demo":
            demo.touch(_sandbox_id())
        return render_template("index.html", csrf_token=app.config["CSRF_TOKEN"],
                               mode=app.config["MODE"], demo_ttl_minutes=demo.SANDBOX_TTL_SECONDS // 60)

    @app.route("/api/status")
    def status():
        path = _db_path()
        from ..groq_categorize import groq_enabled

        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not os.path.exists(path):
            return jsonify({
                "ready": False,
                "reason": "No statements added yet — add one below, or run "
                          "`python -m statement_agent.cli ingest` from the command line.",
                "transaction_count": 0, "document_count": 0, "has_api_key": has_key, "groq": groq_enabled(),
                "mode": app.config["MODE"],
            })
        store = _store()
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        store.close()
        return jsonify({
            "ready": bool(ledger),
            "reason": None if ledger else "No transactions yet — add a statement below.",
            "transaction_count": len(ledger), "document_count": len(documents), "has_api_key": has_key,
            "groq": groq_enabled(), "mode": app.config["MODE"],
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
                    rejected.append({"file": original, "error": "This type of file isn't supported. Use a PDF; a spreadsheet (CSV, TSV, Excel, ODS); a bank download (OFX, QFX, QIF, MT940); a JSON or XML export; a .docx or .html statement; or a JPG/PNG photo."})
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
                job_dir = os.path.join(_upload_dir(), job_id)  # server-generated, one per import
                os.makedirs(job_dir, exist_ok=True)
                dest = os.path.join(job_dir, safe)
                f.save(dest)
                if ext in _CONTAINER_MAIN_PART:
                    problem = xlsx_container_problem(dest, ext)
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
        if not os.path.exists(_db_path()):
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
            "extra_columns": staging.get("extra_columns", []),
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

    @app.route("/api/imports/<job_id>/password", methods=["POST"])
    def post_password(job_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("password"), str):
            return _error("password is required", 400)

        def run(store, job):
            path = os.path.realpath(job.stored_path)
            if not path.startswith(os.path.realpath(_upload_dir()) + os.sep):
                raise ImportConflict("Only files added through this page can be unlocked here.")
            unlock_import(store, job_id, data["password"], dest_path=path)
            job = store.get_job(job_id)
            job.state = ImportState.ANALYZING
            store.save_job(job)
            _schedule(job_id)
            return jsonify(job_view(store.get_job(job_id), store.get_staging(job_id))), 202
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

    # -- corrections -------------------------------------------------------------------------

    def _with_store(fn):
        store = _store()
        try:
            return fn(store)
        except CorrectionError as e:
            return _error(str(e), 400)
        except KeyError:
            return _error("Not found.", 404)
        finally:
            store.close()

    def _txn_view(t, rules_by_id, links_by_txn=None, by_id=None):
        src = t.source
        links = []
        for e in (links_by_txn or {}).get(t.transaction_id, []):
            others = [by_id[m.transaction_id] for m in e.members if m.transaction_id != t.transaction_id and m.transaction_id in by_id]
            links.append({
                "id": e.event_id, "kind": e.kind.value, "status": e.status.value, "reason": e.reason,
                "others": [{"date": o.transaction_date.isoformat() if o.transaction_date else None,
                            "description": o.description_raw, "amount": str(o.amount), "currency": o.currency,
                            "direction": o.direction.value} for o in others[:3]],
            })
        return {
            "economic_type": t.economic_type.value,
            "type_label": ledger_edits.TYPE_LABELS.get(t.economic_type.value, t.economic_type.value.title()),
            "type_source": t.economic_type_source, "type_unsure": (t.economic_type_confidence or 1.0) < 1.0,
            "links": links,
            "id": t.transaction_id,
            "date": t.transaction_date.isoformat() if t.transaction_date else None,
            "description": t.description_raw, "amount": str(t.amount), "currency": t.currency,
            "direction": t.direction.value, "is_purchase": t.economic_type == EconomicType.PURCHASE,
            "kind": t.economic_type.value.replace("_", " ").lower(),
            "category": t.category, "category_source": t.category_source,
            "merchant_name": t.merchant_canonical, "merchant_source": t.merchant_source,
            "why": describe_source(t, rules_by_id), "suggested_pattern": suggested_pattern(t),
            "file": os.path.basename(src.file_path) if src and src.file_path else None,
            "extra_fields": dict(t.extra_fields),
        }

    def _links_by_txn(events):
        out = {}
        for e in events:
            if e.kind.value == "recurring" or e.status.value == "rejected":
                continue
            for m in e.members:
                out.setdefault(m.transaction_id, []).append(e)
        return out

    def _event_json(e, by_id):
        return {
            "id": e.event_id, "kind": e.kind.value, "status": e.status.value, "reason": e.reason,
            "details": e.details, "source": e.source,
            "members": [{"id": m.transaction_id, "role": m.role,
                         **({"date": t.transaction_date.isoformat() if t.transaction_date else None,
                             "description": t.description_raw, "amount": str(t.amount), "currency": t.currency,
                             "direction": t.direction.value} if (t := by_id.get(m.transaction_id)) else {})}
                        for m in e.members],
        }

    @app.route("/api/links", methods=["GET"])
    def list_links():
        if not os.path.exists(_db_path()):
            return jsonify({"links": [], "recurring": []})

        def run(store):
            by_id = {t.transaction_id: t for t in store.all_transactions()}
            events = store.list_events()
            links = [e for e in events if e.kind.value != "recurring"]
            recurring = [e for e in events if e.kind.value == "recurring"]
            order = {"suggested": 0, "matched": 1, "confirmed": 2, "rejected": 3}
            links.sort(key=lambda e: order[e.status.value])
            return jsonify({"links": [_event_json(e, by_id) for e in links],
                            "recurring": [_event_json(e, by_id) for e in recurring]})
        return _with_store(run)

    @app.route("/api/links", methods=["POST"])
    def post_link():
        data = _json_body()
        if data is None or not all(isinstance(data.get(k), str) for k in ("kind", "out_id", "in_id")):
            return _error("kind, out_id and in_id are required", 400)

        def run(store):
            event = ledger_edits.link_manually(store, data["kind"], data["out_id"], data["in_id"])["event"]
            return jsonify({"link": _event_json(event, {t.transaction_id: t for t in store.all_transactions()})})
        return _with_store(run)

    @app.route("/api/links/<event_id>/decision", methods=["POST"])
    def post_link_decision(event_id):
        data = _json_body()
        if data is None or "decision" not in data:
            return _error("decision is required", 400)

        def run(store):
            event = ledger_edits.decide_link(store, event_id, data["decision"])["event"]
            by_id = {t.transaction_id: t for t in store.all_transactions()}
            return jsonify({"link": _event_json(event, by_id) if event else None})
        return _with_store(run)

    def _rule_view(rule, ledger):
        covered = [t for t in ledger if rule.rule_id in (t.category_rule_id, t.merchant_rule_id, t.economic_type_rule_id)]
        return {
            "id": rule.rule_id, "pattern": rule.pattern, "category": rule.category,
            "economic_type": rule.economic_type,
            "type_label": ledger_edits.TYPE_LABELS.get(rule.economic_type) if rule.economic_type else None,
            "merchant_name": rule.merchant_name, "updated_at": rule.updated_at,
            "applied_to": len(covered), "matches": sum(1 for t in ledger if rule_matches(rule, t)),
        }

    @app.route("/api/transactions", methods=["GET"])
    def list_transactions():
        if not os.path.exists(_db_path()):
            return jsonify({"total": 0, "transactions": [], "categories": [], "rules": []})

        def run(store):
            rules = store.list_rules()
            by_id = {r.rule_id: r for r in rules}
            ledger = store.all_transactions()
            txn_by_id = {t.transaction_id: t for t in ledger}
            links_by_txn = _links_by_txn(store.list_events())
            q = (request.args.get("q") or "").strip().lower()
            category = request.args.get("category") or ""
            rows = [
                t for t in ledger
                if (not q or q in f"{t.description_raw} {t.merchant_canonical or ''} {' '.join(t.extra_fields.values())}".lower())
                and (not category or (t.category or "") == ("" if category == "__none__" else category))
                and (category != "__none__" or t.economic_type == EconomicType.PURCHASE)
            ]
            rows.sort(key=lambda t: (t.transaction_date.isoformat() if t.transaction_date else "", t.extraction_sequence or 0), reverse=True)
            try:
                limit = max(1, min(int(request.args.get("limit", 100)), 500))
                offset = max(0, int(request.args.get("offset", 0)))
            except ValueError:
                return _error("limit and offset must be numbers", 400)
            return jsonify({
                "total": len(rows),
                "transactions": [_txn_view(t, by_id, links_by_txn, txn_by_id) for t in rows[offset:offset + limit]],
                "categories": ledger_edits.category_choices(store),
                "types": [{"value": k, "label": v} for k, v in ledger_edits.TYPE_LABELS.items()],
                "rules": [_rule_view(r, ledger) for r in rules],
            })
        return _with_store(run)

    def _json_body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else None

    @app.route("/api/transactions/<transaction_id>/correction", methods=["POST"])
    def post_correction(transaction_id):
        data = _json_body()
        if data is None:
            return _error("Expected a JSON object.", 400)
        fields = {k: data[k] for k in ("category", "merchant_name", "economic_type") if k in data}
        if any(v is not None and not isinstance(v, str) for v in fields.values()):
            return _error("category, merchant_name and economic_type must be text or null", 400)
        scope = data.get("scope", "one")

        def run(store):
            if scope == "one":
                result = ledger_edits.correct_one(store, transaction_id, **fields)
            elif scope == "rule":
                pattern = data.get("pattern")
                if pattern is not None and not isinstance(pattern, str):
                    return _error("pattern must be text", 400)
                result = ledger_edits.add_rule(store, transaction_id, pattern=pattern, **fields)
            else:
                return _error("scope must be 'one' or 'rule'", 400)
            by_id = {r.rule_id: r for r in store.list_rules()}
            txn_by_id = {t.transaction_id: t for t in store.all_transactions()}
            body = {"changed": result["changed"],
                    "transaction": _txn_view(result["transaction"], by_id, _links_by_txn(store.list_events()), txn_by_id)}
            if result.get("rule"):
                body["rule"] = _rule_view(result["rule"], store.all_transactions())
            return jsonify(body)
        return _with_store(run)

    @app.route("/api/categorize/groq", methods=["POST"])
    def post_groq_categorize():
        return _with_store(lambda store: jsonify(ledger_edits.categorize_with_groq(store)))

    @app.route("/api/export.csv", methods=["GET"])
    def export_csv():
        from ..agent.tools import export_csv as to_csv, export_rows

        if not os.path.exists(_db_path()):
            return _error("Nothing to export yet.", 404)

        def run(store):
            try:
                df = date.fromisoformat(request.args["from"]) if request.args.get("from") else None
                dt = date.fromisoformat(request.args["to"]) if request.args.get("to") else None
            except ValueError:
                return _error("from and to must be dates like 2025-04-01", 400)
            body = to_csv(export_rows(store.all_transactions(), date_from=df, date_to=dt,
                                      category=request.args.get("category") or None))
            return app.response_class("\ufeff" + body, mimetype="text/csv", headers={
                "Content-Disposition": f"attachment; filename=transactions-{date.today().isoformat()}.csv",
                "Cache-Control": "no-store"})
        return _with_store(run)

    @app.route("/api/everything", methods=["DELETE"])
    def delete_everything():
        data = _json_body()
        if not data or data.get("confirm") != "DELETE EVERYTHING":
            return _error('To delete everything, send {"confirm": "DELETE EVERYTHING"}.', 400)

        def run(store):
            store.wipe()
            root = _upload_dir()
            if os.path.isdir(root):
                for name in os.listdir(root):
                    shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            return jsonify({"deleted": True})
        return _with_store(run)

    @app.route("/api/imports/<job_id>/suggest-columns", methods=["POST"])
    def suggest_columns_route(job_id):
        from ..groq_categorize import suggest_columns

        def run(store, job):
            staging = store.get_staging(job_id) or {}
            if job.file_kind != "tabular" or not staging.get("headers"):
                raise ImportConflict("Column suggestions only apply to files read as tables.")
            from ..ingest.columns import describe_extra_columns
            headers = staging["headers"]
            rows = [(r["row"], r["cells"]) for r in staging.get("sample_rows", [])]
            columns = [{"index": c.index, "header": c.header, "kind": c.kind}
                       for c in describe_extra_columns(headers, rows, set())]
            roles, note = suggest_columns(columns, list(ROLES))
            return jsonify({"roles": roles, "note": note})
        return _with_job(job_id, run)

    @app.route("/api/rules", methods=["GET"])
    def list_rules():
        def run(store):
            ledger = store.all_transactions()
            return jsonify({"rules": [_rule_view(r, ledger) for r in store.list_rules()]})
        return _with_store(run)

    @app.route("/api/rules/preview", methods=["POST"])
    def preview_rule():
        data = _json_body()
        if data is None or not isinstance(data.get("pattern"), str):
            return _error("pattern is required", 400)
        return _with_store(lambda store: jsonify(ledger_edits.preview_rule(store, data["pattern"])))

    @app.route("/api/rules/<rule_id>", methods=["DELETE"])
    def delete_rule(rule_id):
        return _with_store(lambda store: jsonify(ledger_edits.remove_rule(store, rule_id)))

    # -- questions ---------------------------------------------------------------------------

    @app.route("/api/ask", methods=["POST"])
    def ask():
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or "").strip()
        if not question:
            return _error("question is required", 400)

        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _error("ANTHROPIC_API_KEY is not set on the server.", 500)

        path = _db_path()
        if not os.path.exists(path):
            return _error(f"No ledger found at '{path}'. Add a statement (or run ingest) first.", 400)

        store = _store()
        ledger = store.all_transactions()
        documents = store.all_documents_as_dicts()
        events = store.list_events()
        store.close()

        if not ledger:
            return _error("Ledger is empty. Add a statement (or run ingest) first.", 400)

        from ..agent.loop import run_agent

        try:
            result = run_agent(question, ledger, documents=documents, events=events)
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
