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


def _open(path: Path, data_only: bool = True):
    try:
        return load_workbook(path, data_only=data_only, keep_vba=str(path).lower().endswith(".xlsm"))
    except Exception as exc:  # openpyxl raises many types for bad files
        raise SheetError(f"Could not open workbook: {exc}") from exc


def _csv_rows(path: Path) -> list[list]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(text.splitlines(), dialect)]


def sheet_rows(path: Path) -> dict[str, list[list]]:
    """All non-empty sheets as lists of rows (raw values, strings for CSV)."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return {path.stem: _csv_rows(path)}
    wb = _open(path)
    out = {}
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        while rows and all(is_blank(v) for v in rows[-1]):
            rows.pop()
        out[ws.title] = rows
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


def read_table(path: Path, sheet: str | None = None, must_have: list[str] | None = None) -> dict:
    """Read one table.  Without ``sheet`` the sheet with the most data rows (or the
    first containing a ``must_have`` header) is used."""
    sheets = sheet_rows(path)
    if not sheets:
        raise SheetError("The workbook has no sheets.")
    if sheet:
        if sheet not in sheets:
            raise SheetError(f"Sheet '{sheet}' not found. Sheets: {', '.join(sheets)}")
        chosen = [sheet]
    else:
        chosen = list(sheets)
        if must_have:
            wanted = {norm(n) for n in must_have}
            hits = [s for s in chosen if any(norm(v) in wanted for r in sheets[s][:HEADER_SCAN_ROWS] for v in r
                                             if not is_blank(v))]
            chosen = hits or chosen
        chosen.sort(key=lambda s: -len(sheets[s]))
    name = chosen[0]
    rows = sheets[name]
    t = table_from_rows(rows, header_row_index(rows, must_have))
    t["sheet"] = name
    return t


def inventory(path: Path) -> dict:
    """Describe a file for the pre-processing check: sheets, header row, data rows."""
    path = Path(path)
    info = {"readable": True, "sheets": [], "error": ""}
    if path.suffix.lower() not in SPREADSHEET_EXT:
        return info
    try:
        sheets = sheet_rows(path)
    except (SheetError, OSError, UnicodeError) as e:
        return {"readable": False, "sheets": [], "error": str(e)}
    for name, rows in sheets.items():
        if not rows:
            info["sheets"].append({"name": name, "rows": 0, "columns": [], "header_row": None})
            continue
        h = header_row_index(rows)
        t = table_from_rows(rows, h)
        info["sheets"].append({"name": name, "rows": len(t["records"]), "columns": t["columns"][:60],
                               "header_row": h + 1})
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
