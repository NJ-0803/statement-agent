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


class TestCreatesParentDirectory:
    def test_nested_nonexistent_directory_is_created(self, tmp_path):
        nested = tmp_path / "ledgers" / "alice.db"
        assert not nested.parent.exists()
        store = Store(str(nested))
        assert nested.exists()
        store.close()


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


class TestCategoryDeclaredAndAccountNameRoundTrip:
    def test_both_fields_survive_round_trip(self):
        store, path = _fresh_store()
        try:
            doc = Document(document_id="d1", file_path="x", file_hash="h1", doc_type="expense_sheet")
            store.upsert_document(doc)
            txn = Transaction(
                transaction_id=str(uuid.uuid4()), document_id="d1", transaction_date=date(2025, 6, 1),
                date_raw="2025-06-01", amount=Decimal("10.00"), currency="INR", direction=Direction.DEBIT,
                economic_type=EconomicType.PURCHASE, merchant_raw="Hardware Store",
                category_declared="Home Improvement", account_name="Platinum Card",
                source=SourceRef(file_path="x", file_hash="h1"),
            )
            store.insert_transactions([txn])
            fetched = store.all_transactions()[0]
            assert fetched.category_declared == "Home Improvement"
            assert fetched.account_name == "Platinum Card"
        finally:
            store.close()
            os.remove(path)

    def test_absent_fields_round_trip_as_none(self):
        store, path = _fresh_store()
        try:
            doc = Document(document_id="d1", file_path="x", file_hash="h1", doc_type="bank_statement")
            store.upsert_document(doc)
            txn = Transaction(
                transaction_id=str(uuid.uuid4()), document_id="d1", transaction_date=date(2025, 6, 1),
                date_raw="2025-06-01", amount=Decimal("10.00"), currency="INR", direction=Direction.DEBIT,
                economic_type=EconomicType.PURCHASE, merchant_raw="SWIGGY",
                source=SourceRef(file_path="x", file_hash="h1"),
            )
            store.insert_transactions([txn])
            fetched = store.all_transactions()[0]
            assert fetched.category_declared is None
            assert fetched.account_name is None
        finally:
            store.close()
            os.remove(path)


class TestMigrationAddsNewColumnsToExistingDb:
    """A real, already-ingested ledger.db predates category_declared/account_name —
    CREATE TABLE IF NOT EXISTS never adds a column to an existing table, so opening an
    old-schema DB must migrate it in place rather than erroring or silently dropping data."""

    def test_opening_a_pre_migration_db_adds_the_new_columns(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, file_path TEXT NOT NULL, file_hash TEXT NOT NULL UNIQUE,
                doc_type TEXT NOT NULL, account_label TEXT, currency_declared TEXT, statement_start TEXT,
                statement_end TEXT, opening_balance TEXT, closing_balance TEXT, stated_total_debits TEXT,
                stated_total_credits TEXT, reconciliation_status TEXT NOT NULL, reconciliation_delta TEXT,
                parse_warnings TEXT
            );
            CREATE TABLE transactions (
                transaction_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, transaction_date TEXT,
                date_raw TEXT, date_plausible INTEGER NOT NULL, extraction_sequence INTEGER,
                description_raw TEXT, merchant_raw TEXT, merchant_normalized TEXT, amount TEXT NOT NULL,
                currency TEXT NOT NULL, amount_raw TEXT, direction TEXT NOT NULL, economic_type TEXT NOT NULL,
                economic_type_confidence REAL, category TEXT, category_confidence REAL,
                source_file_path TEXT, source_page INTEGER, source_row INTEGER, source_raw_text TEXT,
                extraction_method TEXT, extraction_confidence REAL, duplicate_of TEXT, duplicate_reason TEXT,
                notes TEXT
            );
        """)
        conn.execute(
            "INSERT INTO documents (document_id, file_path, file_hash, doc_type, reconciliation_status) "
            "VALUES ('d1', 'x', 'h1', 'expense_sheet', 'NO_TOTALS')"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, document_id, date_plausible, amount, currency, "
            "direction, economic_type) VALUES ('t1', 'd1', 1, '10.00', 'INR', 'DEBIT', 'PURCHASE')"
        )
        conn.commit()
        conn.close()

        store = Store(path)  # must not raise, and must add the missing columns
        txns = store.all_transactions()
        assert len(txns) == 1
        assert txns[0].category_declared is None
        assert txns[0].account_name is None
        store.close()


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
