"""Independent verification of the agent's proposed final answer.

This never calls the LLM. It only inspects: (1) the trace of tool calls the
agent actually made during this turn, and (2) the structured final answer it
proposed. Three checks matter:

  - Provenance: every transaction ID the answer cites must be a real ID that
    appeared somewhere in the ledger (not one the model invented).
  - Grounding: every numeric amount the answer claims (via verified_amounts)
    must appear literally in some tool result from THIS turn's trace — i.e.
    it must have come from a deterministic aggregate_spending/compare_periods/
    etc. call, not from the model doing arithmetic in its head. If a claimed
    number can't be found anywhere in the trace, verification fails outright.
  - Prose grounding: any specific decimal figure stated in answer_text/caveats
    (not just the structured verified_amounts field) must also appear
    somewhere in the trace. Found necessary live: a model asked to justify a
    categorization stated a precise-sounding statistical threshold in prose
    that was never checked against anything, because verified_amounts was
    empty — it happened to be correct, but nothing verified that, and a
    wrong number in the same shape would have passed identically. See
    DECISIONS.md for the incident this closes.

The LLM's own proposed status (VERIFIED / VERIFIED_WITH_CAVEATS) can be
downgraded by this check but never upgraded — if it says VERIFIED but claims
an amount that isn't grounded, the real status is INSUFFICIENT_INFORMATION,
never something better than what it earned.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# Observed live (not in any offline test): the model occasionally leaks
# malformed pseudo-XML tool-call artifacts as literal text inside answer_text
# itself, e.g. "...</answer_text>\n<parameter name=\"proposed_status\">VERIFIED"
# — valid JSON, garbage content. A user must never see this. Caught here so
# the loop treats it as a failed turn and retries, rather than displaying it.
_MALFORMED_ARTIFACT_RE = re.compile(r"</\w+>|<parameter\b", re.IGNORECASE)


@dataclass
class ToolCallRecord:
    tool_name: str
    tool_input: dict
    tool_result: object  # dataclass or list of dataclasses returned by statement_agent.agent.tools
    reasoning: str = ""  # the model's own text explaining why it made this call, for the audit log —
    # never fed into grounding/citation checks (those only ever walk tool_result), since this is the
    # model's narration about itself, not data a tool returned


@dataclass
class ClaimedAmount:
    currency: str
    amount: str
    label: str = ""


@dataclass
class FinalAnswer:
    answer_text: str
    proposed_status: str  # "VERIFIED" | "VERIFIED_WITH_CAVEATS" | "INSUFFICIENT_INFORMATION"
    verified_amounts: list[ClaimedAmount] = field(default_factory=list)
    cited_transaction_ids: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    status: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def _normalize_decimal(s: str) -> str | None:
    try:
        return str(Decimal(s).normalize())
    except (InvalidOperation, TypeError, ValueError):
        return None


# [\d,]* absorbs Indian-style comma grouping (₹80,000.00, or the irregular lakh/crore
# grouping like 1,25,000.50) so a real number isn't split mid-digit-run and falsely
# treated as ungrounded — see TestUngroundedProseDecimalFails::test_comma_grouped_number
# in tests/test_verifier.py for the exact failure this guards against.
_DECIMAL_IN_PROSE_RE = re.compile(r"\d[\d,]*\.\d+")


def _extract_decimals(text: str) -> set[str]:
    out: set[str] = set()
    for m in _DECIMAL_IN_PROSE_RE.findall(text):
        norm = _normalize_decimal(m.replace(",", ""))
        if norm is not None:
            out.add(norm)
    return out


def _walk_values(obj, out: set[str]) -> None:
    """Recursively collect every number a tool result could ground a claim in —
    both whole-field numeric values (e.g. amount="80000.00") AND numbers embedded
    inside a longer descriptive string (e.g. a `notes` field reading "...(modified
    z-score 82.1)..."). The embedded-number extraction matters: a whole-string-only
    Decimal parse would treat that entire notes sentence as unparseable and silently
    drop the 82.1 it contains, making a genuinely tool-sourced figure look ungrounded
    the moment the model repeats it — found while adding the prose-decimal check
    below (see DECISIONS.md), not before."""
    if obj is None:
        return
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            _walk_values(getattr(obj, f.name), out)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_values(v, out)
        return
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk_values(v, out)
        return
    if isinstance(obj, (int, float, Decimal)):
        norm = _normalize_decimal(str(obj))
        if norm is not None:
            out.add(norm)
        return
    if isinstance(obj, str):
        norm = _normalize_decimal(obj)
        if norm is not None:
            out.add(norm)
        out.update(_extract_decimals(obj))


def _collect_grounded_numbers(trace: list[ToolCallRecord]) -> set[str]:
    seen: set[str] = set()
    for record in trace:
        _walk_values(record.tool_result, seen)
    return seen


def _collect_ledger_transaction_ids(trace: list[ToolCallRecord]) -> set[str]:
    """Every transaction_id that actually appeared in a tool result this turn —
    the only IDs the model could legitimately have seen and be citing."""
    ids: set[str] = set()

    def walk(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            d = dataclasses.asdict(obj)
            if "transaction_id" in d:
                ids.add(d["transaction_id"])
            for f in dataclasses.fields(obj):
                walk(getattr(obj, f.name))
        elif isinstance(obj, dict):
            if "transaction_id" in obj:
                ids.add(obj["transaction_id"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v)

    for record in trace:
        walk(record.tool_result)
    return ids


# ---------------------------------------------------------------------------
# Evidence: every number a tool returned, with the currency and scope it belongs to
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
_MONTH_IN_PROSE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\.?,?\s*(\d{4})?", re.IGNORECASE)
_CURRENCY_WORDS = {"INR": "INR", "RS": "INR", "RS.": "INR", "₹": "INR", "USD": "USD", "$": "USD",
                   "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}
_MONEY_IN_PROSE_RE = re.compile(
    r"(?:(?P<pre>₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)\s*(?P<a>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<b>\d[\d,]*(?:\.\d+)?)\s*(?P<post>INR|USD|EUR|GBP|rupees|dollars|euros|pounds))",
    re.IGNORECASE)
_NET_WORDS_RE = re.compile(r"\bnet\b|after refunds?|minus (?:the )?refunds?|refund-adjusted", re.IGNORECASE)


@dataclass
class _Fact:
    """One number a tool returned, with what it actually refers to."""

    value: str
    currency: str | None
    tool: str
    scope: dict  # category / account / date_from / date_to / economic_types, as the call asked for them
    basis: str | None = None  # "gross" | "net" | None


def _scope_of(record: ToolCallRecord) -> dict:
    keys = ("category", "account", "date_from", "date_to", "currency", "economic_types", "group_by", "group_field")
    return {k: v for k, v in (record.tool_input or {}).items() if k in keys and v not in (None, "", [])}


def _as_dict(obj) -> dict | None:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj if isinstance(obj, dict) else None


def _facts_from(record: ToolCallRecord) -> list[_Fact]:
    """Currency-tagged numbers, per tool. Anything not recognised here still grounds a claim numerically
    (see _collect_grounded_numbers) — it just can't be checked for currency or scope."""
    scope = _scope_of(record)
    result = _as_dict(record.tool_result)
    facts: list[_Fact] = []

    def add(value, currency, basis=None, extra=None):
        norm = _normalize_decimal(str(value))
        if norm is not None:
            facts.append(_Fact(norm, (currency or "").upper() or None, record.tool_name, {**scope, **(extra or {})}, basis))

    if not isinstance(result, dict):
        return facts

    for ccy, total in (result.get("by_currency") or {}).items():
        for key in ("verified_total", "uncertain_total"):
            if isinstance(total, dict) and total.get(key) is not None:
                add(total[key], ccy, "gross")
    for group, per_currency in (result.get("group_breakdown") or {}).items():
        for ccy, amount in (per_currency or {}).items():
            add(amount, ccy, "gross", {"group": group})
    converted = result.get("converted")
    if isinstance(converted, dict):
        for key in ("verified_total", "uncertain_total"):
            add(converted.get(key), converted.get("currency"), "gross")
    for detail in (result.get("conversion_details") or []):
        if isinstance(detail, dict):
            add(detail.get("original_amount"), detail.get("original_currency"))
            add(detail.get("converted_amount"), (record.tool_input or {}).get("convert_to"))
    for ccy, totals in (result.get("per_currency") or {}).items():  # net_spending
        if isinstance(totals, dict):
            add(totals.get("gross_purchases"), ccy, "gross")
            add(totals.get("net_spending"), ccy, "net")
            add(totals.get("linked_refunds"), ccy, "net")
    for row in (result.get("results") or []):  # search_transactions / get_sources
        if isinstance(row, dict) and row.get("amount") is not None:
            add(row["amount"], row.get("currency"), None,
                {k: v for k, v in (("category", row.get("category")), ("account", row.get("account")),
                                   ("date_from", row.get("date")), ("date_to", row.get("date"))) if v})
    for key in ("first_period", "second_period", "periods", "totals"):  # compare_periods and friends
        section = result.get(key)
        for name, value in (section or {}).items() if isinstance(section, dict) else []:
            if isinstance(value, dict):
                for ccy, total in value.items():
                    if isinstance(total, dict):
                        for field_name in ("verified_total", "uncertain_total"):
                            add(total.get(field_name), ccy, "gross", {"period": name})
                    else:
                        add(total, ccy, "gross", {"period": name})
    return facts


