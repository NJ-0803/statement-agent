"""Standalone financial-document images — a photographed or screenshotted
statement page, not embedded in a PDF. There's no native text layer to try
first for a bare image, so this always routes through vision OCR
(pdf_vision.vision_extract_standalone_image) rather than attempting anything
native.

This module only builds the Document shell — it deliberately makes no API
call itself, matching the pattern in every other parser (the *_native.py /
csv/xlsx parsers own extraction; pipeline.py owns deciding when to call
pdf_vision, so a missing/invalid API key never breaks an import or a unit
test that doesn't need live vision).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .csv_parser import file_hash
from ..schema import Document, Transaction

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@dataclass
class ImageParseResult:
    document: Document
    transactions: list[Transaction] = field(default_factory=list)


def parse_image(path: str) -> ImageParseResult:
    fhash = file_hash(path)
    document = Document(
        document_id=str(uuid.uuid4()),
        file_path=path,
        file_hash=fhash,
        doc_type="unknown",  # not determinable without reading the image content
        currency_declared=None,
        reconciliation_status="NOT_CHECKED",
    )
    return ImageParseResult(document=document, transactions=[])
