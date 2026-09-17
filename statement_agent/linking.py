"""Economic event linking: related transactions joined into one story (NOT_IMPLEMENTED.md §A).

Rebuilt over the whole ledger after every commit, undo and correction, deterministically. Each matcher
states why it linked rows, and every link is one of:

  matched    strong, specific evidence (a shared reference number, or the same merchant AND amount) —
             counted in net figures; you can say "not related"
  suggested  plausible but not proven (e.g. the same amount only) — shown for your yes/no, NOT counted
  confirmed / rejected   your decision, remembered by the link's signature across rebuilds

A refund is never linked on amount alone: "some purchase of the same amount" is exactly the confident-but-
wrong match §A warned about. Matchers here: refunds/reversals, reimbursements, transfers between your own
accounts, card bill payments, and recurring payments/income.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .corrections import merchant_words
from .schema import (
    COUNTED_EVENT_STATUSES, Direction, EconomicEvent, EconomicType, EventKind, EventMember, EventStatus, Transaction,
)

REFUND_WINDOW_DAYS = 120
REVERSAL_WINDOW_DAYS = 15
REIMBURSEMENT_WINDOW_DAYS = 120
TRANSFER_WINDOW_DAYS = 3
CARD_PAYMENT_WINDOW_DAYS = 7

_LINK_NOISE = {"REFUND", "REFUNDED", "RFND", "REVERSAL", "REVERSED", "REV", "RETURN", "RETURNED", "CREDIT",
               "CHARGEBACK", "PAYMENT", "PAID", "TO", "FROM", "BY", "FOR", "THE", "OF", "AND", "IN", "ON", "AT"}
_CADENCES = (  # name, typical days, allowed range
    ("weekly", 7, (5, 9)),
    ("monthly", 30, (26, 35)),
    ("quarterly", 91, (84, 98)),
    ("yearly", 365, (355, 375)),
)


def signature(kind: EventKind, ids: list[str], extra: str = "") -> str:
    raw = f"{kind.value}|{extra}|" + ",".join(sorted(ids))
    return f"{kind.value}:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


def _words(t: Transaction) -> set[str]:
    return {w for w in merchant_words(t.merchant_raw or t.description_raw) if w not in _LINK_NOISE and len(w) > 1}


def _merchant_score(a: Transaction, b: Transaction) -> float:
    if a.merchant_canonical and a.merchant_canonical == b.merchant_canonical:
        return 2.0
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    if wa == wb:
        return 2.0
    overlap = wa & wb
    if len(overlap) / len(wa | wb) >= 0.5:
        return 1.5
    if any(len(w) >= 4 for w in overlap):
        return 0.75
    return 0.0


def _usable(t: Transaction) -> bool:
    return t.transaction_date is not None and t.duplicate_of is None and t.date_plausible


def _account_key(t: Transaction, doc_accounts: dict[str, str]) -> str:
    return t.account_name or doc_accounts.get(t.document_id) or t.document_id


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


@dataclass
class _Draft:
    kind: EventKind
    status: EventStatus
    confidence: float
    reason: str
    members: list[EventMember]
    details: dict
    sig_extra: str = ""


# ---------------------------------------------------------------------------
# matchers
# ---------------------------------------------------------------------------

def _match_refunds(txns: list[Transaction]) -> list[_Draft]:
    credits = sorted((t for t in txns if t.direction == Direction.CREDIT
                      and t.economic_type in (EconomicType.REFUND, EconomicType.REVERSAL)), key=lambda t: t.transaction_date)
    purchases = [t for t in txns if t.direction == Direction.DEBIT and t.economic_type == EconomicType.PURCHASE]
    refunded: dict[str, Decimal] = {}
    drafts = []
    for c in credits:
        window = REVERSAL_WINDOW_DAYS if c.economic_type == EconomicType.REVERSAL else REFUND_WINDOW_DAYS
        scored = []
        for p in purchases:
            if p.currency != c.currency:
                continue
            gap = (c.transaction_date - p.transaction_date).days
            if gap < 0 or gap > window:
                continue
            if p.amount - refunded.get(p.transaction_id, Decimal("0")) < c.amount:
                continue
            ref = bool(c.reference_id and c.reference_id == p.reference_id)
            merchant = _merchant_score(c, p)
            if not ref and merchant == 0:
                continue  # never on amount alone
            score = (3 if ref else 0) + merchant + (1 if p.amount == c.amount else 0) + (0.5 if p.document_id == c.document_id else 0)
            scored.append((score, -gap, p, ref, merchant))
        if not scored:
            continue
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        score, _, p, ref, merchant = scored[0]
        unique = len(scored) == 1 or scored[1][0] <= score - 0.5
        strong = ref or (merchant >= 1.5 and p.amount == c.amount)
        why = []
        if ref:
            why.append(f"the same reference number ({c.reference_id})")
        if merchant >= 1.5:
            why.append("the same merchant")
        elif merchant > 0:
            why.append("a similar merchant name")
        why.append("the same amount" if p.amount == c.amount else f"an amount no larger than the purchase ({_money(p.amount)})")
        reason = f"{'Reversal' if c.economic_type == EconomicType.REVERSAL else 'Refund'} of {_money(c.amount)} on {c.transaction_date} " \
                 f"matches the purchase on {p.transaction_date}: {', '.join(why)}."
        if not unique:
            reason += f" {len(scored) - 1} other purchase(s) could also match, so please check."
        status = EventStatus.MATCHED if strong and unique else EventStatus.SUGGESTED
        if status == EventStatus.MATCHED:
            refunded[p.transaction_id] = refunded.get(p.transaction_id, Decimal("0")) + c.amount
        drafts.append(_Draft(
            EventKind.REFUND, status, 0.95 if status == EventStatus.MATCHED else 0.6, reason,
            [EventMember(p.transaction_id, "purchase"), EventMember(c.transaction_id, "refund")],
            {"refund_amount": str(c.amount), "purchase_amount": str(p.amount), "currency": c.currency,
             "partial": c.amount < p.amount},
        ))
    return drafts


def _match_reimbursements(txns: list[Transaction], taken: set[str]) -> list[_Draft]:
    """Reimbursements rarely name the merchant, so these are only ever suggestions: one earlier purchase of
    exactly the same amount, or failing that exactly two that add up to it."""
    drafts = []
    for c in (t for t in txns if t.economic_type == EconomicType.REIMBURSEMENT and t.transaction_id not in taken):
        pool = [p for p in txns if p.direction == Direction.DEBIT and p.economic_type == EconomicType.PURCHASE
                and p.currency == c.currency and 0 <= (c.transaction_date - p.transaction_date).days <= REIMBURSEMENT_WINDOW_DAYS
                and p.transaction_id not in taken]
        singles = [p for p in pool if p.amount == c.amount]
        chosen: list[Transaction] = []
        if len(singles) == 1:
            chosen = singles
        elif not singles and len(pool) <= 60:
            pairs = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1:] if a.amount + b.amount == c.amount]
            if len(pairs) == 1:
                chosen = list(pairs[0])
        if not chosen:
            continue
        taken.update(p.transaction_id for p in chosen)
        dates = ", ".join(str(p.transaction_date) for p in chosen)
        drafts.append(_Draft(
            EventKind.REIMBURSEMENT, EventStatus.SUGGESTED, 0.5,
            f"Reimbursement of {_money(c.amount)} on {c.transaction_date} equals "
            f"{'the purchase' if len(chosen) == 1 else 'two purchases'} on {dates}. Only the amounts match, so please confirm.",
            [*(EventMember(p.transaction_id, "expense") for p in chosen), EventMember(c.transaction_id, "reimbursement")],
            {"amount": str(c.amount), "currency": c.currency},
        ))
    return drafts


def _pair_across_accounts(outs, ins, doc_accounts, window, *, taken):
    """Unique one-to-one pairs of (out, in): same amount and currency, different accounts, dates close.
    A pair is kept only if neither side has another equally good partner."""
    options: dict[str, list] = {}
    for o in outs:
        for i in ins:
            if o.amount != i.amount or o.currency != i.currency:
                continue
            if _account_key(o, doc_accounts) == _account_key(i, doc_accounts):
                continue
            gap = (i.transaction_date - o.transaction_date).days
            if -1 <= gap <= window:
                options.setdefault(o.transaction_id, []).append((abs(gap), i))
    by_in: dict[str, int] = {}
    for opts in options.values():
        for _, i in opts:
            by_in[i.transaction_id] = by_in.get(i.transaction_id, 0) + 1
    pairs = []
    outs_by_id = {o.transaction_id: o for o in outs}
    for oid, opts in options.items():
        if oid in taken or len(opts) != 1:
            continue
        _, i = opts[0]
        if by_in[i.transaction_id] != 1 or i.transaction_id in taken:
            continue
        taken.update((oid, i.transaction_id))
        pairs.append((outs_by_id[oid], i))
    return pairs


def _match_card_payments(txns, doc_accounts, taken) -> list[_Draft]:
    outs = [t for t in txns if t.direction == Direction.DEBIT and t.economic_type == EconomicType.CREDIT_CARD_PAYMENT]
    ins = [t for t in txns if t.direction == Direction.CREDIT and t.economic_type == EconomicType.CREDIT_CARD_PAYMENT]
    return [
        _Draft(EventKind.CARD_PAYMENT, EventStatus.MATCHED, 0.9,
               f"Card bill payment of {_money(o.amount)} on {o.transaction_date} arrived on the card on {i.transaction_date}.",
               [EventMember(o.transaction_id, "payment"), EventMember(i.transaction_id, "in")],
               {"amount": str(o.amount), "currency": o.currency})
        for o, i in _pair_across_accounts(outs, ins, doc_accounts, CARD_PAYMENT_WINDOW_DAYS, taken=taken)
    ]


def _match_transfers(txns, doc_accounts, taken) -> list[_Draft]:
    outs = [t for t in txns if t.direction == Direction.DEBIT
            and t.economic_type in (EconomicType.TRANSFER, EconomicType.INVESTMENT_TRANSFER, EconomicType.PURCHASE)]
    # money in that reading wasn't sure about: a transfer-rail credit, or a generic bank credit read as income
    ins = [t for t in txns if t.direction == Direction.CREDIT and (
        t.economic_type == EconomicType.TRANSFER
        or (t.economic_type == EconomicType.INCOME and t.economic_type_source in ("auto", "link") and t.economic_type_confidence < 1.0)
    )]
    drafts = []
    for o, i in _pair_across_accounts(outs, ins, doc_accounts, TRANSFER_WINDOW_DAYS, taken=taken):
        strong = o.economic_type == EconomicType.TRANSFER
        drafts.append(_Draft(
            EventKind.TRANSFER, EventStatus.MATCHED if strong else EventStatus.SUGGESTED, 0.85 if strong else 0.55,
            f"{_money(o.amount)} left {_account_key(o, doc_accounts)} on {o.transaction_date} and the same amount arrived in "
            f"{_account_key(i, doc_accounts)} on {i.transaction_date}"
            + (" — a move between your own accounts, not spending or income." if strong
               else ". The money-out row looks like a purchase, so please confirm it was a transfer."),
            [EventMember(o.transaction_id, "out"), EventMember(i.transaction_id, "in")],
            {"amount": str(o.amount), "currency": o.currency},
        ))
    return drafts


def _cadence(gaps: list[int]):
    for name, typical, (lo, hi) in _CADENCES:
        # allow one missed occurrence (a double-length gap) anywhere in the series
        fits = [lo <= g <= hi or (2 * lo <= g <= 2 * hi) for g in gaps]
        singles = sum(lo <= g <= hi for g in gaps)
        if all(fits) and singles >= max(1, len(gaps) - 1):
            return name, typical
    return None, None


def _match_recurring(txns: list[Transaction], ledger_end: date) -> list[_Draft]:
    kinds = {
        Direction.DEBIT: {EconomicType.PURCHASE, EconomicType.TRANSFER, EconomicType.INVESTMENT_TRANSFER,
                          EconomicType.FEE, EconomicType.CREDIT_CARD_PAYMENT},
        Direction.CREDIT: {EconomicType.INCOME, EconomicType.INTEREST},
    }
    groups: dict[tuple, list[Transaction]] = {}
    for t in txns:
        if t.economic_type not in kinds[t.direction]:
            continue
        name = t.merchant_canonical or " ".join(merchant_words(t.merchant_raw or t.description_raw)[:2])
        if len(name.replace(" ", "")) < 3 or name.upper() in {"ATM", "CASH", "ATM WDL"}:
            continue
        groups.setdefault((t.direction, name.upper(), t.currency), []).append(t)

    drafts = []
    for (direction, name, currency), rows in groups.items():
        rows.sort(key=lambda t: t.transaction_date)
        if len(rows) < 3:
            continue
        gaps = [(b.transaction_date - a.transaction_date).days for a, b in zip(rows, rows[1:])]
        cadence, typical = _cadence(gaps)
        if cadence is None:
            continue
        amounts = [r.amount for r in rows]
        median = statistics.median(amounts)
        if median == 0 or any(abs(a - median) / median > Decimal("0.25") for a in amounts):
            continue
        last = rows[-1]
        next_expected = last.transaction_date + timedelta(days=typical)
        grace = {"weekly": 3, "monthly": 7, "quarterly": 14, "yearly": 21}[cadence]
        overdue = ledger_end > next_expected + timedelta(days=grace)
        changed = abs(last.amount - median) / median > Decimal("0.05")
        label = rows[-1].merchant_canonical or name.title()
        notes = []
        if changed:
            notes.append(f"the latest amount ({_money(last.amount)}) differs from the usual {_money(median)}")
        if overdue:
            notes.append(f"it was due around {next_expected} but hasn't appeared since — it may have stopped")
        drafts.append(_Draft(
            EventKind.RECURRING, EventStatus.MATCHED, 0.8,
            f"{label}: {len(rows)} {'payments' if direction == Direction.DEBIT else 'credits'} about "
            f"{cadence}, usually {_money(median)} {currency}." + (f" Note: {'; '.join(notes)}." if notes else ""),
            [EventMember(r.transaction_id, "occurrence") for r in rows],
            {"name": label, "direction": direction.value, "cadence": cadence, "typical_amount": str(median),
             "currency": currency, "count": len(rows), "first_date": str(rows[0].transaction_date),
             "last_date": str(last.transaction_date), "last_amount": str(last.amount),
             "next_expected": str(next_expected), "amount_changed": changed, "possibly_stopped": overdue},
            sig_extra=f"{direction.value}|{name}|{currency}|{cadence}",
        ))
    return drafts


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------

def build_events(ledger: list[Transaction], documents: list[dict], decisions: dict[str, str],
                 manual: list[EconomicEvent] = ()) -> list[EconomicEvent]:
    """All links for this ledger, with your decisions applied. `manual` are links you made yourself; they're
    kept while all their rows still exist, and their rows aren't offered to automatic matchers."""
    by_id = {t.transaction_id: t for t in ledger}
    doc_accounts = {d["document_id"]: d.get("account_label") or "" for d in documents}
    doc_accounts = {k: v for k, v in doc_accounts.items() if v}
    txns = [t for t in ledger if _usable(t)]

    events: list[EconomicEvent] = []
    taken: set[str] = set()
    for e in manual:
        if all(m.transaction_id in by_id for m in e.members):
            events.append(e)
            taken.update(m.transaction_id for m in e.members)

    free = [t for t in txns if t.transaction_id not in taken]
    drafts = _match_refunds(free)
    taken.update(m.transaction_id for d in drafts if d.status == EventStatus.MATCHED for m in d.members if m.role == "refund")
    drafts += _match_card_payments(free, doc_accounts, taken)
    drafts += _match_transfers(free, doc_accounts, taken)
    drafts += _match_reimbursements(free, taken)
    ledger_end = max((t.transaction_date for t in txns), default=date.min)
    drafts += _match_recurring(txns, ledger_end)

    for d in drafts:
        sig = signature(d.kind, [] if d.kind == EventKind.RECURRING else [m.transaction_id for m in d.members], d.sig_extra)
        status = d.status
        decision = decisions.get(sig)
        if decision == "confirmed":
            status = EventStatus.CONFIRMED
        elif decision == "rejected":
            status = EventStatus.REJECTED
        events.append(EconomicEvent(
            event_id=sig, kind=d.kind, status=status, confidence=d.confidence, reason=d.reason,
            members=d.members, signature=sig, details=d.details,
        ))
    return events


