"""Economic event linking and the income type (DECISIONS.md §36). Synthetic bank/savings/card exports are
imported through the real pipeline, so links are rebuilt exactly as they are for a person's own files."""

import os
from decimal import Decimal

import pytest

from statement_agent import ledger_edits
from statement_agent.agent import tools as T
from statement_agent.corrections import CorrectionError
from statement_agent.ingest import pipeline
from statement_agent.linking import build_events
from statement_agent.schema import Direction, EconomicType, EventKind, EventStatus, Transaction
from statement_agent.store import Store
from statement_agent.web.app import create_app

BANK = (
    "Date,Narration,Withdrawal,Deposit,Currency\n"
    "01/04/2025,NEFT SALARY ACME CORP,,50000.00,INR\n"
    "02/04/2025,AMAZON ORDER 1234,3000.00,,INR\n"
    "03/04/2025,FLIPKART ORDER 77,1200.00,,INR\n"
    "05/04/2025,NETFLIX SUBSCRIPTION,649.00,,INR\n"
    "06/04/2025,HOTEL STAY GOA,4500.00,,INR\n"
    "08/04/2025,FLIPKART REFUND 77,,1200.00,INR\n"
    "10/04/2025,AMAZON REFUND 1234,,1000.00,INR\n"
    "11/04/2025,REFUND REF 55,,250.00,INR\n"
    "12/04/2025,NEFT TRANSFER TO SAVINGS,5000.00,,INR\n"
    "15/04/2025,CREDIT CARD BILL PAYMENT,2000.00,,INR\n"
    "18/04/2025,BY CLEARING 998877,,700.00,INR\n"
    "20/04/2025,EMPLOYER REIMBURSEMENT,,4500.00,INR\n"
    "05/05/2025,NETFLIX SUBSCRIPTION,649.00,,INR\n"
    "05/06/2025,NETFLIX SUBSCRIPTION,649.00,,INR\n"
    "20/08/2025,ATM WDL,500.00,,INR\n"
)
SAVINGS = (
    "Date,Narration,Withdrawal,Deposit,Currency\n"
    "13/04/2025,BY CLEARING MAIN AC,,5000.00,INR\n"
    "14/04/2025,ZOMATO,250.00,,INR\n"
)
CARD = (
    "Date,Description,Amount,Currency\n"
    "2025-04-16,PAYMENT RECEIVED THANK YOU,-2000.00,INR\n"
    "2025-04-17,SWIGGY,300.00,INR\n"
)


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
    if job.state.value == "needs_review":
        acks = [i["issue_id"] for i in store.get_staging(job.job_id)["issues"] if i["severity"] == "check"]
        job = pipeline.update_review(store, job.job_id, {"acknowledge": acks})
    assert job.state.value == "ready", store.get_staging(job.job_id)["issues"]
    return pipeline.commit_import(store, job.job_id)


@pytest.fixture
def ledger(store, tmp_path):
    jobs = {name: _import(store, tmp_path, f"{name}.csv", text) for name, text in (("bank", BANK), ("savings", SAVINGS), ("card", CARD))}
    return jobs


def _t(store, needle) -> Transaction:
    [t] = [t for t in store.all_transactions() if t.description_raw == needle]
    return t


def _links(store, kind):
    return [e for e in store.list_events() if e.kind == kind]


def _pair(event):
    return {m.role: m.transaction_id for m in event.members}


class TestIncomeType:
    def test_money_in_gets_a_real_kind(self, store, ledger):
        salary = _t(store, "NEFT SALARY ACME CORP")
        assert (salary.economic_type, salary.economic_type_confidence) == (EconomicType.INCOME, 1.0)
        generic = _t(store, "BY CLEARING 998877")
        assert (generic.economic_type, generic.economic_type_confidence) == (EconomicType.INCOME, 0.5)
        assert _t(store, "FLIPKART REFUND 77").economic_type == EconomicType.REFUND
        assert _t(store, "EMPLOYER REIMBURSEMENT").economic_type == EconomicType.REIMBURSEMENT

    def test_income_total_excludes_transfers_and_refunds(self, store, ledger):
        result = T.aggregate_spending(store.all_transactions(), economic_types=("INCOME",))
        assert result.by_currency["INR"].verified_total == "50700.00"  # salary + the unexplained credit, not the savings transfer


