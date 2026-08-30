"""Ties csv_parser / xlsx_parser / pdf_native / image_parser / quality / pdf_vision
together and writes into the Store. Supported formats: .pdf, .csv, .xlsx, and
standalone statement images (.jpg/.jpeg/.png, routed straight to vision OCR since
there's no native text layer to try first). Anything else is reported as
skipped_unsupported with a clear warning, never silently dropped.

Idempotent per file (via file_hash), tolerant of unsupported file types (warns,
does not crash), and tolerant of a failing/unavailable vision API call — if
vision OCR errors out (no credits, network issue, etc.) that page just
contributes zero transactions and a warning, rather than taking down ingestion
for the whole folder. A page nobody could read becomes INSUFFICIENT_INFORMATION
at answer time, never a silently wrong total.

A PDF with multiple pages needing vision fallback processes them concurrently
(bounded at 4 workers) with retry/backoff on transient failures (pdf_vision.py's
_with_retry) — this dataset never exercises more than one vision page per
document, so the retry path is unit-tested directly (tests/test_pdf_vision.py)
rather than proven end-to-end at real multi-page volume.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .csv_parser import CsvParseResult, parse_csv
from .image_parser import SUPPORTED_IMAGE_EXTENSIONS, parse_image
from .pdf_native import parse_pdf_native
from .quality import assess
from .xlsx_parser import parse_xlsx
from ..resolve import resolve_all
from ..schema import Document
from ..store import Store

SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".xlsx"} | SUPPORTED_IMAGE_EXTENSIONS


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

    if ext in (".csv", ".xlsx"):
        result = parse_csv(path) if ext == ".csv" else parse_xlsx(path)
        return _ingest_tabular_result(result, store)

    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        return _ingest_image(path, store, attempt_vision=attempt_vision)

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
            from concurrent.futures import ThreadPoolExecutor, as_completed

            import anthropic

            from .pdf_vision import _with_retry, vision_extract_page

            # One shared client, reused across concurrent calls (the SDK's client is
            # thread-safe for concurrent requests) rather than a fresh client per page —
            # matters more as page count grows, not at this dataset's scale. Bounded at 4
            # concurrent requests regardless of how many pages need vision, so a large,
            # heavily-scanned document doesn't fire an unbounded burst of API calls at once.
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

            def _extract_one(pq):
                try:
                    return pq, _with_retry(lambda: vision_extract_page(path, pq.page_index, doc, client=client)), None
                except Exception as e:  # noqa: BLE001 - one page's failure must not crash the whole ingest run
                    return pq, None, e

            with ThreadPoolExecutor(max_workers=min(4, len(vision_pages))) as executor:
                futures = [executor.submit(_extract_one, pq) for pq in vision_pages]
                for future in as_completed(futures):
                    pq, vresult, error = future.result()
                    if error is not None:
                        warnings.append(
                            f"vision OCR failed on page {pq.page_index + 1} ({pq.reason}): {type(error).__name__}: {error}"
                        )
                        continue
                    transactions.extend(vresult.transactions)
                    warnings.extend(vresult.warnings)
                    if vresult.statement_start_raw and not doc.statement_start:
                        warnings.append(
                            f"vision reported statement period '{vresult.statement_start_raw}' to "
                            f"'{vresult.statement_end_raw}' but it could not be cross-checked against native extraction"
                        )
        elif vision_pages and not attempt_vision:
            for pq in vision_pages:
                warnings.append(f"page {pq.page_index + 1} needs vision OCR ({pq.reason}) but vision was disabled for this run")

        # Native rows were appended first, then vision-extracted rows for whichever pages needed
        # fallback — so if page 1 needed vision and page 2 didn't, `transactions` would currently
        # have page 2's (native) rows before page 1's (vision) rows, even though page 1 comes first
        # in the actual document. A stable sort by page fixes that before extraction_sequence is
        # assigned in resolve_all — stable, so each page's own internal row order (already correct)
        # is preserved, only the page-to-page interleaving is corrected.
        transactions.sort(key=lambda t: t.source.page if t.source and t.source.page is not None else 0)

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


def _ingest_tabular_result(result: CsvParseResult, store: Store) -> IngestReport:
    """Shared by the .csv and .xlsx branches — both produce the same CsvParseResult
    shape (parse_tabular_rows is the common logic underneath both parsers)."""
    doc = result.document
    if store.has_document(doc.file_hash):
        return IngestReport(doc.file_path, "skipped_duplicate")
    anomalies = resolve_all(doc, result.transactions)
    _attach_anomaly_notes(anomalies)
    store.upsert_document(doc)
    store.insert_transactions(result.transactions)
    warnings = list(doc.parse_warnings)
    if result.rejected_rows:
        warnings.append(f"{len(result.rejected_rows)} row(s) rejected: {[r['reason'] for r in result.rejected_rows]}")
    if anomalies:
        warnings.append(f"{len(anomalies)} transaction(s) flagged for review: {[a.reason[:60] for a in anomalies]}")
    return IngestReport(doc.file_path, "ingested", len(result.transactions), warnings)


def _ingest_image(path: str, store: Store, *, attempt_vision: bool) -> IngestReport:
    image_result = parse_image(path)
    doc = image_result.document
    if store.has_document(doc.file_hash):
        return IngestReport(path, "skipped_duplicate")

    transactions: list = []
    warnings: list[str] = []

    if attempt_vision:
        from .pdf_vision import _with_retry, vision_extract_standalone_image

        try:
            vresult = _with_retry(lambda: vision_extract_standalone_image(path, doc))
            transactions.extend(vresult.transactions)
            warnings.extend(vresult.warnings)
            # a standalone image has no separate native pass to set these ahead of
            # the vision call (unlike the PDF path) — set them from what vision itself
            # reported, same EC-46 disclosure pattern as pdf_native.py when nothing
            # in the document declares a currency at all.
            if doc.currency_declared is None:
                if vresult.currency_declared:
                    doc.currency_declared = vresult.currency_declared
                else:
                    doc.currency_declared = "INR"
                    warnings.append(
                        "CURRENCY: no explicit currency declaration found in this document — defaulted "
                        "to INR (unevidenced assumption, not extracted from the document itself)"
                    )
        except Exception as e:  # noqa: BLE001 - API/network failures must not crash the whole ingest run
            warnings.append(f"vision OCR failed on image: {type(e).__name__}: {e}")
    else:
        warnings.append("image requires vision OCR but vision was disabled for this run")

    anomalies = resolve_all(doc, transactions)
    _attach_anomaly_notes(anomalies)
    if anomalies:
        warnings.append(f"{len(anomalies)} transaction(s) flagged for review: {[a.reason[:60] for a in anomalies]}")

    store.upsert_document(doc)
    store.insert_transactions(transactions)
    return IngestReport(path, "ingested", len(transactions), warnings)


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
