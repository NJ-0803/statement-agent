"""The evaluation harness: a portable question bank, and grading that doesn't trust the verifier that ran."""

import json
import os

from eval.grade import grade_one, render
from eval.run_red_team_bank import load_bank

BANK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "question_bank.json")


def _record(**kw):
    base = {"id": 1, "category": "Basic Correctness", "question": "What did I spend in July?",
            "expected_response_type": "Direct Answer", "severity": "High", "must_not_do": "Invent numbers.",
            "error": None, "proposed_status": "VERIFIED", "answer_text": "You spent INR 500.00.",
            "caveats": [], "cited_transaction_ids": ["t1"], "verified_amounts": [{"currency": "INR", "amount": "500.00"}],
            "tool_trace": [{"tool": "aggregate_spending", "input": {}, "result_numbers": ["500.00"],
                            "result_transaction_ids": ["t1"]}]}
    base.update(kw)
    return base


class TestQuestionBank:
    def test_the_bank_travels_with_the_repo(self):
        questions = load_bank()
        assert len(questions) == 95
        assert {"id", "category", "question", "expected_response_type", "severity", "must_not_do"} <= set(questions[0])
        with open(BANK, encoding="utf-8") as f:
            assert json.load(f)["count"] == 95


class TestGrading:
    def test_a_backed_answer_passes(self):
        assert grade_one(_record())["bucket"] == "pass"

    def test_an_amount_no_tool_returned_is_a_correctness_failure(self):
        g = grade_one(_record(verified_amounts=[{"currency": "INR", "amount": "999.00"}]))
        assert g["bucket"] == "correctness_failure" and "claimed amount" in g["reasons"][0]

    def test_a_citation_no_tool_returned_is_a_correctness_failure(self):
        g = grade_one(_record(cited_transaction_ids=["made-up"]))
        assert g["bucket"] == "correctness_failure" and "cited transaction id" in g["reasons"][0]

    def test_grading_does_not_trust_the_verifier_that_ran(self):
        # the run claims it verified fine; the raw fields say otherwise, and the grade follows the raw fields
        g = grade_one(_record(verification_passed=True, verified_amounts=[{"currency": "INR", "amount": "12345.00"}]))
        assert g["bucket"] == "correctness_failure"

    def test_an_answer_where_a_refusal_was_expected_fails(self):
        g = grade_one(_record(category="Privacy / Unsupported Confidential Details",
                              expected_response_type="Refuse/Clarify"))
        assert g["bucket"] == "correctness_failure"

    def test_a_refusal_where_one_was_expected_is_its_own_bucket(self):
        g = grade_one(_record(expected_response_type="Insufficient Information",
                              proposed_status="INSUFFICIENT_INFORMATION", verified_amounts=[], cited_transaction_ids=[]))
        assert g["bucket"] == "refusal_ok"

    def test_a_missing_caveat_where_one_was_expected_fails(self):
        assert grade_one(_record(expected_response_type="Answer with Caveat"))["bucket"] == "correctness_failure"

    def test_an_api_error_is_not_counted_as_a_model_failure(self):
        g = grade_one(_record(error="APIStatusError: credit balance too low"))
        assert g["bucket"] == "infrastructure_error" and "credit balance" in g["reasons"][0]

    def test_a_run_without_recorded_tool_output_is_not_silently_passed(self):
        g = grade_one(_record(tool_trace=["aggregate_spending"]))  # the older runner recorded names only
        assert g["bucket"] == "needs_human_review" and "could not be re-checked" in g["reasons"][0]

    def test_the_report_separates_the_three_kinds_of_result(self):
        records = [_record(id=1), _record(id=2, error="APIStatusError: credit balance too low"),
                   _record(id=3, verified_amounts=[{"currency": "INR", "amount": "999.00"}])]
        report = render([grade_one(r) for r in records], records)
        assert "| pass | 1 |" in report and "| infrastructure_error | 1 |" in report
        assert "Correctness failures among answered questions: 1/2" in report