class TestRefunds:
    def test_full_refund_with_same_merchant_is_matched(self, store, ledger):
        [e] = [e for e in _links(store, EventKind.REFUND) if _pair(e)["refund"] == _t(store, "FLIPKART REFUND 77").transaction_id]
        assert e.status == EventStatus.MATCHED
        assert _pair(e)["purchase"] == _t(store, "FLIPKART ORDER 77").transaction_id
        assert "the same merchant" in e.reason

    def test_partial_refund_is_only_suggested(self, store, ledger):
        [e] = [e for e in _links(store, EventKind.REFUND) if _pair(e)["refund"] == _t(store, "AMAZON REFUND 1234").transaction_id]
        assert e.status == EventStatus.SUGGESTED and e.details["partial"] is True

    def test_amount_alone_never_links_a_refund(self, store, ledger):
        ref = _t(store, "REFUND REF 55").transaction_id
        assert not any(ref in _pair(e).values() for e in _links(store, EventKind.REFUND))

    def test_net_spending_subtracts_only_counted_refunds(self, store, ledger):
        net = T.net_spending(store.all_transactions(), store.list_events())
        inr = net["per_currency"]["INR"]
        assert inr["linked_refunds"] == "1200.00"
        assert Decimal(inr["net_spending"]) == Decimal(inr["gross_purchases"]) - Decimal("1200.00")
        assert len(net["suggested_refund_links_not_applied"]) == 1
        assert {r["description"] for r in net["unlinked_refunds_not_applied"]} == {"AMAZON REFUND 1234", "REFUND REF 55"}

    def test_confirming_the_partial_refund_counts_it_and_survives_rebuilds(self, store, ledger, tmp_path):
        e = next(e for e in _links(store, EventKind.REFUND) if e.status == EventStatus.SUGGESTED)
        ledger_edits.decide_link(store, e.event_id, "confirmed")
        _import(store, tmp_path, "more.csv", "Date,Description,Amount,Currency\n2025-05-01,TEA,10.00,INR\n")
        assert store.get_event(e.event_id).status == EventStatus.CONFIRMED
        net = T.net_spending(store.all_transactions(), store.list_events())
        assert net["per_currency"]["INR"]["linked_refunds"] == "2200.00"
        ledger_edits.decide_link(store, e.event_id, None)
        assert store.get_event(e.event_id).status == EventStatus.SUGGESTED


class TestTransfersAndCards:
    def test_money_from_your_own_account_is_a_transfer_not_income(self, store, ledger):
        [e] = _links(store, EventKind.TRANSFER)
        assert e.status == EventStatus.MATCHED
        arrived = _t(store, "BY CLEARING MAIN AC")
        assert _pair(e) == {"out": _t(store, "NEFT TRANSFER TO SAVINGS").transaction_id, "in": arrived.transaction_id}
        assert (arrived.economic_type, arrived.economic_type_source, arrived.economic_type_auto) == (EconomicType.TRANSFER, "link", "INCOME")

    def test_saying_not_related_puts_the_type_back(self, store, ledger):
        [e] = _links(store, EventKind.TRANSFER)
        ledger_edits.decide_link(store, e.event_id, "rejected")
        arrived = _t(store, "BY CLEARING MAIN AC")
        assert (arrived.economic_type, arrived.economic_type_source) == (EconomicType.INCOME, "auto")
        assert store.get_event(e.event_id).status == EventStatus.REJECTED

    def test_card_bill_payment_is_linked_to_the_cards_payment_received(self, store, ledger):
        [e] = _links(store, EventKind.CARD_PAYMENT)
        assert e.status == EventStatus.MATCHED
        assert _pair(e) == {"payment": _t(store, "CREDIT CARD BILL PAYMENT").transaction_id,
                            "in": _t(store, "PAYMENT RECEIVED THANK YOU").transaction_id}

    def test_undoing_the_card_import_removes_its_links(self, store, ledger):
        pipeline.rollback_import(store, ledger["card"].job_id)
        assert _links(store, EventKind.CARD_PAYMENT) == []

    def test_same_account_is_never_a_transfer(self):
        out = Transaction("o", "doc1", None, "", amount=Decimal("5"), direction=Direction.DEBIT, economic_type=EconomicType.TRANSFER)
        inn = Transaction("i", "doc1", None, "", amount=Decimal("5"), direction=Direction.CREDIT, economic_type=EconomicType.TRANSFER)
        from datetime import date
        out.transaction_date = inn.transaction_date = date(2025, 1, 1)
        assert build_events([out, inn], [], {}) == []


class TestReimbursementsAndRecurring:
    def test_reimbursement_is_only_ever_suggested(self, store, ledger):
        [e] = _links(store, EventKind.REIMBURSEMENT)
        assert e.status == EventStatus.SUGGESTED
        assert _pair(e) == {"expense": _t(store, "HOTEL STAY GOA").transaction_id,
                            "reimbursement": _t(store, "EMPLOYER REIMBURSEMENT").transaction_id}

    def test_monthly_subscription_is_found_and_flagged_as_possibly_stopped(self, store, ledger):
        [e] = _links(store, EventKind.RECURRING)
        d = e.details
        assert (d["name"], d["cadence"], d["count"], d["typical_amount"]) == ("Netflix Subscription", "monthly", 3, "649.00")
        assert d["next_expected"] == "2025-07-05" and d["possibly_stopped"] is True
        result = T.recurring_payments(store.all_transactions(), store.list_events())
        assert result["count"] == 1
        ledger_edits.decide_link(store, e.event_id, "rejected")
        assert T.recurring_payments(store.all_transactions(), store.list_events())["count"] == 0


