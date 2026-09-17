"""Reads a text PDF statement by column position, the way a person reads its table.

The old PDF reader took each line's first date and its *last* number as the amount, which silently picks up
the running balance on any statement with a Balance column (EC-12) and can't tell a withdrawal column
from a deposit column. This module finds the table's header line (a line whose parts are named like a
date column and a money column), takes each column's position from where its header sits, and places
every word of every following line into its column. The result is an ordinary TableCandidate, so the
PDF then goes through exactly the same column mapping, validation, balance checks and review as a
spreadsheet.

Numbers are placed by their right edge (statements right-align money); text by its left edge. Lines far
below the table (footers, notices) are not treated as table rows, so a notice can never be glued onto the
last transaction's description. Pages whose text runs sideways are turned upright first. Returns None
when no header is found; the line-based reader is then used as before.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from ..normalize import DocumentDateResolver, is_date_plausible
from .sniff import TableCandidate
from .vocab import header_role_scores, looks_like_amount

_HEADER_GAP = 7.0  # words closer than this (pt) belong to the same header cell
_LINE_TOLERANCE = 3.0
_MONEY_TOKEN_RE = re.compile(r"^(?:[-(]?[\d,]+(?:\.\d+)?\)?-?|CR|DR|Cr|Dr|Rs\.?|INR|USD|EUR|GBP|₹|\$|€|£)$")
_PERIOD_RE = re.compile(
    r"(?:Period|Statement\s+(?:period|from))\s*:?\s*(.+?)\s*(?:-|to|–)\s*(\d{1,2}[\s/.-]\w+[\s/.-]\d{2,4})", re.IGNORECASE
)
_INJECTION_KEYWORDS = ("ignore all previous", "ignore previous instructions", "disregard any", "disregard all",
                       "automated processing notice", "system prompt", "you are now")


@dataclass
class _Word:
    x0: float
    x1: float
    top: float
    bottom: float
    text: str


@dataclass
class _Column:
    header: str
    x0: float
    x1: float
    money: bool


def _page_words(page) -> list[_Word]:
    """Words in reading orientation. If most text on the page runs vertically, coordinates are rotated so
    the text reads left to right."""
    words = page.get_text("words")
    dirs = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            dirs.append(tuple(round(v) for v in line.get("dir", (1, 0))))
    dominant = statistics.mode(dirs) if dirs else (1, 0)
    out = []
    for x0, y0, x1, y1, text, *_ in words:
        if dominant == (0, 1):  # text runs downward: rotate 90° counter-clockwise
            x0, y0, x1, y1 = y0, -x1, y1, -x0
        elif dominant == (0, -1):  # text runs upward: rotate 90° clockwise
            x0, y0, x1, y1 = -y1, x0, -y0, x1
        elif dominant == (-1, 0):  # upside down
            x0, y0, x1, y1 = -x1, -y1, -x0, -y0
        out.append(_Word(x0, x1, y0, y1, text))
    return out


def _lines(words: list[_Word]) -> list[list[_Word]]:
    lines: list[list[_Word]] = []
    for w in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        mid = (w.top + w.bottom) / 2
        if lines and abs((lines[-1][0].top + lines[-1][0].bottom) / 2 - mid) <= _LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def _cells(line: list[_Word], gap: float = _HEADER_GAP) -> list[tuple[float, float, str]]:
    cells: list[list[_Word]] = []
    for w in line:
        if cells and w.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(w)
        else:
            cells.append([w])
    return [(c[0].x0, c[-1].x1, " ".join(w.text for w in c)) for c in cells]


def _header_columns(line: list[_Word]) -> list[_Column] | None:
    cells = _cells(line)
    if len(cells) < 2 or any(any(ch.isdigit() for ch in text) and looks_like_amount(text) for _, _, text in cells):
        return None
    roles = [header_role_scores(text) for _, _, text in cells]
    has_date = any(max(r.get("date", 0), r.get("value_date", 0)) >= 0.8 for r in roles)
    has_money = any(max(r.get(k, 0) for k in ("amount", "debit", "credit")) >= 0.8 for r in roles)
    if not (has_date and has_money):
        return None
    money_roles = ("amount", "debit", "credit", "balance")
    return [
        _Column(text, x0, x1, money=max((r.get(k, 0) for k in money_roles), default=0) >= 0.55)
        for (x0, x1, text), r in zip(cells, roles)
    ]


def _is_money_word(text: str) -> bool:
    return bool(_MONEY_TOKEN_RE.match(text))


def _assign(line: list[_Word], columns: list[_Column]) -> list[str]:
    bounds = [(columns[i].x1 + columns[i + 1].x0) / 2 for i in range(len(columns) - 1)]
    parts: list[list[str]] = [[] for _ in columns]
    for w in line:
        j = None
        if _is_money_word(w.text):
            # right-aligned: the money column whose header's right edge is nearest this word's right edge
            candidates = [(abs(c.x1 - w.x1), i) for i, c in enumerate(columns) if c.money]
            near = min(candidates, default=None)
            if near is not None and near[0] <= max(45.0, (columns[near[1]].x1 - columns[near[1]].x0) * 1.5):
                j = near[1]
            elif near is not None and w.text.upper() in ("CR", "DR"):
                j = near[1]
        if j is None:
            j = sum(1 for b in bounds if w.x0 >= b)
        parts[j].append(w.text)
    return [" ".join(p) for p in parts]


def _remap(cells: list[str], columns: list[_Column], reference: list[_Column]) -> list[str]:
    """A later page's own header may sit at slightly different positions; line its cells up with the first
    page's columns by header name (or position, if names differ)."""
    if len(columns) == len(reference):
        return cells
    out = [""] * len(reference)
    for text, col in zip(cells, columns):
        centre = (col.x0 + col.x1) / 2
        k = min(range(len(reference)), key=lambda i: abs((reference[i].x0 + reference[i].x1) / 2 - centre))
        out[k] = f"{out[k]} {text}".strip()
    return out


