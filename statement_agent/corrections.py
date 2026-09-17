"""Correction rules: a person's fixes to categories and merchant names, remembered and re-applied.

A rule says "every transaction whose description contains these words gets category X and/or merchant
name Y". It's matched on *words*, not raw text, because real bank narrations carry a different reference
number on every row ("UPI-SWIGGY-BANGALORE-4829348123"): numbers, card fragments and payment-rail codes
(UPI, NEFT, POS, …) are dropped before matching, so "SWIGGY BANGALORE" matches next month's row too.

Everything here is deterministic. A model never writes a rule and never applies one; the person picks the
words, and the same words always match the same rows. When several rules match, the one with more words
(the more specific) wins, then the most recently changed.
"""

from __future__ import annotations

import re

from .schema import CorrectionRule, Transaction

# Payment-rail and statement noise: carries no information about who was paid.
_NOISE_WORDS = {
    "UPI", "NEFT", "IMPS", "RTGS", "POS", "ECOM", "ACH", "NACH", "ECS", "INB", "IB", "MB", "BIL", "ONL",
    "BILLPAY", "VPS", "VIN", "TXN", "TRAN", "REF", "NO", "P2M", "P2A", "CR", "DR", "VPA", "MMT", "PCD",
}
MAX_CATEGORY_LENGTH = 40
MAX_MERCHANT_LENGTH = 60


class CorrectionError(ValueError):
    """The correction can't be saved as given; the message is plain language for the person."""


def merchant_words(text: str | None) -> list[str]:
    words = re.split(r"[^0-9A-Z&]+", (text or "").upper())
    return [w for w in words if w and not any(c.isdigit() for c in w) and w not in _NOISE_WORDS]


def suggested_pattern(t: Transaction) -> str:
    """What the correction form offers by default: the row's meaningful words, at most four."""
    return " ".join(merchant_words(t.merchant_raw or t.description_raw)[:4])


def clean_pattern(raw: str) -> str:
    words = merchant_words(raw)
    if not words:
        raise CorrectionError("Please give at least one word from the description, like SWIGGY.")
    if sum(len(w) for w in words) < 3:
        raise CorrectionError("Those words are too short to match safely. Please use a longer part of the description.")
    return " ".join(words)


def clean_label(raw: str | None, *, limit: int, what: str) -> str | None:
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if len(text) > limit:
        raise CorrectionError(f"Please keep the {what} under {limit} characters.")
    return text


def rule_matches(rule: CorrectionRule, t: Transaction) -> bool:
    want = rule.pattern.split()
    have = merchant_words(t.merchant_raw or t.description_raw)
    n = len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


def best_rule(t: Transaction, rules: list[CorrectionRule]) -> CorrectionRule | None:
    matching = [r for r in rules if rule_matches(r, t)]
    if not matching:
        return None
    return max(matching, key=lambda r: (len(r.pattern.split()), r.updated_at, r.rule_id))


def assign_merchant_names(transactions: list[Transaction], rules: list[CorrectionRule]) -> None:
    naming = [r for r in rules if r.merchant_name]
    for t in transactions:
        if t.merchant_source == "you":
            continue
        rule = best_rule(t, naming)
        if rule is not None:
            t.merchant_canonical, t.merchant_source, t.merchant_rule_id = rule.merchant_name, "rule", rule.rule_id
        elif t.merchant_source == "rule":
            t.merchant_canonical = t.merchant_source = t.merchant_rule_id = None


def describe_source(t: Transaction, rules_by_id: dict[str, CorrectionRule]) -> dict:
    """Plain-language "why does this row have this category / merchant name" for the UI."""
    def rule_text(rule_id):
        rule = rules_by_id.get(rule_id)
        return f"your rule for descriptions containing “{rule.pattern}”" if rule else "a rule you've since removed"

    category = {
        "keywords": "worked out from the merchant name",
        "file": "the category in your file",
        "rule": rule_text(t.category_rule_id),
        "you": "you set this",
    }.get(t.category_source or "", "no category found")
    merchant = {"rule": rule_text(t.merchant_rule_id), "you": "you set this"}.get(t.merchant_source or "")
    return {"category": category, "merchant": merchant}
