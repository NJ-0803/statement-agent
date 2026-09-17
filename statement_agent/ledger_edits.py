"""Corrections to committed transactions: change one row, or make a rule that covers every matching row now
and in future imports. Each correction is one database transaction that saves the change, logs it, and
re-applies every rule to the whole ledger with the same resolver code an import uses.
"""

from __future__ import annotations

import uuid

from .corrections import (
    MAX_CATEGORY_LENGTH, MAX_MERCHANT_LENGTH, CorrectionError, assign_merchant_names, clean_label, clean_pattern,
    rule_matches, suggested_pattern,
)
from .categories import BUILT_IN, merchant_key
from .resolve import assign_categories, assign_economic_types
from .schema import (
    CorrectionRule, EconomicEvent, EconomicType, EventKind, EventMember, EventStatus, Transaction,
)
from .store import Store

UNSET = object()  # "leave this field alone", as opposed to None ("go back to the automatic value")


def recompute(transactions: list[Transaction], rules: list[CorrectionRule], knowledge: dict | None = None) -> None:
    assign_merchant_names(transactions, rules)
    assign_economic_types(transactions, rules)
    assign_categories(transactions, rules, knowledge)


def category_choices(store: Store) -> list[str]:
    built_in = list(BUILT_IN)
    used = {t.category for t in store.all_transactions() if t.category}
    used |= {r.category for r in store.list_rules() if r.category}
    return built_in + sorted(used - set(built_in))


# The kinds a person can choose, in plain words (UNKNOWN is never offered).
TYPE_LABELS = {
    "PURCHASE": "Purchase (spending)", "INCOME": "Income (salary, pension, dividends…)", "REFUND": "Refund",
    "TRANSFER": "Transfer between accounts or people", "CREDIT_CARD_PAYMENT": "Card bill payment",
    "CASH_WITHDRAWAL": "Cash withdrawal", "FEE": "Bank or card fee", "INTEREST": "Interest",
    "INVESTMENT_TRANSFER": "Investment", "CASHBACK": "Cashback or rewards", "REIMBURSEMENT": "Reimbursement",
    "REVERSAL": "Reversal of an earlier charge",
}


def _clean_type(value):
    if value is UNSET or value is None:
        return value
    if value not in TYPE_LABELS:
        raise CorrectionError("Please choose one of the listed kinds of transaction.")
    return value


def _snapshot(t: Transaction) -> dict:
    return {"category": t.category, "category_source": t.category_source,
            "merchant_name": t.merchant_canonical, "merchant_source": t.merchant_source,
            "economic_type": t.economic_type.value, "economic_type_source": t.economic_type_source}


def _get(store: Store, transaction_id: str) -> Transaction:
    t = store.get_transaction(transaction_id)
    if t is None:
        raise KeyError(transaction_id)
    return t


def _check_category_allowed(t: Transaction, category, economic_type=UNSET) -> None:
    kind = t.economic_type.value if economic_type in (UNSET, None) else economic_type
    if category is not UNSET and category is not None and kind != EconomicType.PURCHASE.value:
        raise CorrectionError(
            "Only purchases have a spending category. This row is a "
            f"{t.economic_type.value.replace('_', ' ').lower()}, so it isn't counted as spending."
        )


