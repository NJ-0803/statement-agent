import os
import tempfile

from statement_agent.agent.tools import aggregate_spending, search_transactions
from statement_agent.agent.verifier import ClaimedAmount, FinalAnswer, ToolCallRecord, verify
from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.store import Store
from tests._dataset import dataset_ledger

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")


def _ledger():
    return dataset_ledger()


class TestGroundedAnswerPasses:
    def test_amount_taken_directly_from_a_real_tool_result_passes(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="You spent 13095.00 INR on dining, verified.",
            proposed_status="VERIFIED",
            verified_amounts=[ClaimedAmount(currency="INR", amount=result.by_currency["INR"].verified_total, label="dining spend")],
        )
        v = verify(answer, trace)
        assert v.passed is True
        assert v.status == "VERIFIED"

    def test_caveats_present_forces_verified_with_caveats_even_if_llm_said_verified(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]
        answer = FinalAnswer(
            answer_text="...",
            proposed_status="VERIFIED",  # LLM over-claims certainty
            verified_amounts=[ClaimedAmount(currency="INR", amount=result.by_currency["INR"].verified_total)],
            caveats=["one duplicate-flagged transaction excluded"],
        )
        v = verify(answer, trace)
        assert v.status == "VERIFIED_WITH_CAVEATS"


class TestFabricatedNumberFails:
    def test_amount_not_found_in_any_tool_result_fails_verification(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="You spent 999999.00 INR on dining.",  # not in the trace at all
            proposed_status="VERIFIED",
            verified_amounts=[ClaimedAmount(currency="INR", amount="999999.00", label="dining spend")],
        )
        v = verify(answer, trace)
        assert v.passed is False
        assert v.status == "INSUFFICIENT_INFORMATION"
        assert any("not grounded" in f for f in v.failures)

    def test_numeric_claim_with_zero_tool_calls_fails(self):
        answer = FinalAnswer(
            answer_text="You spent 5000 INR on dining.",
            proposed_status="VERIFIED",
            verified_amounts=[ClaimedAmount(currency="INR", amount="5000")],
        )
        v = verify(answer, trace=[])
        assert v.passed is False
        assert "zero tool calls" in v.failures[0]


