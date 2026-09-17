"""Readers that turn other financial file formats into plain grids (rows of text cells), so every format goes
through the same structure discovery, column mapping, validation and review as a CSV.

Spreadsheet-like formats (.xls, .ods, .docx and .html tables) become one grid per sheet/table. Record-based
formats (OFX/QFX, QIF, MT940, JSON, XML) become one grid with plain column names (Date, Description,
Amount, …) chosen so the ordinary column mapping recognises them, plus every other field the record had as
its own column — nothing is dropped. Balances a format states (OFX ledger balance, MT940 :60F:/:62F:) are
written as "Opening Balance" / "Closing Balance" rows above the header, where the tabular reader looks.

XML is parsed only after refusing DTDs and entity declarations, so a hostile file can't expand entities.
"""

from __future__ import annotations

import html.parser
import json
import re
import zipfile
from xml.etree import ElementTree as ET

from .sniff import MAX_TABLE_ROWS, TableTooLarge, cell_to_str, detect_encoding

TEXT_TABLE_EXTENSIONS = {".tsv", ".txt", ".tab", ".psv", ".dat"}
SHEET_EXTENSIONS = {".xls", ".ods", ".docx", ".html", ".htm"}
RECORD_EXTENSIONS = {".ofx", ".qfx", ".qif", ".sta", ".mt940", ".940", ".json", ".xml"}
EXTRA_TABULAR_EXTENSIONS = TEXT_TABLE_EXTENSIONS | SHEET_EXTENSIONS | RECORD_EXTENSIONS

Grid = list[list[str]]


class UnsafeFile(ValueError):
    pass


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    encoding, _ = detect_encoding(data)
    return data.decode(encoding, errors="replace")


def _check_size(rows: list) -> None:
    if len(rows) > MAX_TABLE_ROWS:
        raise TableTooLarge(f"more than {MAX_TABLE_ROWS:,} rows")


def _records_to_grid(records: list[dict], preferred: list[str], preamble: Grid | None = None) -> Grid:
    """Union of keys as columns, preferred ones first, in first-seen order otherwise."""
    _check_size(records)
    keys: list[str] = []
    for r in records:
        for k in r:
            if k not in keys:
                keys.append(k)
    ordered = [k for k in preferred if k in keys] + [k for k in keys if k not in preferred]
    grid = list(preamble or [])
    if preamble:
        grid.append([])
    grid.append(ordered)
    for r in records:
        grid.append([cell_to_str(r.get(k, "")) for k in ordered])
    return grid


# ---------------------------------------------------------------------------
# spreadsheet-like
# ---------------------------------------------------------------------------

def read_xls(path: str) -> list[tuple[str, str, Grid]]:
    import xlrd

    book = xlrd.open_workbook(path, on_demand=True)
    try:
        sheets = []
        for sheet in book.sheets():
            _check_size(range(sheet.nrows))
            grid = []
            for r in range(sheet.nrows):
                row = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        row.append(cell_to_str(xlrd.xldate_as_datetime(cell.value, book.datemode)))
                    elif cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer() and abs(cell.value) < 1e15:
                        row.append(str(int(cell.value)))
                    else:
                        row.append(cell_to_str(cell.value))
                grid.append(row)
            state = "visible" if getattr(sheet, "visibility", 0) == 0 else "hidden"
            sheets.append((sheet.name, state, grid))
        return sheets
    finally:
        book.release_resources()


_ODS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}


def _safe_xml(data: bytes) -> ET.Element:
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise UnsafeFile("This XML file declares a DTD or entities, so I didn't open it.")
    return ET.fromstring(data)


def read_ods(path: str) -> list[tuple[str, str, Grid]]:
    with zipfile.ZipFile(path) as z:
        root = _safe_xml(z.read("content.xml"))
    t, tx, of = _ODS["table"], _ODS["text"], _ODS["office"]
    sheets = []
    for table in root.iter(f"{{{t}}}table"):
        grid: Grid = []
        for row in table.iter(f"{{{t}}}table-row"):
            cells = []
            for cell in row:
                if cell.tag not in (f"{{{t}}}table-cell", f"{{{t}}}covered-table-cell"):
                    continue
                repeat = min(int(cell.get(f"{{{t}}}number-columns-repeated", "1")), 200)
                value = cell.get(f"{{{of}}}date-value") or cell.get(f"{{{of}}}value")
                if value is None:
                    value = "\n".join("".join(p.itertext()) for p in cell.iter(f"{{{tx}}}p"))
                cells.extend([value.strip()] * repeat)
            while cells and not cells[-1]:
                cells.pop()
            repeat_rows = min(int(row.get(f"{{{t}}}number-rows-repeated", "1")), 1 if not cells else 50)
            grid.extend([cells] * repeat_rows)
            _check_size(grid)
        while grid and not grid[-1]:
            grid.pop()
        sheets.append((table.get(f"{{{t}}}name", f"Sheet {len(sheets) + 1}"), "visible", grid))
    return sheets


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: str) -> list[tuple[str, str, Grid]]:
    with zipfile.ZipFile(path) as z:
        root = _safe_xml(z.read("word/document.xml"))
    sheets = []
    for n, tbl in enumerate(root.iter(f"{_W}tbl"), 1):
        grid = [
            [" ".join("".join(t.text or "" for t in p.iter(f"{_W}t")) for p in tc.iter(f"{_W}p")).strip()
             for tc in tr.iter(f"{_W}tc")]
            for tr in tbl.iter(f"{_W}tr")
        ]
        _check_size(grid)
        sheets.append((f"Table {n}", "visible", grid))
    return sheets


