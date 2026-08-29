import os
import tempfile
import uuid
from datetime import date
from decimal import Decimal

from statement_agent.schema import Direction, Document, EconomicType, SourceRef, Transaction
from statement_agent.store import Store


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return Store(path), path


class TestDecimalRoundTrip:
    def test_amount_survives_round_trip_exactly(self):
        store, path = _fresh_store()
        try:
            doc = Document(document_id="d1", file_path="x", file_hash="h1", doc_type="bank_statement")
            store.upsert_document(doc)
            txn = Transaction(
                transaction_id=str(uuid.uuid4()), document_id="d1", transaction_date=date(2025, 6, 1),
                date_raw="2025-06-01", amount=Decimal("0.10"), currency="INR", direction=Direction.DEBIT,
                economic_type=EconomicType.PURCHASE, merchant_raw="TEST",
                source=SourceRef(file_path="x", file_hash="h1"),
            )
            store.insert_transactions([txn])
            fetched = store.all_transactions()[0]
            assert fetched.amount == Decimal("0.10")  # would fail if stored as float
            assert (fetched.amount + fetched.amount + fetched.amount) == Decimal("0.30")
        finally:
            store.close()
            os.remove(path)


class TestFileHashIdempotency:
    def test_same_file_hash_is_not_duplicated(self):
        store, path = _fresh_store()
        try:
            doc = Document(document_id="d1", file_path="x", file_hash="same-hash", doc_type="bank_statement")
            store.upsert_document(doc)
            assert store.has_document("same-hash") is True
            assert store.has_document("different-hash") is False
        finally:
            store.close()
            os.remove(path)
