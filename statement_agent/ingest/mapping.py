"""Column-role inference: which source column is the date, the description, the money?

Each column is scored against every canonical role from two independent signals — what its header
*says* (vocab.header_role_scores) and what its values *look like* (dates, amounts, Dr/Cr markers,
currency codes) — then roles are assigned greedily, strongest first. The result carries evidence
per role and a list of plain-language ambiguities. A mapping with any ambiguity is never applied
automatically: the import stops at needs_mapping and a person confirms it.

Nothing here creates a monetary value. It only decides where to read values from; tabular.py
normalizes them deterministically once a mapping is settled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

from ..normalize import DocumentDateResolver
from ..schema import AmountModel
from .sniff import TableCandidate
from .vocab import (
    KNOWN_CURRENCIES, ROLES, currency_code, header_role_scores, looks_like_amount, looks_like_date,
    marker_direction, normalize_header,
)

_SHAPE_CRITICAL = {"date", "value_date", "amount", "debit", "credit", "balance", "marker", "currency"}
_HARD_SHAPE_MIN = {"marker": 0.6, "debit": 0.5, "credit": 0.5, "currency": 0.6}
_ASSIGN_THRESHOLD = 0.3
_TIE_MARGIN = 0.1
_SAMPLE = 300
_EXCEL_SERIAL_RE = re.compile(r"^\d{5}(?:\.0+)?$")
_COMMA_DECIMAL_RE = re.compile(r",\d{2}\s*(?:CR|DR)?\)?-?\s*$", re.IGNORECASE)
_DOT_DECIMAL_RE = re.compile(r"\.\d{1,2}\s*(?:CR|DR)?\)?-?\s*$", re.IGNORECASE)
_DOC_CURRENCY_RE = re.compile(r"\b(INR|USD|EUR|GBP)\b")


class MappingError(ValueError):
    pass


@dataclass
class ColumnMapping:
    roles: dict[str, int]
    headers: list[str]
    amount_model: str | None
    date_order: str | None = None  # "DMY" | "MDY" once pinned by evidence or by the user
    date_order_source: str = "undetermined"  # "evidence" | "user" | "undetermined"
    currency: str | None = None  # document-level currency for rows without their own
    currency_source: str = "assumed"  # "column" | "header" | "document" | "amount_cells" | "user" | "assumed"
    decimal_separator: str = "."
    excel_serial_dates: bool = False
    confidence: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    ambiguities: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    source: str = "inferred"  # "inferred" | "profile" | "user"
    fingerprint: str | None = None
    sheet: str | None = None
    header_row: int | None = None

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.ambiguities or self.missing_required)

    def column(self, role: str) -> int | None:
        return self.roles.get(role)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ColumnMapping":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        clean["roles"] = {k: int(v) for k, v in (clean.get("roles") or {}).items()}
        return cls(**clean)


def header_fingerprint(table: TableCandidate) -> str | None:
    """Same export layout -> same fingerprint, regardless of which month's file it is."""
    if table.header_row is None:
        return None
    norm = "|".join(normalize_header(h)[0] for h in table.headers)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _column_values(table: TableCandidate, j: int) -> list[str]:
    own_header = table.headers[j].strip().lower() if j < len(table.headers) else ""
    out = []
    for _, cells in table.rows:
        # a header repeated inside the body (page breaks) is not a value of its own column
        if j < len(cells) and cells[j] and cells[j].strip().lower() != own_header:
            out.append(cells[j])
            if len(out) >= _SAMPLE:
                break
    return out


def _shape(values: list[str]) -> dict[str, float]:
    if not values:
        return {"date": 0.0, "amount": 0.0, "marker": 0.0, "currency": 0.0, "text": 0.0, "serial": 0.0}
    n = len(values)
    dates = sum(1 for v in values if looks_like_date(v))
    amounts = sum(1 for v in values if looks_like_amount(v) and not looks_like_date(v))
    markers = sum(1 for v in values if marker_direction(v))
    currencies = sum(1 for v in values if currency_code(v))
    serials = sum(1 for v in values if _EXCEL_SERIAL_RE.match(v) and 20000 <= float(v) <= 80000)
    texty = [v for v in values if not looks_like_amount(v) and not looks_like_date(v)]
    text_ratio = len(texty) / n
    if texty and (sum(len(v) for v in texty) / len(texty) < 3 or len(set(texty)) < 2):
        text_ratio *= 0.5
    return {
        "date": dates / n, "amount": amounts / n, "marker": markers / n, "currency": currencies / n,
        "text": text_ratio, "serial": serials / n,
    }