class _TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[Grid] = []
        self._stack: list[Grid] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def read_html(path: str) -> list[tuple[str, str, Grid]]:
    parser = _TableParser()
    parser.feed(_read_text(path))
    parser.close()
    return [(f"Table {n}", "visible", grid) for n, grid in enumerate(parser.tables, 1) if grid]


# ---------------------------------------------------------------------------
# record-based
# ---------------------------------------------------------------------------

_OFX_TAG_RE = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.IGNORECASE)


def _ofx_date(raw: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else raw


def read_ofx(path: str) -> Grid:
    text = _read_text(path)
    currency = (re.search(r"<CURDEF>\s*([A-Z]{3})", text, re.IGNORECASE) or [None, ""])[1].upper()
    names = {"DTPOSTED": "Date", "DTUSER": "Transaction date (user)", "DTAVAIL": "Value date", "TRNAMT": "Amount",
             "NAME": "Description", "MEMO": "Memo", "FITID": "Reference", "CHECKNUM": "Cheque number",
             "TRNTYPE": "Transaction kind", "SIC": "Merchant code", "PAYEEID": "Payee id", "REFNUM": "Reference number"}
    records = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, re.IGNORECASE | re.DOTALL):
        rec = {}
        for tag, value in _OFX_TAG_RE.findall(block):
            tag, value = tag.upper(), value.strip()
            if not value:
                continue
            key = names.get(tag, tag.title())
            rec[key] = _ofx_date(value) if tag.startswith("DT") else value
        if currency:
            rec.setdefault("Currency", currency)
        if rec:
            records.append(rec)
    preamble = []
    ledger = re.search(r"<LEDGERBAL>.*?<BALAMT>\s*([-\d.,]+).*?(?:<DTASOF>\s*(\d{8}))?", text, re.IGNORECASE | re.DOTALL)
    if ledger:
        preamble.append(["Closing Balance", ledger.group(1)])
    acct = re.search(r"<ACCTID>\s*([^<\r\n]+)", text, re.IGNORECASE)
    if acct:
        preamble.insert(0, ["Account No", acct.group(1).strip()])
    return _records_to_grid(records, ["Date", "Description", "Amount", "Currency", "Memo", "Reference"], preamble)


def _qif_date(raw: str) -> str:
    raw = raw.strip().replace(" ", "")
    m = re.match(r"^(\d{1,2})/(\d{1,2})'(\d{1,2})$", raw)  # 4/15'25 -> 4/15/2025 (Quicken's 2000s form)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{2000 + int(m.group(3))}"
    return raw.replace("'", "/")


def read_qif(path: str) -> Grid:
    names = {"D": "Date", "T": "Amount", "U": "Amount (alt)", "P": "Description", "M": "Memo", "N": "Cheque number",
             "L": "Category", "C": "Cleared", "A": "Address"}
    records, rec = [], {}
    for line in _read_text(path).splitlines():
        line = line.rstrip()
        if not line or line.startswith("!"):
            continue
        if line.startswith("^"):
            if rec:
                records.append(rec)
            rec = {}
            continue
        code, value = line[0], line[1:].strip()
        key = names.get(code, f"Field {code}")
        if code == "D":
            value = _qif_date(value)
        rec[key] = f"{rec[key]} {value}" if key in rec and code == "A" else value
    if rec:
        records.append(rec)
    return _records_to_grid(records, ["Date", "Description", "Amount", "Category", "Memo", "Cheque number"])


_MT940_61 = re.compile(
    r"^(?P<vdate>\d{6})(?P<edate>\d{4})?(?P<mark>R?[CD])(?P<fund>[A-Z])?(?P<amount>[\d,]+)"
    r"(?P<type>[A-Z][A-Z0-9]{3})?(?P<ref>[^/\n]*)(?://(?P<bankref>.*))?"
)


def _mt940_amount(raw: str) -> str:
    return raw.replace(",", ".")