class TestFabricatedCitationFails:
    def test_citing_a_transaction_id_never_returned_by_a_tool_fails(self):
        ledger = _ledger()
        result = search_transactions(ledger, category="Dining")
        trace = [ToolCallRecord("search_transactions", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="Based on transaction abc123...",
            proposed_status="VERIFIED",
            cited_transaction_ids=["not-a-real-id-ever-returned"],
        )
        v = verify(answer, trace)
        assert v.passed is False
        assert any("never appeared" in f for f in v.failures)

    def test_citing_a_real_id_that_was_actually_returned_passes(self):
        ledger = _ledger()
        result = search_transactions(ledger, category="Dining")
        real_id = result.results[0].transaction_id
        trace = [ToolCallRecord("search_transactions", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="See transaction.",
            proposed_status="VERIFIED",
            cited_transaction_ids=[real_id],
        )
        v = verify(answer, trace)
        assert v.passed is True


class TestUngroundedProseDecimalFails:
    """The real live gap this closes: a model justifying a categorization stated a
    plausible-sounding statistical threshold in prose (not in verified_amounts) that was
    never checked against anything — it happened to be correct, but nothing verified
    that, and a wrong number in the same shape would have passed identically."""

    def test_decimal_figure_in_answer_text_not_backed_by_any_tool_result_fails(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="This was flagged using a threshold of 3.5, which is standard for this method.",
            proposed_status="VERIFIED",
        )
        v = verify(answer, trace)
        assert v.passed is False
        assert any("3.5" in f for f in v.failures)

    def test_decimal_figure_that_genuinely_appears_in_a_tool_result_passes(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]
        real_total = result.by_currency["INR"].verified_total  # e.g. "13095.00"

        answer = FinalAnswer(
            answer_text=f"You spent {real_total} INR on dining, which is what the tool returned.",
            proposed_status="VERIFIED",
        )
        v = verify(answer, trace)
        assert v.passed is True

    def test_comma_grouped_number_in_prose_is_not_falsely_split(self):
        # a naive \d+\.\d+ regex would extract "645.11" out of "3,645.11", missing the
        # real grounded value entirely and falsely flagging a correctly-cited figure
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]
        real_total = result.by_currency["INR"].verified_total

        # build a comma-grouped prose rendering of the same real number
        from decimal import Decimal

        grouped = f"{Decimal(real_total):,.2f}"
        answer = FinalAnswer(answer_text=f"You spent ₹{grouped} on dining.", proposed_status="VERIFIED")
        v = verify(answer, trace)
        assert v.passed is True

    def test_decimal_in_a_caveat_is_checked_too_not_just_answer_text(self):
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]

        answer = FinalAnswer(
            answer_text="Here is your dining spend.",
            proposed_status="VERIFIED_WITH_CAVEATS",
            caveats=["Confidence for this figure is approximately 91.7%, based on internal scoring."],
        )
        v = verify(answer, trace)
        assert v.passed is False
        assert any("91.7" in f for f in v.failures)

    def test_integer_counts_in_prose_are_not_flagged(self):
        # deliberately narrow to decimals: "3 transactions" is not expected to be
        # individually traceable to one tool-result string the way a precise decimal is
        ledger = _ledger()
        result = aggregate_spending(ledger, category="Dining")
        trace = [ToolCallRecord("aggregate_spending", {"category": "Dining"}, result)]

        answer = FinalAnswer(answer_text="Found 3 transactions in this category.", proposed_status="VERIFIED")
        v = verify(answer, trace)
        assert v.passed is True


class TestNoCrashOnEmptyOrMalformedInput:
    def test_no_amounts_no_citations_still_verifies(self):
        answer = FinalAnswer(answer_text="I don't have enough information.", proposed_status="INSUFFICIENT_INFORMATION")
        v = verify(answer, trace=[])
        assert v.passed is True
        assert v.status == "INSUFFICIENT_INFORMATION"

    def test_garbage_status_string_never_treated_as_fully_verified(self):
        answer = FinalAnswer(answer_text="...", proposed_status="TOTALLY_SURE_TRUST_ME")
        v = verify(answer, trace=[])
        assert v.status != "VERIFIED"


class TestMalformedAnswerTextArtifactsRejected:
    """Found live (not by any offline test) via a real browser session: the model
    occasionally leaks stray tool-call-like XML fragments into answer_text itself,
    e.g. '...spend pattern.</answer_text>\\n<parameter name="proposed_status">VERIFIED'.
    This must never reach a user; the verifier rejects it and forces a retry.
    """

    def test_stray_closing_tag_rejected(self):
        answer = FinalAnswer(
            answer_text='Croma Retail is a statistical outlier.</answer_text>\n<parameter name="proposed_status">VERIFIED_WITH_CAVEATS',
            proposed_status="VERIFIED_WITH_CAVEATS",
        )
        v = verify(answer, trace=[])
        assert v.passed is False
        assert v.status == "INSUFFICIENT_INFORMATION"
        assert any("malformed" in f for f in v.failures)

    def test_clean_answer_text_with_no_tags_is_unaffected(self):
        # no decimal figure here specifically so this only exercises the malformed-artifact
        # check in isolation — see TestUngroundedProseDecimalFails for the decimal-grounding check
        answer = FinalAnswer(answer_text="Your dining spend was reviewed and is available on request.", proposed_status="INSUFFICIENT_INFORMATION")
        v = verify(answer, trace=[])
        assert v.passed is True  # no amounts/citations/decimal figures claimed, nothing to reject

    def test_ordinary_html_style_text_without_tool_tags_not_falsely_flagged(self):
        # a legitimate answer mentioning e.g. a merchant description containing '<' should not
        # be treated as a malformed artifact unless it actually looks like a closing/parameter tag
        answer = FinalAnswer(answer_text="Spend was less than 10000 INR this month.", proposed_status="INSUFFICIENT_INFORMATION")
        v = verify(answer, trace=[])
        assert v.passed is True


