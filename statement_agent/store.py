"""SQLite-backed ledger. Amounts are stored as TEXT (Decimal-safe), never REAL/float —
binary floating point cannot represent 0.10 exactly, which is unacceptable for money.

File-hash uniqueness on `documents` makes re-ingestion idempotent: running the
pipeline twice on an unchanged folder never double-inserts a statement (EC:
duplicate statement file / re-running ingestion should not double the ledger).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from .schema import Direction, Document, EconomicType, ExtractionMethod, SourceRef, Transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    doc_type TEXT NOT NULL,
    account_label TEXT,
    currency_declared TEXT,
    statement_start TEXT,
    statement_end TEXT,
    opening_balance TEXT,
    closing_balance TEXT,
    stated_total_debits TEXT,
    stated_total_credits TEXT,
    reconciliation_status TEXT NOT NULL,
    reconciliation_delta TEXT,
    parse_warnings TEXT  -- newline-joined
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    transaction_date TEXT,
    date_raw TEXT,
    date_plausible INTEGER NOT NULL,
    description_raw TEXT,
    merchant_raw TEXT,
    merchant_normalized TEXT,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_raw TEXT,
    direction TEXT NOT NULL,
    economic_type TEXT NOT NULL,
    economic_type_confidence REAL,
    category TEXT,
    category_confidence REAL,
    source_file_path TEXT,
    source_page INTEGER,
    source_row INTEGER,
    source_raw_text TEXT,
    extraction_method TEXT,
    extraction_confidence REAL,
    duplicate_of TEXT,
    duplicate_reason TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_txn_doc ON transactions(document_id);
CREATE INDEX IF NOT EXISTS idx_txn_economic_type ON transactions(economic_type);
"""


def _dec(v) -> str | None:
    return str(v) if v is not None else None


def _undec(v) -> Decimal | None:
    return Decimal(v) if v is not None else None


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def has_document(self, file_hash: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM documents WHERE file_hash = ?", (file_hash,)).fetchone()
        return row is not None

    def upsert_document(self, doc: Document) -> None:
        self.conn.execute(
            """
            INSERT INTO documents (document_id, file_path, file_hash, doc_type, account_label,
                currency_declared, statement_start, statement_end, opening_balance, closing_balance,
                stated_total_debits, stated_total_credits, reconciliation_status, reconciliation_delta,
                parse_warnings)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_hash) DO UPDATE SET
                doc_type=excluded.doc_type, account_label=excluded.account_label,
                currency_declared=excluded.currency_declared, statement_start=excluded.statement_start,
                statement_end=excluded.statement_end, opening_balance=excluded.opening_balance,
                closing_balance=excluded.closing_balance, stated_total_debits=excluded.stated_total_debits,
                stated_total_credits=excluded.stated_total_credits,
                reconciliation_status=excluded.reconciliation_status,
                reconciliation_delta=excluded.reconciliation_delta, parse_warnings=excluded.parse_warnings
            """,
            (
                doc.document_id, doc.file_path, doc.file_hash, doc.doc_type, doc.account_label,
                doc.currency_declared,
                doc.statement_start.isoformat() if doc.statement_start else None,
                doc.statement_end.isoformat() if doc.statement_end else None,
                _dec(doc.opening_balance), _dec(doc.closing_balance),
                _dec(doc.stated_total_debits), _dec(doc.stated_total_credits),
                doc.reconciliation_status, _dec(doc.reconciliation_delta),
                "\n".join(doc.parse_warnings),
            ),
        )
        self.conn.commit()

    def insert_transactions(self, txns: list[Transaction]) -> None:
        rows = []
        for t in txns:
            src = t.source
            rows.append((
                t.transaction_id, t.document_id,
                t.transaction_date.isoformat() if t.transaction_date else None,
                t.date_raw, int(t.date_plausible),
                t.description_raw, t.merchant_raw, t.merchant_normalized,
                _dec(t.amount), t.currency, t.amount_raw, t.direction.value,
                t.economic_type.value, t.economic_type_confidence,
                t.category, t.category_confidence,
                src.file_path if src else None, src.page if src else None, src.row if src else None,
                src.raw_text if src else None, src.extraction_method.value if src else None,
                src.extraction_confidence if src else None,
                t.duplicate_of, t.duplicate_reason, t.notes,
            ))
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO transactions (
                transaction_id, document_id, transaction_date, date_raw, date_plausible,
                description_raw, merchant_raw, merchant_normalized, amount, currency, amount_raw, direction,
                economic_type, economic_type_confidence, category, category_confidence,
                source_file_path, source_page, source_row, source_raw_text, extraction_method,
                extraction_confidence, duplicate_of, duplicate_reason, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        self.conn.commit()

    def delete_transactions_for_document(self, document_id: str) -> None:
        self.conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
        self.conn.commit()

    def all_transactions(self) -> list[Transaction]:
        rows = self.conn.execute("SELECT * FROM transactions").fetchall()
        return [_row_to_transaction(r) for r in rows]

    def all_documents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents").fetchall()

    def all_documents_as_dicts(self) -> list[dict]:
        """Plain-dict form, decoupled from sqlite3.Row — for handing to the agent tools layer."""
        return [dict(r) for r in self.all_documents()]

    def update_transaction_fields(self, transaction_id: str, **fields) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [transaction_id]
        self.conn.execute(f"UPDATE transactions SET {set_clause} WHERE transaction_id = ?", values)
        self.conn.commit()


def _row_to_transaction(r: sqlite3.Row) -> Transaction:
    return Transaction(
        transaction_id=r["transaction_id"],
        document_id=r["document_id"],
        transaction_date=date.fromisoformat(r["transaction_date"]) if r["transaction_date"] else None,
        date_raw=r["date_raw"] or "",
        date_plausible=bool(r["date_plausible"]),
        description_raw=r["description_raw"] or "",
        merchant_raw=r["merchant_raw"],
        merchant_normalized=r["merchant_normalized"],
        amount=_undec(r["amount"]) or Decimal("0"),
        currency=r["currency"],
        amount_raw=r["amount_raw"] or "",
        direction=Direction(r["direction"]),
        economic_type=EconomicType(r["economic_type"]),
        economic_type_confidence=r["economic_type_confidence"] if r["economic_type_confidence"] is not None else 1.0,
        category=r["category"],
        category_confidence=r["category_confidence"],
        source=SourceRef(
            file_path=r["source_file_path"] or "",
            file_hash="",
            page=r["source_page"],
            row=r["source_row"],
            raw_text=r["source_raw_text"] or "",
            extraction_method=ExtractionMethod(r["extraction_method"]) if r["extraction_method"] else ExtractionMethod.NATIVE_TEXT,
            extraction_confidence=r["extraction_confidence"] if r["extraction_confidence"] is not None else 1.0,
        ),
        duplicate_of=r["duplicate_of"],
        duplicate_reason=r["duplicate_reason"],
        notes=r["notes"] or "",
    )