def _months_in(text: str) -> set[tuple[int, int | None]]:
    out = set()
    for name, year in _MONTH_IN_PROSE_RE.findall(text or ""):
        month = next((i for full, i in _MONTHS.items() if full.startswith(name.lower())), None)
        if month:
            out.add((month, int(year) if year else None))
    return out


def _scope_covers_month(scope: dict, month: int, year: int | None) -> bool:
    start, end = scope.get("date_from"), scope.get("date_to")
    if not start and not end:
        return True  # an unfiltered call covers everything
    try:
        first = date.fromisoformat(start) if start else date.min
        last = date.fromisoformat(end) if end else date.max
    except ValueError:
        return True
    years = [year] if year else range(first.year, last.year + 1)
    for y in years:
        try:
            month_start = date(y, month, 1)
        except ValueError:
            continue
        month_end = date(y + (month == 12), (month % 12) + 1, 1)
        if month_start < last + _ONE_DAY and month_end > first:
            return True
    return False


_ONE_DAY = timedelta(days=1)


def _known_categories(facts: list[_Fact]) -> set[str]:
    """Category names worth checking a claim against: the built-in list plus any this turn's calls used."""
    from ..categories import BUILT_IN

    names = set(BUILT_IN)
    for f in facts:
        for key in ("category", "group"):
            if isinstance(f.scope.get(key), str):
                names.add(f.scope[key])
    return names


