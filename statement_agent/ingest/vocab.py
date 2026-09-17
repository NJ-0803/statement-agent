"""Shared vocabulary for tabular structure discovery (sniff.py) and column-role inference (mapping.py).

Header meaning is scored, not exact-matched: "Withdrawal Amt.", "Withdrawal Amount (INR )" and
"WithdrawalAmount" all normalize to the same tokens, and a currency code inside a header is pulled
out as document evidence rather than breaking the match. Value shape (does this cell look like a
date? an amount? a Dr/Cr marker?) lives here too, so header detection and role inference judge
cells identically.
"""

from __future__ import annotations

import re

from ..normalize import DocumentDateResolver

ROLES = (
    "date", "value_date", "description", "amount", "debit", "credit", "marker",
    "balance", "currency", "reference", "category", "account", "notes",
)

# Plain-language labels, shown in the mapping preview instead of role codes.
ROLE_LABELS = {
    "date": "Date",
    "value_date": "Value date (when the bank processed it)",
    "description": "Description",
    "amount": "Amount",
    "debit": "Money out",
    "credit": "Money in",
    "marker": "Money in or out marker (Dr/Cr)",
    "balance": "Balance",
    "currency": "Currency",
    "reference": "Reference or cheque number",
    "category": "Category",
    "account": "Account",
    "notes": "Notes",
}

ROLE_SYNONYMS: dict[str, set[str]] = {
    "date": {
        "date", "txn date", "tran date", "trans date", "transaction date", "posting date", "posted date",
        "post date", "booking date", "book date", "timestamp", "transaction date and time", "txn dt", "tran dt",
        "date time", "datetime", "transaction dt",
    },
    "value_date": {"value date", "value dt", "val date", "effective date"},
    "description": {
        "description", "narration", "particulars", "details", "merchant", "merchant name", "transaction details",
        "transaction remarks", "transaction description", "payee", "beneficiary",
    },
    "amount": {
        "amount", "amt", "value", "transaction amount", "txn amount", "txn amt", "amount in", "local amount",
    },
    "debit": {
        "debit", "debits", "debit amount", "debit amt", "withdrawal", "withdrawals", "withdrawal amt",
        "withdrawal amount", "dr", "dr amount", "money out", "paid out", "outflow", "spent",
    },
    "credit": {
        "credit", "credits", "credit amount", "credit amt", "deposit", "deposits", "deposit amt", "deposit amount",
        "cr", "cr amount", "money in", "paid in", "inflow", "received",
    },
    "marker": {
        "dr cr", "cr dr", "type", "transaction type", "txn type", "debit credit", "credit debit", "d c",
        "indicator", "dr cr indicator",
    },
    "balance": {
        "balance", "closing balance", "running balance", "available balance", "bal", "balance amount",
        "balance after", "ledger balance",
    },
    "currency": {"currency", "ccy", "transaction currency", "curr", "currency code"},
    "reference": {
        "reference", "ref", "ref no", "reference no", "reference number", "chq ref no", "chq no", "cheque no",
        "cheque number", "chqno", "utr", "utr no", "transaction id", "txn id", "chq no ref no",
    },
    "category": {"category"},
    "account": {"account", "account name"},
    "notes": {"notes", "note", "remarks", "transaction notes", "memo", "comments", "comment"},
}

# Substring cues for headers that match no synonym at all ("Posting Dt of Txn", "Withdrawls").
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "description": ("narrat", "particular", "descr", "remark"),
    "debit": ("withdraw", "debit"),
    "credit": ("deposit", "credit"),
    "balance": ("balance",),
    "reference": ("cheque", "chq", "ref"),
    "amount": ("amount", "amt"),
}

_CURRENCY_HEADER_TOKENS = {"inr": "INR", "rs": "INR", "usd": "USD", "eur": "EUR", "gbp": "GBP"}
_CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}
KNOWN_CURRENCIES = {"INR", "USD", "EUR", "GBP"}

CREDIT_MARKERS = {"cr", "credit", "c", "deposit"}
DEBIT_MARKERS = {"dr", "debit", "d", "withdrawal"}

_AMOUNT_SHAPE_RE = re.compile(
    r"^\s*(?:₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?\s*[-(]?\s*\d[\d,.]*\s*\)?\s*(?:CR|DR|Cr|Dr|cr|dr)?\s*-?\s*(?:INR|USD|EUR|GBP)?\s*$"
)
SUMMARY_ROW_RE = re.compile(
    r"^\s*(?:opening|closing)\s+bal|^\s*(?:grand\s+)?totals?\b|brought\s+forward|carried\s+forward|"
    r"^\s*[bc]\s*/\s*f\b|statement\s+summary",
    re.IGNORECASE,
)
OPENING_BALANCE_RE = re.compile(r"^\s*opening\s+bal", re.IGNORECASE)
CLOSING_BALANCE_RE = re.compile(r"^\s*closing\s+bal", re.IGNORECASE)

_DATE_PROBE = DocumentDateResolver()


def normalize_header(raw: str) -> tuple[str, str | None]:
    """Returns (normalized header text, currency declared inside the header if any)."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw or "")  # "TransactionType" -> "Transaction Type"
    currency = next((code for sym, code in _CURRENCY_SYMBOLS.items() if sym in text), None)
    tokens = re.sub(r"[^0-9a-z]+", " ", text.lower()).split()
    kept = []
    for tok in tokens:
        if tok in _CURRENCY_HEADER_TOKENS and len(tokens) > 1:
            currency = currency or _CURRENCY_HEADER_TOKENS[tok]
            continue
        kept.append(tok)
    return " ".join(kept), currency


def header_role_scores(raw: str) -> dict[str, float]:
    """Score a header against every role: 1.0 exact synonym, 0.8 close token match,
    0.55 keyword cue. A header that exactly names one role scores at most 0.3 for any
    other, so "Value Date" is never a strong candidate for the transaction date."""
    norm, _ = normalize_header(raw)
    if not norm:
        return {}
    tokens = set(norm.split())
    exact = {role for role, syns in ROLE_SYNONYMS.items() if norm in syns}
    scores: dict[str, float] = {}
    for role, syns in ROLE_SYNONYMS.items():
        if role in exact:
            scores[role] = 1.0
            continue
        score = 0.0
        for syn in syns:
            syn_tokens = set(syn.split())
            if syn_tokens <= tokens and len(tokens) <= len(syn_tokens) + 2 and len(syn) > 2:
                score = max(score, 0.8)
        if score == 0.0 and any(k in norm for k in ROLE_KEYWORDS.get(role, ())):
            score = 0.55
        if exact and score:
            score = min(score, 0.3)
        if score:
            scores[role] = score
    return scores


def looks_like_date(value: str) -> bool:
    return bool(value) and _DATE_PROBE.parse(value).value is not None


def looks_like_amount(value: str) -> bool:
    return bool(value) and bool(_AMOUNT_SHAPE_RE.match(value)) and any(ch.isdigit() for ch in value)


def marker_direction(value: str) -> str | None:
    v = (value or "").strip().lower().rstrip(".")
    if v in CREDIT_MARKERS:
        return "CREDIT"
    if v in DEBIT_MARKERS:
        return "DEBIT"
    return None


def currency_code(value: str) -> str | None:
    v = (value or "").strip()
    if v.upper() in KNOWN_CURRENCIES:
        return v.upper()
    if v in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[v]
    if v.lower().rstrip(".") == "rs":
        return "INR"
    return None
