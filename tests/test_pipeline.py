import os
import tempfile

from statement_agent.ingest.pipeline import ingest_folder
from statement_agent.store import Store

DATASET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset_public")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return Store(path), path


class TestFullFolderIngestion:
    def test_ingests_every_supported_file(self):
        store, path = _fresh_store()
        try:
            reports = ingest_folder(DATASET, store, attempt_vision=False)
            statuses = {r.file_path: r.status for r in reports}
            ingested = [s for s in statuses.values() if s == "ingested"]
            assert len(ingested) == 7  # 5 PDFs + 2 CSVs

            readme_reports = [r for r in reports if r.file_path.endswith("README.md")]
            assert readme_reports and readme_reports[0].status == "skipped_unsupported"
        finally:
            store.close()
            os.remove(path)

    def test_reingesting_same_folder_is_idempotent(self):
        store, path = _fresh_store()
        try:
            ingest_folder(DATASET, store, attempt_vision=False)
            count_first = len(store.all_transactions())
            reports = ingest_folder(DATASET, store, attempt_vision=False)
            count_second = len(store.all_transactions())

            assert count_first == count_second
            assert all(r.status in ("skipped_duplicate", "skipped_unsupported") for r in reports)
        finally:
            store.close()
            os.remove(path)

    def test_vision_disabled_does_not_crash_on_scanned_pdf(self):
        store, path = _fresh_store()
        try:
            reports = ingest_folder(DATASET, store, attempt_vision=False)
            scanned = next(r for r in reports if "axis_bank" in r.file_path)
            assert scanned.status == "ingested"  # ingestion succeeds even though it yields 0 transactions
            assert scanned.transaction_count == 0
            assert any("vision was disabled" in w for w in scanned.warnings)
        finally:
            store.close()
            os.remove(path)


class TestXlsxAndImageIngestion:
    def test_xlsx_and_image_both_discovered_as_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            shutil.copy(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"), os.path.join(tmp, "expenses.xlsx"))
            shutil.copy(os.path.join(FIXTURES, "sample_statement_image.png"), os.path.join(tmp, "statement.png"))

            store, path = _fresh_store()
            try:
                reports = ingest_folder(tmp, store, attempt_vision=False)
                assert len(reports) == 2
                assert all(r.status != "skipped_unsupported" for r in reports)
            finally:
                store.close()
                os.remove(path)

    def test_xlsx_transactions_actually_ingested(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            shutil.copy(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"), os.path.join(tmp, "expenses.xlsx"))

            store, path = _fresh_store()
            try:
                reports = ingest_folder(tmp, store, attempt_vision=False)
                assert reports[0].status == "ingested"
                assert reports[0].transaction_count == 4
                assert len(store.all_transactions()) == 4
            finally:
                store.close()
                os.remove(path)

    def test_image_without_vision_ingests_with_zero_transactions_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            shutil.copy(os.path.join(FIXTURES, "sample_statement_image.png"), os.path.join(tmp, "statement.png"))

            store, path = _fresh_store()
            try:
                reports = ingest_folder(tmp, store, attempt_vision=False)
                assert reports[0].status == "ingested"
                assert reports[0].transaction_count == 0
                assert any("vision was disabled" in w for w in reports[0].warnings)
            finally:
                store.close()
                os.remove(path)

    def test_xlsx_is_idempotent_on_reingestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            shutil.copy(os.path.join(FIXTURES, "personal_expenses_sample.xlsx"), os.path.join(tmp, "expenses.xlsx"))

            store, path = _fresh_store()
            try:
                ingest_folder(tmp, store, attempt_vision=False)
                count_first = len(store.all_transactions())
                reports = ingest_folder(tmp, store, attempt_vision=False)
                assert reports[0].status == "skipped_duplicate"
                assert len(store.all_transactions()) == count_first
            finally:
                store.close()
                os.remove(path)


class TestSingleBadFileDoesNotAbortFolder:
    def test_folder_with_one_malformed_csv_still_ingests_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            shutil.copy(
                os.path.join(FIXTURES, "malformed_missing_headers.csv"),
                os.path.join(tmp, "malformed_missing_headers.csv"),
            )
            shutil.copy(
                os.path.join(DATASET, "expenses", "personal_expenses_q2_2025.csv"),
                os.path.join(tmp, "personal_expenses_q2_2025.csv"),
            )

            store, path = _fresh_store()
            try:
                reports = ingest_folder(tmp, store, attempt_vision=False)
                assert len(reports) == 2
                good = next(r for r in reports if "personal_expenses" in r.file_path)
                bad = next(r for r in reports if "malformed_missing_headers" in r.file_path)
                assert good.status == "ingested" and good.transaction_count == 6
                assert bad.status == "ingested" and bad.transaction_count == 0  # rejected gracefully, not crashed
            finally:
                store.close()
                os.remove(path)