def read_pdf_table(path: str) -> TableCandidate | None:
    import pymupdf

    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            return None
        pages = [(i, _lines(_page_words(page))) for i, page in enumerate(doc)]

    reference: list[_Column] | None = None
    columns: list[_Column] | None = None
    preamble: list[tuple[int, list[str]]] = []
    rows: list[tuple[int, list[str]]] = []
    row_pages: dict[int, int] = {}
    outside: list[str] = []  # text not in the table (above the first header, between pages, footers)
    all_text: list[str] = []
    n = 0

    for page_index, lines in pages:
        before_first_table = reference is None
        page_header = None
        for k, line in enumerate(lines[:60]):
            cols = _header_columns(line)
            if cols:
                page_header = k
                columns = cols
                reference = reference or cols
                break
        pitches = []
        last_mid = None
        for k, line in enumerate(lines):
            text = " ".join(w.text for w in line)
            all_text.append(text)
            n += 1
            if columns is None or (page_header is not None and k < page_header):
                if before_first_table:
                    preamble.append((n, [text]))
                else:
                    outside.append(text)
                continue
            if k == page_header:
                continue
            mid = (line[0].top + line[0].bottom) / 2
            if last_mid is not None:
                gap = mid - last_mid
                typical = statistics.median(pitches) if pitches else gap
                if pitches and gap > max(2.2 * typical, typical + 20):
                    # far below the table: a footer or notice, never a continuation of the last row
                    outside.extend(" ".join(w.text for w in l) for l in lines[k:])
                    all_text.extend(" ".join(w.text for w in l) for l in lines[k + 1:])
                    break
                pitches.append(gap)
            last_mid = mid
            cells = _remap(_assign(line, columns), columns, reference)
            rows.append((n, cells))
            row_pages[n] = page_index + 1

    if reference is None or not rows:
        return None
    table = TableCandidate(
        sheet=None, header_row=0, headers=[c.header for c in reference], rows=rows, preamble=preamble,
        score=1.0, evidence=[f"PDF table read by column position ({len(reference)} columns)"],
        row_pages=row_pages, document_hints=_document_hints(all_text, outside),
    )
    return table


def _document_hints(all_text: list[str], outside: list[str]) -> dict:
    full = "\n".join(all_text)
    lowered = full.lower()
    hints: dict = {"outside_lines": outside, "warnings": []}
    from .pdf_native import _classify_doc_type  # one classifier for both PDF readers

    doc_type = _classify_doc_type(full)
    hints["doc_type"] = None if doc_type == "unknown" else doc_type
    m = _PERIOD_RE.search(full)
    if m:
        resolver = DocumentDateResolver()
        for g in (m.group(1), m.group(2)):
            resolver.observe(g)
        resolver.resolve_convention()
        hints["statement_start"] = resolver.parse(m.group(1)).value
        hints["statement_end"] = resolver.parse(m.group(2)).value
    acct = re.search(r"(Card ending\s*\d+|Account\s*(?:No\.?|Number)?\s*:?\s*[\dX*]{4,})", full, re.IGNORECASE)
    hints["account_label"] = acct.group(1) if acct else None
    for kw in _INJECTION_KEYWORDS:
        if kw in lowered:
            hints["warnings"].append(
                f"SECURITY: instruction-like text detected in document body (matched {kw!r}); "
                "treated as inert content, not parsed as a transaction or followed as an instruction"
            )
            break
    return hints


def apply_document_hints(document, transactions, hints: dict) -> None:
    from .statement_totals import apply_to_document, read_figures

    if hints.get("doc_type"):
        document.doc_type = hints["doc_type"]
    document.account_label = document.account_label or hints.get("account_label")
    document.statement_start = hints.get("statement_start")
    document.statement_end = hints.get("statement_end")
    document.parse_warnings.extend(hints.get("warnings", []))
    figures = read_figures(hints.get("outside_lines", []))
    for name, attr in (("opening", "opening_balance"), ("closing", "closing_balance")):
        if getattr(document, attr) is not None:
            figures.values.pop(name, None)
    apply_to_document(document, figures)
    for t in transactions:
        if t.transaction_date and (document.statement_start or document.statement_end):
            t.date_plausible = is_date_plausible(t.transaction_date, statement_start=document.statement_start,
                                                 statement_end=document.statement_end)
            if not t.date_plausible:
                t.notes = (t.notes + " | date outside plausible statement range — excluded from totals until reviewed").strip(" |")
