"""Offline tests for the agent loop's plumbing (run_agent) using a stub Anthropic
client — no live API call, no credits needed. This does NOT test whether the model
picks the right tools for a question (that needs a real model, see
eval/run_red_team_bank.py); it tests that run_agent's own mechanics — dispatching
tool calls, threading the model's reasoning text into the trace, returning
AgentRunResult correctly — work regardless of what the model says.
"""

import uuid
from datetime import date
from decimal import Decimal

from statement_agent.agent.loop import run_agent
from statement_agent.schema import Direction, EconomicType, SourceRef, Transaction


class _Block:
    """Minimal stand-in for an anthropic SDK content block — only the attributes
    loop.py actually reads (type, text, id, name, input)."""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content):
        self.content = content


class _StubMessages:
    def __init__(self, responses):
        self._responses = iter(responses)

    def create(self, **kwargs):
        return next(self._responses)


class _StubClient:
    def __init__(self, responses):
        self.messages = _StubMessages(responses)


def _ledger():
    txn_id = str(uuid.uuid4())
    return [
        Transaction(
            transaction_id=txn_id,
            document_id="doc1",
            transaction_date=date(2025, 7, 1),
            date_raw="2025-07-01",
            description_raw="TEST MERCHANT",
            merchant_raw="TEST MERCHANT",
            amount=Decimal("100.00"),
            currency="INR",
            direction=Direction.DEBIT,
            economic_type=EconomicType.PURCHASE,
            category="Shopping",
            source=SourceRef(file_path="test.csv", file_hash="abc", extraction_confidence=1.0),
        )
    ], txn_id


class TestReasoningCapture:
    def test_tool_call_reasoning_is_captured_on_the_trace(self):
        ledger, txn_id = _ledger()
        responses = [
            _Response([
                _Block("text", text="Checking transactions for the test merchant."),
                _Block("tool_use", id="tu1", name="search_transactions", input={}),
            ]),
            _Response([
                _Block("text", text="Citing the transaction I found."),
                _Block("tool_use", id="tu2", name="final_answer", input={
                    "answer_text": "Found one transaction.",
                    "proposed_status": "VERIFIED",
                    "cited_transaction_ids": [txn_id],
                }),
            ]),
        ]
        client = _StubClient(responses)

        result = run_agent("test question", ledger, client=client)

        assert len(result.trace) == 1
        assert result.trace[0].reasoning == "Checking transactions for the test merchant."
        assert result.trace[0].tool_name == "search_transactions"

    def test_final_reasoning_is_captured_separately_from_answer_text(self):
        ledger, txn_id = _ledger()
        responses = [
            _Response([
                _Block("text", text="Looking this up."),
                _Block("tool_use", id="tu1", name="search_transactions", input={}),
            ]),
            _Response([
                _Block("text", text="Citing the transaction I found."),
                _Block("tool_use", id="tu2", name="final_answer", input={
                    "answer_text": "Found one transaction.",
                    "proposed_status": "VERIFIED",
                    "cited_transaction_ids": [txn_id],
                }),
            ]),
        ]
        client = _StubClient(responses)

        result = run_agent("test question", ledger, client=client)

        assert result.final_reasoning == "Citing the transaction I found."
        assert result.final_answer.answer_text == "Found one transaction."
        assert result.final_reasoning != result.final_answer.answer_text

    def test_no_text_block_leaves_reasoning_empty_not_broken(self):
        ledger, txn_id = _ledger()
        responses = [
            _Response([
                _Block("tool_use", id="tu1", name="search_transactions", input={}),
            ]),
            _Response([
                _Block("tool_use", id="tu2", name="final_answer", input={
                    "answer_text": "Found one transaction.",
                    "proposed_status": "VERIFIED",
                    "cited_transaction_ids": [txn_id],
                }),
            ]),
        ]
        client = _StubClient(responses)

        result = run_agent("test question", ledger, client=client)

        assert result.trace[0].reasoning == ""
        assert result.final_reasoning == ""

    def test_reasoning_never_affects_verification_outcome(self):
        # a model that "reasons" about a fabricated amount must still fail
        # verification on the actual grounding check, since reasoning text is
        # never walked by the grounding/citation logic
        ledger, txn_id = _ledger()
        responses = [
            _Response([
                _Block("text", text="I'm confident the total is 999999."),
                _Block("tool_use", id="tu1", name="search_transactions", input={}),
            ]),
            _Response([
                _Block("text", text="Reporting the confident total."),
                _Block("tool_use", id="tu2", name="final_answer", input={
                    "answer_text": "Total is 999999.",
                    "proposed_status": "VERIFIED",
                    "verified_amounts": [{"currency": "INR", "amount": "999999"}],
                }),
            ]),
        ]
        client = _StubClient(responses)

        result = run_agent("test question", ledger, client=client, max_attempts=1)

        assert result.verification.passed is False
        assert result.final_answer.proposed_status == "INSUFFICIENT_INFORMATION"