class TestClaimsAreBoundToTheirEvidence:
    """From the completion brief: a tool result only supports a claim in the same currency, period and
    category, and a money figure in prose needs backing even when it's a whole number. All four cases below
    passed before this check existed."""

    def _trace(self):
        return [ToolCallRecord(
            "aggregate_spending",
            {"category": "Dining", "date_from": "2025-07-01", "date_to": "2025-07-31"},
            {"by_currency": {"INR": {"verified_total": "500.00", "uncertain_total": "0", "verified_count": 2,
                                     "uncertain_count": 0, "uncertain_reasons": []}}},
        )]

    def _verify(self, text, claims=(), status="VERIFIED"):
        return verify(FinalAnswer(answer_text=text, proposed_status=status, verified_amounts=list(claims)), self._trace())

    def test_the_same_number_in_another_currency_is_not_evidence(self):
        v = self._verify("You spent USD 500.00 on dining in July.", [ClaimedAmount("USD", "500.00", "July dining")])
        assert not v.passed and "not USD" in v.failures[0]

    def test_a_different_month_is_not_evidence(self):
        v = self._verify("You spent INR 500.00 on dining in August.", [ClaimedAmount("INR", "500.00", "August dining")])
        assert not v.passed and "does not belong to the period" in v.failures[0]

    def test_a_different_category_is_not_evidence(self):
        v = self._verify("You spent INR 500.00 on groceries in July.", [ClaimedAmount("INR", "500.00", "July groceries")])
        assert not v.passed and "is for Dining" in v.failures[0]

    def test_a_whole_number_in_prose_needs_backing_too(self):
        v = self._verify("You spent INR 999999 in July.")
        assert not v.passed and "no matching number" in v.failures[0]

    def test_calling_a_gross_total_net_of_refunds_fails(self):
        v = self._verify("Net of refunds you spent INR 500.00 on dining in July.", [ClaimedAmount("INR", "500.00", "July dining")])
        assert not v.passed and "net of refunds" in v.failures[0]

    def test_the_matching_claim_still_passes(self):
        v = self._verify("You spent INR 500.00 on dining in July.", [ClaimedAmount("INR", "500.00", "July dining")])
        assert v.passed and v.status == "VERIFIED"

    def test_net_figure_from_net_spending_passes(self):
        trace = [ToolCallRecord("net_spending", {"date_from": "2025-07-01", "date_to": "2025-07-31"},
                                {"per_currency": {"INR": {"gross_purchases": "10000.00", "linked_refunds": "3000.00",
                                                          "net_spending": "7000.00"}}})]
        answer = FinalAnswer(answer_text="After refunds you spent INR 7000.00 in July.", proposed_status="VERIFIED",
                             verified_amounts=[ClaimedAmount("INR", "7000.00", "July net spending")])
        assert verify(answer, trace).passed

    def test_a_refusal_that_made_no_tool_calls_is_not_treated_as_a_claim(self):
        answer = FinalAnswer(answer_text="I can't answer that; spending was under INR 10000 at most.",
                             proposed_status="INSUFFICIENT_INFORMATION")
        assert verify(answer, trace=[]).passed  # nothing was looked up, and nothing is being certified

    def test_an_unsupported_figure_alongside_real_tool_calls_still_fails(self):
        v = self._verify("Dining was INR 500.00, and your rent is about INR 45000.",
                         [ClaimedAmount("INR", "500.00", "July dining")])
        assert not v.passed and "45000" in v.failures[0]