def _check_claim(claim: ClaimedAmount, norm: str, facts: list[_Fact], categories: set[str], prose: str) -> list[str]:
    """A claim must match a tool number that is in the SAME currency, and whose call covers the period and
    category the answer attributes it to. A number that only ever appeared untagged (no currency anywhere in
    that result) is left alone: it can be grounded, but there's nothing to check it against."""
    out: list[str] = []
    same_value = [f for f in facts if f.value == norm]
    tagged = [f for f in same_value if f.currency]
    if not tagged:
        return out
    claimed_currency = (claim.currency or "").upper()
    supporting = [f for f in tagged if f.currency == claimed_currency]
    if not supporting:
        out.append(
            f"claimed amount {claim.amount} {claim.currency} ({claim.label}) matches a tool result in "
            f"{', '.join(sorted({f.currency for f in tagged}))}, not {claim.currency or 'no currency'} — "
            f"currencies are never interchangeable, so this claim is not supported"
        )
        return out

    text = f"{claim.label} {prose}"
    months = _months_in(text)
    if months and not any(_scope_covers_month(f.scope, m, y) for f in supporting for (m, y) in months):
        asked = ", ".join(sorted(f"{m:02d}/{y or '????'}" for m, y in months))
        ranges = ", ".join(sorted({f"{f.scope.get('date_from', 'start')}..{f.scope.get('date_to', 'end')}" for f in supporting}))
        out.append(
            f"claimed amount {claim.amount} {claim.currency} ({claim.label}) is attributed to {asked}, but the "
            f"tool result behind it covers {ranges} — the number does not belong to the period it's claimed for"
        )

    mentioned = {c for c in categories if re.search(rf"\b{re.escape(c)}\b", text, re.IGNORECASE)}
    scoped = {f.scope.get("category") or f.scope.get("group") for f in supporting} - {None}
    if mentioned and scoped and not (mentioned & {str(c) for c in scoped}):
        out.append(
            f"claimed amount {claim.amount} {claim.currency} ({claim.label}) is attributed to "
            f"{', '.join(sorted(mentioned))}, but the tool result behind it is for {', '.join(sorted(str(c) for c in scoped))}"
        )
    return out


