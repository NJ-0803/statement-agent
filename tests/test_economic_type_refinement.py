"""Tests for resolve.refine_economic_type — none of these patterns (ATM withdrawal,
bank transfer, fee, interest, reversal, reimbursement) exist in dataset_public/,
so these are synthetic fixtures exercising patterns a held-out grading document
could plausibly contain. Getting economic_type wrong directly causes a wrong
spend total, so this is worth covering even without real data to test against.
"""

import uuid
from datetime import date
from decimal import Decimal

from statement_agent.resolve import refine_economic_type
from statement_agent.schema import Direction, EconomicType, Transaction


def _txn(description: str, direction: Direction, economic_type: EconomicType) -> Transaction:
    return Transaction(
        transaction_id=str(uuid.uuid4()),
        document_id="d1",
        transaction_date=date(2025, 6, 1),
        date_raw="2025-06-01",
        description_raw=description,
        merchant_raw=description,
        amount=Decimal("1000.00"),
        direction=direction,
        economic_type=economic_type,
    )


class TestDebitRefinement:
    def test_atm_withdrawal_reclassified_from_purchase(self):
        t = _txn("ATM CASH WITHDRAWAL", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CASH_WITHDRAWAL

    def test_neft_transfer_reclassified(self):
        t = _txn("NEFT TO JOHN DOE", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.TRANSFER

    def test_imps_transfer_reclassified(self):
        t = _txn("IMPS/123456/TRANSFER", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.TRANSFER

    def test_bare_transfer_word_in_merchant_name_not_reclassified(self):
        # EC: a merchant literally named "TRANSFER CAFE" must not become a bank transfer
        t = _txn("TRANSFER CAFE BLR", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.PURCHASE

    def test_annual_fee_reclassified(self):
        t = _txn("ANNUAL FEE CREDIT CARD", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.FEE

    def test_interest_charge_reclassified(self):
        t = _txn("INTEREST CHARGED", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.INTEREST

    def test_ordinary_merchant_purchase_unaffected(self):
        t = _txn("SWIGGY BANGALORE", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.PURCHASE


class TestCreditRefinement:
    def test_reversal_reclassified_from_refund(self):
        t = _txn("TRANSACTION REVERSAL", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.REVERSAL

    def test_reimbursement_reclassified(self):
        t = _txn("EMPLOYER REIMBURSEMENT", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.REIMBURSEMENT

    def test_payment_received_reclassified_to_card_payment(self):
        t = _txn("PAYMENT RECEIVED THANK YOU", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CREDIT_CARD_PAYMENT

    def test_ordinary_refund_unaffected(self):
        t = _txn("AMAZON REFUND", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.REFUND

    def test_cashback_credit_reclassified(self):
        t = _txn("CASHBACK", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CASHBACK

    def test_reward_redemption_credit_reclassified(self):
        t = _txn("REWARDS REDEMPTION", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CASHBACK

    def test_bare_reward_word_alone_not_reclassified(self):
        # a merchant credit that just happens to say "reward" without a qualifying
        # word (redemption/points) shouldn't be force-classified — same discipline as EC-47
        t = _txn("REWARD STORE REFUND", Direction.CREDIT, EconomicType.REFUND)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.REFUND


class TestEdgeCaseCatalogueScenarios:
    """Direct scenarios from the external 50-item edge-case catalogue (EC-01, EC-20, EC-27)."""

    def test_ec01_bank_side_card_bill_payment_not_left_as_generic_purchase(self):
        t = _txn("ICICI CARD PAYMENT", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CREDIT_CARD_PAYMENT

    def test_ec20_investment_transfer_to_named_brokerage_reclassified(self):
        t = _txn("Transfer to Zerodha", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.INVESTMENT_TRANSFER

    def test_ec20_mutual_fund_sip_reclassified(self):
        t = _txn("MUTUAL FUND SIP", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.INVESTMENT_TRANSFER

    def test_ec27_emi_principal_reclassified_as_liability_not_purchase(self):
        t = _txn("EMI PRINCIPAL APPLE STORE", Direction.DEBIT, EconomicType.PURCHASE)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.TRANSFER


class TestNeverDowngradesAnAlreadySpecificType:
    def test_does_not_touch_a_transaction_already_typed_as_credit_card_payment(self):
        # simulates pdf_native.py already having classified this at extraction time;
        # refine_economic_type must not run its (redundant) logic and flip it to something else
        t = _txn("PAYMENT RECEIVED", Direction.CREDIT, EconomicType.CREDIT_CARD_PAYMENT)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.CREDIT_CARD_PAYMENT

    def test_does_not_touch_a_transaction_already_typed_as_transfer(self):
        t = _txn("SOME LABEL", Direction.DEBIT, EconomicType.TRANSFER)
        refine_economic_type(t)
        assert t.economic_type == EconomicType.TRANSFER