def apply_link_effects(ledger: list[Transaction], events: list[EconomicEvent]) -> None:
    """The one place a link changes a row: money that arrived from your own account is a TRANSFER, not income.
    Rows whose type came from an earlier link go back to their automatic type first, so an undone link
    leaves nothing behind. Your own and rule-set types are never touched."""
    by_id = {t.transaction_id: t for t in ledger}
    for t in ledger:
        if t.economic_type_source == "link" and t.economic_type_auto:
            t.economic_type, t.economic_type_source = EconomicType(t.economic_type_auto), "auto"
    for e in events:
        if e.kind != EventKind.TRANSFER or e.status not in COUNTED_EVENT_STATUSES:
            continue
        for m in e.members:
            t = by_id.get(m.transaction_id)
            if t is None or t.economic_type_source not in ("auto", None):
                continue
            if m.role == "in" or (m.role == "out" and e.status == EventStatus.CONFIRMED):
                if t.economic_type != EconomicType.TRANSFER:
                    t.economic_type, t.economic_type_source = EconomicType.TRANSFER, "link"
                    if t.category_source != "you":
                        t.category = t.category_confidence = t.category_source = t.category_rule_id = None


def counted_refunds(events: list[EconomicEvent]) -> dict[str, list[tuple[str, Decimal]]]:
    """purchase transaction id -> [(refund transaction id, amount)] for links that count."""
    out: dict[str, list[tuple[str, Decimal]]] = {}
    for e in events:
        if e.kind != EventKind.REFUND or e.status not in COUNTED_EVENT_STATUSES:
            continue
        purchase = next(m.transaction_id for m in e.members if m.role == "purchase")
        refund = next(m.transaction_id for m in e.members if m.role == "refund")
        out.setdefault(purchase, []).append((refund, Decimal(e.details["refund_amount"])))
    return out
