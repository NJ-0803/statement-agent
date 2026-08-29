"""Runs the gold-answer eval harness (eval/gold_qa.py) as part of the standard
pytest suite, one test per case, so a failure points at exactly which gold
question broke rather than requiring a separate manual run.
"""

import pytest

from eval.gold_qa import GOLD_CASES, _build_ledger


@pytest.fixture(scope="module")
def ledger():
    return _build_ledger()


@pytest.mark.parametrize("case", GOLD_CASES, ids=[c.case_id for c in GOLD_CASES])
def test_gold_case(case, ledger):
    passed, detail = case.check(ledger)
    assert passed, detail
