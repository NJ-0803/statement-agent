"""SQLite-backed ledger. Amounts are stored as TEXT (Decimal-safe), never REAL/float —
binary floating point cannot represent 0.10 exactly, which is unacceptable for money.

File-hash uniqueness on `documents` makes re-ingestion idempotent: running the
pipeline twice on an unchanged folder never double-inserts a statement (EC:
duplicate statement file / re-running ingestion should not double the ledger).

Staged imports live here too: `import_jobs` (state + the staged analysis as JSON, so a review can
span several browser requests) and `mapping_profiles` (column layouts a person confirmed, keyed by
header fingerprint). `commit_import` and `rollback_import` each run as ONE database transaction —
an import lands completely or not at all, and undoing one never touches another's rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

from .schema import (
    Direction, Document, EconomicType, ExtractionMethod, ImportJob, ImportState, SourceRef, Transaction,
)

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
    extraction_sequence INTEGER,
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
    category_declared TEXT,
    account_name TEXT,
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

CREATE TABLE IF NOT EXISTS import_jobs (
    job_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    state TEXT NOT NULL,
    original_filename TEXT,
    stored_path TEXT,
    file_hash TEXT,
    file_kind TEXT,
    parser_version TEXT,
    created_at TEXT,
    updated_at TEXT,
    error_summary TEXT,
    document_id TEXT,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    mapping_fingerprint TEXT,
    staging_json TEXT
);

CREATE TABLE IF NOT EXISTS mapping_profiles (
    fingerprint TEXT PRIMARY KEY,
    mapping_json TEXT NOT NULL,
    confirmations INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_confirmed_at TEXT
);
"""


def _dec(v) -> str | None:
    return str(v) if v is not None else None


def _undec(v) -> Decimal | None:
    return Decimal(v) if v is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after a ledger.db may already exist on disk — CREATE TABLE IF NOT EXISTS