def _role_shape(role: str, shape: dict[str, float]) -> float:
    if role in ("date", "value_date"):
        return max(shape["date"], shape["serial"])
    if role in ("amount", "debit", "credit", "balance"):
        return shape["amount"]
    if role == "marker":
        return shape["marker"]
    if role == "currency":
        return shape["currency"]
    if role == "description":
        return shape["text"]
    return 0.6  # reference / category / account / notes: value shape says little either way


def _score_columns(table: TableCandidate) -> tuple[dict[tuple[str, int], float], dict[int, dict], dict[int, dict]]:
    scores: dict[tuple[str, int], float] = {}
    shapes: dict[int, dict] = {}
    header_scores: dict[int, dict] = {}
    for j, header in enumerate(table.headers):
        values = _column_values(table, j)
        shape = _shape(values)
        shapes[j] = shape
        hs = header_role_scores(header) if table.header_row is not None else {}
        header_scores[j] = hs
        for role in ROLES:
            h = hs.get(role, 0.0)
            if not values:
                # An empty column (a Deposit column on a withdrawals-only statement) has no values to
                # contradict its name — judge it on the name alone, with neutral shape.
                if h > 0:
                    scores[(role, j)] = 0.65 * h + 0.35 * 0.5
                continue
            s = _role_shape(role, shape)
            if role in _HARD_SHAPE_MIN and s < _HARD_SHAPE_MIN[role]:
                continue
            if h > 0:
                score = 0.65 * h + 0.35 * s
                if role in _SHAPE_CRITICAL and s < 0.5:
                    score *= 0.5  # named like this role, but the values disagree
            elif role in ("date", "amount", "description"):
                score = 0.45 * s  # value shape alone can propose, never confirm
            else:
                continue
            if score >= _ASSIGN_THRESHOLD:
                scores[(role, j)] = score
    return scores, shapes, header_scores


def decide_amount_model(roles: dict[str, int], table: TableCandidate) -> str | None:
    if "debit" in roles and "credit" in roles:
        return AmountModel.DEBIT_CREDIT.value
    if "amount" in roles and "marker" in roles:
        return AmountModel.AMOUNT_WITH_MARKER.value
    if "amount" in roles:
        if "balance" in roles:
            values = _column_values(table, roles["amount"])
            signed = any(v.lstrip().startswith(("-", "(")) or re.search(r"(CR|DR|-)\s*$", v, re.IGNORECASE) for v in values)
            if not signed:
                return AmountModel.AMOUNT_WITH_BALANCE.value
        return AmountModel.SIGNED.value
    return None


