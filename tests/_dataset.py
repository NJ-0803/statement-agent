"""One import of dataset_public/ per test run.

Rebuilding it per test dominated the suite's runtime. The folder is imported once into a template database;
each caller gets a private copy of that file and freshly loaded objects, so tests can still mutate what
they get without affecting each other.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from functools import lru_cache

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset_public")


@lru_cache(maxsize=1)
def template_path() -> str:
    from statement_agent.ingest.pipeline import ingest_folder
    from statement_agent.store import Store

    directory = tempfile.mkdtemp(prefix="statement-agent-dataset-")
    path = os.path.join(directory, "dataset.db")
    store = Store(path)
    try:
        ingest_folder(DATASET, store, attempt_vision=False)
    finally:
        store.close()
    return path


def copy_ledger_db(target: str) -> str:
    shutil.copyfile(template_path(), target)
    return target


def dataset_ledger():
    """Fresh Transaction objects for the whole sample folder."""
    from statement_agent.store import Store

    with tempfile.TemporaryDirectory() as d:
        store = Store(copy_ledger_db(os.path.join(d, "l.db")))
        try:
            return store.all_transactions()
        finally:
            store.close()


def dataset_ledger_and_documents():
    from statement_agent.store import Store

    with tempfile.TemporaryDirectory() as d:
        store = Store(copy_ledger_db(os.path.join(d, "l.db")))
        try:
            return store.all_transactions(), store.all_documents_as_dicts()
        finally:
            store.close()
