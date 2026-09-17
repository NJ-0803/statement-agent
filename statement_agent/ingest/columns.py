"""Every column is kept. A column that isn't one of the core roles (date, amount, description, …) is not
"unknown": it's stored on each transaction as extra information, with a *kind* judged from its values
(date, time, number, money, code, category-like, card number, …) and, where its name says so, a *meaning*
(payment method, location, counterparty, tax, …). Nothing here changes a transaction's core fields; it
makes sure no column a file carried is lost, and that the agent can search and group by it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .vocab import looks_like_amount, looks_like_date, normalize_header

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?$")
_PERCENT_RE = re.compile(r"^-?\d+(?:[.,]\d+)?\s*%$")
_CARD_RE = re.compile(r"^[\dXx*•\- ]{12,23}$")
_CODE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9/_\-.#:]{4,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_UPI_RE = re.compile(r"^[\w.\-]+@[A-Za-z]+$")
_BOOL = {"yes", "no", "y", "n", "true", "false", "0", "1"}

# header words -> meaning; the first matching entry wins
_MEANINGS: list[tuple[str, tuple[str, ...]]] = [
    ("time", ("time", "hour", "uhrzeit", "heure", "hora", "समय")),
    ("card number", ("card no", "card number", "card num", "pan", "kartennummer", "numero carte")),
    ("account number", ("account no", "account number", "a c no", "iban", "kontonummer", "acct")),
    ("payment method", ("payment method", "payment mode", "mode", "channel", "method", "instrument", "zahlungsart")),
    ("counterparty", ("payee", "payer", "beneficiary", "counterparty", "vendor", "supplier", "merchant", "sender",
                      "recipient", "empfänger", "auftraggeber", "bénéficiaire", "beneficiario")),
    ("location", ("location", "city", "country", "place", "address", "store", "branch", "ort", "ville", "ciudad")),
    ("status", ("status", "state")),
    ("tax", ("tax", "gst", "vat", "tds", "cess", "mwst", "tva", "iva")),
    ("fee", ("fee", "charge", "commission", "gebühr", "frais")),
    ("exchange rate", ("exchange rate", "fx rate", "conversion rate", "rate")),
    ("original amount", ("original amount", "foreign amount", "amount in", "billed amount", "settlement amount")),
    ("tags", ("tag", "tags", "label", "labels")),
    ("person", ("employee", "member", "cardholder", "card holder", "user", "name", "spent by")),
    ("project", ("project", "cost centre", "cost center", "department", "client")),
    ("invoice", ("invoice", "bill no", "receipt", "order id", "order no")),
    ("quantity", ("qty", "quantity", "units")),
    ("points", ("points", "reward", "cashback")),
]


@dataclass
class ExtraColumn:
    index: int
    header: str
    kind: str  # date | time | money | number | percent | card number | code | email | upi id | yes/no | category | text | empty
    meaning: str | None
    distinct: int
    sample: list[str]

    def to_dict(self) -> dict:
        return {"index": self.index, "header": self.header, "kind": self.kind, "meaning": self.meaning,
                "distinct": self.distinct, "sample": self.sample}


def _looks_like_card(v: str) -> bool:
    if not _CARD_RE.match(v):
        return False
    digits = sum(c.isdigit() for c in v)
    masked = any(c in "Xx*•" for c in v)
    return (masked and digits >= 4) or (not masked and 13 <= digits <= 19)


def _kind(values: list[str]) -> str:
    if not values:
        return "empty"
    n = len(values)

    def share(pred) -> float:
        return sum(1 for v in values if pred(v)) / n

    if share(lambda v: bool(_TIME_RE.match(v))) >= 0.8:
        return "time"
    if share(_looks_like_card) >= 0.8:
        return "card number"
    if share(looks_like_date) >= 0.8:
        return "date"
    if share(lambda v: bool(_PERCENT_RE.match(v))) >= 0.8:
        return "percent"
    if share(lambda v: v.lower() in _BOOL) >= 0.9 and len({v.lower() for v in values}) <= 2:
        return "yes/no"
    if share(lambda v: bool(_EMAIL_RE.match(v))) >= 0.8:
        return "email"
    if share(lambda v: bool(_UPI_RE.match(v))) >= 0.8:
        return "upi id"
    if share(looks_like_amount) >= 0.8:
        has_money_marks = any(re.search(r"[₹$€£]|\.\d{2}\b|,\d{2}\b", v) for v in values)
        return "money" if has_money_marks else "number"
    if share(lambda v: bool(_CODE_RE.match(v))) >= 0.8:
        return "code"
    distinct = len(set(values))
    if n >= 4 and distinct <= max(2, n // 3) and max(len(v) for v in values) <= 40:
        return "category"
    return "text"


def _meaning(header: str) -> str | None:
    norm, _ = normalize_header(header)
    padded = f" {norm} "
    for meaning, words in _MEANINGS:
        if any(f" {normalize_header(w)[0]} " in padded for w in words):
            return meaning
    return None


def describe_extra_columns(headers: list[str], rows: list[tuple[int, list[str]]], used: set[int]) -> list[ExtraColumn]:
    out = []
    for j, header in enumerate(headers):
        if j in used:
            continue
        values = [cells[j].strip() for _, cells in rows[:500] if j < len(cells) and cells[j] and cells[j].strip()]
        values = [v for v in values if v.lower() != header.strip().lower()]
        out.append(ExtraColumn(
            index=j, header=header, kind=_kind(values), meaning=_meaning(header),
            distinct=len(set(values)), sample=list(dict.fromkeys(values))[:3],
        ))
    return out


def mask_card_number(value: str) -> str:
    """Keeps only the last four digits of a card number (PCI DSS: don't store a full PAN)."""
    digits = [c for c in value if c.isdigit()]
    if len(digits) < 12:
        return value
    return "•••• " + "".join(digits[-4:])
