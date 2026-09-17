"""Phase 1, part 2 (DECISIONS.md §35): correction-driven categories and merchant names — one-row changes,
rules that cover matching rows now and in future imports, precedence, undo, and the audit log."""

import os
from datetime import date
from decimal import Decimal

import pytest

from statement_agent import ledger_edits
from statement_agent.agent.tools import aggregate_spending
from statement_agent.corrections import CorrectionError, best_rule, merchant_words, rule_matches, suggested_pattern
from statement_agent.ingest import pipeline
from statement_agent.resolve import assign_categories
from statement_agent.schema import CorrectionRule, Direction, EconomicType, Transaction
from statement_agent.store import Store, transaction_from_dict, transaction_to_dict
from statement_agent.web.app import create_app

CSV = (
    "Date,Narration,Withdrawal,Deposit,Currency\n"
    "13/04/2025,UPI-SWIGGY-BANGALORE-482934812,450.00,,INR\n"
    "14/04/2025,UPI-SWIGGY-BANGALORE-990011223,300.00,,INR\n"
    "15/04/2025,POS 4411XXXX RAJU GENERAL STORE,120.00,,INR\n"
    "16/04/2025,NEFT-ACME PAYROLL-N12345,,50000.00,INR\n"
)
NEXT_MONTH = (
    "Date,Narration,Withdrawal,Deposit,Currency\n"
    "13/05/2025,UPI-SWIGGY-MUMBAI-111222333,610.00,,INR\n"
    "14/05/2025,POS 4411XXXX RAJU GENERAL STORE,80.00,,INR\n"
)


def _txn(desc, **kw):
    kw.setdefault("economic_type", EconomicType.PURCHASE)
    return Transaction(transaction_id=kw.pop("tid", desc), document_id="d", transaction_date=date(2025, 4, 1),
                       date_raw="", description_raw=desc, merchant_raw=desc, amount=Decimal("10"), **kw)


def _rule(pattern, category=None, name=None, rid=None, updated="2026-09-01"):
    return CorrectionRule(rule_id=rid or pattern, pattern=pattern, category=category, merchant_name=name, updated_at=updated)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "ledger.db"))
    yield s
    s.close()


