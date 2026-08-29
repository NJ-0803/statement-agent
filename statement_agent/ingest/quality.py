"""Decides whether native extraction is trustworthy enough, or a page needs vision OCR.

Tier 1 (pdf_native) runs on every PDF unconditionally — it's free and lossless
when it works. This module is the gate for tier 2: only pages that actually
look broken get rasterized and sent to the vision model, per the cost/accuracy
tradeoff (a vision call costs real money and time; it should not run on pages
that already parsed cleanly).
"""

from __future__ import annotations

from dataclasses import dataclass

from .pdf_native import PdfParseResult


@dataclass
class PageQuality:
    page_index: int
    needs_vision: bool
    reason: str


def assess(result: PdfParseResult, total_pages: int) -> list[PageQuality]:
    assessments: list[PageQuality] = []

    txns_by_page: dict[int, int] = {}
    for t in result.transactions:
        p = (t.source.page - 1) if t.source and t.source.page else 0
        txns_by_page[p] = txns_by_page.get(p, 0) + 1

    for page_idx in range(total_pages):
        text = result.page_texts.get(page_idx, "")
        char_count = len(text.strip())
        txn_count = txns_by_page.get(page_idx, 0)

        if char_count == 0:
            assessments.append(PageQuality(page_idx, True, "no extractable text — likely a scanned/image-only page"))
            continue

        # a page with substantial text but zero recognized transaction rows is
        # suspicious for a statement page (garbled table extraction), UNLESS
        # it's plausibly a cover/summary page (short, no obvious tabular density)
        line_count = text.count("\n") + 1
        if txn_count == 0 and line_count >= 6 and char_count > 200:
            assessments.append(
                PageQuality(page_idx, True, f"{line_count} lines of text but 0 parsed transactions — extraction likely failed")
            )
            continue

        assessments.append(PageQuality(page_idx, False, f"{txn_count} transaction(s) parsed natively"))

    return assessments
