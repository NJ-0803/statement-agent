"""Canonical data model. Every parser, regardless of source format, produces these types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class EconomicType(str, Enum):
    """What a transaction represents economically, independent of spend category.

    Resolved BEFORE category, so a card repayment or a transfer never gets
    counted as consumption spend just because it has a merchant-looking label.
    """

    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    TRANSFER = "TRANSFER"
    CREDIT_CARD_PAYMENT = "CREDIT_CARD_PAYMENT"
    CASH_WITHDRAWAL = "CASH_WITHDRAWAL"
    REIMBURSEMENT = "REIMBURSEMENT"
    FEE = "FEE"
    INTEREST = "INTEREST"
    REVERSAL = "REVERSAL"
    INVESTMENT_TRANSFER = "INVESTMENT_TRANSFER"  # money moved to a brokerage/MF/FD — asset allocation, not consumption
    CASHBACK = "CASHBACK"  # merchant/card cashback or rewards redemption — not ordinary income or a merchant refund
    UNKNOWN = "UNKNOWN"


class Direction(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ExtractionMethod(str, Enum):
    NATIVE_TEXT = "NATIVE_TEXT"
    NATIVE_TABLE = "NATIVE_TABLE"
    VISION_OCR = "VISION_OCR"
    CSV_ROW = "CSV_ROW"
    XLSX_ROW = "XLSX_ROW"


@dataclass
class SourceRef:
    """Where a value came from. Every transaction must carry one — this is the provenance chain."""

    file_path: str
    file_hash: str
    page: int | None = None
    row: int | None = None
    raw_text: str = ""
    extraction_method: ExtractionMethod = ExtractionMethod.NATIVE_TEXT
    extraction_confidence: float = 1.0


@dataclass
class Document:
    document_id: str
    file_path: str
    file_hash: str
    doc_type: str  # bank_statement | credit_card_statement | expense_sheet | unknown
    account_label: str | None = None
    currency_declared: str | None = None
    statement_start: date | None = None
    statement_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    stated_total_debits: Decimal | None = None
    stated_total_credits: Decimal | None = None
    reconciliation_status: str = "NOT_CHECKED"  # RECONCILED | MISMATCH | NOT_CHECKED | NO_TOTALS | CANNOT_CHECK
    reconciliation_delta: Decimal | None = None
    reconciliation_detail: str = ""  # plain language: which checks ran against which stated figures, and how they came out
    opening_balance_derived: bool = False  # True when no opening balance was stated and it was worked out from
    # the first row's running balance — then the balance check only proves continuity, and the detail says so
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Transaction:
    transaction_id: str
    document_id: str

    transaction_date: date | None
    date_raw: str
    date_plausible: bool = True  # False if the parsed date falls outside a sane bound

    extraction_sequence: int | None = None  # this transaction's 0-based position in the ORIGINAL source
    # document's own row order (page-ascending, top-to-bottom for PDFs; row-ascending for CSV/XLSX) — set
    # once at resolution time from extraction order, never from transaction_date. This is what makes "is
    # this statement sorted by date" an answerable question: comparing extraction_sequence order against
    # transaction_date order is meaningful; re-sorting by date and checking if it's sorted by date is not.

    description_raw: str = ""
    merchant_raw: str | None = None
    merchant_normalized: str | None = None  # canonicalized for grouping/dedup; merchant_raw stays the citation value

    amount: Decimal = Decimal("0")
    currency: str = "INR"
    amount_raw: str = ""
    direction: Direction = Direction.DEBIT

    economic_type: EconomicType = EconomicType.UNKNOWN
    economic_type_confidence: float = 1.0

    category: str | None = None  # only set when economic_type == PURCHASE
    category_confidence: float | None = None
    category_declared: str | None = None  # the source file's OWN category label, if it has one (e.g. an
    # "Account Name"-style expense sheet's "Category" column) — kept separate from `category` (our own
    # keyword-derived taxonomy) so a declared label is never silently overwritten by a failed keyword
    # match; assign_categories() falls back to this when categorize() can't match the merchant text.

    account_name: str | None = None  # which of the source's own accounts/cards this row belongs to (e.g.
    # "Platinum Card", "Checking"), when the source file declares one — distinct from Document.account_label
    # (a whole document's single stated account), since one tabular expense sheet can span several accounts.

    source: SourceRef | None = None

    duplicate_of: str | None = None  # transaction_id of the canonical row, if this is a probable duplicate
    duplicate_reason: str | None = None

    notes: str = ""

    value_date: date | None = None  # the bank's value/effective date, kept separate from transaction_date —
    # never collapsed into it at ingestion (a statement's "Value Dt" and "Txn Date" can differ by days)
    reference_id: str | None = None  # cheque/UTR/reference number, when the source declares one
    balance_after: Decimal | None = None  # the source's own running balance after this row, if stated
    field_confidence: dict[str, float] = field(default_factory=dict)  # per-field: "date", "amount",
    # "direction", "currency" — 1.0 = read directly or confirmed by the user; lower = inferred
    field_reasons: dict[str, str] = field(default_factory=dict)  # per-field reason code (ingest/confidence.py
    # REASONS) saying HOW the value was arrived at — the "derived rule" half of every field's provenance
    import_job_id: str | None = None  # which ImportJob committed this row — what makes undo-by-import possible


# ---------------------------------------------------------------------------
# Staged imports — upload -> analyze -> map -> review -> commit (-> rollback)
# ---------------------------------------------------------------------------

class ImportState(str, Enum):
    UPLOADED = "uploaded"
    ANALYZING = "analyzing"
    NEEDS_MAPPING = "needs_mapping"  # a required column (date, or how amounts are laid out) isn't certain
    NEEDS_REVIEW = "needs_review"  # mapping is fine, but some rows/assumptions need the user's check
    READY = "ready"  # nothing unresolved — can be committed
    COMMITTED = "committed"
    FAILED = "failed"  # nothing usable came out (including zero transactions) — never committed
    ROLLED_BACK = "rolled_back"
    DUPLICATE = "duplicate"  # these exact file bytes are already in the ledger
    CANCELLED = "cancelled"


TERMINAL_IMPORT_STATES = {
    ImportState.COMMITTED, ImportState.FAILED, ImportState.ROLLED_BACK, ImportState.DUPLICATE, ImportState.CANCELLED,
}


class AmountModel(str, Enum):
    """How a tabular source lays out money — represented explicitly rather than forcing
    every file through a single signed "amount" column."""

    SIGNED = "signed"  # one amount column; direction from its sign / parentheses / CR-DR suffix
    DEBIT_CREDIT = "debit_credit"  # separate money-out and money-in columns, exactly one filled per row
    AMOUNT_WITH_MARKER = "amount_with_marker"  # amount plus a Dr/Cr (or debit/credit) type column
    AMOUNT_WITH_BALANCE = "amount_with_balance"  # unsigned amount; direction only from validated balance changes


class IssueSeverity(str, Enum):
    BLOCKING = "blocking"  # can't commit until fixed (new mapping) or the row is explicitly left out
    CHECK = "check"  # can commit once the user has acknowledged it
    INFO = "info"  # shown, never blocks


@dataclass
class ValidationIssue:
    issue_id: str  # deterministic (rule + location), so an acknowledgement survives re-analysis
    rule: str
    severity: IssueSeverity
    message: str  # plain language, shown as-is to the user
    suggested_action: str
    target: str | None = None  # the row/transaction key this is about ("row:12" / "txn:<id>"), None = whole file
    field: str | None = None
    evidence: str = ""  # the source cells/text the issue is about
    resolution: str | None = None  # "excluded" | "acknowledged" | None


@dataclass
class RawTransactionCandidate:
    """One source row, preserved before normalization. Every row ends in exactly one
    outcome — a transaction, an explicitly ignored non-transaction row, or an issue —
    so no row can be silently lost."""

    key: str  # "row:<n>" for tabular sources
    source_row: int
    cells: dict[str, str]
    outcome: str  # "transaction" | "ignored" | "issue"
    reason: str = ""
    transaction_id: str | None = None


@dataclass
class ImportJob:
    job_id: str
    state: ImportState
    original_filename: str
    stored_path: str
    owner: str = "local"
    file_hash: str | None = None
    file_kind: str | None = None  # "tabular" | "pdf" | "image"
    parser_version: str = ""
    created_at: str = ""
    updated_at: str = ""
    error_summary: str | None = None
    document_id: str | None = None
    transaction_count: int = 0
    mapping_fingerprint: str | None = None