def _mt940_date(yymmdd: str) -> str:
    return f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def read_mt940(path: str) -> Grid:
    text = _read_text(path)
    fields: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = re.match(r"^:(\d{2}[A-Z]?):(.*)$", line)
        if m:
            fields.append((m.group(1), m.group(2)))
        elif fields and line.strip() and not line.startswith("-}"):
            tag, value = fields[-1]
            fields[-1] = (tag, f"{value}\n{line.strip()}")
    records, preamble, currency = [], [], ""
    for tag, value in fields:
        if tag == "25":
            preamble.append(["Account No", value.strip()])
        elif tag in ("60F", "60M", "62F", "62M"):
            m = re.match(r"([CD])(\d{6})([A-Z]{3})([\d,]+)", value.strip())
            if m:
                currency = m.group(3)
                amount = ("-" if m.group(1) == "D" else "") + _mt940_amount(m.group(4))
                label = "Opening Balance" if tag.startswith("60") else "Closing Balance"
                if not any(p[0] == label for p in preamble):
                    preamble.append([label, amount])
        elif tag == "61":
            m = _MT940_61.match(value.split("\n")[0])
            if not m:
                continue
            mark = m.group("mark")
            sign = "-" if mark in ("D", "RC") else ""
            rec = {"Value date": _mt940_date(m.group("vdate")), "Date": _mt940_date(m.group("vdate")),
                   "Amount": sign + _mt940_amount(m.group("amount")), "Reference": (m.group("ref") or "").strip(),
                   "Transaction kind": m.group("type") or "", "Bank reference": (m.group("bankref") or "").strip()}
            if m.group("edate"):
                rec["Date"] = f"{rec['Value date'][:4]}-{m.group('edate')[:2]}-{m.group('edate')[2:]}"
            if currency:
                rec["Currency"] = currency
            records.append(rec)
        elif tag == "86" and records:
            info = " ".join(value.split())
            records[-1]["Description"] = re.sub(r"\?\d{2}", " ", info).strip()
    return _records_to_grid(records, ["Date", "Value date", "Description", "Amount", "Currency", "Reference"], preamble)


def _flatten(obj, prefix: str = "") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    elif isinstance(obj, list):
        if all(not isinstance(v, (dict, list)) for v in obj):
            out[prefix] = ", ".join(str(v) for v in obj)
        else:
            for i, v in enumerate(obj[:5]):
                out.update(_flatten(v, f"{prefix}.{i + 1}"))
    else:
        out[prefix] = obj
    return out


def _best_record_list(obj, depth: int = 0):
    """The largest list of objects anywhere in the document — usually the transactions."""
    best = None
    if isinstance(obj, list) and obj and sum(isinstance(v, dict) for v in obj) >= max(1, len(obj) * 0.8):
        best = obj
    children = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    if depth < 6:
        for child in children:
            found = _best_record_list(child, depth + 1)
            if found is not None and (best is None or len(found) > len(best)):
                best = found
    return best


def _short_keys(records: list[dict]) -> list[dict]:
    """Use each flattened key's last part as its column name when that doesn't collide."""
    all_keys = {k for r in records for k in r}
    last = {}
    for k in all_keys:
        last.setdefault(k.split(".")[-1], set()).add(k)
    rename = {k: k.split(".")[-1] for k in all_keys if len(last[k.split(".")[-1]]) == 1}
    return [{rename.get(k, k): v for k, v in r.items()} for r in records]


def read_json(path: str) -> Grid:
    data = json.loads(_read_text(path))
    records = _best_record_list(data)
    if not records:
        raise ValueError("no list of transaction records found in this JSON file")
    return _records_to_grid(_short_keys([_flatten(r) for r in records if isinstance(r, dict)]), [])


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _xml_record(el: ET.Element) -> dict:
    rec = {}
    for k, v in el.attrib.items():
        rec[_local(k)] = v

    def walk(node, prefix):
        for child in node:
            name = f"{prefix}.{_local(child.tag)}" if prefix else _local(child.tag)
            for k, v in child.attrib.items():
                if _local(k).lower() in ("ccy", "currency"):
                    rec.setdefault("Currency", v)
                else:
                    rec.setdefault(f"{name}@{_local(k)}", v)
            if len(child):
                walk(child, name)
            elif (child.text or "").strip():
                key = name if name not in rec else f"{name}.{len(rec)}"
                rec[key] = child.text.strip()

    walk(el, "")
    return rec


def read_xml(path: str) -> Grid:
    with open(path, "rb") as f:
        root = _safe_xml(f.read())
    best, best_count = None, 0
    for parent in root.iter():
        counts: dict[str, list] = {}
        for child in parent:
            if len(child) or child.attrib:
                counts.setdefault(child.tag, []).append(child)
        for tag, items in counts.items():
            if len(items) > best_count:
                best, best_count = items, len(items)
    if not best:
        raise ValueError("no repeated transaction records found in this XML file")
    records = _short_keys([_xml_record(el) for el in best])
    # ISO 20022 camt.05x: amount sign comes from CdtDbtInd, handled as a Dr/Cr marker column
    return _records_to_grid(records, ["BookgDt.Dt", "Dt", "Date", "Amt", "Amount", "CdtDbtInd", "Currency"])


def read_sheets(path: str, ext: str) -> list[tuple[str, str, Grid]]:
    if ext == ".xls":
        return read_xls(path)
    if ext == ".ods":
        return read_ods(path)
    if ext == ".docx":
        return read_docx(path)
    if ext in (".html", ".htm"):
        return read_html(path)
    reader = {".ofx": read_ofx, ".qfx": read_ofx, ".qif": read_qif, ".sta": read_mt940, ".mt940": read_mt940,
              ".940": read_mt940, ".json": read_json, ".xml": read_xml}[ext]
    return [("records", "visible", reader(path))]