# never adds a column to an existing table, so a real, already-ingested ledger needs an
# explicit migration rather than requiring a --fresh re-ingest every time the schema grows.
_COLUMN_MIGRATIONS = {
    "transactions": [
        ("category_declared", "TEXT"),
        ("account_name", "TEXT"),
        ("value_date", "TEXT"),
        ("reference_id", "TEXT"),
        ("balance_after", "TEXT"),
        ("field_confidence", "TEXT"),  # JSON object
        ("import_job_id", "TEXT"),
        ("field_reasons", "TEXT"),  # JSON object
    ],
    "documents": [
        ("import_job_id", "TEXT"),
        ("reconciliation_detail", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_import_job ON transactions(import_job_id)")


class DuplicateDocument(Exception):
    """The same file bytes were committed by another import in the meantime."""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # timeout: the web app's background import worker and request threads each hold their own
        # connection; a short write lock from one must make the other wait, not fail
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        _migrate(self.conn)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def has_document(self, file_hash: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM documents WHERE file_hash = ?", (file_hash,)).fetchone()
        return row is not None

    # -- documents & transactions -------------------------------------------------

    def _write_document(self, doc: Document, import_job_id: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO documents (document_id, file_path, file_hash, doc_type, account_label,
                currency_declared, statement_start, statement_end, opening_balance, closing_balance,
                stated_total_debits, stated_total_credits, reconciliation_status, reconciliation_delta,
                parse_warnings, import_job_id, reconciliation_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_hash) DO UPDATE SET
                doc_type=excluded.doc_type, account_label=excluded.account_label,
                currency_declared=excluded.currency_declared, statement_start=excluded.statement_start,
                statement_end=excluded.statement_end, opening_balance=excluded.opening_balance,
                closing_balance=excluded.closing_balance, stated_total_debits=excluded.stated_total_debits,
                stated_total_credits=excluded.stated_total_credits,
                reconciliation_status=excluded.reconciliation_status,
                reconciliation_delta=excluded.reconciliation_delta, parse_warnings=excluded.parse_warnings,
                reconciliation_detail=excluded.reconciliation_detail
            """,
            (
                doc.document_id, doc.file_path, doc.file_hash, doc.doc_type, doc.account_label,
                doc.currency_declared,
                doc.statement_start.isoformat() if doc.statement_start else None,
                doc.statement_end.isoformat() if doc.statement_end else None,
                _dec(doc.opening_balance), _dec(doc.closing_balance),
                _dec(doc.stated_total_debits), _dec(doc.stated_total_credits),
                doc.reconciliation_status, _dec(doc.reconciliation_delta),
                "\n".join(doc.parse_warnings), import_job_id, doc.reconciliation_detail or None,
            ),
        )

    def _write_transactions(self, txns: list[Transaction]) -> None:
        rows = []
        for t in txns:
            src = t.source
            rows.append((
                t.transaction_id, t.document_id,
                t.transaction_date.isoformat() if t.transaction_date else None,
                t.date_raw, int(t.date_plausible), t.extraction_sequence,
                t.description_raw, t.merchant_raw, t.merchant_normalized,
                _dec(t.amount), t.currency, t.amount_raw, t.direction.value,
                t.economic_type.value, t.economic_type_confidence,
                t.category, t.category_confidence, t.category_declared, t.account_name,
                src.file_path if src else None, src.page if src else None, src.row if src else None,
                src.raw_text if src else None, src.extraction_method.value if src else None,
                src.extraction_confidence if src else None,
                t.duplicate_of, t.duplicate_reason, t.notes,
                t.value_date.isoformat() if t.value_date else None, t.reference_id, _dec(t.balance_after),
                json.dumps(t.field_confidence) if t.field_confidence else None, t.import_job_id,
                json.dumps(t.field_reasons) if t.field_reasons else None,
            ))
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO transactions (
                transaction_id, document_id, transaction_date, date_raw, date_plausible, extraction_sequence,
                description_raw, merchant_raw, merchant_normalized, amount, currency, amount_raw, direction,
                economic_type, economic_type_confidence, category, category_confidence,
                category_declared, account_name,
                source_file_path, source_page, source_row, source_raw_text, extraction_method,
                extraction_confidence, duplicate_of, duplicate_reason, notes,
                value_date, reference_id, balance_after, field_confidence, import_job_id, field_reasons
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    def upsert_document(self, doc: Document) -> None:
        self._write_document(doc)
        self.conn.commit()

    def insert_transactions(self, txns: list[Transaction]) -> None:
        self._write_transactions(txns)
        self.conn.commit()

    def delete_transactions_for_document(self, document_id: str) -> None:
        self.conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
        self.conn.commit()

    def all_transactions(self) -> list[Transaction]:
        rows = self.conn.execute("SELECT * FROM transactions").fetchall()
        return [_row_to_transaction(r) for r in rows]

    def transactions_for_import(self, job_id: str) -> list[Transaction]:
        rows = self.conn.execute("SELECT * FROM transactions WHERE import_job_id = ?", (job_id,)).fetchall()
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

    # -- import jobs ------------------------------------------------------------------

    def save_job(self, job: ImportJob, staging: dict | None = None) -> None:
        job.updated_at = _now()
        job.created_at = job.created_at or job.updated_at
        values = (
            job.job_id, job.owner, ImportState(job.state).value, job.original_filename, job.stored_path, job.file_hash,
            job.file_kind, job.parser_version, job.created_at, job.updated_at, job.error_summary, job.document_id,
            job.transaction_count, job.mapping_fingerprint,
        )
        self.conn.execute(
            """
            INSERT INTO import_jobs (job_id, owner, state, original_filename, stored_path, file_hash, file_kind,
                parser_version, created_at, updated_at, error_summary, document_id, transaction_count,
                mapping_fingerprint)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                state=excluded.state, stored_path=excluded.stored_path, file_hash=excluded.file_hash,
                file_kind=excluded.file_kind, parser_version=excluded.parser_version,
                updated_at=excluded.updated_at, error_summary=excluded.error_summary,
                document_id=excluded.document_id, transaction_count=excluded.transaction_count,
                mapping_fingerprint=excluded.mapping_fingerprint
            """,
            values,
        )
        if staging is not None:
            self.conn.execute(
                "UPDATE import_jobs SET staging_json = ? WHERE job_id = ?", (json.dumps(staging, default=str), job.job_id)
            )
        self.conn.commit()

    def get_job(self, job_id: str) -> ImportJob | None:
        row = self.conn.execute("SELECT * FROM import_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def get_staging(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT staging_json FROM import_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return json.loads(row["staging_json"]) if row and row["staging_json"] else None

    def list_jobs(self, limit: int = 50) -> list[ImportJob]:
        rows = self.conn.execute(
            "SELECT * FROM import_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def fail_interrupted_jobs(self) -> int:
        """A job left 'analyzing' by a process that died will never finish on its own."""
        cur = self.conn.execute(
            "UPDATE import_jobs SET state = ?, error_summary = ?, updated_at = ? WHERE state IN (?, ?)",
            (ImportState.FAILED.value, "Reading this file was interrupted. Please try again.", _now(),
             ImportState.ANALYZING.value, ImportState.UPLOADED.value),
        )
        self.conn.commit()
        return cur.rowcount

    # -- mapping profiles ---------------------------------------------------------------

    def get_profile(self, fingerprint: str | None) -> dict | None:
        if not fingerprint:
            return None
        row = self.conn.execute("SELECT mapping_json FROM mapping_profiles WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return json.loads(row["mapping_json"]) if row else None

    def _write_profile(self, fingerprint: str, mapping: dict) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO mapping_profiles (fingerprint, mapping_json, confirmations, created_at, last_confirmed_at)
            VALUES (?,?,1,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET mapping_json=excluded.mapping_json,
                confirmations=mapping_profiles.confirmations + 1, last_confirmed_at=excluded.last_confirmed_at
            """,
            (fingerprint, json.dumps(mapping), now, now),
        )

    # -- atomic commit / rollback --------------------------------------------------------------

    def commit_import(self, job: ImportJob, document: Document, transactions: list[Transaction],
                      *, profile: dict | None = None) -> list[Transaction]:
        """Persist a reviewed import in one database transaction. Returns transactions from
        OTHER documents newly flagged as cross-document duplicates of this import's rows."""
        from .resolve import detect_cross_document_duplicates

        if not transactions:
            raise ValueError("an import with zero transactions can never be committed")
        try:
            with self.conn:
                if self.conn.execute("SELECT 1 FROM documents WHERE file_hash = ?", (document.file_hash,)).fetchone():
                    raise DuplicateDocument(document.file_hash)
                for t in transactions:
                    t.import_job_id = job.job_id
                self._write_document(document, import_job_id=job.job_id)
                self._write_transactions(transactions)
                ledger = [_row_to_transaction(r) for r in self.conn.execute("SELECT * FROM transactions").fetchall()]
                flagged = detect_cross_document_duplicates(ledger)
                if flagged:
                    self._write_transactions(flagged)
                if profile and profile.get("fingerprint"):
                    self._write_profile(profile["fingerprint"], profile)
                job.state = ImportState.COMMITTED
                job.document_id = document.document_id
                job.transaction_count = len(transactions)
                job.updated_at = _now()
                self.conn.execute(
                    "UPDATE import_jobs SET state=?, document_id=?, transaction_count=?, updated_at=?, error_summary=NULL WHERE job_id=?",
                    (job.state.value, job.document_id, job.transaction_count, job.updated_at, job.job_id),
                )
        except Exception:
            for t in transactions:
                t.import_job_id = None
            raise
        return flagged

    def rollback_import(self, job: ImportJob) -> int:
        """Remove exactly this import's document and rows. Duplicate flags that OTHER imports'
        rows carry pointing at the removed rows are cleared, so no dangling reference survives."""
        with self.conn:
            ids = [r[0] for r in self.conn.execute(
                "SELECT transaction_id FROM transactions WHERE import_job_id = ?", (job.job_id,))]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"UPDATE transactions SET duplicate_of = NULL, duplicate_reason = NULL "
                    f"WHERE duplicate_of IN ({marks}) AND (import_job_id IS NULL OR import_job_id != ?)",
                    (*chunk, job.job_id),
                )
            self.conn.execute("DELETE FROM transactions WHERE import_job_id = ?", (job.job_id,))
            self.conn.execute("DELETE FROM documents WHERE import_job_id = ?", (job.job_id,))
            job.state = ImportState.ROLLED_BACK
            job.updated_at = _now()
            self.conn.execute("UPDATE import_jobs SET state=?, updated_at=? WHERE job_id=?",
                              (job.state.value, job.updated_at, job.job_id))
        return len(ids)


def _row_to_job(r: sqlite3.Row) -> ImportJob:
    return ImportJob(
        job_id=r["job_id"], state=ImportState(r["state"]), original_filename=r["original_filename"] or "",
        stored_path=r["stored_path"] or "", owner=r["owner"], file_hash=r["file_hash"], file_kind=r["file_kind"],
        parser_version=r["parser_version"] or "", created_at=r["created_at"] or "", updated_at=r["updated_at"] or "",
        error_summary=r["error_summary"], document_id=r["document_id"], transaction_count=r["transaction_count"] or 0,
        mapping_fingerprint=r["mapping_fingerprint"],
    )


def _row_to_transaction(r: sqlite3.Row) -> Transaction:
    keys = r.keys()
    return Transaction(
        transaction_id=r["transaction_id"],
        document_id=r["document_id"],
        transaction_date=date.fromisoformat(r["transaction_date"]) if r["transaction_date"] else None,
        date_raw=r["date_raw"] or "",
        date_plausible=bool(r["date_plausible"]),
        extraction_sequence=r["extraction_sequence"],
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
        category_declared=r["category_declared"],
        account_name=r["account_name"],
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
        value_date=date.fromisoformat(r["value_date"]) if "value_date" in keys and r["value_date"] else None,
        reference_id=r["reference_id"] if "reference_id" in keys else None,
        balance_after=_undec(r["balance_after"]) if "balance_after" in keys else None,
        field_confidence=json.loads(r["field_confidence"]) if "field_confidence" in keys and r["field_confidence"] else {},
        import_job_id=r["import_job_id"] if "import_job_id" in keys else None,
        field_reasons=json.loads(r["field_reasons"]) if "field_reasons" in keys and r["field_reasons"] else {},
    )


# -- JSON round-trip for staged (not yet committed) documents and transactions ----------------

def document_to_dict(doc: Document) -> dict:
    return {
        "document_id": doc.document_id, "file_path": doc.file_path, "file_hash": doc.file_hash,
        "doc_type": doc.doc_type, "account_label": doc.account_label, "currency_declared": doc.currency_declared,
        "statement_start": doc.statement_start.isoformat() if doc.statement_start else None,
        "statement_end": doc.statement_end.isoformat() if doc.statement_end else None,
        "opening_balance": _dec(doc.opening_balance), "closing_balance": _dec(doc.closing_balance),
        "stated_total_debits": _dec(doc.stated_total_debits), "stated_total_credits": _dec(doc.stated_total_credits),
        "reconciliation_status": doc.reconciliation_status, "reconciliation_delta": _dec(doc.reconciliation_delta),
        "parse_warnings": list(doc.parse_warnings), "reconciliation_detail": doc.reconciliation_detail,
    }


def document_from_dict(d: dict) -> Document:
    return Document(
        document_id=d["document_id"], file_path=d["file_path"], file_hash=d["file_hash"], doc_type=d["doc_type"],
        account_label=d.get("account_label"), currency_declared=d.get("currency_declared"),
        statement_start=date.fromisoformat(d["statement_start"]) if d.get("statement_start") else None,
        statement_end=date.fromisoformat(d["statement_end"]) if d.get("statement_end") else None,
        opening_balance=_undec(d.get("opening_balance")), closing_balance=_undec(d.get("closing_balance")),
        stated_total_debits=_undec(d.get("stated_total_debits")), stated_total_credits=_undec(d.get("stated_total_credits")),
        reconciliation_status=d.get("reconciliation_status", "NOT_CHECKED"),
        reconciliation_delta=_undec(d.get("reconciliation_delta")), parse_warnings=list(d.get("parse_warnings") or []),
        reconciliation_detail=d.get("reconciliation_detail") or "",
    )


def transaction_to_dict(t: Transaction) -> dict:
    src = t.source
    return {
        "transaction_id": t.transaction_id, "document_id": t.document_id,
        "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
        "date_raw": t.date_raw, "date_plausible": t.date_plausible, "extraction_sequence": t.extraction_sequence,
        "description_raw": t.description_raw, "merchant_raw": t.merchant_raw, "merchant_normalized": t.merchant_normalized,
        "amount": str(t.amount), "currency": t.currency, "amount_raw": t.amount_raw, "direction": t.direction.value,
        "economic_type": t.economic_type.value, "economic_type_confidence": t.economic_type_confidence,
        "category": t.category, "category_confidence": t.category_confidence, "category_declared": t.category_declared,
        "account_name": t.account_name, "duplicate_of": t.duplicate_of, "duplicate_reason": t.duplicate_reason,
        "notes": t.notes, "value_date": t.value_date.isoformat() if t.value_date else None,
        "reference_id": t.reference_id, "balance_after": _dec(t.balance_after),
        "field_confidence": dict(t.field_confidence), "field_reasons": dict(t.field_reasons),
        "import_job_id": t.import_job_id,
        "source": None if src is None else {
            "file_path": src.file_path, "file_hash": src.file_hash, "page": src.page, "row": src.row,
            "raw_text": src.raw_text, "extraction_method": src.extraction_method.value,
            "extraction_confidence": src.extraction_confidence,
        },
    }


def transaction_from_dict(d: dict) -> Transaction:
    src = d.get("source")
    return Transaction(
        transaction_id=d["transaction_id"], document_id=d["document_id"],
        transaction_date=date.fromisoformat(d["transaction_date"]) if d.get("transaction_date") else None,
        date_raw=d.get("date_raw", ""), date_plausible=d.get("date_plausible", True),
        extraction_sequence=d.get("extraction_sequence"), description_raw=d.get("description_raw", ""),
        merchant_raw=d.get("merchant_raw"), merchant_normalized=d.get("merchant_normalized"),
        amount=Decimal(d["amount"]), currency=d["currency"], amount_raw=d.get("amount_raw", ""),
        direction=Direction(d["direction"]), economic_type=EconomicType(d["economic_type"]),
        economic_type_confidence=d.get("economic_type_confidence", 1.0), category=d.get("category"),
        category_confidence=d.get("category_confidence"), category_declared=d.get("category_declared"),
        account_name=d.get("account_name"), duplicate_of=d.get("duplicate_of"),
        duplicate_reason=d.get("duplicate_reason"), notes=d.get("notes", ""),
        value_date=date.fromisoformat(d["value_date"]) if d.get("value_date") else None,
        reference_id=d.get("reference_id"), balance_after=_undec(d.get("balance_after")),
        field_confidence=dict(d.get("field_confidence") or {}), import_job_id=d.get("import_job_id"),
        field_reasons=dict(d.get("field_reasons") or {}),
        source=None if src is None else SourceRef(
            file_path=src["file_path"], file_hash=src.get("file_hash", ""), page=src.get("page"), row=src.get("row"),
            raw_text=src.get("raw_text", ""), extraction_method=ExtractionMethod(src["extraction_method"]),
            extraction_confidence=src.get("extraction_confidence", 1.0),
        ),
    )