def infer_mapping(table: TableCandidate, *, profile: dict | None = None) -> ColumnMapping:
    fingerprint = header_fingerprint(table)
    if profile and fingerprint and profile.get("fingerprint") == fingerprint:
        mapping = ColumnMapping.from_dict(profile)
        if len(mapping.headers) == len(table.headers):
            mapping.source = "profile"
            mapping.sheet, mapping.header_row = table.sheet, table.header_row
            mapping.ambiguities, mapping.missing_required = [], []
            mapping.evidence.setdefault("_file", []).insert(0, "Same column layout as a file you confirmed before")
            return mapping

    scores, shapes, header_scores = _score_columns(table)
    roles: dict[str, int] = {}
    confidence: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for (role, j), score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if role in roles or j in roles.values():
            continue
        roles[role] = j
        confidence[role] = round(score, 2)
        h = header_scores[j].get(role, 0.0)
        reasons = []
        if h >= 0.8:
            reasons.append(f"column '{table.headers[j]}' is named like this")
        elif h > 0:
            reasons.append(f"column '{table.headers[j]}' partly matches this name")
        else:
            reasons.append(f"column {j + 1} has no usable name; judged from its values only")
        shape_value = _role_shape(role, shapes[j])
        if role in _SHAPE_CRITICAL or role == "description":
            reasons.append(f"{shape_value:.0%} of its values fit")
        evidence[role] = reasons

    ambiguities: list[str] = []

    if "date" not in roles and "value_date" in roles:
        roles["date"] = roles.pop("value_date")
        confidence["date"] = confidence.pop("value_date")
        evidence["date"] = evidence.pop("value_date") + ["used as the transaction date: it's the only date column"]

    excel_serial = False
    if "date" in roles and shapes[roles["date"]]["serial"] > shapes[roles["date"]]["date"]:
        excel_serial = True
        evidence["date"].append("values are Excel date serial numbers")

    # A strong runner-up for a required role means we genuinely can't tell which column is meant.
    for role in ("date", "amount", "debit", "credit"):
        if role not in roles:
            continue
        chosen = roles[role]
        rivals = [
            (s, j) for (r, j), s in scores.items()
            if r == role and j != chosen and j not in roles.values()
        ]
        if rivals:
            best_rival, rj = max(rivals)
            if best_rival >= confidence[role] - _TIE_MARGIN:
                ambiguities.append(
                    f"Both '{table.headers[chosen]}' and '{table.headers[rj]}' could be the "
                    f"{_role_word(role)} column."
                )

    amount_model = decide_amount_model(roles, table)
    if amount_model == AmountModel.DEBIT_CREDIT.value and "amount" in roles:
        # separate money-out/money-in columns win; a stray "amount" guess would only confuse the preview
        for d in (roles, confidence, evidence):
            d.pop("amount", None)
    missing_required: list[str] = []
    date_col = roles.get("date")
    if date_col is None or not (header_scores[date_col].get("date") or header_scores[date_col].get("value_date")):
        missing_required.append("date")
    if amount_model is None:
        missing_required.append("amount")
        if ("debit" in roles) != ("credit" in roles):
            only = "debit" if "debit" in roles else "credit"
            ambiguities.append(
                f"I found a money-{'out' if only == 'debit' else 'in'} column ('{table.headers[roles[only]]}') "
                "but not its partner column."
            )
    else:
        money_roles = ["debit", "credit"] if amount_model == AmountModel.DEBIT_CREDIT.value else ["amount"]
        if any(header_scores[roles[r]].get(r, 0) == 0 for r in money_roles):
            missing_required.append("amount")

    for role in ("date", "amount", "debit", "credit"):
        if role in roles and role not in missing_required:
            h = header_scores[roles[role]].get(role, 0) or header_scores[roles[role]].get("value_date", 0)
            if 0 < h < 0.8:
                ambiguities.append(f"I think '{table.headers[roles[role]]}' is the {_role_word(role)} column — please confirm.")

    if table.header_row is None and ("date" in roles or amount_model):
        ambiguities.append("This file has no header row, so I guessed the columns from their values — please check them.")

    mapping = ColumnMapping(
        roles=roles, headers=list(table.headers), amount_model=amount_model, confidence=confidence,
        evidence=evidence, ambiguities=ambiguities, missing_required=missing_required,
        fingerprint=fingerprint, sheet=table.sheet, header_row=table.header_row, excel_serial_dates=excel_serial,
    )
    _settle_currency(mapping, table)
    _settle_decimal_separator(mapping, table)
    _settle_date_order(mapping, table)
    return mapping


def _role_word(role: str) -> str:
    return {"date": "date", "amount": "amount", "debit": "money-out", "credit": "money-in"}.get(role, role)


def _money_columns(mapping: ColumnMapping) -> list[int]:
    return [mapping.roles[r] for r in ("amount", "debit", "credit", "balance") if r in mapping.roles]


def _settle_currency(mapping: ColumnMapping, table: TableCandidate) -> None:
    if mapping.currency_source == "user":
        return
    notes = mapping.evidence.setdefault("_file", [])
    if "currency" in mapping.roles:
        mapping.currency_source = "column"
        notes.append(f"Currency is read from the '{table.headers[mapping.roles['currency']]}' column")
    header_hints = {normalize_header(table.headers[j])[1] for j in _money_columns(mapping)} - {None}
    if len(header_hints) == 1:
        mapping.currency = header_hints.pop()
        mapping.currency_source = "header" if mapping.currency_source != "column" else "column"
        notes.append(f"Column names say amounts are in {mapping.currency}")
        return
    preamble_text = " ".join(" ".join(cells) for _, cells in table.preamble)
    doc_hits = set(_DOC_CURRENCY_RE.findall(preamble_text))
    if len(doc_hits) == 1:
        mapping.currency = doc_hits.pop()
        if mapping.currency_source != "column":
            mapping.currency_source = "document"
        notes.append(f"The top of the file mentions {mapping.currency}")
        return
    cell_hits = set()
    for j in _money_columns(mapping):
        for v in _column_values(table, j):
            for sym, code in (("₹", "INR"), ("Rs", "INR"), ("$", "USD"), ("€", "EUR"), ("£", "GBP")):
                if sym in v:
                    cell_hits.add(code)
    if len(cell_hits) == 1 and mapping.currency_source != "column":
        mapping.currency = cell_hits.pop()
        mapping.currency_source = "amount_cells"
        notes.append(f"Amounts are written with {mapping.currency} symbols")