def _import(store, tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    job = pipeline.create_import(store, str(p), name)
    job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
    assert job.state.value == "ready", store.get_staging(job.job_id)["issues"]
    return pipeline.commit_import(store, job.job_id)


def _by_desc(store, needle):
    return [t for t in store.all_transactions() if needle in t.description_raw]


class TestMatching:
    def test_reference_numbers_and_rail_codes_are_not_part_of_the_words(self):
        assert merchant_words("UPI-SWIGGY-BANGALORE-482934812") == ["SWIGGY", "BANGALORE"]
        assert merchant_words("POS 4411XXXX RAJU GENERAL STORE") == ["RAJU", "GENERAL", "STORE"]

    def test_a_rule_matches_next_months_row_with_a_different_reference(self):
        rule = _rule("SWIGGY")
        assert rule_matches(rule, _txn("UPI-SWIGGY-MUMBAI-111222333"))
        assert not rule_matches(rule, _txn("SWIGGYMART ORDER"))  # whole words only

    def test_words_must_be_side_by_side_in_order(self):
        rule = _rule("GENERAL STORE")
        assert rule_matches(rule, _txn("RAJU GENERAL STORE"))
        assert not rule_matches(rule, _txn("GENERAL MEDICAL STORE"))

    def test_the_more_specific_rule_wins_then_the_newest(self):
        rules = [_rule("SWIGGY", "Dining"), _rule("SWIGGY INSTAMART", "Groceries"),
                 _rule("SWIGGY", "Takeaway", rid="newer", updated="2026-09-10")]
        assert best_rule(_txn("UPI SWIGGY INSTAMART 123"), rules).category == "Groceries"
        assert best_rule(_txn("UPI SWIGGY 123"), rules).category == "Takeaway"

    def test_suggested_pattern_uses_the_meaningful_words(self):
        assert suggested_pattern(_txn("UPI-SWIGGY-BANGALORE-482934812")) == "SWIGGY BANGALORE"


class TestPrecedence:
    def test_you_beat_rule_beats_keywords_beats_file(self):
        mine = _txn("SWIGGY 1", category="Treats", category_source="you", tid="a")
        ruled = _txn("SWIGGY 2", tid="b")
        keyword = _txn("ZOMATO 3", tid="c")
        declared = _txn("HARDWARE PLACE", category_declared="Home", tid="d")
        assign_categories([mine, ruled, keyword, declared], [_rule("SWIGGY", "Takeaway")])
        assert (mine.category, mine.category_source) == ("Treats", "you")
        assert (ruled.category, ruled.category_source, ruled.category_rule_id) == ("Takeaway", "rule", "SWIGGY")
        assert (keyword.category, keyword.category_source) == ("Dining", "keywords")
        assert (declared.category, declared.category_source) == ("Home", "file")

    def test_rules_never_give_a_non_purchase_a_category(self):
        t = _txn("SWIGGY REFUND", economic_type=EconomicType.REFUND)
        assign_categories([t], [_rule("SWIGGY", "Takeaway")])
        assert t.category is None


class TestCorrections:
    def test_one_row_change_sticks_and_can_be_undone(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]
        assert raju.category is None
        result = ledger_edits.correct_one(store, raju.transaction_id, category="Groceries")
        assert result["changed"] == 0  # only the row itself, which was written directly
        raju = store.get_transaction(raju.transaction_id)
        assert (raju.category, raju.category_source) == ("Groceries", "you")

        ledger_edits.correct_one(store, raju.transaction_id, category=None)
        raju = store.get_transaction(raju.transaction_id)
        assert (raju.category, raju.category_source) == (None, None)
        assert [e["action"] for e in store.corrections_log(transaction_id=raju.transaction_id)] == ["set_one", "set_one"]

    def test_a_rule_fixes_existing_rows_and_future_imports(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        first = _by_desc(store, "SWIGGY")[0]
        assert first.category == "Dining" and first.category_source == "keywords"
        result = ledger_edits.add_rule(store, first.transaction_id, pattern="swiggy", category="Takeaway", merchant_name="Swiggy")
        assert result["changed"] == 2
        assert {(t.category, t.category_source, t.merchant_canonical) for t in _by_desc(store, "SWIGGY")} == {("Takeaway", "rule", "Swiggy")}

        job = pipeline.create_import(store, str(tmp_path / "may.csv"), "may.csv")
        (tmp_path / "may.csv").write_text(NEXT_MONTH)
        job = pipeline.analyze_import(store, job.job_id, attempt_vision=False)
        preview = store.get_staging(job.job_id)
        swiggy = next(t for t in preview["preview_transactions"] if "SWIGGY" in t["description"])
        assert swiggy["category"] == "Takeaway" and swiggy["why"]["category"].startswith("your rule")
        assert preview["rule_matches"] == 1
        pipeline.commit_import(store, job.job_id)
        mumbai = _by_desc(store, "MUMBAI")[0]
        assert (mumbai.category, mumbai.merchant_canonical, mumbai.category_rule_id) == ("Takeaway", "Swiggy", result["rule"].rule_id)

    def test_a_rule_does_not_override_a_row_you_set_by_hand(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        a, b = _by_desc(store, "SWIGGY")
        ledger_edits.correct_one(store, b.transaction_id, category="Party")
        ledger_edits.add_rule(store, a.transaction_id, pattern="SWIGGY", category="Takeaway")
        assert store.get_transaction(b.transaction_id).category == "Party"
        assert ledger_edits.preview_rule(store, "swiggy")["set_by_you"] == 1

    def test_making_a_rule_from_a_row_you_set_hands_that_row_to_the_rule(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        a = _by_desc(store, "SWIGGY")[0]
        ledger_edits.correct_one(store, a.transaction_id, category="Party")
        ledger_edits.add_rule(store, a.transaction_id, pattern="SWIGGY", category="Takeaway")
        assert store.get_transaction(a.transaction_id).category_source == "rule"

    def test_removing_a_rule_puts_rows_back_to_automatic(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]
        rule = ledger_edits.add_rule(store, raju.transaction_id, category="Groceries", merchant_name="Raju Store")["rule"]
        assert rule.pattern == "RAJU GENERAL STORE"
        assert ledger_edits.remove_rule(store, rule.rule_id)["changed"] == 1
        raju = store.get_transaction(raju.transaction_id)
        assert (raju.category, raju.merchant_canonical, raju.merchant_source) == (None, None, None)
        assert [e["action"] for e in store.corrections_log()] == ["add_rule", "remove_rule"]
        assert store.list_rules() == []

    def test_updating_a_rule_keeps_its_other_field(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]
        ledger_edits.add_rule(store, raju.transaction_id, pattern="RAJU", category="Groceries")
        ledger_edits.add_rule(store, raju.transaction_id, pattern="RAJU", merchant_name="Raju")
        [rule] = store.list_rules()
        assert (rule.category, rule.merchant_name) == ("Groceries", "Raju")
        assert [e["action"] for e in store.corrections_log()] == ["add_rule", "update_rule"]

    def test_merchant_name_rules_work_on_income_but_categories_do_not(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        salary = _by_desc(store, "ACME")[0]
        with pytest.raises(CorrectionError, match="Only purchases"):
            ledger_edits.correct_one(store, salary.transaction_id, category="Salary")
        ledger_edits.add_rule(store, salary.transaction_id, pattern="ACME PAYROLL", merchant_name="Acme salary")
        assert store.get_transaction(salary.transaction_id).merchant_canonical == "Acme salary"

    @pytest.mark.parametrize("pattern, message", [
        ("123 456", "at least one word"), ("A B", "too short"), ("ZOMATO", "aren't in this transaction"),
    ])
    def test_bad_patterns_are_refused_in_plain_language(self, store, tmp_path, pattern, message):
        _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]
        with pytest.raises(CorrectionError, match=message):
            ledger_edits.add_rule(store, raju.transaction_id, pattern=pattern, category="Groceries")
        assert store.list_rules() == [] and store.corrections_log() == []

    def test_a_failed_save_changes_nothing(self, store, tmp_path, monkeypatch):
        _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]

        def boom(txns):
            raise RuntimeError("disk full")

        monkeypatch.setattr(store, "_write_correction_fields", boom)
        with pytest.raises(RuntimeError):
            ledger_edits.add_rule(store, raju.transaction_id, category="Groceries")
        assert store.list_rules() == [] and store.corrections_log() == []

    def test_rolling_back_an_import_keeps_the_rules(self, store, tmp_path):
        job = _import(store, tmp_path, "apr.csv", CSV)
        raju = _by_desc(store, "RAJU")[0]
        ledger_edits.add_rule(store, raju.transaction_id, category="Groceries")
        pipeline.rollback_import(store, job.job_id)
        assert store.all_transactions() == [] and len(store.list_rules()) == 1

    def test_answers_group_by_the_name_you_chose(self, store, tmp_path):
        _import(store, tmp_path, "apr.csv", CSV)
        first = _by_desc(store, "SWIGGY")[0]
        ledger_edits.add_rule(store, first.transaction_id, pattern="SWIGGY", merchant_name="Swiggy")
        result = aggregate_spending(store.all_transactions(), group_by="merchant")
        assert result.group_breakdown["Swiggy"] == {"INR": "750.00"}

    def test_fields_survive_staging_json(self):
        t = _txn("X", category_source="rule", category_rule_id="r1", merchant_canonical="Xco", merchant_source="rule", merchant_rule_id="r1",
                 direction=Direction.DEBIT)
        back = transaction_from_dict(transaction_to_dict(t))
        assert (back.category_rule_id, back.merchant_canonical, back.merchant_source) == ("r1", "Xco", "rule")


class TestWeb:
    @pytest.fixture
    def client(self, tmp_path):
        s = Store(str(tmp_path / "ledger.db"))
        _import(s, tmp_path, "apr.csv", CSV)
        s.close()
        return create_app(db_path=str(tmp_path / "ledger.db"), upload_dir=str(tmp_path / "up")).test_client()

    def _h(self, client):
        return {"X-CSRF-Token": client.application.config["CSRF_TOKEN"]}

    def test_list_filter_and_explain(self, client):
        data = client.get("/api/transactions?q=swiggy").get_json()
        assert data["total"] == 2 and "Dining" in data["categories"]
        assert data["transactions"][0]["why"]["category"] == "worked out from the merchant name"
        none = client.get("/api/transactions?category=__none__").get_json()
        assert [t["description"] for t in none["transactions"]] == ["POS 4411XXXX RAJU GENERAL STORE"]

    def test_rule_round_trip(self, client):
        raju = client.get("/api/transactions?q=raju").get_json()["transactions"][0]
        preview = client.post("/api/rules/preview", json={"pattern": raju["suggested_pattern"]}, headers=self._h(client)).get_json()
        assert preview["matches"] == 1
        res = client.post(f"/api/transactions/{raju['id']}/correction",
                          json={"scope": "rule", "pattern": "raju general", "category": "Groceries"}, headers=self._h(client))
        assert res.status_code == 200
        body = res.get_json()
        assert body["rule"]["pattern"] == "RAJU GENERAL" and body["rule"]["applied_to"] == 1
        assert body["transaction"]["why"]["category"] == "your rule for descriptions containing “RAJU GENERAL”"
        assert client.get("/api/transactions?category=Groceries").get_json()["total"] == 1

        rule_id = client.get("/api/rules").get_json()["rules"][0]["id"]
        assert client.delete(f"/api/rules/{rule_id}", headers=self._h(client)).get_json() == {"changed": 1}
        assert client.delete(f"/api/rules/{rule_id}", headers=self._h(client)).status_code == 404

    def test_errors_are_plain_and_csrf_is_required(self, client):
        raju = client.get("/api/transactions?q=raju").get_json()["transactions"][0]
        url = f"/api/transactions/{raju['id']}/correction"
        assert client.post(url, json={"category": "X"}).status_code == 403
        assert client.post(url, json={"category": 5}, headers=self._h(client)).status_code == 400
        assert client.post(url, json={"scope": "all", "category": "X"}, headers=self._h(client)).status_code == 400
        res = client.post(url, json={"scope": "rule", "pattern": "zomato", "category": "X"}, headers=self._h(client))
        assert res.status_code == 400 and "aren't in this transaction" in res.get_json()["error"]
        assert client.post("/api/transactions/nope/correction", json={"category": "X"}, headers=self._h(client)).status_code == 404
        assert client.post(url, json={"category": "x" * 41}, headers=self._h(client)).status_code == 400
