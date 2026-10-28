"""Stage 2: read the vendor files into standardized internal records.

Each source cell keeps its original text; only safe formatting is changed
(trimming, collapsing repeated spaces, joining wrapped lines in names).  The
transformation and its reason are stored on the field.  Identifiers are never
altered: problems with them are flagged.
"""
from __future__ import annotations

import re

from . import sheets as sh
from .fields import CRITICAL_FIELDS, fv, propose_column_map
from .sheets import as_text, is_blank, norm

MULTILINE_FIELDS = {"description", "features", "specifications", "safety_info", "contents"}


def gtin_check(code: str) -> str:
    """'' when ``code`` is a valid GTIN-8/12/13/14, else the reason it is not."""
    if not code.isdigit():
        return "contains characters other than digits"
    if len(code) not in (8, 12, 13, 14):
        return f"has {len(code)} digits (GTIN-8/12/13/14 expected)"
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    if (10 - total % 10) % 10 != digits[-1]:
        return "check digit does not match"
    return ""


def clean(field: str, raw) -> tuple[str, str]:
    """(normalized text, transformation description)."""
    text = as_text(raw)
    changes = []
    if isinstance(raw, float) and raw.is_integer():
        changes.append("number cell read as whole-number text")
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    if t != text:
        changes.append("normalized line breaks / non-breaking spaces")
    if field in MULTILINE_FIELDS:
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
        out, blank = [], False
        for ln in lines:
            if not ln:
                blank = bool(out)
                continue
            if blank:
                out.append("")
            out.append(ln)
            blank = False
        new = "\n".join(out)
    elif field in CRITICAL_FIELDS:
        new = t.strip()
    else:
        new = re.sub(r"\s+", " ", t).strip()
    if new != t and new != t.strip():
        changes.append("collapsed repeated spaces / line breaks")
    elif new != t:
        changes.append("trimmed surrounding spaces")
    return new, "; ".join(changes)


def detect_vendor_sheet(path) -> dict:
    """The table in a vendor file most likely to hold products: the sheet with a
    SKU-like header and the most rows."""
    from .fields import SOURCE_FIELDS
    return sh.read_table(path, must_have=SOURCE_FIELDS["sku"] + SOURCE_FIELDS["barcode"])


def build_records(tables: list[dict], column_map: dict[str, dict[str, str]], at: str) -> tuple[dict, list, list]:
    """Create products from vendor tables.

    ``tables``: [{"file_id", "file", "sheet", "columns", "records"}]
    ``column_map``: {file_id: {source column: internal field}}
    Returns (products by key, order, findings)."""
    products, order, findings = {}, [], []
    n = 0
    for t in tables:
        cmap = column_map.get(t["file_id"]) or propose_column_map(t["columns"])
        for rec in t["records"]:
            n += 1
            key = f"P{n:05d}"
            p = {"key": key, "source": {"file": t["file"], "file_id": t["file_id"], "sheet": t["sheet"],
                                        "row": rec["row"]},
                 "original": {c: as_text(v) for c, v in rec["values"].items() if not is_blank(v)},
                 "fields": {}, "match": {}, "images": [], "evidence": [], "review": {"status": "pending"},
                 "eligible": True, "created_at": at}
            for col, field in cmap.items():
                if not field or col not in rec["values"]:
                    continue
                raw = rec["values"][col]
                if is_blank(raw):
                    continue
                val, transform = clean(field, raw)
                if field in p["fields"]:  # two columns mapped to the same field: keep both visible
                    prev = p["fields"][field]
                    if prev["value"] != val:
                        findings.append({"key": key, "field": field, "severity": "warning", "code": "SOURCE_CONFLICT",
                                         "detail": f"Columns '{prev['source'][7:]}' and '{col}' disagree: "
                                                   f"'{prev['value']}' vs '{val}'. The first is kept."})
                    continue
                p["fields"][field] = fv(val, original=as_text(raw), source=f"vendor:{col}", cls="directly_sourced",
                                        transform=transform, reason="Vendor input" + (
                                            "; safe formatting only" if transform else ""))
            _identifier_checks(p, rec["values"], cmap, findings)
            products[key] = p
            order.append(key)
    _duplicates(products, order, findings)
    return products, order, findings


def _identifier_checks(p, raw_values, cmap, findings):
    key = p["key"]
    sku = p["fields"].get("sku", {}).get("value", "")
    if not sku:
        findings.append({"key": key, "field": "sku", "severity": "blocker", "code": "MISSING_SKU",
                         "detail": "No vendor SKU on this row."})
    elif re.search(r"\s", sku):
        findings.append({"key": key, "field": "sku", "severity": "warning", "code": "SKU_WHITESPACE",
                         "detail": f"SKU '{sku}' contains spaces."})
    bc = p["fields"].get("barcode")
    if bc:
        col = next((c for c, f in cmap.items() if f == "barcode"), None)
        raw = raw_values.get(col)
        code = bc["value"]
        if isinstance(raw, float) and not raw.is_integer() or re.search(r"e\+?\d+$", code, re.I):
            findings.append({"key": key, "field": "barcode", "severity": "blocker", "code": "BARCODE_SCIENTIFIC",
                             "detail": f"Barcode '{code}' looks like a number shown in scientific notation; digits "
                                       "may have been lost in the vendor file."})
        elif (isinstance(raw, (int, float)) and len(code) in (7, 11, 12, 13) and gtin_check(code)
              and not gtin_check(code.zfill(len(code) + 1))):
            findings.append({"key": key, "field": "barcode", "severity": "warning", "code": "BARCODE_LEADING_ZERO",
                             "detail": f"Barcode '{code}' was a number cell; it is valid with a leading zero "
                                       f"('{code.zfill(len(code) + 1)}'). Not changed: confirm with the vendor."})
        else:
            why = gtin_check(code)
            if why:
                findings.append({"key": key, "field": "barcode", "severity": "warning", "code": "BARCODE_INVALID",
                                 "detail": f"Barcode '{code}' {why}."})


def _duplicates(products, order, findings):
    by_sku, by_bc = {}, {}
    for k in order:
        f = products[k]["fields"]
        if f.get("sku"):
            by_sku.setdefault(norm(f["sku"]["value"]), []).append(k)
        if f.get("barcode"):
            by_bc.setdefault(f["barcode"]["value"], []).append(k)
    for keys in by_sku.values():
        if len(keys) > 1:
            for k in keys:
                products[k]["duplicate_sku"] = [x for x in keys if x != k]
                findings.append({"key": k, "field": "sku", "severity": "blocker", "code": "DUPLICATE_SKU",
                                 "detail": "Same SKU as " + ", ".join(_where(products[x]) for x in keys if x != k)})
    for code, keys in by_bc.items():
        if len(keys) > 1:
            for k in keys:
                products[k]["duplicate_barcode"] = [x for x in keys if x != k]
                findings.append({"key": k, "field": "barcode", "severity": "blocker", "code": "DUPLICATE_BARCODE",
                                 "detail": f"Barcode {code} is also on " + ", ".join(
                                     _where(products[x]) for x in keys if x != k)})


def _where(p) -> str:
    s = p["source"]
    sku = p["fields"].get("sku", {}).get("value", "")
    return f"{sku or '(no SKU)'} ({s['file']} row {s['row']})"
