"""Independent verification of the agent's proposed final answer.

This never calls the LLM. It only inspects: (1) the trace of tool calls the
agent actually made during this turn, and (2) the structured final answer it
proposed. Two checks matter:

  - Provenance: every transaction ID the answer cites must be a real ID that
    appeared somewhere in the ledger (not one the model invented).
  - Grounding: every numeric amount the answer claims must appear literally
    in some tool result from THIS turn's trace — i.e. it must have come from
    a deterministic aggregate_spending/compare_periods/etc. call, not from
    the model doing arithmetic in its head. If a claimed number can't be
    found anywhere in the trace, verification fails outright.

The LLM's own proposed status (VERIFIED / VERIFIED_WITH_CAVEATS) can be
downgraded by this check but never upgraded — if it says VERIFIED but claims
an amount that isn't grounded, the real status is INSUFFICIENT_INFORMATION,
never something better than what it earned.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
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


def _walk_values(obj, out: set[str]) -> None:
    """Recursively collect every string/number-looking leaf value from a tool
    result, regardless of whether it's a dataclass, dict, list, or scalar."""
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
    if isinstance(obj, (str, int, float, Decimal)):
        norm = _normalize_decimal(str(obj))
        if norm is not None:
            out.add(norm)


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

    if failures:
        return VerificationResult(status="INSUFFICIENT_INFORMATION", passed=False, failures=failures)

    status = final_answer.proposed_status
    if status == "VERIFIED" and final_answer.caveats:
        status = "VERIFIED_WITH_CAVEATS"  # LLM can't self-certify VERIFIED while listing caveats
    if status not in ("VERIFIED", "VERIFIED_WITH_CAVEATS", "INSUFFICIENT_INFORMATION"):
        status = "VERIFIED_WITH_CAVEATS"  # unknown/malformed status is never trusted to mean fully clean

    return VerificationResult(status=status, passed=True, failures=[])
