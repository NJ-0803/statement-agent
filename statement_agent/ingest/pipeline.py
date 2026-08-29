"""Ties csv_parser / pdf_native / quality / pdf_vision together and writes into the Store.

Idempotent per file (via file_hash), tolerant of unsupported file types (warns,
does not crash), and tolerant of a failing/unavailable vision API call — if
vision OCR errors out (no credits, network issue, etc.) that page just
contributes zero transactions and a warning, rather than taking down ingestion
for the whole folder. A page nobody could read becomes INSUFFICIENT_INFORMATION
at answer time, never a silently wrong total.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .csv_parser import parse_csv
from .pdf_native import parse_pdf_native
from .quality import assess
from ..resolve import resolve_all
from ..schema import Document
from ..store import Store

SUPPORTED_EXTENSIONS = {".pdf", ".csv"}


@dataclass
class IngestReport:
    file_path: str
    status: str  # ingested | skipped_duplicate | skipped_unsupported | failed
    transaction_count: int = 0
    warnings: list[str] = field(default_factory=list)


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


def ingest_file(path: str, store: Store, *, attempt_vision: bool = True) -> IngestReport:
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        result = parse_csv(path)
        doc = result.document
        if store.has_document(doc.file_hash):
            return IngestReport(path, "skipped_duplicate")
        anomalies = resolve_all(doc, result.transactions)
        _attach_anomaly_notes(anomalies)
        store.upsert_document(doc)
        store.insert_transactions(result.transactions)
        warnings = list(doc.parse_warnings)
        if result.rejected_rows:
            warnings.append(f"{len(result.rejected_rows)} row(s) rejected: {[r['reason'] for r in result.rejected_rows]}")
        if anomalies:
            warnings.append(f"{len(anomalies)} transaction(s) flagged for review: {[a.reason[:60] for a in anomalies]}")
        return IngestReport(path, "ingested", len(result.transactions), warnings)

    if ext == ".pdf":
        native = parse_pdf_native(path)
        doc = native.document
        if store.has_document(doc.file_hash):
            return IngestReport(path, "skipped_duplicate")

        transactions = list(native.transactions)
        warnings = list(doc.parse_warnings)

        with_pdf_open = _page_count(path)
        page_quality = assess(native, with_pdf_open)
        vision_pages = [p for p in page_quality if p.needs_vision]

        if vision_pages and attempt_vision:
            from .pdf_vision import vision_extract_page

            for pq in vision_pages:
                try:
                    vresult = vision_extract_page(path, pq.page_index, doc)
                    transactions.extend(vresult.transactions)
                    warnings.extend(vresult.warnings)
                    if vresult.statement_start_raw and not doc.statement_start:
                        warnings.append(
                            f"vision reported statement period '{vresult.statement_start_raw}' to "
                            f"'{vresult.statement_end_raw}' but it could not be cross-checked against native extraction"
                        )
                except Exception as e:  # noqa: BLE001 - API/network failures must not crash the whole ingest run
                    warnings.append(
                        f"vision OCR failed on page {pq.page_index + 1} ({pq.reason}): {type(e).__name__}: {e}"
                    )
        elif vision_pages and not attempt_vision:
            for pq in vision_pages:
                warnings.append(f"page {pq.page_index + 1} needs vision OCR ({pq.reason}) but vision was disabled for this run")

        anomalies = resolve_all(doc, transactions)
        _attach_anomaly_notes(anomalies)
        if anomalies:
            warnings.append(f"{len(anomalies)} transaction(s) flagged for review: {[a.reason[:60] for a in anomalies]}")

        store.upsert_document(doc)
        store.insert_transactions(transactions)
        return IngestReport(path, "ingested", len(transactions), warnings)

    return IngestReport(path, "skipped_unsupported")


def _attach_anomaly_notes(anomalies) -> None:
    """Anomaly flags aren't a separate table — persisted onto the transaction's own
    notes field so they survive the store round-trip without a schema addition."""
    for flag in anomalies:
        t = flag.transaction
        t.notes = f"{t.notes} | FLAGGED: {flag.reason}".strip(" |")


def _page_count(path: str) -> int:
    import pymupdf

    with pymupdf.open(path) as d:
        return d.page_count


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

    # Cross-document duplicate detection runs once over the WHOLE ledger, after every
    # file is in — detect_duplicates() during per-file resolution can only ever see one
    # document at a time, so a transaction repeated across two different source files
    # (overlapping statement periods, or the same statement re-ingested under a new
    # filename with different byte content) would otherwise never be compared at all.
    from ..resolve import detect_cross_document_duplicates

    ledger = store.all_transactions()
    newly_flagged = detect_cross_document_duplicates(ledger)
    if newly_flagged:
        store.insert_transactions(newly_flagged)

    return reports
