"""Native (non-OCR) PDF statement extraction via pdfplumber word positions.

Rows are reconstructed from (x, y) word coordinates rather than raw text-stream
order — raw stream order can be scrambled relative to visual layout (confirmed
on this dataset: pypdf's plain `extract_text()` interleaved rows on the Cobalt
statement in a way that looked like two overlapping tables; grouping by `top`
position and sorting by `x0` within each row reproduces the actual visual
table cleanly, with no interleaving). Each line is then classified by anchors:
a date token at the start and an amount token at the end. Anything in between
is the description; anything that matches neither anchor is treated as
metadata (headers, footers) and never becomes a transaction — this is also
the structural defense against prompt injection embedded in a statement: an
instruction-like sentence has no date/amount anchors, so it can never be
parsed into a transaction row, regardless of what it says.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field


from ..ingest.csv_parser import file_hash
from ..normalize import DocumentDateResolver, infer_decimal_separator, normalize_amount
from ..schema import Direction, Document, EconomicType, ExtractionMethod, SourceRef, Transaction
from .confidence import set_field
from .statement_totals import apply_to_document, is_summary_label, is_summary_text, read_figures

_DATE_ANCHOR_RE = re.compile(
    r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
    r"(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december)"
    r"\.?,?\s+(?:\d{1,2},?\s+)?\d{4})",
    re.IGNORECASE,
)
_AMOUNT_ANCHOR_RE = re.compile(
    r"(?:₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?\s*\(?-?\s*[\d,]+\.\d{2}\)?\s*(?:CR|DR)?\s*$",
    re.IGNORECASE,
)
_HEADER_HINT_RE = re.compile(r"^(date|transaction|details|amount)\b", re.IGNORECASE)
_INJECTION_KEYWORDS = (
    "ignore all previous",
    "ignore previous instructions",
    "disregard any",
    "disregard all",
    "report every",
    "automated processing notice",
    "verified and non-disputable",
    "system prompt",
    "you are now",
)

_CC_KEYWORDS = ("credit limit", "minimum amount due", "payment due date")
_PERIOD_RE = re.compile(
    r"Period:\s*([\d]{1,2}\s+\w+\s+\d{4})\s*-\s*([\d]{1,2}\s+\w+\s+\d{4})", re.IGNORECASE
)
_CURRENCY_HINT_RE = re.compile(r"Currency:\s*([A-Z]{3})", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"(Card ending\s*\d+|Account\s*(?:No\.?|Number)?\s*[\dX]+)", re.IGNORECASE)


@dataclass
class PdfParseResult:
    document: Document
    transactions: list[Transaction] = field(default_factory=list)
    page_texts: dict[int, str] = field(default_factory=dict)
    skipped_lines: list[dict] = field(default_factory=list)  # {"page": int, "top": float, "text": str, "reason": str}


def _group_lines(page, tolerance: float = 2.0) -> list[tuple[float, str]]:
    words = page.extract_words()
    buckets: dict[float, list] = defaultdict(list)
    for w in words:
        # snap to nearest existing bucket within tolerance so words on the same
        # visual line don't get split by sub-pixel `top` differences
        key = next((k for k in buckets if abs(k - w["top"]) <= tolerance), w["top"])
        buckets[key].append(w)
    lines = []
    for top in sorted(buckets.keys()):
        text = " ".join(w["text"] for w in sorted(buckets[top], key=lambda w: w["x0"]))
        lines.append((top, text))
    return lines


def _classify_doc_type(full_text: str) -> str:
    lowered = full_text.lower()
    if any(k in lowered for k in _CC_KEYWORDS):
        return "credit_card_statement"
    if "card ending" in lowered or "credit card" in lowered:
        return "credit_card_statement"
    if "account statement" in lowered or "bank" in lowered:
        return "bank_statement"
    return "unknown"


def parse_pdf_native(path: str) -> PdfParseResult:
    import pdfplumber  # imported here: only the line-based reader needs it, and it costs ~60ms to load

    fhash = file_hash(path)
    document = Document(
        document_id=str(uuid.uuid4()),
        file_path=path,
        file_hash=fhash,
        doc_type="unknown",
        reconciliation_status="NOT_CHECKED",
    )

    all_lines: list[tuple[int, float, str]] = []  # (page_index, top, text)
    page_texts: dict[int, str] = {}

    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            lines = _group_lines(page)
            page_texts[pi] = "\n".join(t for _, t in lines)
            for top, text in lines:
                all_lines.append((pi, top, text))

    full_text = "\n".join(page_texts.values())
    document.doc_type = _classify_doc_type(full_text)

    m = _PERIOD_RE.search(full_text)
    if m:
        resolver = DocumentDateResolver()
        resolver.observe(m.group(1))
        resolver.observe(m.group(2))
        resolver.resolve_convention()
        start = resolver.parse(m.group(1)).value
        end = resolver.parse(m.group(2)).value
        document.statement_start = start
        document.statement_end = end

    ccy_m = _CURRENCY_HINT_RE.search(full_text)
    if ccy_m:
        document.currency_declared = ccy_m.group(1).upper()
    else:
        # No currency evidence anywhere in the document. INR is used as the parsing
        # default (matches this dataset's own convention — see dataset README), but per
        # EC-46 that default must be disclosed, never silently presented as evidenced.
        document.currency_declared = "INR"
        document.parse_warnings.append(
            "CURRENCY: no explicit currency declaration found in this document — defaulted to INR "
            "(unevidenced assumption, not extracted from the document itself)"
        )

    acct_m = _ACCOUNT_RE.search(full_text)
    document.account_label = acct_m.group(1) if acct_m else None

    lowered_full = full_text.lower()
    for kw in _INJECTION_KEYWORDS:
        if kw in lowered_full:
            document.parse_warnings.append(
                f"SECURITY: instruction-like text detected in document body (matched {kw!r}); "
                "treated as inert content, not parsed as a transaction or followed as an instruction"
            )
            break

    decimal_separator = infer_decimal_separator(
        [_AMOUNT_ANCHOR_RE.search(t).group(0) for _, _, t in all_lines if _AMOUNT_ANCHOR_RE.search(t)])

    date_resolver = DocumentDateResolver()
    for _, _, text in all_lines:
        dm = _DATE_ANCHOR_RE.match(text)
        if dm:
            date_resolver.observe(dm.group(0))
    date_resolver.resolve_convention()
    date_evidence = date_resolver._convention is not None

    # Stated balances/totals, read from every line; a dated "Opening Balance 10,000.00" line is one of
    # these, never a transaction.
    figure_lines = []
    for _, _, text in all_lines:
        dm = _DATE_ANCHOR_RE.match(text.strip())
        am = _AMOUNT_ANCHOR_RE.search(text.strip()) if dm else None
        if dm and am and am.start() > dm.end():
            if not is_summary_label(text.strip()[dm.end():am.start()]):
                continue  # a transaction row; its description may merely contain a label's words
        figure_lines.append(text)
    apply_to_document(document, read_figures(figure_lines))

    transactions: list[Transaction] = []
    skipped: list[dict] = []
    pending_description_parts: list[str] = []
    pending_row: Transaction | None = None

    for page_idx, top, text in all_lines:
        stripped = text.strip()
        if not stripped:
            continue

        date_m = _DATE_ANCHOR_RE.match(stripped)
        amount_m = _AMOUNT_ANCHOR_RE.search(stripped) if date_m else None

        if date_m and amount_m and amount_m.start() > date_m.end():
            # flush any pending multi-line description merge from a previous row first
            pending_row = None
            pending_description_parts = []

            raw_date = date_m.group(0)
            raw_amount = stripped[amount_m.start():].strip()
            description = stripped[date_m.end():amount_m.start()].strip()
            if is_summary_label(description):
                skipped.append({"page": page_idx, "top": top, "text": stripped, "reason": "statement summary line (balance or total), not a transaction"})
                continue

            parsed_date = date_resolver.parse(raw_date)
            parsed_amount = normalize_amount(raw_amount, default_currency=document.currency_declared or "INR",
                                             decimal_separator=decimal_separator)
            if parsed_date.value is None or parsed_amount is None:
                skipped.append({"page": page_idx, "top": top, "text": stripped, "reason": "date/amount parse failed after anchor match"})
                continue

            economic_type = EconomicType.PURCHASE
            if parsed_amount.direction == Direction.CREDIT:
                economic_type = (
                    EconomicType.CREDIT_CARD_PAYMENT if "payment received" in description.lower() else EconomicType.REFUND
                )

            plausible = True
            from ..normalize import is_date_plausible

            plausible = is_date_plausible(
                parsed_date.value, statement_start=document.statement_start, statement_end=document.statement_end
            )

            txn = Transaction(
                transaction_id=str(uuid.uuid4()),
                document_id=document.document_id,
                transaction_date=parsed_date.value,
                date_raw=raw_date,
                date_plausible=plausible,
                description_raw=description,
                merchant_raw=description or None,
                amount=parsed_amount.amount,
                currency=parsed_amount.currency,
                amount_raw=raw_amount,
                direction=parsed_amount.direction,
                economic_type=economic_type,
                source=SourceRef(
                    file_path=path,
                    file_hash=fhash,
                    page=page_idx + 1,
                    raw_text=stripped,
                    extraction_method=ExtractionMethod.NATIVE_TEXT,
                    extraction_confidence=1.0,
                ),
            )
            if parsed_date.confidence >= 1.0:
                set_field(txn, "date", "date_unambiguous")
            else:
                set_field(txn, "date", "date_order_from_document" if date_evidence else "date_order_default")
            set_field(txn, "amount", "amount_separator_assumed" if parsed_amount.ambiguous_separator else "amount_read")
            set_field(txn, "direction", "direction_sign")
            # recorded so a staged import can apply a user-confirmed currency to exactly these rows
            set_field(txn, "currency", "currency_symbol" if not parsed_amount.currency_inferred
                      else "currency_assumed" if any(w.startswith("CURRENCY:") for w in document.parse_warnings)
                      else "currency_file")
            if parsed_date.confidence < 1.0:
                txn.notes = f"date assumption: {parsed_date.assumption}"
            if not plausible:
                txn.notes = (txn.notes + " | date outside plausible statement range — excluded from totals until reviewed").strip(" |")
            transactions.append(txn)
            pending_row = txn
            continue

        if is_summary_text(stripped) and _AMOUNT_ANCHOR_RE.search(stripped):
            skipped.append({"page": page_idx, "top": top, "text": stripped, "reason": "statement summary line (balance or total), not a transaction"})
            pending_row = None
            continue

        if _HEADER_HINT_RE.match(stripped) or stripped.startswith("***") or "automated processing notice" in stripped.lower():
            skipped.append({"page": page_idx, "top": top, "text": stripped, "reason": "header/boilerplate/non-transaction text"})
            pending_row = None
            continue

        if pending_row is not None and len(stripped) < 60 and not _AMOUNT_ANCHOR_RE.fullmatch(stripped):
            # likely a wrapped continuation of the previous row's multi-line description
            pending_row.description_raw = f"{pending_row.description_raw} {stripped}".strip()
            pending_row.merchant_raw = pending_row.description_raw
            pending_row.source.raw_text += f" | {stripped}"
            continue

        skipped.append({"page": page_idx, "top": top, "text": stripped, "reason": "no date/amount anchor; not a transaction row"})
        pending_row = None

    return PdfParseResult(document=document, transactions=transactions, page_texts=page_texts, skipped_lines=skipped)
