"""Staged imports: upload -> analyze -> (map) -> (review) -> commit -> (rollback).

An import is a job with a state (schema.ImportState), not a synchronous "parse and persist":

  * analyze_import  reads the file into a staged, NOT-yet-persisted result — for CSV/XLSX that is
                    sniff (structure) + mapping (column roles) + tabular (rows -> transactions/issues);
                    for PDFs and images it's native extraction with vision-OCR fallback, as before.
  * update_mapping / update_review  re-run the deterministic analysis with the user's column choices
                    and decisions (leave a row out, confirm an assumption, say which way money moved).
  * commit_import   persists document + transactions + job state in ONE database transaction, and
                    refuses outright if there are zero transactions or anything is still unresolved.
  * rollback_import removes exactly that import's rows.

Spreadsheet analysis is recomputed from the stored file on every change (cheap and deterministic).
PDF/image extraction is computed once and kept in the job's staging JSON, since vision OCR costs
real money and time.

ingest_file / ingest_folder (the CLI path) use the same stages non-interactively: they commit only
when the column mapping is certain, leave out rows with blocking problems (reported, as rejected
rows always were), accept disclosed assumptions (reported as warnings), and never commit a file
that produced zero transactions.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from ..corrections import describe_source
from ..resolve import resolve_all
from ..schema import (
    Direction, Document, ImportJob, ImportState, IssueSeverity, TERMINAL_IMPORT_STATES, ValidationIssue,
)
from ..store import (
    DuplicateDocument, Store, document_from_dict, document_to_dict, transaction_from_dict, transaction_to_dict,
)
from .confidence import explain, gate_issues, low_confidence_fields, set_field
from .csv_parser import CsvParseResult, file_hash, parse_sniffed
from .image_parser import SUPPORTED_IMAGE_EXTENSIONS, parse_image
from .mapping import ColumnMapping, MappingError, apply_user_mapping, header_fingerprint, infer_mapping
from .pdf_native import parse_pdf_native
from .quality import assess
from .sniff import TableTooLarge, sniff_file
from .tabular import ReviewDecisions
from ..schema import ExtractionMethod
from .vocab import KNOWN_CURRENCIES

PARSER_VERSION = "staged-import-2"
SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".xlsx"} | SUPPORTED_IMAGE_EXTENSIONS
TABULAR_EXTENSIONS = {".csv", ".xlsx"}
MAX_PDF_PAGES = 200
PREVIEW_ROWS = 20

STATE_MESSAGES = {
    ImportState.UPLOADED: "Waiting to be read.",
    ImportState.ANALYZING: "Reading your file…",
    ImportState.NEEDS_MAPPING: "Please check how I read the columns.",
    ImportState.NEEDS_REVIEW: "A few items need your check.",
    ImportState.READY: "Everything checks out. Ready to add.",
    ImportState.COMMITTED: "Added to your statements.",
    ImportState.FAILED: "I couldn't use this file.",
    ImportState.DUPLICATE: "You've already added this file.",
    ImportState.ROLLED_BACK: "Removed from your statements.",
    ImportState.CANCELLED: "Cancelled.",
}


class ImportConflict(Exception):
    """The requested action doesn't fit the import's current state (e.g. committing before review)."""


@dataclass
class IngestReport:
    file_path: str
    status: str  # ingested | needs_mapping | needs_review | no_transactions | skipped_duplicate | skipped_unsupported | failed
    transaction_count: int = 0
    warnings: list[str] = field(default_factory=list)
    job_id: str | None = None


def discover_files(root: str) -> tuple[list[str], list[str]]:
    """Returns (supported_files, unsupported_files_seen)."""
    supported, unsupported = [], []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            (supported if ext in SUPPORTED_EXTENSIONS else unsupported).append(path)
    return sorted(supported), sorted(unsupported)


def file_kind(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in TABULAR_EXTENSIONS:
        return "tabular"
    if ext == ".pdf":
        return "pdf"
    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    return None


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def create_import(store: Store, stored_path: str, original_filename: str, *, owner: str = "local",
                  job_id: str | None = None) -> ImportJob:
    job = ImportJob(
        job_id=job_id or str(uuid.uuid4()), state=ImportState.UPLOADED, original_filename=original_filename,
        stored_path=stored_path, owner=owner, file_kind=file_kind(stored_path), parser_version=PARSER_VERSION,
    )
    store.save_job(job)
    return job


def _get(store: Store, job_id: str) -> ImportJob:
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    return job


def analyze_import(store: Store, job_id: str, *, attempt_vision: bool = True) -> ImportJob:
    job = _get(store, job_id)
    job.state, job.error_summary = ImportState.ANALYZING, None
    store.save_job(job)
    staging: dict = {"kind": job.file_kind, "decisions": ReviewDecisions().to_dict(), "warnings": []}
    try:
        job.file_hash = file_hash(job.stored_path)
        if store.has_document(job.file_hash):
            job.state = ImportState.DUPLICATE
            job.error_summary = STATE_MESSAGES[ImportState.DUPLICATE]
            store.save_job(job, staging)
            return job
        if job.file_kind == "tabular":
            _analyze_tabular(store, job, staging)
        elif job.file_kind == "pdf":
            _analyze_pdf(job, staging, attempt_vision=attempt_vision)
        elif job.file_kind == "image":
            _analyze_image(job, staging, attempt_vision=attempt_vision)
        else:
            raise ValueError("unsupported file type")
        _evaluate(store, job, staging)
    except TableTooLarge as e:
        _fail(store, job, staging, f"This file is too big to read here ({e}).")
    except Exception as e:  # noqa: BLE001 - one unreadable file becomes a failed job, never a crash
        _fail(store, job, staging, f"I couldn't read this file ({type(e).__name__}: {e}).")
    return job


def _fail(store: Store, job: ImportJob, staging: dict, message: str) -> None:
    job.state, job.error_summary, job.transaction_count = ImportState.FAILED, message, 0
    store.save_job(job, staging)


def update_mapping(store: Store, job_id: str, data: dict) -> ImportJob:
    job, staging = _editable(store, job_id)
    if job.file_kind != "tabular":
        raise ImportConflict("Column choices only apply to CSV and Excel files.")
    sniffed = sniff_file(job.stored_path)
    current = staging.get("table") or {}
    sheet = data.get("sheet", current.get("sheet"))
    header_row = data.get("header_row", current.get("header_row"))
    table = sniffed.select(sheet, header_row) if ("sheet" in data or "header_row" in data or current) else sniffed.best
    if table is None:
        raise MappingError("That table doesn't exist in this file.")
    base = ColumnMapping.from_dict(staging["mapping"]) if staging.get("mapping") else infer_mapping(table)
    mapping = apply_user_mapping(table, base, data) if "roles" in data else infer_mapping(table)
    staging["table"] = {"sheet": table.sheet, "header_row": table.header_row}
    staging["mapping"] = mapping.to_dict()
    _evaluate(store, job, staging)
    return job


def update_review(store: Store, job_id: str, data: dict) -> ImportJob:
    job, staging = _editable(store, job_id)
    decisions = ReviewDecisions.from_dict(staging.get("decisions"))
    decisions.excluded |= set(data.get("exclude", []))
    decisions.excluded -= set(data.get("include", []))
    decisions.acknowledged |= set(data.get("acknowledge", []))
    decisions.acknowledged -= set(data.get("unacknowledge", []))
    for key, value in (data.get("directions") or {}).items():
        if value is None:
            decisions.directions.pop(key, None)
        elif value in (Direction.DEBIT.value, Direction.CREDIT.value):
            decisions.directions[key] = value
        else:
            raise MappingError("direction must be DEBIT or CREDIT")
    currency = data.get("currency")
    if currency is not None and str(currency).upper() not in KNOWN_CURRENCIES:
        raise MappingError(f"currency must be one of {sorted(KNOWN_CURRENCIES)}")
    date_order = data.get("date_order")
    if date_order is not None and date_order not in ("DMY", "MDY"):
        raise MappingError("date_order must be DMY or MDY")
    if (currency or date_order) and job.file_kind == "tabular" and staging.get("mapping"):
        mapping = ColumnMapping.from_dict(staging["mapping"])
        if currency:
            mapping.currency, mapping.currency_source = str(currency).upper(), "user"
        if date_order:
            mapping.date_order, mapping.date_order_source = date_order, "user"
        staging["mapping"] = mapping.to_dict()
    elif currency:
        staging["currency_override"] = str(currency).upper()
    staging["decisions"] = decisions.to_dict()
    _evaluate(store, job, staging)
    return job


def _editable(store: Store, job_id: str) -> tuple[ImportJob, dict]:
    job = _get(store, job_id)
    if job.state in TERMINAL_IMPORT_STATES or job.state in (ImportState.UPLOADED, ImportState.ANALYZING):
        raise ImportConflict(f"This import can't be changed now ({STATE_MESSAGES[job.state]})")
    return job, store.get_staging(job_id) or {}


def commit_import(store: Store, job_id: str) -> ImportJob:
    job = _get(store, job_id)
    if job.state == ImportState.COMMITTED:
        return job
    if job.state != ImportState.READY:
        raise ImportConflict(f"This import isn't ready to add yet: {STATE_MESSAGES[job.state]}")
    staging = store.get_staging(job_id) or {}
    document, transactions, mapping = _evaluate(store, job, staging)  # recomputed, never trusted from an old preview
    if job.state != ImportState.READY:
        raise ImportConflict(f"This import isn't ready to add yet: {STATE_MESSAGES[job.state]}")
    profile = mapping.to_dict() if mapping is not None and mapping.fingerprint else None
    try:
        store.commit_import(job, document, transactions, profile=profile)
    except DuplicateDocument:
        job.state, job.error_summary = ImportState.DUPLICATE, STATE_MESSAGES[ImportState.DUPLICATE]
        store.save_job(job)
        raise ImportConflict(job.error_summary) from None
    staging["summary"] = _summary(document, transactions)
    store.save_job(job, staging)
    return job


def rollback_import(store: Store, job_id: str) -> ImportJob:
    job = _get(store, job_id)
    if job.state != ImportState.COMMITTED:
        raise ImportConflict("Only an added import can be undone.")
    store.rollback_import(job)
    return job


def cancel_import(store: Store, job_id: str) -> ImportJob:
    job = _get(store, job_id)
    if job.state == ImportState.COMMITTED:
        raise ImportConflict("This import was already added — undo it instead.")
    if job.state not in TERMINAL_IMPORT_STATES:
        job.state = ImportState.CANCELLED
        store.save_job(job)
    return job


# ---------------------------------------------------------------------------
# Analysis per file kind
# ---------------------------------------------------------------------------

def _analyze_tabular(store: Store, job: ImportJob, staging: dict) -> None:
    sniffed = sniff_file(job.stored_path)
    table = sniffed.best
    if table is None:
        staging["table"], staging["mapping"] = None, None
        return
    mapping = infer_mapping(table, profile=store.get_profile(header_fingerprint(table)))
    staging["table"] = {"sheet": table.sheet, "header_row": table.header_row}
    staging["mapping"] = mapping.to_dict()


def _run_tabular(job: ImportJob, staging: dict, decisions: ReviewDecisions) -> CsvParseResult:
    sniffed = sniff_file(job.stored_path)
    mapping = ColumnMapping.from_dict(staging["mapping"]) if staging.get("mapping") else None
    method = ExtractionMethod.XLSX_ROW if job.stored_path.lower().endswith(".xlsx") else ExtractionMethod.CSV_ROW
    result = parse_sniffed(job.stored_path, sniffed, extraction_method=method, mapping=mapping, decisions=decisions)
    staging["sniff"] = {
        "kind": sniffed.kind, "encoding": sniffed.encoding, "delimiter": sniffed.delimiter,
        "sheets": sniffed.sheets, "evidence": sniffed.evidence,
        "alternatives": [
            {"sheet": c.sheet, "header_row": c.header_row, "label": c.label, "score": round(c.score, 2), "evidence": c.evidence}
            for c in sniffed.candidates[:8]
        ],
    }
    table = result.table
    staging["headers"] = table.headers if table else []
    staging["sample_rows"] = [{"row": n, "cells": cells} for n, cells in table.rows[:PREVIEW_ROWS]] if table else []
    staging["preamble"] = [{"row": n, "cells": cells} for n, cells in table.preamble[:10]] if table else []
    return result


def _analyze_pdf(job: ImportJob, staging: dict, *, attempt_vision: bool) -> None:
    import pymupdf

    with pymupdf.open(job.stored_path) as d:
        pages = d.page_count
    if pages > MAX_PDF_PAGES:
        raise TableTooLarge(f"{pages} pages; the limit is {MAX_PDF_PAGES}")

    native = parse_pdf_native(job.stored_path)
    doc = native.document
    transactions = list(native.transactions)
    warnings = staging["warnings"]
    vision_pages = [p for p in assess(native, pages) if p.needs_vision]

    if vision_pages and attempt_vision and os.environ.get("ANTHROPIC_API_KEY"):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        import anthropic

        from .pdf_vision import _with_retry, vision_extract_page

        # One shared, thread-safe client; bounded at 4 concurrent requests so a large scanned
        # document doesn't fire an unbounded burst of API calls.
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        def _extract_one(pq):
            try:
                return pq, _with_retry(lambda: vision_extract_page(job.stored_path, pq.page_index, doc, client=client)), None
            except Exception as e:  # noqa: BLE001 - one page's failure must not sink the whole document
                return pq, None, e

        with ThreadPoolExecutor(max_workers=min(4, len(vision_pages))) as executor:
            for future in as_completed([executor.submit(_extract_one, pq) for pq in vision_pages]):
                pq, vresult, error = future.result()
                if error is not None:
                    warnings.append(f"vision OCR failed on page {pq.page_index + 1} ({pq.reason}): {type(error).__name__}: {error}")
                    continue
                transactions.extend(vresult.transactions)
                warnings.extend(vresult.warnings)
                if vresult.statement_start_raw and not doc.statement_start:
                    warnings.append(
                        f"vision reported statement period '{vresult.statement_start_raw}' to "
                        f"'{vresult.statement_end_raw}' but it could not be cross-checked against native extraction"
                    )
    elif vision_pages:
        why = "vision was disabled for this run" if not attempt_vision else "no ANTHROPIC_API_KEY is set, so vision was disabled for this run"
        for pq in vision_pages:
            warnings.append(f"page {pq.page_index + 1} needs vision OCR ({pq.reason}) but {why}")

    # native rows first, then vision rows — a stable sort by page restores document order
    transactions.sort(key=lambda t: t.source.page if t.source and t.source.page is not None else 0)
    staging["document"] = document_to_dict(doc)
    staging["transactions"] = [transaction_to_dict(t) for t in transactions]
    staging["ignored_lines"] = [
        {"page": s["page"] + 1, "text": s["text"][:200], "reason": s["reason"]} for s in native.skipped_lines[:500]
    ]
    staging["vision_pages"] = [p.page_index + 1 for p in vision_pages]


def _analyze_image(job: ImportJob, staging: dict, *, attempt_vision: bool) -> None:
    doc = parse_image(job.stored_path).document
    transactions: list = []
    warnings = staging["warnings"]
    if attempt_vision and os.environ.get("ANTHROPIC_API_KEY"):
        from .pdf_vision import _with_retry, vision_extract_standalone_image

        try:
            vresult = _with_retry(lambda: vision_extract_standalone_image(job.stored_path, doc))
            transactions.extend(vresult.transactions)
            warnings.extend(vresult.warnings)
            if doc.currency_declared is None:
                if vresult.currency_declared:
                    doc.currency_declared = vresult.currency_declared
                else:
                    doc.currency_declared = "INR"
                    doc.parse_warnings.append(
                        "CURRENCY: no explicit currency declaration found in this document — defaulted "
                        "to INR (unevidenced assumption, not extracted from the document itself)"
                    )
                    for t in transactions:
                        t.field_confidence["currency"] = 0.5
        except Exception as e:  # noqa: BLE001 - API/network failures become a readable message
            warnings.append(f"vision OCR failed on image: {type(e).__name__}: {e}")
    else:
        why = "vision was disabled for this run" if not attempt_vision else "no ANTHROPIC_API_KEY is set, so vision was disabled for this run"
        warnings.append(f"image requires vision OCR but {why}")
    staging["document"] = document_to_dict(doc)
    staging["transactions"] = [transaction_to_dict(t) for t in transactions]
    staging["ignored_lines"] = []


# ---------------------------------------------------------------------------
# Evaluation: staged data + decisions -> issues, state, preview
# ---------------------------------------------------------------------------

def _evaluate(store: Store, job: ImportJob, staging: dict):
    decisions = ReviewDecisions.from_dict(staging.get("decisions"))
    mapping: ColumnMapping | None = None
    ignored: list[dict] = []
    needs_mapping = False

    if job.file_kind == "tabular":
        result = _run_tabular(job, staging, decisions)
        mapping = result.mapping
        staging["mapping"] = mapping.to_dict() if mapping else None
        document, transactions, issues = result.document, result.transactions, list(result.issues)
        ignored = [{"key": c.key, "row": c.source_row, "reason": c.reason} for c in result.candidates if c.outcome == "ignored"]
        needs_mapping = mapping is None or (mapping.needs_confirmation and mapping.source != "user")
        counts = {
            "rows": len(result.candidates),
            "transactions": len(transactions),
            "ignored": len(ignored),
            "issues": sum(1 for c in result.candidates if c.outcome == "issue"),
        }
    else:
        document = document_from_dict(staging["document"])
        all_txns = [transaction_from_dict(d) for d in staging.get("transactions", [])]
        transactions = [t for t in all_txns if f"txn:{t.transaction_id}" not in decisions.excluded]
        override = staging.get("currency_override")
        if override:
            document.currency_declared = override
            document.parse_warnings = [w for w in document.parse_warnings if not w.startswith("CURRENCY:")]
            for t in transactions:
                if t.field_confidence.get("currency", 1.0) < 1.0:
                    t.currency = override
                    set_field(t, "currency", "currency_confirmed")
        issues = _document_issues(document, transactions, staging, override)
        ignored = [{"key": f"line:{i}", **line} for i, line in enumerate(staging.get("ignored_lines", []))]
        ignored += [{"key": f"txn:{t.transaction_id}", "reason": "left out by you"} for t in all_txns if f"txn:{t.transaction_id}" in decisions.excluded]
        counts = {"rows": len(all_txns) + len(staging.get("ignored_lines", [])), "transactions": len(transactions),
                  "ignored": len(ignored), "issues": 0}

    rules = store.list_rules()
    anomalies = resolve_all(document, transactions, rules) if transactions else []
    for flag in anomalies:
        t = flag.transaction
        t.notes = f"{t.notes} | FLAGGED: {flag.reason}".strip(" |")
    if document.reconciliation_status == "MISMATCH" and not any(i.rule == "balance_break" for i in issues):
        left_out = sum(1 for k in decisions.excluded if k.startswith(("row:", "txn:")))
        issues.append(_doc_issue(
            "reconciliation_mismatch", IssueSeverity.CHECK,
            "The transactions don't add up to the statement's own figures. " + document.reconciliation_detail
            + (f" You left out {left_out} row{'s' if left_out != 1 else ''}, which can explain this." if left_out else
               " Something may be missing or misread."),
            "Compare with your statement; confirm to add it anyway (it will stay flagged).",
        ))
    elif document.reconciliation_status == "CANNOT_CHECK":
        issues.append(_doc_issue(
            "reconciliation_unavailable", IssueSeverity.INFO, document.reconciliation_detail,
            "Nothing to do; shown so you know these rows weren't checked against the statement's totals.",
        ))
    issues.extend(gate_issues(transactions, issues, row_key=_row_key))
    for issue in issues:
        if issue.issue_id in decisions.acknowledged and issue.severity != IssueSeverity.BLOCKING:
            issue.resolution = "acknowledged"

    unresolved = [i for i in issues if i.resolution is None and i.severity in (IssueSeverity.BLOCKING, IssueSeverity.CHECK)]
    if needs_mapping:
        job.state = ImportState.NEEDS_MAPPING
        job.error_summary = None
    elif not transactions:
        if job.file_kind == "tabular":
            job.state = ImportState.NEEDS_MAPPING
            job.error_summary = "No transactions could be read with these column choices."
        else:
            job.state = ImportState.FAILED
            reasons = staging.get("warnings") or ["no transaction rows were found"]
            job.error_summary = "I didn't find any transactions I could read. " + "; ".join(reasons[:3])
    elif unresolved:
        job.state, job.error_summary = ImportState.NEEDS_REVIEW, None
    else:
        job.state, job.error_summary = ImportState.READY, None

    job.transaction_count = len(transactions)
    job.mapping_fingerprint = mapping.fingerprint if mapping else None
    staging["issues"] = [_issue_dict(i) for i in issues]
    staging["counts"] = counts
    staging["ignored_rows"] = ignored[:300]
    rules_by_id = {r.rule_id: r for r in rules}
    staging["preview_transactions"] = [_txn_preview(t, rules_by_id) for t in transactions[:PREVIEW_ROWS]]
    staging["rule_matches"] = sum(1 for t in transactions if t.category_source == "rule" or t.merchant_source == "rule")
    staging["summary"] = _summary(document, transactions)
    staging["anomaly_count"] = len(anomalies)
    store.save_job(job, staging)
    return document, transactions, mapping


def _row_key(t) -> str:
    return f"row:{t.source.row}" if t.source and t.source.row is not None else f"txn:{t.transaction_id}"


def _doc_issue(rule, severity, message, action, *, target=None, evidence="") -> ValidationIssue:
    return ValidationIssue(f"{rule}:{target or 'file'}", rule, severity, message, action, target=target, evidence=evidence)


def _document_issues(document: Document, transactions, staging: dict, override: str | None) -> list[ValidationIssue]:
    issues = []
    if not override and any(w.startswith("CURRENCY:") for w in document.parse_warnings):
        issues.append(_doc_issue(
            "currency_assumed", IssueSeverity.CHECK,
            f"This statement doesn't say which currency it's in. I read the amounts as {document.currency_declared or 'INR'}.",
            "Confirm the currency, or pick the right one.",
        ))
    for w in document.parse_warnings:
        if w.startswith("SECURITY:"):
            issues.append(_doc_issue(
                "instruction_like_text", IssueSeverity.INFO,
                "This statement contains text that reads like instructions to a computer. I ignored it — it can't change your numbers.",
                "Nothing to do; shown so you know.", evidence=w,
            ))
    unread = [w for w in staging.get("warnings", []) if "vision" in w.lower()]
    if unread and transactions:
        issues.append(_doc_issue(
            "pages_unread", IssueSeverity.CHECK,
            f"I couldn't read every page of this file ({len(unread)} problem(s)), so some transactions may be missing.",
            "Confirm to add what I could read, or cancel and try a clearer copy.", evidence="; ".join(unread[:3]),
        ))
    if any(t.field_reasons.get("date") == "date_order_default" for t in transactions):
        issues.append(_doc_issue(
            "date_order_assumed", IssueSeverity.CHECK,
            "Some dates in this statement, like 05/07/2025, could be read two ways, and nothing in it settles "
            "which. I read them as day/month/year.",
            "Confirm that's right, or cancel if this statement uses month/day/year.",
        ))
    for t in transactions:
        if not t.date_plausible:
            issues.append(_doc_issue(
                "date_implausible", IssueSeverity.CHECK,
                f"'{t.description_raw}' is dated {t.transaction_date}, far outside this statement's period — it may be misread.",
                "Leave it out, or confirm it's right.", target=f"txn:{t.transaction_id}", evidence=t.source.raw_text if t.source else "",
            ))
    if not transactions:
        issues.append(_doc_issue(
            "no_transactions", IssueSeverity.BLOCKING, "I didn't find any transactions I could read in this file.",
            "Try a clearer copy, or a CSV/Excel download from your bank.",
        ))
    return issues


def _issue_dict(i: ValidationIssue) -> dict:
    d = asdict(i)
    d["severity"] = i.severity.value
    return d


def _txn_preview(t, rules_by_id=None) -> dict:
    return {
        "key": _row_key(t),
        "date": t.transaction_date.isoformat() if t.transaction_date else None,
        "description": t.description_raw, "amount": str(t.amount), "currency": t.currency,
        "direction": t.direction.value, "balance_after": str(t.balance_after) if t.balance_after is not None else None,
        "row": t.source.row if t.source else None, "page": t.source.page if t.source else None,
        "flagged": "FLAGGED:" in t.notes or t.duplicate_of is not None,
        "source_text": (t.source.raw_text if t.source else "")[:300],
        "unsure": [{"field": f, "confidence": c, "reason": explain(r)} for f, c, r in low_confidence_fields(t)],
        "category": t.category, "merchant_name": t.merchant_canonical,
        "why": describe_source(t, rules_by_id or {}),
    }


def _summary(document: Document, transactions) -> dict:
    money_in: dict[str, Decimal] = {}
    money_out: dict[str, Decimal] = {}
    for t in transactions:
        if t.duplicate_of is not None:
            continue
        bucket = money_in if t.direction == Direction.CREDIT else money_out
        bucket[t.currency] = bucket.get(t.currency, Decimal("0")) + t.amount
    dates = sorted(t.transaction_date for t in transactions if t.transaction_date)
    return {
        "transaction_count": len(transactions),
        "date_from": dates[0].isoformat() if dates else None,
        "date_to": dates[-1].isoformat() if dates else None,
        "money_in": {k: str(v) for k, v in sorted(money_in.items())},
        "money_out": {k: str(v) for k, v in sorted(money_out.items())},
        "flagged": sum(1 for t in transactions if "FLAGGED:" in t.notes or t.duplicate_of is not None),
        "account": document.account_label,
        "reconciliation": document.reconciliation_status,
        "reconciliation_detail": document.reconciliation_detail,
    }


def job_view(job: ImportJob, staging: dict | None) -> dict:
    staging = staging or {}
    return {
        "id": job.job_id, "file": job.original_filename, "kind": job.file_kind, "state": job.state.value,
        "message": job.error_summary or STATE_MESSAGES[job.state], "transaction_count": job.transaction_count,
        "created_at": job.created_at, "updated_at": job.updated_at,
        "summary": staging.get("summary"), "counts": staging.get("counts"),
        "open_issues": sum(1 for i in staging.get("issues", []) if i["resolution"] is None and i["severity"] != "info"),
    }


# ---------------------------------------------------------------------------
# Non-interactive path (CLI / folder ingest)
# ---------------------------------------------------------------------------

def ingest_file(path: str, store: Store, *, attempt_vision: bool = True) -> IngestReport:
    if file_kind(path) is None:
        return IngestReport(path, "skipped_unsupported")
    job = create_import(store, path, os.path.basename(path))
    job = analyze_import(store, job.job_id, attempt_vision=attempt_vision)
    staging = store.get_staging(job.job_id) or {}
    warnings = list(staging.get("warnings", []))
    if staging.get("document"):
        warnings = list(staging["document"].get("parse_warnings", [])) + warnings

    if job.state == ImportState.DUPLICATE:
        return IngestReport(path, "skipped_duplicate", job_id=job.job_id)
    if job.state == ImportState.NEEDS_MAPPING:
        m = staging.get("mapping") or {}
        detail = [f"missing required column(s): {m['missing_required']}" for _ in [0] if m.get("missing_required")]
        detail += [f"column mapping needs confirmation: {a}" for a in m.get("ambiguities", [])]
        return IngestReport(path, "needs_mapping", 0, warnings + detail + ([job.error_summary] if job.error_summary else []), job.job_id)
    if job.state == ImportState.FAILED:
        status = "no_transactions" if "any transactions" in (job.error_summary or "") else "failed"
        return IngestReport(path, status, 0, warnings + [job.error_summary], job.job_id)

    issues = staging.get("issues", [])
    if job.state == ImportState.NEEDS_REVIEW:
        # Non-interactive: leave out rows with blocking problems (reported), accept disclosed checks (reported).
        blocking_rows = [i for i in issues if i["severity"] == "blocking" and i["target"]]
        checks = [i for i in issues if i["severity"] == "check" and i["resolution"] is None]
        job = update_review(store, job.job_id, {
            "exclude": [i["target"] for i in blocking_rows], "acknowledge": [i["issue_id"] for i in checks],
        })
        if blocking_rows:
            warnings.append(f"{len(blocking_rows)} row(s) rejected: {[i['message'] for i in blocking_rows]}")
        warnings += [f"accepted without review: {i['message']}" for i in checks]
    if job.state != ImportState.READY:
        return IngestReport(path, job.state.value, 0, warnings + [job.error_summary or STATE_MESSAGES[job.state]], job.job_id)

    job = commit_import(store, job.job_id)
    staging = store.get_staging(job.job_id) or {}
    if staging.get("anomaly_count"):
        warnings.append(f"{staging['anomaly_count']} transaction(s) flagged for review")
    return IngestReport(path, "ingested", job.transaction_count, warnings, job.job_id)


def ingest_folder(root: str, store: Store, *, attempt_vision: bool = True) -> list[IngestReport]:
    supported, unsupported = discover_files(root)
    reports = []
    for path in supported:
        try:
            reports.append(ingest_file(path, store, attempt_vision=attempt_vision))
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the whole folder
            reports.append(IngestReport(path, "failed", warnings=[f"{type(e).__name__}: {e}"]))
    for path in unsupported:
        reports.append(IngestReport(path, "skipped_unsupported", warnings=["unrecognized file extension"]))
    # Cross-document duplicate detection now runs inside every commit (Store.commit_import), against
    # the whole ledger, so a separate post-folder pass is no longer needed.
    return reports
