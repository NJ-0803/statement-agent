"""Shared vocabulary for tabular structure discovery (sniff.py) and column-role inference (mapping.py).

Header meaning is scored, not exact-matched: "Withdrawal Amt.", "Withdrawal Amount (INR )" and
"WithdrawalAmount" all normalize to the same tokens, and a currency code inside a header is pulled
out as document evidence rather than breaking the match. Value shape (does this cell look like a
date? an amount? a Dr/Cr marker?) lives here too, so header detection and role inference judge
cells identically.
"""

from __future__ import annotations

import re
import unicodedata

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

# Other languages' common bank-export headers (accents folded; see _fold). Kept apart so the English list
# above stays readable; merged into ROLE_SYNONYMS below.
_INTERNATIONAL_SYNONYMS: dict[str, set[str]] = {
    "date": {
        "datum", "buchungstag", "buchungsdatum", "transaktionsdatum", "date operation", "date de l operation",
        "date comptable", "fecha", "fecha operacion", "fecha de operacion", "data", "data operazione",
        "data contabile", "data movimento", "data transacao", "तारीख", "दिनांक", "लेनदेन तिथि", "तिथि",
    },
    "value_date": {"wertstellung", "valuta", "valutadatum", "date de valeur", "fecha valor", "data valuta", "मूल्य तिथि"},
    "description": {
        "verwendungszweck", "buchungstext", "beschreibung", "libelle", "libelle operation", "description operation",
        "concepto", "descripcion", "detalle", "descrizione", "causale", "descricao", "historico", "विवरण", "ब्यौरा",
    },
    "amount": {"betrag", "umsatz", "montant", "importe", "importo", "valor", "राशि", "रकम"},
    "debit": {"soll", "lastschrift", "debit euros", "cargo", "cargos", "dare", "uscite", "addebiti", "नामे", "निकासी"},
    "credit": {"haben", "gutschrift", "credit euros", "abono", "abonos", "avere", "entrate", "accrediti", "जमा"},
    "balance": {"saldo", "kontostand", "solde", "saldo disponible", "शेष", "बकाया"},
    "currency": {"wahrung", "devise", "moneda", "valuta divisa", "divisa", "moeda", "मुद्रा"},
    "reference": {"referenz", "reference operation", "referencia", "riferimento", "संदर्भ"},
    "category": {"kategorie", "categorie", "categoria", "श्रेणी"},
    "account": {"konto", "compte", "cuenta", "conto", "खाता"},
    "notes": {"notiz", "bemerkung", "observaciones", "note", "टिप्पणी"},
}
_EXTRA_ENGLISH = {
    "date": {"posted on", "created at", "created", "transaction time", "date of transaction", "trans date time",
             "booking date time", "settlement date", "time stamp", "bookgdt dt", "bookg dt", "bookg dt dt", "dtposted"},
    "value_date": {"valdt dt", "val dt", "val dt dt"},
    "description": {"details of transaction", "narrative", "payee name", "merchant description", "ustrd",
                    "rmtinf ustrd", "addtlntryinf", "transaction narration", "purpose"},
    "amount": {"amount inr", "transaction value", "trnamt", "amt", "net amount", "total amount", "amount rs"},
    "debit": {"amount debited", "debited", "debit inr", "paid", "expense", "expenses", "outgoing"},
    "credit": {"amount credited", "credited", "credit inr", "income", "incoming"},
    "category": {"expense type", "type of expense", "expense category", "spend category", "spending category",
                 "category name", "expense head", "budget category", "subcategory", "sub category", "spend type",
                 "purpose category", "classification", "category type", "kategorie"},
    "marker": {"cdtdbtind", "cdt dbt ind", "credit debit indicator", "cr dr indicator", "dr or cr"},
}
for _role, _syns in (*_INTERNATIONAL_SYNONYMS.items(), *_EXTRA_ENGLISH.items()):
    ROLE_SYNONYMS[_role] |= _syns

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

CREDIT_MARKERS = {"cr", "credit", "c", "deposit", "crdt", "haben", "in", "incoming", "जमा"}
DEBIT_MARKERS = {"dr", "debit", "d", "withdrawal", "dbit", "soll", "out", "outgoing", "नामे"}

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


def _fold(text: str) -> str:
    """Drops accents from Latin letters ("libellé" -> "libelle", "währung" -> "wahrung") but keeps every
    other script intact — Devanagari vowel signs are combining marks too, and must not be stripped."""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def normalize_header(raw: str) -> tuple[str, str | None]:
    """Returns (normalized header text, currency declared inside the header if any)."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw or "")  # "TransactionType" -> "Transaction Type"
    currency = next((code for sym, code in _CURRENCY_SYMBOLS.items() if sym in text), None)
    folded = _fold(text.lower())
    tokens = "".join(ch if ch.isalnum() or unicodedata.category(ch).startswith("M") else " " for ch in folded).split()
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