def correct_one(store: Store, transaction_id: str, *, category=UNSET, merchant_name=UNSET, economic_type=UNSET) -> dict:
    """Changes just this row. None puts a field back to the automatic value (rules, then keywords)."""
    t = _get(store, transaction_id)
    economic_type = _clean_type(economic_type)
    _check_category_allowed(t, category, economic_type)
    before = _snapshot(t)
    if economic_type is not UNSET:
        if economic_type is None:
            if t.economic_type_source == "you":
                t.economic_type_source = "auto"
        else:
            t.economic_type_auto = t.economic_type_auto or t.economic_type.value
            t.economic_type, t.economic_type_source, t.economic_type_rule_id = EconomicType(economic_type), "you", None
            t.economic_type_confidence = 1.0
    if category is not UNSET:
        category = clean_label(category, limit=MAX_CATEGORY_LENGTH, what="category")
        key = merchant_key(t.merchant_raw or t.description_raw)
        if category is None:
            if t.category_source == "you":
                t.category_source = None
                store.forget(key, "you")
        else:
            if t.category_source in ("groq", "learned"):
                # correcting a guess: other guessed rows from this merchant follow, and future imports too
                store.learn([(key, category, "you", None)])
            t.category, t.category_confidence, t.category_source, t.category_rule_id = category, 1.0, "you", None
    if merchant_name is not UNSET:
        merchant_name = clean_label(merchant_name, limit=MAX_MERCHANT_LENGTH, what="merchant name")
        if merchant_name is None:
            t.merchant_canonical = t.merchant_source = t.merchant_rule_id = None
        else:
            t.merchant_canonical, t.merchant_source, t.merchant_rule_id = merchant_name, "you", None
    if category is UNSET and merchant_name is UNSET and economic_type is UNSET:
        raise CorrectionError("Nothing to change.")
    changed = store.apply_correction(
        t, action="set_one", compute=recompute,
        detail={"before": before, "requested": {k: v for k, v in (("category", category), ("merchant_name", merchant_name),
                                                           ("economic_type", economic_type)) if v is not UNSET}},
    )
    return {"changed": changed, "transaction": _get(store, transaction_id)}


def add_rule(store: Store, transaction_id: str, *, pattern: str | None = None, category=UNSET, merchant_name=UNSET,
             economic_type=UNSET) -> dict:
    """Makes (or updates) the rule for these words, starting from the row the person was looking at."""
    t = _get(store, transaction_id)
    words = clean_pattern(pattern if pattern is not None else suggested_pattern(t))
    category = UNSET if category is UNSET else clean_label(category, limit=MAX_CATEGORY_LENGTH, what="category")
    merchant_name = UNSET if merchant_name is UNSET else clean_label(merchant_name, limit=MAX_MERCHANT_LENGTH, what="merchant name")
    economic_type = _clean_type(economic_type)
    if category in (UNSET, None) and merchant_name in (UNSET, None) and economic_type in (UNSET, None):
        raise CorrectionError("A rule needs a kind, a category or a merchant name to set.")
    _check_category_allowed(t, category, economic_type)

    rule = store.find_rule(words) or CorrectionRule(rule_id=str(uuid.uuid4()), pattern=words, source_transaction_id=t.transaction_id)
    action = "update_rule" if rule.created_at else "add_rule"
    before_rule = {"category": rule.category, "merchant_name": rule.merchant_name, "economic_type": rule.economic_type}
    if economic_type is not UNSET:
        rule.economic_type = economic_type
    if category is not UNSET:
        rule.category = category
    if merchant_name is not UNSET:
        rule.merchant_name = merchant_name
    if not rule_matches(rule, t):
        raise CorrectionError(f"The words “{words}” aren't in this transaction's description, so the rule wouldn't cover it.")

    before = _snapshot(t)
    # the rule should visibly apply to the row it was made from, so a one-row choice for the same field yields
    if category is not UNSET and t.category_source == "you":
        t.category_source = None
    if merchant_name is not UNSET and t.merchant_source == "you":
        t.merchant_canonical = t.merchant_source = None
    if economic_type is not UNSET and t.economic_type_source == "you":
        t.economic_type_source = "auto"
    changed = store.apply_correction(
        t, rule=rule, action=action, compute=recompute,
        detail={"pattern": words, "rule_before": before_rule,
                "rule_after": {"category": rule.category, "merchant_name": rule.merchant_name,
                               "economic_type": rule.economic_type}, "row_before": before},
    )
    return {"changed": changed, "rule": store.get_rule(rule.rule_id), "transaction": _get(store, transaction_id)}


def remove_rule(store: Store, rule_id: str) -> dict:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise KeyError(rule_id)
    changed = store.apply_correction(
        None, remove_rule_id=rule_id, action="remove_rule", compute=recompute,
        detail={"pattern": rule.pattern, "category": rule.category, "merchant_name": rule.merchant_name},
    )
    return {"changed": changed}


