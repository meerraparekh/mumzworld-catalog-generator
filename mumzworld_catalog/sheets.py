"""Reading spreadsheets whose layout is not known in advance.

Vendor files, NetSuite exports and reference lists arrive with different sheet
names, column orders and header positions.  These helpers find the header row,
read every row as raw values and describe a workbook for the inventory shown
before processing.  Nothing here changes a value; text conversion of
identifiers is in :func:`as_text`.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

SPREADSHEET_EXT = (".xlsx", ".xlsm", ".csv")
HEADER_SCAN_ROWS = 15


class SheetError(ValueError):
    pass


def norm(text) -> str:
    """Comparison key: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def as_text(value) -> str:
    """A cell value as text without losing identifier digits.

    Integers stored as floats (``5012345678900.0``) lose the ``.0``; real
    decimals are kept as written.  Leading zeros survive only if the file kept
    them (a text cell) - a number cell that already lost them is flagged by the
    barcode checks, never padded here."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10] if isinstance(value, datetime) and not (
            value.hour or value.minute or value.second) else value.isoformat()
    return str(value)


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _open(path: Path, data_only: bool = True, read_only: bool = False):
    try:
        return load_workbook(path, data_only=data_only, read_only=read_only,
                             keep_vba=not read_only and str(path).lower().endswith(".xlsm"))
    except Exception as exc:  # openpyxl raises many types for bad files
        raise SheetError(f"Could not open workbook: {exc}") from exc


def _csv_reader(path: Path):
    head = path.open("rb").read(65536)
    enc = "utf-8-sig"
    for e in ("utf-8-sig", "cp1252"):
        try:
            head.decode(e)
            enc = e
            break
        except UnicodeDecodeError:
            enc = "latin-1"
    sample = head.decode(enc, errors="replace")
    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    fh = path.open("r", encoding=enc, errors="replace", newline="")
    return fh, csv.reader(fh, dialect)


def iter_rows(path: Path, sheet: str | None = None):
    """Stream the rows of one sheet (the first if not named) as lists, without
    loading the workbook into memory.  CSV files have one sheet, named after the file."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        fh, reader = _csv_reader(path)
        with fh:
            for r in reader:
                yield list(r)
        return
    wb = _open(path, read_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        for r in ws.iter_rows(values_only=True):
            yield list(r)
    finally:
        wb.close()  # read-only workbooks keep the file open (and locked on Windows) until closed


def sheet_names(path: Path) -> list[str]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return [path.stem]
    wb = _open(path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def sheet_rows(path: Path, sheets: list[str] | None = None, limit: int | None = None) -> dict[str, list[list]]:
    """Sheets as lists of rows (raw values, strings for CSV); trailing blank rows dropped.
    ``limit`` reads only the first rows of each sheet."""
    path = Path(path)
    out = {}
    for name in sheets or sheet_names(path):
        rows = []
        for r in iter_rows(path, name if path.suffix.lower() != ".csv" else None):
            rows.append(r)
            if limit and len(rows) >= limit:
                break
        while rows and all(is_blank(v) for v in rows[-1]):
            rows.pop()
        out[name] = rows
    return out


def header_row_index(rows: list[list], must_have: list[str] | None = None) -> int:
    """0-based index of the most header-like row among the first rows.

    A header row has many distinct short text cells.  If ``must_have`` names are
    given, the first row containing any of them (by :func:`norm`) wins."""
    scan = rows[:HEADER_SCAN_ROWS]
    if must_have:
        wanted = {norm(n) for n in must_have}
        for i, r in enumerate(scan):
            if any(norm(v) in wanted for v in r if not is_blank(v)):
                return i
    best, best_score = 0, -1
    for i, r in enumerate(scan):
        texts = [v for v in r if isinstance(v, str) and v.strip() and len(v) <= 80]
        score = len(set(t.strip().lower() for t in texts)) - sum(1 for v in r if isinstance(v, (int, float)))
        if score > best_score:
            best, best_score = i, score
    return best


def table_from_rows(rows: list[list], header_idx: int) -> dict:
    headers = rows[header_idx] if rows else []
    columns, seen = [], {}
    for i, h in enumerate(headers):
        name = str(h).strip() if not is_blank(h) else f"Column {get_column_letter(i + 1)}"
        if name in seen:  # keep both values of repeated headers
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        columns.append(name)
    records = []
    for n, r in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if all(is_blank(v) for v in r):
            continue
        extra = len(r) - len(columns)
        for k in range(extra):
            columns.append(f"Column {get_column_letter(len(columns) + 1)}")
        records.append({"row": n, "values": {c: (r[i] if i < len(r) else None) for i, c in enumerate(columns)}})
    used = [c for c in columns if not c.startswith("Column ") or any(not is_blank(x["values"].get(c)) for x in records)]
    for x in records:
        x["values"] = {c: x["values"].get(c) for c in used}
    return {"header_row": header_idx + 1, "columns": used, "records": records}


# -- fast column reader for very large workbooks --------------------------------------
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


class FastBook:
    """Direct XML access to an .xlsx: sheet list, shared strings (read once) and
    streaming rows.  Many times faster than openpyxl on huge workbooks.  Values are
    the cell text as stored (numbers as written, not converted), so identifiers
    keep every digit; dates stay as Excel serial numbers."""

    def __init__(self, path: Path):
        import xml.etree.ElementTree as ET
        import zipfile
        self.ET = ET
        try:
            self.zf = zipfile.ZipFile(path)
            wb = ET.fromstring(self.zf.read("xl/workbook.xml"))
            rels = ET.fromstring(self.zf.read("xl/_rels/workbook.xml.rels"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            raise SheetError(f"Could not open workbook: {e}") from e
        targets = {r.get("Id"): r.get("Target") for r in rels}
        self.sheets = {}
        for el in wb.iter(f"{_NS}sheet"):
            t = (targets.get(el.get(f"{_RNS}id")) or "").lstrip("/")
            self.sheets[el.get("name")] = t if t.startswith("xl/") else f"xl/{t}"
        self._shared = None

    def close(self):
        self.zf.close()

    @property
    def shared(self) -> list[str]:
        if self._shared is None:
            self._shared = []
            if "xl/sharedStrings.xml" in self.zf.namelist():
                with self.zf.open("xl/sharedStrings.xml") as fh:
                    for _, el in self.ET.iterparse(fh):
                        if el.tag == f"{_NS}si":
                            self._shared.append("".join(t.text or "" for t in el.iter(f"{_NS}t")))
                            el.clear()
        return self._shared

    def dimension_rows(self, sheet: str) -> int | None:
        """Row count from the sheet's <dimension>, read from the start of its XML."""
        with self.zf.open(self.sheets[sheet]) as fh:
            head = fh.read(4096).decode("utf-8", errors="ignore")
        m = re.search(r'<(?:\w+:)?dimension ref="[A-Z]+\d+:[A-Z]+(\d+)"', head)
        return int(m.group(1)) if m else None

    def rows(self, sheet: str, columns: set[int] | None = None, typed: bool = False):
        """Stream ``(row number, {column index: text})``; only ``columns`` are kept when given.
        With ``typed``, number cells come back as int/float."""
        if sheet not in self.sheets:
            raise SheetError(f"Sheet '{sheet}' not found.")
        shared = None
        with self.zf.open(self.sheets[sheet]) as fh:
            row_no, cells = 0, {}
            for _, el in self.ET.iterparse(fh):
                tag = el.tag
                if tag == f"{_NS}c":
                    ref = el.get("r") or ""
                    ci = _col_index(ref) if ref else len(cells)
                    if columns is None or ci in columns:
                        t = el.get("t")
                        if t == "inlineStr":
                            val = "".join(x.text or "" for x in el.iter(f"{_NS}t"))
                        else:
                            v = el.find(f"{_NS}v")
                            val = v.text if v is not None else None
                            if val is not None and t == "s":
                                shared = shared if shared is not None else self.shared
                                val = shared[int(val)]
                            elif val is not None and t == "b":
                                val = "TRUE" if val == "1" else "FALSE"
                            elif val is not None and typed and t in (None, "n"):
                                try:
                                    f = float(val)
                                    val = int(f) if f.is_integer() else f
                                except ValueError:
                                    pass
                            elif val is not None and t in (None, "n") and ("E" in val or "e" in val):
                                try:
                                    f = float(val)
                                    val = str(int(f)) if f.is_integer() else val
                                except ValueError:
                                    pass
                        if val not in (None, ""):
                            cells[ci] = val
                    el.clear()
                elif tag == f"{_NS}row":
                    r = el.get("r")
                    row_no = int(r) if r else row_no + 1
                    yield row_no, cells
                    cells = {}
                    el.clear()


def fast_rows(path: Path, sheet: str, columns: set[int] | None = None):
    book = FastBook(path)
    try:
        yield from book.rows(sheet, columns)
    finally:
        book.close()


def _fast_heads(path: Path):
    book = FastBook(path)
    heads, sizes = {}, {}
    try:
        for name in book.sheets:
            rows, last = [], 0
            for n, cells in book.rows(name, typed=True):
                while last + 1 < n and len(rows) < HEADER_SCAN_ROWS:  # rows the file leaves out are blank
                    rows.append([])
                    last += 1
                if len(rows) >= HEADER_SCAN_ROWS:
                    break
                rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
                last = n
            while rows and all(is_blank(v) for v in rows[-1]):
                rows.pop()
            heads[name] = rows
            sizes[name] = book.dimension_rows(name)
    finally:
        book.close()
    return heads, sizes


def heads_and_sizes(path: Path) -> tuple[dict[str, list[list]], dict[str, int | None]]:
    """First rows and stored row count of every sheet, opening the workbook once."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return sheet_rows(path, limit=HEADER_SCAN_ROWS), {path.stem: None}
    try:
        return _fast_heads(path)
    except (SheetError, KeyError, ValueError, OSError):
        pass  # unusual file layout: let openpyxl read it
    wb = _open(path, read_only=True)
    heads, sizes = {}, {}
    try:
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) >= HEADER_SCAN_ROWS:
                    break
            while rows and all(is_blank(v) for v in rows[-1]):
                rows.pop()
            heads[ws.title] = rows
            # Only the stored dimension: asking openpyxl for max_row without one scans the sheet.
            sizes[ws.title] = getattr(ws, "_max_row", None) or None
    finally:
        wb.close()
    return heads, sizes


def pick_sheets(path: Path, must_have: list[str] | None = None) -> tuple[list[str], dict]:
    """Sheets ordered by likelihood of holding the table: those whose first rows contain
    a ``must_have`` header first, then by size.  Reads only the first rows of each."""
    heads, sizes = heads_and_sizes(path)
    names = list(heads)
    if must_have:
        wanted = {norm(n) for n in must_have}
        hits = [s for s in names if any(norm(v) in wanted for r in heads[s] for v in r if not is_blank(v))]
        names = hits or names
    names = [n for n in names if heads[n]] or names
    names.sort(key=lambda n: -(sizes.get(n) or 0))
    return names, heads


def read_table(path: Path, sheet: str | None = None, must_have: list[str] | None = None) -> dict:
    """Read one table.  Without ``sheet`` the largest sheet (among those containing a
    ``must_have`` header, if any) is used."""
    path = Path(path)
    if sheet:
        if path.suffix.lower() != ".csv" and sheet not in sheet_names(path):
            raise SheetError(f"Sheet '{sheet}' not found. Sheets: {', '.join(sheet_names(path))}")
        name = sheet
    else:
        names, _ = pick_sheets(path, must_have)
        if not names:
            raise SheetError("The workbook has no sheets.")
        name = names[0]
    rows = sheet_rows(path, [name])[name]
    t = table_from_rows(rows, header_row_index(rows, must_have))
    t["sheet"] = name
    return t


def inventory(path: Path) -> dict:
    """Describe a file for the pre-processing check: sheets, header row, data rows.
    Only the first rows are read; the row count comes from the stored dimensions
    (counted for small files that do not record it)."""
    path = Path(path)
    info = {"readable": True, "sheets": [], "error": ""}
    if path.suffix.lower() not in SPREADSHEET_EXT:
        return info
    try:
        heads, sizes = heads_and_sizes(path)
    except (SheetError, OSError, UnicodeError) as e:
        return {"readable": False, "sheets": [], "error": str(e)}
    small = path.stat().st_size < 20 * 1024 * 1024
    for name, rows in heads.items():
        if not rows:
            info["sheets"].append({"name": name, "rows": 0, "columns": [], "header_row": None})
            continue
        h = header_row_index(rows)
        cols = table_from_rows(rows, h)["columns"]
        size = sizes.get(name)
        if size is None and small:
            size = sum(1 for _ in iter_rows(path, name if path.suffix.lower() != ".csv" else None))
        info["sheets"].append({"name": name, "rows": max(0, size - h - 1) if size else None,
                               "columns": cols[:60], "header_row": h + 1})
    return info


def data_validations(path: Path) -> dict[str, dict[str, list[str]]]:
    """Inline drop-down lists defined in a workbook: {sheet: {column letter: values}}.

    Only literal lists (``"a,b,c"``) and ranges on the same workbook are read;
    other formulas are skipped (reported by the caller as unreadable)."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return {}
    wb = _open(path)
    out: dict[str, dict[str, list[str]]] = {}
    for ws in wb.worksheets:
        for dv in getattr(ws.data_validations, "dataValidation", []):
            if dv.type != "list" or not dv.formula1:
                continue
            values = _validation_values(wb, ws, dv.formula1)
            if values is None:
                continue
            for rng in str(dv.sqref).split():
                col = re.match(r"\$?([A-Z]+)", rng)
                if col:
                    out.setdefault(ws.title, {})[col.group(1)] = values
    return out


def _validation_values(wb, ws, formula: str):
    f = formula.strip()
    if f.startswith('"') and f.endswith('"'):
        return [v.strip() for v in f[1:-1].split(",") if v.strip()]
    m = re.fullmatch(r"=?(?:'?([^'!]+)'?!)?\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)", f)
    if not m:
        return None
    sheet = m.group(1) or ws.title
    if sheet not in wb.sheetnames:
        return None
    src = wb[sheet]
    vals = []
    for row in src[f"{m.group(2)}{m.group(3)}:{m.group(4)}{m.group(5)}"]:
        for c in row:
            if not is_blank(c.value):
                vals.append(as_text(c.value).strip())
    return vals