def _settle_decimal_separator(mapping: ColumnMapping, table: TableCandidate) -> None:
    comma = dot = 0
    for j in _money_columns(mapping):
        for v in _column_values(table, j):
            if _COMMA_DECIMAL_RE.search(v) and "." not in v.rsplit(",", 1)[-1]:
                comma += 1
            elif _DOT_DECIMAL_RE.search(v):
                dot += 1
    if comma and not dot:
        mapping.decimal_separator = ","
        mapping.evidence.setdefault("_file", []).append("Amounts use a comma for decimals (e.g. 1.234,56)")
    elif comma and dot:
        mapping.ambiguities.append("Some amounts use '.' for decimals and others use ',' — I can't tell which is right.")


def _settle_date_order(mapping: ColumnMapping, table: TableCandidate) -> None:
    if mapping.date_order_source == "user" or "date" not in mapping.roles:
        return
    resolver = DocumentDateResolver()
    for j in [mapping.roles["date"]] + ([mapping.roles["value_date"]] if "value_date" in mapping.roles else []):
        for v in _column_values(table, j):
            resolver.observe(v)
    resolver.resolve_convention()
    if resolver._convention:
        mapping.date_order = resolver._convention
        mapping.date_order_source = "evidence"
        word = "day/month/year" if resolver._convention == "DMY" else "month/day/year"
        mapping.evidence.setdefault("date", []).append(f"dates are {word} (some dates only make sense that way)")


def apply_user_mapping(table: TableCandidate, base: ColumnMapping, data: dict) -> ColumnMapping:
    """Validate a mapping the user confirmed in the preview. Raises MappingError with a
    plain-language message when it can't work, rather than silently fixing it up."""
    raw_roles = data.get("roles")
    if not isinstance(raw_roles, dict):
        raise MappingError("roles must be an object of role -> column number")
    roles: dict[str, int] = {}
    for role, col in raw_roles.items():
        if col is None or col == "":
            continue
        if role not in ROLES:
            raise MappingError(f"unknown role '{role}'")
        try:
            j = int(col)
        except (TypeError, ValueError):
            raise MappingError(f"column for '{role}' must be a number") from None
        if not 0 <= j < len(table.headers):
            raise MappingError(f"column {j} for '{role}' doesn't exist in this file")
        if j in roles.values():
            raise MappingError(f"column '{table.headers[j]}' is used for two different things")
        roles[role] = j

    if "date" not in roles:
        raise MappingError("Please choose which column holds the date.")
    model = decide_amount_model(roles, table)
    if model is None:
        raise MappingError("Please choose an Amount column, or both a Money out and a Money in column.")

    mapping = ColumnMapping(
        roles=roles, headers=list(table.headers), amount_model=model,
        confidence={r: 1.0 for r in roles}, evidence={r: ["confirmed by you"] for r in roles},
        source="user", fingerprint=header_fingerprint(table), sheet=table.sheet, header_row=table.header_row,
        excel_serial_dates=_shape(_column_values(table, roles["date"]))["serial"] > 0.5,
    )
    date_order = data.get("date_order", base.date_order if base.date_order_source == "user" else None)
    if date_order:
        if date_order not in ("DMY", "MDY"):
            raise MappingError("date_order must be DMY or MDY")
        mapping.date_order, mapping.date_order_source = date_order, "user"
    currency = data.get("currency", base.currency if base.currency_source == "user" else None)
    if currency:
        if str(currency).upper() not in KNOWN_CURRENCIES:
            raise MappingError(f"currency must be one of {sorted(KNOWN_CURRENCIES)}")
        mapping.currency, mapping.currency_source = str(currency).upper(), "user"
    decimal = data.get("decimal_separator")
    _settle_currency(mapping, table)
    _settle_decimal_separator(mapping, table)
    if decimal in (".", ","):
        mapping.decimal_separator = decimal
        mapping.ambiguities = [a for a in mapping.ambiguities if "decimals" not in a]
    _settle_date_order(mapping, table)
    return mapping