def preview_rule(store: Store, pattern: str) -> dict:
    """What a rule with these words would cover, before it's saved."""
    words = clean_pattern(pattern)
    probe = CorrectionRule(rule_id="preview", pattern=words)
    matches = [t for t in store.all_transactions() if rule_matches(probe, t)]
    return {
        "pattern": words,
        "matches": len(matches),
        "purchases": sum(1 for t in matches if t.economic_type == EconomicType.PURCHASE),
        "set_by_you": sum(1 for t in matches if t.category_source == "you" or t.merchant_source == "you"),
        "examples": sorted({t.description_raw for t in matches})[:5],
    }


def decide_link(store: Store, event_id: str, decision: str | None) -> dict:
    if decision not in ("confirmed", "rejected", None):
        raise CorrectionError("decision must be confirmed, rejected or null")
    event = store.get_event(event_id)
    if event is None:
        raise KeyError(event_id)
    store.decide_link(event, decision)
    return {"event": store.get_event(event_id)}


_MANUAL_ROLES = {
    EventKind.REFUND: ("purchase", "refund"),
    EventKind.REIMBURSEMENT: ("expense", "reimbursement"),
    EventKind.TRANSFER: ("out", "in"),
    EventKind.CARD_PAYMENT: ("payment", "in"),
}


def link_manually(store: Store, kind: str, out_id: str, in_id: str) -> dict:
    """Links two rows yourself: money out first (purchase / expense / transfer out / card payment), then the
    money in that belongs with it."""
    try:
        kind_enum = EventKind(kind)
    except ValueError:
        raise CorrectionError("Please choose refund, reimbursement, transfer or card payment.") from None
    if kind_enum not in _MANUAL_ROLES:
        raise CorrectionError("Please choose refund, reimbursement, transfer or card payment.")
    out_t, in_t = _get(store, out_id), _get(store, in_id)
    if out_t.direction.value != "DEBIT" or in_t.direction.value != "CREDIT":
        raise CorrectionError("Choose the money-out row first and the money-in row second.")
    if out_t.currency != in_t.currency:
        raise CorrectionError("These two rows are in different currencies, so they can't be linked.")
    if any(m.transaction_id in (out_id, in_id) for e in store.list_events()
           if e.kind != EventKind.RECURRING and e.status.value != "rejected" for m in e.members):
        raise CorrectionError("One of these rows is already linked. Say “not related” on that link first.")
    out_role, in_role = _MANUAL_ROLES[kind_enum]
    event = EconomicEvent(
        event_id=f"you:{uuid.uuid4().hex[:16]}", kind=kind_enum, status=EventStatus.CONFIRMED, confidence=1.0,
        reason="You linked these yourself.",
        members=[EventMember(out_id, out_role), EventMember(in_id, in_role)], signature=f"you:{uuid.uuid4().hex}",
        details={"refund_amount": str(in_t.amount), "purchase_amount": str(out_t.amount), "amount": str(in_t.amount),
                 "currency": in_t.currency, "partial": in_t.amount < out_t.amount},
        source="you",
    )
    store.add_manual_link(event)
    return {"event": store.get_event(event.event_id)}


def categorize_with_groq(store: Store) -> dict:
    """Asks Groq about every uncategorized purchase merchant already in the ledger, remembers the answers,
    and re-applies categories. Returns how many merchants were answered and rows changed."""
    from .groq_categorize import groq_enabled, suggest_categories

    if not groq_enabled():
        raise CorrectionError("Groq isn't set up. Add GROQ_API_KEY to the .env file and restart the app.")
    knowledge = store.merchant_knowledge()
    unknown = sorted({
        merchant_key(t.merchant_raw or t.description_raw) for t in store.all_transactions()
        if t.economic_type == EconomicType.PURCHASE and t.category is None and t.category_source != "you"
    } - {""} - set(knowledge))
    if not unknown:
        return {"asked": 0, "answered": 0, "changed": 0, "note": "Every purchase already has a category."}
    answers, note = suggest_categories(unknown, category_choices(store))
    store.learn([(k, c, "groq", model) for k, (c, model) in answers.items()])
    changed = store.apply_correction(None, action="groq_categorize", compute=recompute,
                                     detail={"asked": len(unknown), "answered": len(answers)})
    return {"asked": len(unknown), "answered": len(answers), "changed": changed, "note": note}
