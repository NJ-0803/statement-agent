"""Reads the figures a statement states about itself — opening/closing balance and total debits/credits —
so reconciliation has something real to check the extracted rows against.

Only labelled figures are used, and a label that shows up with two different values (a "balance brought
forward" printed on every page, say) is dropped with a warning rather than guessed between. Handles the
common layouts: "Opening Balance: 10,000.00" on its own line, several labels with their figures on one
line, and a summary strip where the labels sit on one line and the figures on the next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from ..normalize import normalize_amount

FIGURES = ("opening", "closing", "total_debits", "total_credits")

_LABELS: dict[str, str] = {
    "opening": r"opening\s+bal(?:ance)?|previous\s+(?:statement\s+)?balance|last\s+statement\s+balance|"
               r"balance\s+(?:b/?f|brought\s+forward)|brought\s+forward",
    "closing": r"closing\s+bal(?:ance)?|new\s+balance|balance\s+(?:c/?f|carried\s+forward)|carried\s+forward",
    "total_debits": r"total\s+(?:debits?|withdrawals?|debit\s+amount|money\s+out)(?!\s+count)|debits?\s+total",
    "total_credits": r"total\s+(?:credits?|deposits?|credit\s+amount|money\s+in)(?!\s+count)|credits?\s+total",
}
LABEL_RE = re.compile("|".join(f"(?P<{k}>{v})" for k, v in _LABELS.items()), re.IGNORECASE)

_MONEY_RE = re.compile(
    r"(?:₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)?\s*\(?-?\s*\d[\d,]*\.\d{2}\)?(?:\s*(?:CR|DR)\b)?", re.IGNORECASE
)


@dataclass
class StatedFigures:
    values: dict[str, str] = field(default_factory=dict)  # figure -> raw text as printed
    conflicts: list[str] = field(default_factory=list)

    def add(self, name: str, raw: str) -> None:
        raw = raw.strip()
        if name in self.conflicts:
            return
        old = self.values.get(name)
        if old is not None and _plain(old) != _plain(raw):
            del self.values[name]
            self.conflicts.append(name)
            return
        self.values[name] = raw

    def warnings(self) -> list[str]:
        words = {"opening": "opening balance", "closing": "closing balance",
                 "total_debits": "total money out", "total_credits": "total money in"}
        return [f"RECONCILIATION: the statement shows more than one different {words[n]}, so it wasn't used"
                for n in self.conflicts]


def _plain(raw: str) -> str:
    return re.sub(r"[^\d.()\-A-Za-z]", "", raw).upper()


def is_summary_text(text: str) -> bool:
    return bool(LABEL_RE.search(text))


def is_summary_label(text: str) -> bool:
    """The whole text is just a label ("Opening Balance", "BALANCE B/F:") — the test for a dated row, where
    a merchant name can contain a label ("NEW BALANCE ATHLETICS")."""
    return bool(LABEL_RE.fullmatch(text.strip(" :-*.")))


def read_figures(lines: list[str], into: StatedFigures | None = None) -> StatedFigures:
    figures = into or StatedFigures()
    pending: list[str] = []  # labels from a previous line that carried no figures
    for line in lines:
        labels = [(m.lastgroup, m.start(), m.end()) for m in LABEL_RE.finditer(line)]
        monies = list(_MONEY_RE.finditer(line))
        if labels and not monies:
            pending = [name for name, _, _ in labels]
            continue
        if not labels and pending:
            figures_only = not re.search(r"[A-Za-z]", _MONEY_RE.sub("", line))
            if figures_only and len(monies) == len(pending):
                for name, m in zip(pending, monies):
                    figures.add(name, m.group(0))
            pending = []
            continue
        pending = []
        for i, (name, _, end) in enumerate(labels):
            stop = labels[i + 1][1] if i + 1 < len(labels) else len(line)
            m = next((m for m in monies if end <= m.start() < stop), None)
            if m:
                figures.add(name, m.group(0))
    return figures


def signed_balance(raw: str, *, credit_card: bool, default_currency: str = "INR") -> Decimal | None:
    """A balance as a signed number in the statement's own sense: money in the account for a bank
    statement (an overdrawn "Dr" balance is negative), money owed for a card (a "Cr" balance is negative)."""
    parsed = normalize_amount(raw, default_currency=default_currency)
    if parsed is None:
        return None
    text = raw.strip()
    negative = text.startswith("-") or text.startswith("(") or bool(re.search(r"\)\s*$|-\s*$", text))
    suffix = re.search(r"\b(CR|DR)\s*$", text, re.IGNORECASE)
    if suffix:
        negative = suffix.group(1).upper() == ("CR" if credit_card else "DR")
    return -parsed.amount if negative else parsed.amount


def total_amount(raw: str, default_currency: str = "INR") -> Decimal | None:
    parsed = normalize_amount(raw, default_currency=default_currency)
    return parsed.amount if parsed else None


def apply_to_document(document, figures: StatedFigures) -> None:
    credit_card = document.doc_type == "credit_card_statement"
    ccy = document.currency_declared or "INR"
    v = figures.values
    if "opening" in v:
        document.opening_balance = signed_balance(v["opening"], credit_card=credit_card, default_currency=ccy)
    if "closing" in v:
        document.closing_balance = signed_balance(v["closing"], credit_card=credit_card, default_currency=ccy)
    if "total_debits" in v:
        document.stated_total_debits = total_amount(v["total_debits"], ccy)
    if "total_credits" in v:
        document.stated_total_credits = total_amount(v["total_credits"], ccy)
    document.parse_warnings.extend(figures.warnings())
