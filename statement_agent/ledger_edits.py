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
from .resolve import _CATEGORY_KEYWORDS, assign_categories
from .schema import CorrectionRule, EconomicType, Transaction
from .store import Store

UNSET = object()  # "leave this field alone", as opposed to None ("go back to the automatic value")


def recompute(transactions: list[Transaction], rules: list[CorrectionRule]) -> None:
    assign_merchant_names(transactions, rules)
    assign_categories(transactions, rules)


def category_choices(store: Store) -> list[str]:
    built_in = [name for name, _ in _CATEGORY_KEYWORDS] + ["Other"]
    used = {t.category for t in store.all_transactions() if t.category}
    used |= {r.category for r in store.list_rules() if r.category}
    return built_in + sorted(used - set(built_in))


def _snapshot(t: Transaction) -> dict:
    return {"category": t.category, "category_source": t.category_source,
            "merchant_name": t.merchant_canonical, "merchant_source": t.merchant_source}


def _get(store: Store, transaction_id: str) -> Transaction:
    t = store.get_transaction(transaction_id)
    if t is None:
        raise KeyError(transaction_id)
    return t


def _check_category_allowed(t: Transaction, category) -> None:
    if category is not UNSET and category is not None and t.economic_type != EconomicType.PURCHASE:
        raise CorrectionError(
            "Only purchases have a spending category. This row is a "
            f"{t.economic_type.value.replace('_', ' ').lower()}, so it isn't counted as spending."
        )


def correct_one(store: Store, transaction_id: str, *, category=UNSET, merchant_name=UNSET) -> dict:
    """Changes just this row. None puts a field back to the automatic value (rules, then keywords)."""
    t = _get(store, transaction_id)
    _check_category_allowed(t, category)
    before = _snapshot(t)
    if category is not UNSET:
        category = clean_label(category, limit=MAX_CATEGORY_LENGTH, what="category")
        if category is None:
            t.category_source = None if t.category_source == "you" else t.category_source
        else:
            t.category, t.category_confidence, t.category_source, t.category_rule_id = category, 1.0, "you", None
    if merchant_name is not UNSET:
        merchant_name = clean_label(merchant_name, limit=MAX_MERCHANT_LENGTH, what="merchant name")
        if merchant_name is None:
            t.merchant_canonical = t.merchant_source = t.merchant_rule_id = None
        else:
            t.merchant_canonical, t.merchant_source, t.merchant_rule_id = merchant_name, "you", None
    if category is UNSET and merchant_name is UNSET:
        raise CorrectionError("Nothing to change.")
    changed = store.apply_correction(
        t, action="set_one", compute=recompute,
        detail={"before": before, "requested": {k: v for k, v in (("category", category), ("merchant_name", merchant_name)) if v is not UNSET}},
    )
    return {"changed": changed, "transaction": _get(store, transaction_id)}


def add_rule(store: Store, transaction_id: str, *, pattern: str | None = None, category=UNSET, merchant_name=UNSET) -> dict:
    """Makes (or updates) the rule for these words, starting from the row the person was looking at."""
    t = _get(store, transaction_id)
    words = clean_pattern(pattern if pattern is not None else suggested_pattern(t))
    category = UNSET if category is UNSET else clean_label(category, limit=MAX_CATEGORY_LENGTH, what="category")
    merchant_name = UNSET if merchant_name is UNSET else clean_label(merchant_name, limit=MAX_MERCHANT_LENGTH, what="merchant name")
    if category in (UNSET, None) and merchant_name in (UNSET, None):
        raise CorrectionError("A rule needs a category or a merchant name to set.")
    _check_category_allowed(t, category)

    rule = store.find_rule(words) or CorrectionRule(rule_id=str(uuid.uuid4()), pattern=words, source_transaction_id=t.transaction_id)
    action = "update_rule" if rule.created_at else "add_rule"
    before_rule = {"category": rule.category, "merchant_name": rule.merchant_name}
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
    changed = store.apply_correction(
        t, rule=rule, action=action, compute=recompute,
        detail={"pattern": words, "rule_before": before_rule,
                "rule_after": {"category": rule.category, "merchant_name": rule.merchant_name}, "row_before": before},
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
