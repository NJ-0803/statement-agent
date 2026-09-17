"""Field-level confidence, reason codes, and the gate that keeps low-confidence fields out of the ledger
until a person has looked at them.

Every parser records, for each required field (date, amount, direction, currency), a confidence and a
reason code saying HOW the value was arrived at. A number alone ("0.6") can't be explained to anyone; a
reason code can ("this date could be read two ways, so I used the file's other dates to decide").

The gate is the roadmap's release rule "no low-confidence required field is automatically committed":
any required field under CONFIDENCE_THRESHOLD must be covered by an open-or-acknowledged review issue.
Some reasons are file-wide by nature (an assumed currency applies to every row that lacks one), so one
file-level issue covers them all instead of one item per row; the rest get one issue per row.
"""

from __future__ import annotations

from ..schema import ExtractionMethod, IssueSeverity, Transaction, ValidationIssue

REQUIRED_FIELDS = ("date", "amount", "direction", "currency")
CONFIDENCE_THRESHOLD = 0.85

# code -> (confidence, plain-language explanation shown to the user)
REASONS: dict[str, tuple[float, str]] = {
    # date
    "date_unambiguous": (1.0, "the date could only be read one way"),
    "date_excel_serial": (1.0, "the date was stored as an Excel date number"),
    "date_order_from_document": (0.9, "the date could be read two ways; I followed the other dates in this file"),
    "date_order_default": (0.6, "the date could be read two ways and nothing in the file settled it, so I used the usual format"),
    "date_order_confirmed": (1.0, "you confirmed the date format"),
    # amount
    "amount_read": (1.0, "the amount was read directly from the file"),
    # direction
    "direction_column": (1.0, "the amount was in the money-out or money-in column"),
    "direction_sign": (1.0, "the amount's sign or Cr/Dr ending says which way the money moved"),
    "direction_marker": (1.0, "the Dr/Cr column says which way the money moved"),
    "direction_unmarked": (0.5, "the Dr/Cr column was empty and the amount has no sign, so I assumed money out"),
    "direction_sign_assumed": (0.6, "a minus sign could mean money in or out here, and nothing in the file settled which"),
    "direction_from_balance": (0.9, "the running balance changed by exactly this amount"),
    "direction_confirmed": (1.0, "you said which way the money moved"),
    # currency
    "currency_row": (1.0, "the row states its currency"),
    "currency_symbol": (1.0, "the amount carries a currency symbol or code"),
    "currency_file": (1.0, "the file states its currency"),
    "currency_assumed": (0.5, "the file doesn't say which currency it's in, so I assumed one"),
    "currency_confirmed": (1.0, "you confirmed the currency"),
    # whole row
    "read_from_image": (0.75, "this row was read from a picture of the page, which can misread characters"),
}

# Reason codes that one file-level issue covers for every row carrying them.
FILE_LEVEL_RULE = {
    "date_order_default": "date_order_assumed",
    "currency_assumed": "currency_assumed",
    "direction_sign_assumed": "sign_convention_assumed",
    "read_from_image": "read_from_image",
}

_FIELD_WORDS = {"date": "date", "amount": "amount", "direction": "money in or out", "currency": "currency"}


def set_field(t: Transaction, field_name: str, reason: str, confidence: float | None = None) -> None:
    """Records how a field was arrived at. Confidence defaults to the reason's own."""
    t.field_reasons[field_name] = reason
    t.field_confidence[field_name] = REASONS[reason][0] if confidence is None else confidence


def explain(reason: str | None) -> str:
    return REASONS[reason][1] if reason in REASONS else "no reason recorded"


def low_confidence_fields(t: Transaction) -> list[tuple[str, float, str | None]]:
    """(field, confidence, reason) for every required field under the threshold. A vision-read row with no
    per-field record still counts as low confidence through its extraction confidence."""
    out = []
    for f in REQUIRED_FIELDS:
        conf = t.field_confidence.get(f)
        reason = t.field_reasons.get(f)
        if conf is None and t.source is not None and t.source.extraction_method == ExtractionMethod.VISION_OCR:
            conf, reason = t.source.extraction_confidence, "read_from_image"
        if conf is not None and conf < CONFIDENCE_THRESHOLD:
            out.append((f, conf, reason))
    return out


def gate_issues(transactions: list[Transaction], issues: list[ValidationIssue], *, row_key) -> list[ValidationIssue]:
    """Returns the review issues needed so that no low-confidence required field goes uncovered.

    `row_key(t)` gives the review target for a transaction ("row:12" / "txn:<id>"). Existing issues count
    as coverage: a file-level issue with the matching rule, or any issue about the same row and field.
    """
    have_rules = {i.rule for i in issues if i.target is None}
    covered_rows = {(i.target, i.field) for i in issues if i.target is not None}
    new: list[ValidationIssue] = []
    image_rows = 0
    for t in transactions:
        for f, conf, reason in low_confidence_fields(t):
            file_rule = FILE_LEVEL_RULE.get(reason)
            if file_rule == "read_from_image":
                image_rows += 1
                break  # one count per row, whichever fields are affected
            if file_rule and file_rule in have_rules:
                continue
            key = row_key(t)
            if (key, f) in covered_rows:
                continue
            covered_rows.add((key, f))
            where = f"row {t.source.row}" if t.source and t.source.row is not None else f"'{t.description_raw}'"
            new.append(ValidationIssue(
                issue_id=f"low_confidence:{key}:{f}", rule="low_confidence", severity=IssueSeverity.CHECK,
                message=f"I'm not sure about the {_FIELD_WORDS[f]} on {where} ({t.amount} — {t.description_raw}): {explain(reason)}.",
                suggested_action="Confirm it's right, or leave the row out." if f != "direction"
                else "Tell me which way the money moved, or leave the row out.",
                target=key, field=f, evidence=t.source.raw_text[:300] if t.source else "",
            ))
    if image_rows and "read_from_image" not in have_rules:
        new.append(ValidationIssue(
            issue_id="read_from_image:file", rule="read_from_image", severity=IssueSeverity.CHECK,
            message=f"I read {image_rows} transaction{'s' if image_rows != 1 else ''} from a picture of the page. "
                    "Pictures can be misread, so please compare them with your statement.",
            suggested_action="Check the list above against your statement; leave out any row that's wrong, then confirm.",
        ))
    return new
