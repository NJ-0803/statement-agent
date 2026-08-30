"""The plan -> act -> check agent loop.

Claude is given the tools in `tools.py` plus a terminal `final_answer` tool.
It decides which tools to call and in what order; every call is executed
against the real ledger and the result fed back. When it calls
`final_answer`, the proposed answer is run through `verifier.verify` against
the accumulated trace of everything it actually looked up this conversation.
If verification fails, the failure is fed back and the model gets another
attempt (bounded by max_attempts); if it still can't produce a grounded
answer, the loop returns INSUFFICIENT_INFORMATION honestly rather than
surfacing an unverified number.

This module makes real Anthropic API calls and cannot be unit-tested without
network access / a funded API key — the parts that matter for correctness
(the tools and the verifier) are unit-tested independently in
tests/test_tools.py and tests/test_verifier.py without touching the network.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import date

from . import tools as T
from .prompts import SYSTEM_PROMPT
from .verifier import ClaimedAmount, FinalAnswer, ToolCallRecord, VerificationResult, verify
from ..schema import Transaction

AGENT_MODEL = "claude-sonnet-5"
MAX_ATTEMPTS = 3
MAX_TOOL_ITERATIONS = 12

_CATEGORY_ENUM = ["Dining", "Groceries", "Transport", "Travel", "Entertainment", "Subscriptions",
                   "Utilities", "Shopping", "Healthcare", "Personal Care"]
_ECONOMIC_TYPE_ENUM = ["PURCHASE", "REFUND", "TRANSFER", "CREDIT_CARD_PAYMENT", "CASH_WITHDRAWAL",
                        "REIMBURSEMENT", "FEE", "INTEREST", "REVERSAL", "UNKNOWN"]

TOOL_SCHEMAS = [
    {
        "name": "list_documents",
        "description": (
            "Lists every source document/statement in the ledger — file path, bank/account label, "
            "declared currency, statement period, transaction count, and a `warnings` field carrying "
            "any security/data-quality flags already computed at ingest time. Call this whenever a "
            "question refers to a specific bank, card, or statement by name (e.g. 'the Cobalt "
            "statement', 'my Axis account') — a bank/institution name is usually NOT a merchant string "
            "inside transactions, so search_transactions alone cannot find it. Use the returned "
            "file_path with summarize_statement or as a filter. Always check `warnings` for 'anything "
            "unusual' questions."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_transactions",
        "description": (
            "Search individual transactions with filters. Returns raw rows with source citations — "
            "does NOT compute any total. Use sort_by + limit=1 to find a single extreme transaction "
            "(e.g. 'my biggest expense' as a single-transaction question) — never sort or pick the "
            "largest/smallest yourself from an unsorted list. "
            "Result has `results` (the rows, capped at 200 by default), `total_matched` (how many rows "
            "actually matched the filters), and `truncated` (true if `results` is a partial subset of "
            "`total_matched`). If `truncated` is true, the rows you got are NOT the complete match set — "
            "say so explicitly rather than presenting them as if they were everything, and narrow the "
            "filters (tighter date range, a category, etc.) if the user needs the full set. "
            "sort_by='extraction_order' returns rows in the ORIGINAL source document's own row order "
            "(each row's extraction_sequence), not sorted by any transaction field — use this, never "
            "date_asc, to answer a question about the document's own row/date ordering (e.g. 'is this "
            "statement sorted by date?'). Sorting by date and then checking if the result is sorted by "
            "date is circular and proves nothing about the source document. "
            "sort_by='closest_to_amount' (with target_amount set, typically from a prior `compute` call) "
            "finds the transaction(s) nearest a target value — e.g. 'which transaction is closest to my "
            "average spend' after computing that average."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": _CATEGORY_ENUM},
                "economic_types": {"type": "array", "items": {"type": "string", "enum": _ECONOMIC_TYPE_ENUM}},
                "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "merchant_contains": {"type": "string"},
                "currency": {"type": "string"},
                "include_flagged": {"type": "boolean", "description": "include duplicate-flagged/implausible-date rows (default true)"},
                "sort_by": {"type": "string", "enum": ["amount_desc", "amount_asc", "date_desc", "date_asc", "extraction_order", "closest_to_amount"]},
                "target_amount": {"type": "string", "description": "required for sort_by='closest_to_amount' — the value to sort by proximity to, as a decimal string"},
                "limit": {"type": "integer", "description": "max rows to return, applied after sorting"},
            },
        },
    },
    {
        "name": "compute",
        "description": (
            "Deterministic arithmetic over numbers you ALREADY have from another tool call this turn — "
            "average, sum, difference, min, or max. NEVER a substitute for aggregate_spending (which "
            "remains the only way to compute a spend total) — this is for a simple derived value from "
            "numbers you've already retrieved, e.g. the average of a highest and lowest transaction "
            "amount you got from search_transactions. Always use this instead of doing the arithmetic "
            "yourself — the result is a real tool output, so it's grounded the same way every other "
            "number in your answer must be. 'difference' takes exactly 2 values and computes "
            "values[0] - values[1] (order matters)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["average", "sum", "difference", "min", "max"]},
                "values": {"type": "array", "items": {"type": "string"}, "description": "decimal strings, copied exactly from a prior tool result"},
            },
            "required": ["operation", "values"],
        },
    },
    {
        "name": "aggregate_spending",
        "description": (
            "Compute a spend total, the only correct way to answer 'how much did I spend on X'. "
            "Returns per-currency verified_total (clean) and uncertain_total (flagged duplicates / "
            "implausible dates) — always separate, never blended. Optionally breaks down by group_by. "
            "When `category` is set, also returns possibly_missing_uncategorized_count: other purchases "
            "in the same date/currency scope that couldn't be confidently categorized at all, so they "
            "were never checked against this category and might belong to it. If this is nonzero, the "
            "total is a floor, not a guaranteed-complete figure — say so. "
            "Set `convert_to` to also get one combined total in a single target currency — every "
            "transaction is converted using the real exchange rate quoted for ITS OWN date (never "
            "today's rate, never one blended rate), from a bundled historical ECB rate file, with the "
            "rate and its date returned per transaction in `conversion_details` for citation. The "
            "converted total is ADDITIONAL to by_currency, never a replacement for it — always report "
            "both. Any transaction that couldn't be converted (rate unavailable) is excluded from the "
            "converted total and listed in failed_conversion_ids — disclose this, never silently drop it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": _CATEGORY_ENUM},
                "economic_types": {"type": "array", "items": {"type": "string", "enum": _ECONOMIC_TYPE_ENUM}, "description": "default: [PURCHASE]"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "currency": {"type": "string"},
                "group_by": {"type": "string", "enum": ["month", "category", "merchant"]},
                "convert_to": {"type": "string", "description": "3-letter currency code, e.g. INR — converts and sums everything into this one currency, alongside the normal per-currency breakdown"},
            },
        },
    },
    {
        "name": "compare_periods",
        "description": "Compare spend between two date ranges. The only correct way to answer month-over-month or period comparison questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": _CATEGORY_ENUM},
                "currency": {"type": "string"},
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
            },
            "required": ["period_a_start", "period_a_end", "period_b_start", "period_b_end"],
        },
    },
    {
        "name": "generate_chart",
        "description": (
            "Renders a chart (bar/line/pie) from the SAME grouped totals aggregate_spending computes — "
            "use this when the user asks to 'show', 'chart', 'visualize', 'plot', or 'graph' spending, "
            "instead of only describing numbers in prose. Returns `chart_path` (a PNG file on disk — "
            "mention it exists so the user can view it) and `data` (the exact values plotted, so you can "
            "also describe them in words). Never blends currencies into one chart — if the matched "
            "transactions span more than one currency, pass `currency` to scope it, or you'll get an "
            "error explaining why. 'month' groups chronologically; 'category'/'merchant' alphabetically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
                "group_by": {"type": "string", "enum": ["month", "category", "merchant"]},
                "category": {"type": "string", "enum": _CATEGORY_ENUM},
                "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "currency": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["chart_type", "group_by"],
        },
    },
    {
        "name": "find_disputable_transactions",
        "description": "Returns transactions flagged as possible duplicates, statistical outliers, or implausible dates — for 'anything I should dispute/review' questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "summarize_statement",
        "description": "Summarize one specific source document by its file path (totals by currency, by category, and flagged transactions).",
        "input_schema": {
            "type": "object",
            "properties": {"source_file": {"type": "string"}},
            "required": ["source_file"],
        },
    },
    {
        "name": "get_sources",
        "description": (
            "Look up full source/provenance detail for a specific list of transaction IDs already seen "
            "in a prior tool result. Same shape as search_transactions: `results`, `total_matched`, "
            "`truncated` — if you pass more than 200 IDs, only the first 200 come back and `truncated` "
            "is true, so say so rather than treating the returned rows as the complete set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"transaction_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["transaction_ids"],
        },
    },
    {
        "name": "dataset_coverage",
        "description": (
            "Returns the actual date range and currencies present in the ledger. Call this before "
            "answering any question about a specific time period, to check the ledger actually has data "
            "for it. Also returns `coverage_gaps` — contiguous calendar-month ranges with ZERO "
            "transactions strictly between min_date and max_date (e.g. statements never uploaded for a "
            "quarter). If a question's date range overlaps a gap, disclose it explicitly — a total "
            "computed across a silent gap is a floor, not a complete figure, the same way an "
            "uncategorized-transaction count is."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resolve_period",
        "description": (
            "Deterministically resolves a relative or named time period into exact start/end ISO dates. "
            "ALWAYS use this instead of computing date ranges yourself — it correctly handles the "
            "year-boundary case (e.g. 'last quarter' asked while currently in Q1 resolves to Q4 of the "
            "PREVIOUS year, not an invalid or wrong-year quarter). Accepts: 'this_month', 'last_month', "
            "'this_quarter', 'last_quarter', 'this_year', 'last_year', 'last_7_days', 'last_30_days', "
            "'last_90_days', or an explicit 'YYYY-MM', 'YYYY-QN', or 'YYYY'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "as_of": {"type": "string", "description": "ISO date to resolve relative periods against; defaults to today if omitted"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "resolve_date",
        "description": (
            "Deterministically resolves ONE raw date string, the same way ambiguous document dates are "
            "resolved at ingestion. ALWAYS call this instead of parsing a numeric date yourself whenever "
            "a question contains an explicit numeric date like '05/07/2026' — both parts are <=12, so "
            "it's genuinely ambiguous (5 July vs 7 May) and must never be guessed silently. Returns "
            "`date` (ISO), `confidence` (1.0 = unambiguous format like an ISO date or a textual month "
            "name; <1.0 = ambiguous, resolved via a locale-default DD/MM guess), and `assumption` "
            "(non-empty when a guess was used) — if `assumption` is non-empty, disclose it in your answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"raw": {"type": "string", "description": "the date string as written, e.g. '05/07/2026' or '5 July 2025'"}},
            "required": ["raw"],
        },
    },
    {
        "name": "final_answer",
        "description": "Call this to give your final answer. This is the ONLY way to complete a turn — do not just write prose.",
        "input_schema": {
            "type": "object",
            "properties": {
                "answer_text": {"type": "string", "description": "The natural-language answer for the user."},
                "proposed_status": {"type": "string", "enum": ["VERIFIED", "VERIFIED_WITH_CAVEATS", "INSUFFICIENT_INFORMATION"]},
                "verified_amounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "currency": {"type": "string"},
                            "amount": {"type": "string", "description": "copied exactly from a tool result, as a string"},
                            "label": {"type": "string"},
                        },
                        "required": ["currency", "amount"],
                    },
                },
                "cited_transaction_ids": {"type": "array", "items": {"type": "string"}},
                "caveats": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer_text", "proposed_status"],
        },
    },
]


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _dispatch(tool_name: str, tool_input: dict, ledger: list[Transaction], documents: list[dict]):
    if tool_name == "list_documents":
        return T.list_documents(ledger, documents)
    if tool_name == "search_transactions":
        return T.search_transactions(
            ledger,
            category=tool_input.get("category"),
            economic_types=tuple(tool_input["economic_types"]) if tool_input.get("economic_types") else None,
            date_from=_parse_date(tool_input.get("date_from")),
            date_to=_parse_date(tool_input.get("date_to")),
            merchant_contains=tool_input.get("merchant_contains"),
            currency=tool_input.get("currency"),
            include_flagged=tool_input.get("include_flagged", True),
            sort_by=tool_input.get("sort_by"),
            target_amount=tool_input.get("target_amount"),
            limit=tool_input.get("limit"),
        )
    if tool_name == "compute":
        return T.compute(tool_input["operation"], tool_input["values"])
    if tool_name == "aggregate_spending":
        return T.aggregate_spending(
            ledger,
            category=tool_input.get("category"),
            economic_types=tuple(tool_input["economic_types"]) if tool_input.get("economic_types") else ("PURCHASE",),
            date_from=_parse_date(tool_input.get("date_from")),
            date_to=_parse_date(tool_input.get("date_to")),
            currency=tool_input.get("currency"),
            group_by=tool_input.get("group_by"),
            convert_to=tool_input.get("convert_to"),
        )
    if tool_name == "compare_periods":
        return T.compare_periods(
            ledger,
            category=tool_input.get("category"),
            currency=tool_input.get("currency"),
            period_a=(_parse_date(tool_input["period_a_start"]), _parse_date(tool_input["period_a_end"])),
            period_b=(_parse_date(tool_input["period_b_start"]), _parse_date(tool_input["period_b_end"])),
        )
    if tool_name == "generate_chart":
        return T.generate_chart(
            ledger,
            chart_type=tool_input["chart_type"],
            group_by=tool_input["group_by"],
            category=tool_input.get("category"),
            date_from=_parse_date(tool_input.get("date_from")),
            date_to=_parse_date(tool_input.get("date_to")),
            currency=tool_input.get("currency"),
            title=tool_input.get("title"),
        )
    if tool_name == "find_disputable_transactions":
        return T.find_disputable_transactions(ledger)
    if tool_name == "summarize_statement":
        return T.summarize_statement(ledger, source_file=tool_input["source_file"])
    if tool_name == "get_sources":
        return T.get_sources(ledger, tool_input["transaction_ids"])
    if tool_name == "dataset_coverage":
        return T.dataset_coverage(ledger)
    if tool_name == "resolve_period":
        return T.resolve_period(tool_input["period"], as_of=_parse_date(tool_input.get("as_of")))
    if tool_name == "resolve_date":
        return T.resolve_date(tool_input["raw"])
    raise ValueError(f"unknown tool: {tool_name}")


def _to_jsonable(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _parse_final_answer(input_dict: dict) -> FinalAnswer:
    return FinalAnswer(
        answer_text=input_dict.get("answer_text", ""),
        proposed_status=input_dict.get("proposed_status", "INSUFFICIENT_INFORMATION"),
        verified_amounts=[
            ClaimedAmount(currency=a["currency"], amount=a["amount"], label=a.get("label", ""))
            for a in input_dict.get("verified_amounts", [])
        ],
        cited_transaction_ids=input_dict.get("cited_transaction_ids", []),
        caveats=input_dict.get("caveats", []),
    )


@dataclass
class AgentRunResult:
    final_answer: FinalAnswer
    verification: VerificationResult
    trace: list[ToolCallRecord] = field(default_factory=list)
    attempts: int = 0
    final_reasoning: str = ""  # the model's own text alongside the final_answer call that won —
    # the "why" behind the answer, for the audit log; separate from answer_text, which is what's
    # actually shown to the user


def run_agent(
    question: str,
    ledger: list[Transaction],
    *,
    documents: list[dict] | None = None,
    client=None,
    max_attempts: int = MAX_ATTEMPTS,
) -> AgentRunResult:
    import anthropic

    documents = documents or []
    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": question}]
    trace: list[ToolCallRecord] = []
    verify_attempts = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=AGENT_MODEL, max_tokens=4096, system=SYSTEM_PROMPT, tools=TOOL_SCHEMAS, messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        # Text the model wrote alongside this turn's tool calls — its own explanation of why it's
        # calling what it's calling, captured for the audit log (ToolCallRecord.reasoning /
        # AgentRunResult.final_reasoning below). Never fed into verification: the grounding/citation
        # checks only ever inspect tool_result, never this narration about the model's own intent.
        reasoning = "\n".join(b.text for b in response.content if b.type == "text").strip()

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Call the final_answer tool to complete your response."})
            continue

        final_call = next((b for b in tool_uses if b.name == "final_answer"), None)
        result_blocks = []
        for tu in tool_uses:
            if tu.name == "final_answer":
                continue
            try:
                result = _dispatch(tu.name, tu.input, ledger, documents)
                trace.append(ToolCallRecord(tu.name, tu.input, result, reasoning=reasoning))
                result_blocks.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(_to_jsonable(result))})
            except Exception as e:  # noqa: BLE001 - a bad tool call must not crash the whole answer
                result_blocks.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"error: {e}", "is_error": True})

        if final_call is None:
            messages.append({"role": "user", "content": result_blocks})
            continue

        final_answer = _parse_final_answer(final_call.input)
        verification = verify(final_answer, trace)
        verify_attempts += 1

        if verification.passed:
            return AgentRunResult(final_answer, verification, trace, verify_attempts, final_reasoning=reasoning)

        if verify_attempts >= max_attempts:
            fallback = FinalAnswer(
                answer_text=(
                    "I couldn't produce a fully verified answer to this question — the numbers I found "
                    "didn't pass my own consistency checks, so I'm not going to guess. "
                    f"Specifically: {'; '.join(verification.failures)}"
                ),
                proposed_status="INSUFFICIENT_INFORMATION",
                caveats=verification.failures,
            )
            return AgentRunResult(fallback, verification, trace, verify_attempts, final_reasoning=reasoning)

        result_blocks.append({
            "type": "tool_result",
            "tool_use_id": final_call.id,
            "content": f"Verification failed: {verification.failures}. Re-check with tools and call final_answer again with only grounded, cited values.",
            "is_error": True,
        })
        messages.append({"role": "user", "content": result_blocks})

    fallback = FinalAnswer(
        answer_text="I wasn't able to reach a verified answer within the allowed number of steps.",
        proposed_status="INSUFFICIENT_INFORMATION",
    )
    return AgentRunResult(
        fallback,
        VerificationResult(status="INSUFFICIENT_INFORMATION", passed=False, failures=["max tool iterations exceeded"]),
        trace,
        verify_attempts,
        final_reasoning=reasoning,
    )