def _check_prose_money(prose: str, grounded: set[str], facts: list[_Fact]) -> list[str]:
    """A money figure written out in prose ("INR 999999", "₹1,200") must be grounded even when it has no
    decimal part — the decimal-only check let confident round numbers through with no backing at all."""
    out: list[str] = []
    for m in _MONEY_IN_PROSE_RE.finditer(prose or ""):
        raw = m.group("a") or m.group("b")
        symbol = (m.group("pre") or m.group("post") or "").upper().rstrip(".")
        currency = _CURRENCY_WORDS.get(symbol, {"RUPEES": "INR", "DOLLARS": "USD", "EUROS": "EUR", "POUNDS": "GBP"}.get(symbol))
        norm = _normalize_decimal(raw.replace(",", ""))
        if norm is None:
            continue
        if norm not in grounded:
            out.append(
                f"answer text states {symbol} {raw} with no matching number in any tool result this turn — "
                f"a money figure must come from a tool, not from the model"
            )
            continue
        tagged = [f for f in facts if f.value == norm and f.currency]
        if currency and tagged and not any(f.currency == currency for f in tagged):
            out.append(
                f"answer text states {symbol} {raw}, but that number came back in "
                f"{', '.join(sorted({f.currency for f in tagged}))} — the currency in the answer is wrong"
            )
    return out


def _check_basis(prose: str, facts: list[_Fact], claims: list[ClaimedAmount]) -> list[str]:
    """Saying a figure is net of refunds requires a net figure behind it (net_spending), not a gross total."""
    if not _NET_WORDS_RE.search(prose or ""):
        return []
    claimed = {_normalize_decimal(c.amount) for c in claims} - {None}
    behind = [f for f in facts if f.value in claimed] if claimed else facts
    if behind and not any(f.basis == "net" for f in behind):
        return ["the answer describes a figure as net of refunds, but every supporting tool result is a gross "
                "total — use net_spending for a net figure, or say the figure is gross"]
    return []


def verify(final_answer: FinalAnswer, trace: list[ToolCallRecord]) -> VerificationResult:
    failures: list[str] = []

    if _MALFORMED_ARTIFACT_RE.search(final_answer.answer_text):
        failures.append(
            "answer_text contains malformed formatting artifacts (stray tool-call-like tags) — "
            "treated as an unreliable/glitched response, not shown to the user as-is"
        )
        return VerificationResult(status="INSUFFICIENT_INFORMATION", passed=False, failures=failures)

    if final_answer.verified_amounts and not trace:
        failures.append("numeric claim(s) made with zero tool calls in this turn — arithmetic must come from a tool, not the model")
        return VerificationResult(status="INSUFFICIENT_INFORMATION", passed=False, failures=failures)

    seen_ids = _collect_ledger_transaction_ids(trace)
    unknown_ids = [tid for tid in final_answer.cited_transaction_ids if tid not in seen_ids]
    if unknown_ids:
        failures.append(f"cited transaction id(s) never appeared in this turn's tool results: {unknown_ids}")

    grounded_numbers = _collect_grounded_numbers(trace)
    facts = [f for record in trace for f in _facts_from(record)]
    prose = " ".join([final_answer.answer_text, *final_answer.caveats])
    categories = _known_categories(facts)

    for claim in final_answer.verified_amounts:
        norm = _normalize_decimal(claim.amount)
        if norm is None:
            failures.append(f"claimed amount {claim.amount!r} is not a valid number")
            continue
        if norm not in grounded_numbers:
            failures.append(
                f"claimed amount {claim.amount} {claim.currency} ({claim.label}) does not match any number "
                f"returned by a tool this turn — not grounded, treated as possible fabrication"
            )
            continue
        failures.extend(_check_claim(claim, norm, facts, categories, prose))

    ungrounded_prose_decimals = sorted(_extract_decimals(prose) - grounded_numbers)
    if ungrounded_prose_decimals:
        failures.append(
            f"answer text states specific decimal figure(s) {ungrounded_prose_decimals} that do not appear "
            f"in any tool result this turn — these were never checked via verified_amounts, so this is a "
            f"free-text claim with no structural backing; treated as a possible fabrication"
        )

    if trace or final_answer.proposed_status != "INSUFFICIENT_INFORMATION":
        # an honest "I can't answer this" that made no tool calls isn't claiming a figure is true
        failures.extend(_check_prose_money(prose, grounded_numbers, facts))
    failures.extend(_check_basis(prose, facts, final_answer.verified_amounts))

    if failures:
        return VerificationResult(status="INSUFFICIENT_INFORMATION", passed=False, failures=failures)

    status = final_answer.proposed_status
    if status == "VERIFIED" and final_answer.caveats:
        status = "VERIFIED_WITH_CAVEATS"  # LLM can't self-certify VERIFIED while listing caveats
    if status not in ("VERIFIED", "VERIFIED_WITH_CAVEATS", "INSUFFICIENT_INFORMATION"):
        status = "VERIFIED_WITH_CAVEATS"  # unknown/malformed status is never trusted to mean fully clean

    return VerificationResult(status=status, passed=True, failures=[])
