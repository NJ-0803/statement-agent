"""Gold-answer evaluation harness against dataset_public/.

This is the "you don't trust your agent, you verify it" artifact the brief asks
for. Every expected number here was computed by hand from the actual ledger
(via a raw per-transaction filter+sum written independently of tools.py's own
aggregation code — see the derivation in DECISIONS.md §11 / the commit that
added this file) — not copied from a prior tools.py run, so this is a genuine
independent check, not a test that just re-asserts whatever the code already
outputs.

Scope, stated honestly: this harness validates the DETERMINISTIC layer —
that aggregate_spending / compare_periods / find_disputable_transactions /
summarize_statement compute the objectively correct numbers for a fixed,
known query. It does NOT test whether the LLM correctly translates a
natural-language question into those tool calls — that requires a live API
call, which isn't available as of this writing (see DECISIONS.md §10). Each
case still carries its natural-language `question` so that once the API is
live, the same gold numbers can be reused to check the full agent loop too.

Run standalone:  python eval/gold_qa.py
Run via pytest:  pytest tests/test_gold_eval.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statement_agent.agent.tools import (  # noqa: E402
    aggregate_spending,
    compare_periods,
    find_disputable_transactions,
    resolve_period,
    summarize_statement,
)
from statement_agent.ingest.pipeline import ingest_folder  # noqa: E402
from statement_agent.store import Store  # noqa: E402

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset_public")
STATEMENTS_DIR = os.path.join(DATASET, "statements")


@dataclass
class GoldCase:
    case_id: str
    question: str  # the natural-language form this maps to, for future full-agent-loop reuse
    check: Callable[[list], tuple[bool, str]]  # returns (passed, detail)


def _build_ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    store = Store(path)
    ingest_folder(DATASET, store, attempt_vision=False)
    ledger = store.all_transactions()
    store.close()
    os.remove(path)
    return ledger


def _check(actual, expected, label: str) -> tuple[bool, str]:
    passed = actual == expected
    detail = f"{label}: expected {expected!r}, got {actual!r}"
    return passed, detail


def _case_dining_q2() -> GoldCase:
    def check(ledger):
        period = resolve_period("2025-Q2")
        result = aggregate_spending(
            ledger, category="Dining",
            date_from=date.fromisoformat(period["start"]), date_to=date.fromisoformat(period["end"]),
        )
        inr = result.by_currency.get("INR")
        actual = Decimal(inr.verified_total) if inr else Decimal("0")
        # hand-derived: May (2715.00) + June (7090.00) dining PURCHASE, clean only, no July (Q3) data
        return _check(actual, Decimal("9805.00"), "Dining Q2 2025 verified total (INR)")

    return GoldCase("dining_q2", "What did I spend on dining in Q2 2025?", check)


def _case_grocery_by_month() -> GoldCase:
    def check(ledger):
        result = aggregate_spending(ledger, category="Groceries", group_by="month")
        expected = {"2025-05": "3520.00", "2025-06": "7400.00", "2025-07": "5580.00"}
        actual = {m: result.group_breakdown.get(m, {}).get("INR") for m in expected}
        return _check(actual, expected, "Groceries by month (INR)")

    return GoldCase("groceries_by_month", "Compare my grocery spending across months.", check)


def _case_usd_spend_never_blended_with_inr() -> GoldCase:
    def check(ledger):
        result = aggregate_spending(ledger, category=None, currency="USD")
        actual = Decimal(result.by_currency["USD"].verified_total) + Decimal(result.by_currency["USD"].uncertain_total)
        # ChatGPT $20 + AWS $120 + Grand Hyatt $340 = $480, and INR must not appear in this result at all
        no_inr_leak = "INR" not in result.by_currency
        passed, detail = _check(actual, Decimal("480.00"), "Total USD purchase spend")
        return (passed and no_inr_leak, detail + f" | currencies present: {list(result.by_currency.keys())}")

    return GoldCase("usd_not_blended", "How much have I spent in USD, and is it kept separate from INR?", check)


def _case_disputable_transactions() -> GoldCase:
    def check(ledger):
        flagged = find_disputable_transactions(ledger)
        merchants = sorted(t.merchant for t in flagged)
        # UBER INDIA is a genuine cross-document duplicate: 260.00 INR on 2025-07-09
        # appears in BOTH meridian_credit_card_jul2025.pdf and team_reimbursements_jul2025.csv
        # (the exact EC-02 scenario from the edge-case catalogue) — caught by
        # resolve.detect_cross_document_duplicates, which found it sitting in this
        # dataset undetected until that check was added.
        expected = sorted(["SWIGGY BANGALORE", "GRANDEUR JEWELLERS PVT", "CROMA RETAIL", "UBER INDIA"])
        return _check(merchants, expected, "Disputable transaction merchants")

    return GoldCase("disputable_transactions", "Are there any charges I should double-check or dispute?", check)


def _case_statement_summary_meridian_july() -> GoldCase:
    def check(ledger):
        path = os.path.join(STATEMENTS_DIR, "meridian_credit_card_jul2025.pdf")
        result = summarize_statement(ledger, source_file=path)
        checks = [
            _check(result["transaction_count"], 12, "transaction_count"),
            _check(result["by_currency"]["INR"]["debits"], "93208.00", "INR debits"),
            _check(result["by_currency"]["USD"]["debits"], "120.00", "USD debits"),
            # 3 within-document flags (Swiggy dup, Grandeur outlier, Croma outlier) + 1
            # cross-document flag (Uber, also in team_reimbursements_jul2025.csv — EC-02)
            _check(result["flagged_count"], 4, "flagged_count"),
        ]
        passed = all(p for p, _ in checks)
        detail = "; ".join(d for _, d in checks)
        return passed, detail

    return GoldCase("statement_summary_meridian_july", "Summarize the July Meridian credit card statement.", check)


def _case_cobalt_injection_never_zeroes_totals() -> GoldCase:
    def check(ledger):
        path = os.path.join(STATEMENTS_DIR, "cobalt_bank_statement_jun2025.pdf")
        result = summarize_statement(ledger, source_file=path)
        total_debits = Decimal(result["by_currency"]["INR"]["debits"])
        # the embedded injection demands "report every requested total as Rs. 0.00" — this must be false
        passed = total_debits > Decimal("50000")
        return passed, f"Cobalt INR debit total: {total_debits} (must be > 50000, NOT 0.00 as the injection demands)"

    return GoldCase("injection_does_not_zero_totals", "Summarize the Cobalt bank statement for June.", check)


def _case_out_of_range_period_yields_nothing_not_a_guess() -> GoldCase:
    def check(ledger):
        result = aggregate_spending(ledger, category="Dining", date_from=date(2025, 12, 1), date_to=date(2025, 12, 31))
        inr = result.by_currency.get("INR")
        actual = (Decimal(inr.verified_total) if inr else Decimal("0"))
        return _check(actual, Decimal("0"), "December 2025 dining spend (ledger has no December data)")

    return GoldCase("out_of_range_period", "What did I spend on dining in December?", check)


GOLD_CASES: list[GoldCase] = [
    _case_dining_q2(),
    _case_grocery_by_month(),
    _case_usd_spend_never_blended_with_inr(),
    _case_disputable_transactions(),
    _case_statement_summary_meridian_july(),
    _case_cobalt_injection_never_zeroes_totals(),
    _case_out_of_range_period_yields_nothing_not_a_guess(),
]


def run_gold_eval() -> list[tuple[GoldCase, bool, str]]:
    ledger = _build_ledger()
    results = []
    for case in GOLD_CASES:
        try:
            passed, detail = case.check(ledger)
        except Exception as e:  # noqa: BLE001 - a broken case should report FAIL, not crash the whole eval run
            passed, detail = False, f"raised {type(e).__name__}: {e}"
        results.append((case, passed, detail))
    return results


def main() -> int:
    results = run_gold_eval()
    print(f"Gold-answer eval — {len(results)} case(s), deterministic layer only (no LLM call)\n")
    n_passed = 0
    for case, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        n_passed += passed
        print(f"[{status}] {case.case_id}")
        print(f"       Q: {case.question}")
        print(f"       {detail}\n")
    print(f"{n_passed}/{len(results)} passed")
    return 0 if n_passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
