"""Shared test setup.

Many tests start from the same thing: the whole `dataset_public/` folder imported into a fresh ledger.
Doing that per test dominated the suite's runtime (about 0.3s each, dozens of times), so it's done once per
session and copied into each test's own database file. The copy is a byte-for-byte SQLite file copy, so
each test still gets an isolated, writable ledger.
"""

import shutil

import pytest

from statement_agent.ingest import pipeline
from statement_agent.store import Store


@pytest.fixture(scope="session")
def dataset_ledger_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("template") / "dataset.db"
    store = Store(str(path))
    reports = pipeline.ingest_folder("dataset_public", store, attempt_vision=False)
    store.close()
    return str(path), reports


@pytest.fixture
def dataset_db(dataset_ledger_template, tmp_path):
    """Path to a ready-made ledger holding dataset_public/, private to this test."""
    template, _ = dataset_ledger_template
    path = str(tmp_path / "ledger.db")
    shutil.copyfile(template, path)
    return path


@pytest.fixture
def dataset_store(dataset_db):
    store = Store(dataset_db)
    yield store
    store.close()