class TestCorrectingTheKind:
    def test_one_row_kind_change_and_back(self, store, ledger):
        t = _t(store, "BY CLEARING 998877")
        ledger_edits.correct_one(store, t.transaction_id, economic_type="REFUND")
        t = store.get_transaction(t.transaction_id)
        assert (t.economic_type, t.economic_type_source) == (EconomicType.REFUND, "you")
        ledger_edits.correct_one(store, t.transaction_id, economic_type=None)
        t = store.get_transaction(t.transaction_id)
        assert (t.economic_type, t.economic_type_source) == (EconomicType.INCOME, "auto")

    def test_a_kind_rule_and_its_removal(self, store, ledger):
        t = _t(store, "HOTEL STAY GOA")
        rule = ledger_edits.add_rule(store, t.transaction_id, pattern="HOTEL", economic_type="TRANSFER")["rule"]
        t = store.get_transaction(t.transaction_id)
        assert (t.economic_type, t.economic_type_source, t.category) == (EconomicType.TRANSFER, "rule", None)
        ledger_edits.remove_rule(store, rule.rule_id)
        t = store.get_transaction(t.transaction_id)
        assert (t.economic_type, t.category) == (EconomicType.PURCHASE, "Travel")

    def test_unknown_kind_is_refused(self, store, ledger):
        with pytest.raises(CorrectionError):
            ledger_edits.correct_one(store, _t(store, "SWIGGY").transaction_id, economic_type="LOTTERY")


class TestManualLinks:
    def test_link_two_rows_yourself(self, store, ledger):
        purchase, refund = _t(store, "ZOMATO"), _t(store, "REFUND REF 55")
        event = ledger_edits.link_manually(store, "refund", purchase.transaction_id, refund.transaction_id)["event"]
        assert event.status == EventStatus.CONFIRMED and event.source == "you"
        net = T.net_spending(store.all_transactions(), store.list_events())
        assert net["per_currency"]["INR"]["linked_refunds"] == "1450.00"
        with pytest.raises(CorrectionError, match="already linked"):
            ledger_edits.link_manually(store, "refund", purchase.transaction_id, refund.transaction_id)
        ledger_edits.decide_link(store, event.event_id, "rejected")
        assert store.get_event(event.event_id) is None

    def test_wrong_order_is_refused(self, store, ledger):
        with pytest.raises(CorrectionError, match="money-out row first"):
            ledger_edits.link_manually(store, "refund", _t(store, "REFUND REF 55").transaction_id, _t(store, "ZOMATO").transaction_id)


class TestWeb:
    @pytest.fixture
    def client(self, tmp_path):
        s = Store(str(tmp_path / "ledger.db"))
        for name, text in (("bank", BANK), ("savings", SAVINGS)):
            _import(s, tmp_path, f"{name}.csv", text)
        s.close()
        return create_app(db_path=str(tmp_path / "ledger.db"), upload_dir=str(tmp_path / "up")).test_client()

    def _h(self, client):
        return {"X-CSRF-Token": client.application.config["CSRF_TOKEN"]}

    def test_links_listing_decision_and_row_view(self, client):
        data = client.get("/api/links").get_json()
        assert data["links"][0]["status"] == "suggested"
        assert data["recurring"][0]["details"]["cadence"] == "monthly"
        link_id = next(l["id"] for l in data["links"] if l["kind"] == "reimbursement")
        res = client.post(f"/api/links/{link_id}/decision", json={"decision": "confirmed"}, headers=self._h(client))
        assert res.get_json()["link"]["status"] == "confirmed"
        assert client.post(f"/api/links/{link_id}/decision", json={"decision": "maybe"}, headers=self._h(client)).status_code == 400
        assert client.post("/api/links/nope/decision", json={"decision": None}, headers=self._h(client)).status_code == 404

        hotel = client.get("/api/transactions?q=hotel").get_json()["transactions"][0]
        assert hotel["links"][0]["status"] == "confirmed" and hotel["type_label"].startswith("Purchase")
        clearing = client.get("/api/transactions?q=998877").get_json()
        assert clearing["transactions"][0]["type_unsure"] is True
        assert any(t["value"] == "INCOME" for t in clearing["types"])

    def test_kind_correction_over_http(self, client):
        row = client.get("/api/transactions?q=998877").get_json()["transactions"][0]
        url = f"/api/transactions/{row['id']}/correction"
        res = client.post(url, json={"economic_type": "TRANSFER"}, headers=self._h(client))
        assert res.status_code == 200 and res.get_json()["transaction"]["type_source"] == "you"
        assert client.post(url, json={"economic_type": "LOTTERY"}, headers=self._h(client)).status_code == 400

    def test_manual_link_over_http(self, client):
        rows = client.get("/api/transactions?limit=100").get_json()["transactions"]
        zomato = next(r for r in rows if r["description"] == "ZOMATO")
        ref = next(r for r in rows if r["description"] == "REFUND REF 55")
        res = client.post("/api/links", json={"kind": "refund", "out_id": zomato["id"], "in_id": ref["id"]}, headers=self._h(client))
        assert res.status_code == 200 and res.get_json()["link"]["source"] == "you"
        assert client.post("/api/links", json={"kind": "gift"}, headers=self._h(client)).status_code == 400
