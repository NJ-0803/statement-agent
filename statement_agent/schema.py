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
    reconciliation_status: str = "NOT_CHECKED"  # RECONCILED | MISMATCH | NOT_CHECKED | NO_TOTALS
    reconciliation_delta: Decimal | None = None
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Transaction:
    transaction_id: str
    document_id: str

    transaction_date: date | None
    date_raw: str
    date_plausible: bool = True  # False if the parsed date falls outside a sane bound

    description_raw: str = ""
    merchant_raw: str | None = None

    amount: Decimal = Decimal("0")
    currency: str = "INR"
    amount_raw: str = ""
    direction: Direction = Direction.DEBIT

    economic_type: EconomicType = EconomicType.UNKNOWN
    economic_type_confidence: float = 1.0

    category: str | None = None  # only set when economic_type == PURCHASE
    category_confidence: float | None = None

    source: SourceRef | None = None

    duplicate_of: str | None = None  # transaction_id of the canonical row, if this is a probable duplicate
    duplicate_reason: str | None = None

    notes: str = ""
