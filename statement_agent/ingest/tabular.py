"""Deterministic normalization and validation of a mapped table.

Once mapping.py has settled *where* to read each field, this turns every source row into exactly
one outcome — a transaction, an explicitly ignored row (blank, repeated header, opening/closing
balance line, a wrapped narration line, or a row the user left out), or a validation issue — so
the per-file count of silently lost rows is zero by construction. Original cell values are kept on
every candidate and on every transaction's SourceRef.

All four amount layouts are handled explicitly, and a running balance column (when present) is
checked row by row: a break in continuity is surfaced as an issue rather than trusted.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..normalize import DocumentDateResolver, normalize_amount
from .confidence import set_field
from .statement_totals import StatedFigures, read_figures
from ..schema import (
    AmountModel, Direction, Document, EconomicType, ExtractionMethod, IssueSeverity, RawTransactionCandidate,
    SourceRef, Transaction, ValidationIssue,
)
from .mapping import ColumnMapping
from .sniff import TableCandidate
from .vocab import (
    CLOSING_BALANCE_RE, OPENING_BALANCE_RE, SUMMARY_ROW_RE, currency_code, looks_like_amount, marker_direction,
)

_ZERO_LIKE = {"", "-", "0", "0.0", "0.00", "0,00", "nil"}
_EXPLICIT_SIGN_RE = re.compile(r"^\s*[-(]|(?:CR|DR|-)\s*$", re.IGNORECASE)
_PREAMBLE_FIGURE_RE = re.compile(r"([-(]?\s*(?:₹|Rs\.?|INR)?\s*\d[\d,]*(?:[.,]\d{1,2})?\)?(?:\s*(?:CR|DR))?)", re.IGNORECASE)
_TEXT_ONLY_ROLES = {"description", "notes", "reference"}
_TOTAL_ROW_RE = re.compile(r"^\s*(?:grand\s+)?totals?\s*:?\s*$", re.IGNORECASE)


@dataclass
class ReviewDecisions:
    """What the user decided in the review step. Keys are candidate keys ("row:12") or issue ids."""

    excluded: set[str] = field(default_factory=set)
    acknowledged: set[str] = field(default_factory=set)
    directions: dict[str, str] = field(default_factory=dict)  # key -> "DEBIT" | "CREDIT"

    def to_dict(self) -> dict:
        return {"excluded": sorted(self.excluded), "acknowledged": sorted(self.acknowledged), "directions": dict(self.directions)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ReviewDecisions":
        data = data or {}
        return cls(set(data.get("excluded", [])), set(data.get("acknowledged", [])), dict(data.get("directions", {})))


@dataclass
class TabularResult:
    document: Document
    transactions: list[Transaction]
    candidates: list[RawTransactionCandidate]
    issues: list[ValidationIssue]


def parse_money(raw: str, *, decimal_separator: str, default_currency: str):
    if not looks_like_amount(raw):
        return None
    text = raw
    if decimal_separator == ",":
        text = raw.replace(".", "").replace(",", ".")
    return normalize_amount(text, default_currency=default_currency)


def _issue(rule, severity, message, action, *, target=None, field_name=None, evidence="") -> ValidationIssue:
    return ValidationIssue(
        issue_id=f"{rule}:{target or 'file'}", rule=rule, severity=severity, message=message,
        suggested_action=action, target=target, field=field_name, evidence=evidence,
    )


def normalize_table(
    table: TableCandidate,
    mapping: ColumnMapping,
    *,
    path: str,
    fhash: str,
    extraction_method: ExtractionMethod = ExtractionMethod.CSV_ROW,
    default_currency: str = "INR",
    decisions: ReviewDecisions | None = None,
) -> TabularResult:
    decisions = decisions or ReviewDecisions()
    roles = mapping.roles
    model = mapping.amount_model
    width = len(table.headers)
    doc_type = "bank_statement" if ("balance" in roles or model == AmountModel.DEBIT_CREDIT.value) else "expense_sheet"
    document = Document(
        document_id=str(uuid.uuid4()), file_path=path, file_hash=fhash, doc_type=doc_type,
        currency_declared=mapping.currency, reconciliation_status="NO_TOTALS",
    )

    def get(cells: list[str], role: str) -> str:
        j = roles.get(role)
        return cells[j].strip() if j is not None and j < len(cells) else ""

    resolver = DocumentDateResolver(locale_default=mapping.date_order or "DMY")
    for _, cells in table.rows:
        resolver.observe(get(cells, "date"))
    resolver.resolve_convention()
    resolver_has_evidence = resolver._convention is not None
    if mapping.date_order_source == "user":
        resolver._convention = mapping.date_order

    doc_currency = mapping.currency or default_currency
    header_lower = [h.lower() for h in table.headers]
    candidates: list[RawTransactionCandidate] = []
    issues: list[ValidationIssue] = []
    transactions: list[Transaction] = []
    by_key: dict[str, Transaction] = {}
    signed_amount: dict[str, bool] = {}
    assumed_currency_used = ambiguous_date_used = False
    last_txn_candidate: RawTransactionCandidate | None = None
    stated_opening = stated_closing = None
    stated = StatedFigures()

    for _, cells in table.preamble:
        joined = " ".join(c for c in cells if c)
        read_figures([joined], into=stated)
        for regex in (OPENING_BALANCE_RE, CLOSING_BALANCE_RE):
            if regex.search(joined):
                # the figure may sit in its own cell or share one with the label ("Opening Balance: 6,000.00")
                money = next((c for c in reversed(cells) if looks_like_amount(c)), None)
                if money is None:
                    tail = _PREAMBLE_FIGURE_RE.search(joined[regex.search(joined).end():])
                    money = tail.group(1) if tail else None
                parsed = parse_money(money, decimal_separator=mapping.decimal_separator, default_currency=doc_currency) if money else None
                if parsed:
                    if regex is OPENING_BALANCE_RE:
                        stated_opening = parsed.amount
                    else:
                        stated_closing = parsed.amount

    for row_no, raw_cells in table.rows:
        cells = list(raw_cells) + [""] * (width - len(raw_cells))
        key = f"row:{row_no}"
        cell_map = {table.headers[j]: cells[j] for j in range(min(width, len(cells))) if cells[j]}
        cand = RawTransactionCandidate(key=key, source_row=row_no, cells=cell_map, outcome="ignored")
        candidates.append(cand)

        if not any(cells):
            cand.reason = "blank row"
            continue
        filled = [c.lower() for c in cells if c]
        if filled and all(c in header_lower for c in filled) and len(filled) >= 2:
            cand.reason = "repeated header row"
            continue
        if key in decisions.excluded:
            cand.reason = "left out by you"
            continue

        raw_date = get(cells, "date")
        money_raw = {r: get(cells, r) for r in ("amount", "debit", "credit")}
        has_money = any(v.lower() not in _ZERO_LIKE for v in money_raw.values())
        def any_cell(regex):  # per cell: a summary label usually sits after the date, not at the row's start
            return any(regex.search(c) for c in cells if c)

        if any_cell(SUMMARY_ROW_RE) and (not raw_date or not has_money or SUMMARY_ROW_RE.search(raw_date)):
            balance_raw = get(cells, "balance") or next((v for v in money_raw.values() if v), "")
            parsed_bal = parse_money(balance_raw, decimal_separator=mapping.decimal_separator, default_currency=doc_currency) if balance_raw else None
            if parsed_bal and any_cell(OPENING_BALANCE_RE) and stated_opening is None:
                stated_opening = parsed_bal.amount
            if parsed_bal and any_cell(CLOSING_BALANCE_RE):
                stated_closing = parsed_bal.amount
            read_figures([" ".join(c for c in cells if c)], into=stated)
            if any_cell(_TOTAL_ROW_RE) and model == AmountModel.DEBIT_CREDIT.value:
                # a bare "Total" row under money-out / money-in columns states both totals
                for role, figure in (("debit", "total_debits"), ("credit", "total_credits")):
                    if money_raw[role].lower() not in _ZERO_LIKE:
                        stated.add(figure, money_raw[role])
            cand.reason = "summary line (opening/closing balance or total), not a transaction"
            continue

        if not raw_date and not has_money:
            text_roles_filled = {r for r in roles if get(cells, r)} <= _TEXT_ONLY_ROLES
            desc = get(cells, "description")
            if last_txn_candidate is not None and desc and text_roles_filled:
                prev = by_key[last_txn_candidate.key]
                prev.description_raw = f"{prev.description_raw} {desc}".strip()
                prev.merchant_raw = prev.description_raw
                prev.source.raw_text += f" | {cell_map}"
                cand.reason = f"continuation of row {last_txn_candidate.source_row}'s description"
            else:
                cand.reason = "no date or amount — not a transaction row"
            continue

        row_issues: list[ValidationIssue] = []
        evidence = ", ".join(f"{k}: {v}" for k, v in cell_map.items())

        def fail(rule, message, action, reason, field_name=None):
            row_issues.append(_issue(rule, IssueSeverity.BLOCKING, message, action, target=key, field_name=field_name, evidence=evidence))
            cand.reason = cand.reason or reason

        parsed_date = None
        date_reason = "date_unambiguous"
        if not raw_date:
            fail("missing_date", f"Row {row_no} has an amount but no date.", "Leave this row out, or fix the file and add it again.", "missing date", "date")
        elif mapping.excel_serial_dates and re.fullmatch(r"\d{5}(?:\.0+)?", raw_date):
            parsed_date = date(1899, 12, 30) + timedelta(days=int(float(raw_date)))
            date_reason = "date_excel_serial"
        else:
            pd = resolver.parse(raw_date)
            if pd.value is None:
                fail("unparseable_date", f"I couldn't read the date '{raw_date}' on row {row_no}.", "Leave this row out, or check which column holds the date.", f"unparseable date: {raw_date!r}", "date")
            else:
                parsed_date = pd
                if pd.confidence < 1.0:
                    if mapping.date_order_source == "user":
                        date_reason = "date_order_confirmed"
                    elif resolver_has_evidence:
                        date_reason = "date_order_from_document"
                    else:
                        date_reason, ambiguous_date_used = "date_order_default", True

        parsed_amount = None
        direction = None
        direction_reason = "direction_sign"
        if model == AmountModel.DEBIT_CREDIT.value:
            d_raw, c_raw = money_raw["debit"], money_raw["credit"]
            d_set, c_set = d_raw.lower() not in _ZERO_LIKE, c_raw.lower() not in _ZERO_LIKE
            if d_set and c_set:
                fail("both_debit_and_credit", f"Row {row_no} has both money out ({d_raw}) and money in ({c_raw}).", "Leave this row out, or check the column choices.", "both money-out and money-in filled", "amount")
            elif not d_set and not c_set:
                fail("no_amount", f"Row {row_no} has no amount in either the money-out or money-in column.", "Leave this row out.", "missing amount in both money-out and money-in columns", "amount")
            else:
                raw_amount = d_raw if d_set else c_raw
                parsed_amount = parse_money(raw_amount, decimal_separator=mapping.decimal_separator, default_currency=doc_currency)
                direction = Direction.DEBIT if d_set else Direction.CREDIT
                direction_reason = "direction_column"
                if parsed_amount is None:
                    fail("unparseable_amount", f"I couldn't read the amount '{raw_amount}' on row {row_no}.", "Leave this row out.", f"unparseable amount: {raw_amount!r}", "amount")
        else:
            raw_amount = money_raw["amount"]
            if not raw_amount:
                fail("missing_amount", f"Row {row_no} has a date but no amount.", "Leave this row out.", "missing amount", "amount")
            else:
                parsed_amount = parse_money(raw_amount, decimal_separator=mapping.decimal_separator, default_currency=doc_currency)
                if parsed_amount is None:
                    fail("unparseable_amount", f"I couldn't read the amount '{raw_amount}' on row {row_no}.", "Leave this row out.", f"unparseable amount: {raw_amount!r}", "amount")
                else:
                    direction = parsed_amount.direction
                    signed_amount[key] = bool(_EXPLICIT_SIGN_RE.search(raw_amount))
                    if model == AmountModel.AMOUNT_WITH_MARKER.value:
                        marked = marker_direction(get(cells, "marker"))
                        if marked:
                            if signed_amount[key] and marked != direction.value:
                                row_issues.append(_issue(
                                    "sign_marker_disagree", IssueSeverity.CHECK,
                                    f"On row {row_no} the amount's sign says money {'in' if direction == Direction.CREDIT else 'out'}, but the type column says '{get(cells, 'marker')}'. I went with the type column.",
                                    "Confirm this is right, or leave the row out.", target=key, field_name="direction", evidence=evidence,
                                ))
                            direction, direction_reason = Direction(marked), "direction_marker"
                        elif not signed_amount[key]:
                            direction_reason = "direction_unmarked"
                    elif model == AmountModel.AMOUNT_WITH_BALANCE.value:
                        direction, direction_reason = None, None  # settled only by the balance pass below

        row_currency = None
        currency_inferred = False
        currency_reason = "currency_row"
        if parsed_amount is not None:
            ccy_cell = get(cells, "currency")
            if ccy_cell:
                row_currency = currency_code(ccy_cell)
                if row_currency is None:
                    fail("unknown_currency", f"Row {row_no} has a currency I don't support: '{ccy_cell}'.", "Leave this row out.", f"unsupported currency: {ccy_cell!r}", "currency")
            if row_currency is None and not parsed_amount.currency_inferred:
                row_currency, currency_reason = parsed_amount.currency, "currency_symbol"
            if row_currency is None:
                row_currency = doc_currency
                currency_inferred = mapping.currency is None
                currency_reason = ("currency_assumed" if currency_inferred
                                   else "currency_confirmed" if mapping.currency_source == "user" else "currency_file")

        if row_issues and any(i.severity == IssueSeverity.BLOCKING for i in row_issues):
            cand.outcome = "issue"
            issues.extend(row_issues)
            continue
        issues.extend(row_issues)

        if key in decisions.directions:
            direction, direction_reason = Direction(decisions.directions[key]), "direction_confirmed"

        balance_after = None
        bal_raw = get(cells, "balance")
        if bal_raw:
            parsed_bal = parse_money(bal_raw, decimal_separator=mapping.decimal_separator, default_currency=doc_currency)
            balance_after = parsed_bal.amount * (-1 if parsed_bal.direction == Direction.CREDIT and bal_raw.strip().startswith(("-", "(")) else 1) if parsed_bal else None

        value_date = None
        if get(cells, "value_date"):
            vd = resolver.parse(get(cells, "value_date"))
            value_date = vd.value

        description = get(cells, "description")
        txn = Transaction(
            transaction_id=str(uuid.uuid4()),
            document_id=document.document_id,
            transaction_date=parsed_date if isinstance(parsed_date, date) else parsed_date.value,
            date_raw=raw_date,
            description_raw=description or "(blank description)",
            merchant_raw=description or None,
            amount=parsed_amount.amount,
            currency=row_currency,
            amount_raw=money_raw["amount"] or money_raw["debit"] or money_raw["credit"],
            direction=direction or Direction.DEBIT,
            economic_type=EconomicType.PURCHASE if (direction or Direction.DEBIT) == Direction.DEBIT else EconomicType.REFUND,
            notes=get(cells, "notes"),
            category_declared=get(cells, "category") or None,
            account_name=get(cells, "account") or None,
            value_date=value_date,
            reference_id=get(cells, "reference") or None,
            balance_after=balance_after,
            source=SourceRef(
                file_path=path, file_hash=fhash, row=row_no, raw_text=str(cell_map),
                extraction_method=extraction_method, extraction_confidence=1.0,
            ),
        )
        set_field(txn, "date", date_reason)
        set_field(txn, "amount", "amount_read")
        if direction_reason:
            set_field(txn, "direction", direction_reason)
        set_field(txn, "currency", currency_reason)
        if not isinstance(parsed_date, date) and parsed_date.confidence < 1.0:
            txn.notes = (txn.notes + f" | date assumption: {parsed_date.assumption}").strip(" |")
        if currency_inferred:
            assumed_currency_used = True
            txn.notes = (txn.notes + " | currency inferred (not stated on row)").strip(" |")
        cand.outcome, cand.transaction_id, cand.reason = "transaction", txn.transaction_id, ""
        transactions.append(txn)
        by_key[key] = txn
        last_txn_candidate = cand

    transactions, balance_issues = _balance_pass(
        transactions, candidates, model, decisions, stated_opening, document
    )
    issues.extend(balance_issues)
    if stated_closing is not None:
        document.closing_balance = stated_closing
    if stated_opening is not None:
        document.opening_balance = stated_opening
    for figure, attr in (("total_debits", "stated_total_debits"), ("total_credits", "stated_total_credits")):
        if figure in stated.values:
            parsed_total = parse_money(stated.values[figure], decimal_separator=mapping.decimal_separator, default_currency=doc_currency)
            setattr(document, attr, parsed_total.amount if parsed_total else None)
    document.parse_warnings.extend(w for w in stated.warnings() if "balance" not in w)

    if assumed_currency_used:
        issues.append(_issue(
            "currency_assumed", IssueSeverity.CHECK,
            f"This file doesn't say which currency it's in. I read the amounts as {doc_currency}.",
            "Confirm the currency, or pick the right one.", field_name="currency",
        ))
    if ambiguous_date_used and mapping.date_order_source != "user":
        order = "day/month/year" if (mapping.date_order or "DMY") == "DMY" else "month/day/year"
        issues.append(_issue(
            "date_order_assumed", IssueSeverity.CHECK,
            f"Some dates, like 05/07/2025, could be read two ways. I read them as {order}.",
            "Confirm the date format, or switch it.", field_name="date",
        ))
    if table.duplicate_headers:
        issues.append(_issue(
            "duplicate_headers", IssueSeverity.INFO,
            f"This file has more than one column named {', '.join(repr(h) for h in table.duplicate_headers)}; I numbered them so you can tell them apart.",
            "Check the column choices if anything looks off.",
        ))
    if not transactions:
        issues.append(_issue(
            "no_transactions", IssueSeverity.BLOCKING,
            "I didn't find any transactions I could read in this file.",
            "Check the column choices, or try a different file.",
        ))

    for issue in issues:
        if issue.issue_id in decisions.acknowledged and issue.severity != IssueSeverity.BLOCKING:
            issue.resolution = "acknowledged"

    return TabularResult(document=document, transactions=transactions, candidates=candidates, issues=issues)


def _balance_pass(transactions, candidates, model, decisions, stated_opening, document):
    """Validates balance continuity (or, for AMOUNT_WITH_BALANCE, derives direction from it).

    Statements run oldest-first or newest-first; rather than guess from dates (often equal on
    busy days), both orders are tried and the one where more balance steps check out is used."""
    issues: list[ValidationIssue] = []
    with_balance = [t for t in transactions if t.balance_after is not None]
    if len(with_balance) < 2 and model != AmountModel.AMOUNT_WITH_BALANCE.value:
        return transactions, issues

    def step_ok(prev_bal, t, direction):
        signed = t.amount if direction == Direction.CREDIT else -t.amount
        return prev_bal + signed == t.balance_after

    def fits(order):
        hits = 0
        for a, b in zip(order, order[1:]):
            if a.balance_after is None or b.balance_after is None:
                continue
            if model == AmountModel.AMOUNT_WITH_BALANCE.value:
                hits += step_ok(a.balance_after, b, Direction.DEBIT) or step_ok(a.balance_after, b, Direction.CREDIT)
            else:
                hits += step_ok(a.balance_after, b, b.direction)
        return hits

    order = list(transactions)
    if fits(list(reversed(order))) > fits(order):
        order.reverse()

    key_of = {c.transaction_id: c for c in candidates if c.transaction_id}
    dropped: set[str] = set()
    prev_bal = stated_opening
    breaks = 0
    for i, t in enumerate(order):
        cand = key_of[t.transaction_id]
        if model == AmountModel.AMOUNT_WITH_BALANCE.value:
            if cand.key in decisions.directions:
                pass
            elif prev_bal is not None and t.balance_after is not None and step_ok(prev_bal, t, Direction.DEBIT):
                t.direction = Direction.DEBIT
                set_field(t, "direction", "direction_from_balance")
            elif prev_bal is not None and t.balance_after is not None and step_ok(prev_bal, t, Direction.CREDIT):
                t.direction = Direction.CREDIT
                set_field(t, "direction", "direction_from_balance")
            else:
                why = "it's the first row and the file has no opening balance" if prev_bal is None else "the balance change doesn't match the amount"
                issues.append(_issue(
                    "direction_unknown", IssueSeverity.BLOCKING,
                    f"I can't tell whether row {cand.source_row} ({t.amount} — {t.description_raw}) is money in or money out: {why}.",
                    "Tell me which it is, or leave the row out.", target=cand.key, field_name="direction", evidence=t.source.raw_text,
                ))
                cand.outcome, cand.reason, cand.transaction_id = "issue", "direction could not be derived from the balance", None
                dropped.add(t.transaction_id)
                prev_bal = t.balance_after if t.balance_after is not None else prev_bal
                continue
            t.economic_type = EconomicType.PURCHASE if t.direction == Direction.DEBIT else EconomicType.REFUND
        elif prev_bal is not None and t.balance_after is not None and not step_ok(prev_bal, t, t.direction):
            breaks += 1
            signed = t.amount if t.direction == Direction.CREDIT else -t.amount
            issues.append(_issue(
                "balance_break", IssueSeverity.CHECK,
                f"The balance on row {cand.source_row} doesn't follow from the row before it "
                f"(expected {prev_bal + signed}, the file says {t.balance_after}). A row may be missing or misread.",
                "Check this row against your statement; confirm to keep it.", target=cand.key, field_name="balance", evidence=t.source.raw_text,
            ))
        if t.balance_after is not None:
            prev_bal = t.balance_after

    kept = [t for t in transactions if t.transaction_id not in dropped]
    ordered_kept = [t for t in order if t.transaction_id not in dropped and t.balance_after is not None]
    if ordered_kept:
        first = ordered_kept[0]
        signed_first = first.amount if first.direction == Direction.CREDIT else -first.amount
        document.opening_balance = stated_opening if stated_opening is not None else first.balance_after - signed_first
        document.opening_balance_derived = stated_opening is None
        document.closing_balance = ordered_kept[-1].balance_after
    return kept, issues
