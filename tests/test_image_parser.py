import os

from statement_agent.ingest.image_parser import SUPPORTED_IMAGE_EXTENSIONS, parse_image

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestImageParser:
    def test_supported_extensions(self):
        assert SUPPORTED_IMAGE_EXTENSIONS == {".jpg", ".jpeg", ".png"}

    def test_builds_document_shell_without_any_api_call(self):
        # parse_image() must never touch the network — it only builds the Document
        # shell; the actual vision call is a separate step owned by pipeline.py
        fixture = os.path.join(FIXTURES, "malformed_missing_headers.csv")  # any real file, content irrelevant here
        result = parse_image(fixture)
        assert result.document.file_path == fixture
        assert result.document.doc_type == "unknown"
        assert result.transactions == []
