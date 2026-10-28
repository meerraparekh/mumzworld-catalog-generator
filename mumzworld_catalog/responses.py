"""Section 8: merge vendor response files into the working catalog, field by field.

Two layouts are read:

* the query sheet sent back with the *Vendor Answer* column filled in
  (Query ID / SKU / Field Code / Vendor Answer), and
* a product sheet: a SKU column plus columns named like internal fields or
  attributes.

Nothing is replaced row-wide.  A reviewer-locked value is never overwritten, a
different answer to a field an earlier response already answered is a
conflict, and unknown or ambiguous SKUs go to the exceptions list.
"""
from __future__ import annotations

from . import sheets as sh
from .fields import SOURCE_FIELDS, fv, label, resolve_field_name, set_field
from .normalize import clean, gtin_check
from .sheets import as_text, is_blank, norm
from .storage import now

ANSWER_NAMES = ["vendor answer", "answer", "vendor response", "response", "reply", "vendor value", "value provided"]


def _col(columns, names):
    keys = {norm(n) for n in names}
    return next((c for c in columns if norm(c) in keys), None)


def read_answers(path, attributes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """[(sku, field, value, location)], [exceptions].  ``attributes`` maps attribute
    name -> the internal field it is mapped from ('' if none): an answer for such an
    attribute updates that field, and the attribute is mapped again from it."""
    answers, exceptions = [], []
    attr_norm = {norm(a): a for a in attributes}

    def field_of(name):
        k = norm(name)
        if k in attr_norm:
            a = attr_norm[k]
            return attributes[a] or f"attr:{a}"
        f = resolve_field_name(name)
        if f and f.startswith("attr:") and norm(f[5:]) in attr_norm:
            a = attr_norm[norm(f[5:])]
            return attributes[a] or f"attr:{a}"
        return f

    for sheet, rows in sh.sheet_rows(path).items():
        if not rows:
            continue
        t = sh.table_from_rows(rows, sh.header_row_index(rows, SOURCE_FIELDS["sku"] + ["query id"]))
        cols = t["columns"]
        sku_c = _col(cols, SOURCE_FIELDS["sku"])
        qid_c = _col(cols, ["query id", "query"])
        code_c = _col(cols, ["field code"])
        fld_c = _col(cols, ["field", "field requiring clarification", "attribute"])
        ans_c = _col(cols, ANSWER_NAMES)
        if not sku_c:
            if any(not is_blank(v) for r in t["records"] for v in r["values"].values()):
                exceptions.append({"type": "NO SKU COLUMN", "sheet": sheet, "row": "", "sku": "", "field": "",
                                   "value": "", "detail": "Sheet has no SKU column; not read."})
            continue
        if ans_c and (code_c or fld_c):
            for r in t["records"]:
                v = r["values"]
                raw = v.get(ans_c)
                name = as_text(v.get(code_c)).strip() if code_c and not is_blank(v.get(code_c)) else as_text(
                    v.get(fld_c)).strip()
                answers.append({"sku": as_text(v.get(sku_c)).strip(), "field": field_of(name) or "",
                                "field_name": name, "raw": raw, "query_id": as_text(v.get(qid_c)).strip() if qid_c
                                else "", "sheet": sheet, "row": r["row"]})
            continue
        for c in cols:
            if c == sku_c:
                continue
            f = field_of(c)
            for r in t["records"]:
                raw = r["values"].get(c)
                if is_blank(raw):
                    continue
                if not f:
                    exceptions.append({"type": "UNKNOWN COLUMN", "sheet": sheet, "row": r["row"],
                                       "sku": as_text(r["values"].get(sku_c)), "field": c, "value": as_text(raw),
                                       "detail": f"Column '{c}' is not a catalog field or attribute; not merged."})
                    continue
                answers.append({"sku": as_text(r["values"].get(sku_c)).strip(), "field": f, "field_name": c,
                                "raw": raw, "query_id": "", "sheet": sheet, "row": r["row"]})
    return answers, exceptions


def merge(catalog: dict, queries: dict, path, name: str, attributes: dict[str, str]) -> dict:
    at = now()
    answers, exceptions = read_answers(path, attributes)
    products = catalog["products"]
    by_sku = {}
    for k in catalog["order"]:
        s = products[k]["fields"].get("sku", {}).get("value")
        if s:
            by_sku.setdefault(norm(s), []).append(k)
    qindex = {(q["key"], q["field"]): q for q in queries.get("queries", [])}
    qids = {q["id"]: q for q in queries.get("queries", [])}
    summary = {"file": name, "at": at, "answers_read": len(answers), "skus_matched": set(), "fields_updated": 0,
               "confirmed_same": 0, "conflicts": [], "unanswered": 0, "touched": set()}
    for a in answers:
        loc = f"{a['sheet']} row {a['row']}"
        if is_blank(a["raw"]):
            summary["unanswered"] += 1
            continue
        keys = by_sku.get(norm(a["sku"]), [])
        q = qids.get(a["query_id"]) if a["query_id"] else None
        if q and norm(q["sku"]) == norm(a["sku"]):
            keys = [q["key"]]
            a["field"] = a["field"] or q["field"]
        if not a["sku"] or not keys:
            exceptions.append({"type": "UNKNOWN SKU", "sheet": a["sheet"], "row": a["row"], "sku": a["sku"],
                               "field": a["field_name"], "value": as_text(a["raw"]),
                               "detail": "No product in this ticket has this SKU."})
            continue
        if len(keys) > 1:
            exceptions.append({"type": "AMBIGUOUS SKU", "sheet": a["sheet"], "row": a["row"], "sku": a["sku"],
                               "field": a["field_name"], "value": as_text(a["raw"]),
                               "detail": f"{len(keys)} products share this SKU; not merged automatically."})
            continue
        if not a["field"]:
            exceptions.append({"type": "UNKNOWN FIELD", "sheet": a["sheet"], "row": a["row"], "sku": a["sku"],
                               "field": a["field_name"], "value": as_text(a["raw"]),
                               "detail": f"'{a['field_name']}' is not a catalog field or attribute."})
            continue
        key, field = keys[0], a["field"]
        p = products[key]
        summary["skus_matched"].add(key)
        field_for_clean = field if not field.startswith("attr:") else "colour"
        val, transform = clean(field_for_clean, a["raw"])
        if field == "images":
            exceptions.append({"type": "IMAGES IN RESPONSE", "sheet": a["sheet"], "row": a["row"], "sku": a["sku"],
                               "field": "images", "value": val,
                               "detail": "Image answers are not files: upload the images to the ticket."})
            continue
        cur = p["fields"].get(field)
        q = qindex.get((key, field)) or next((qindex[(key, f"attr:{a}")] for a, src in attributes.items()
                                              if src == field and (key, f"attr:{a}") in qindex), None)
        answer = {"file": name, "at": at, "value": val, "location": loc}
        if q:
            q["answers"].append(answer)
        status, note = "ok", ""
        if field == "barcode" and gtin_check(val):
            status, note = "warning", f"Answered barcode {gtin_check(val)}."
        if cur and cur.get("locked"):
            if as_text(cur.get("value")) != val:
                summary["conflicts"].append({"key": key, "sku": a["sku"], "field": label(field),
                                             "current": cur.get("value"), "response": val,
                                             "detail": "Reviewer-approved value kept; the response differs."})
                cur.setdefault("conflicts", []).append(answer)
                if q:
                    q["status"] = "conflict"
            continue
        if cur and cur.get("source", "").startswith("response:"):
            prev = as_text(cur.get("original") or cur.get("value"))
            if prev == val:
                summary["confirmed_same"] += 1
                continue
            summary["conflicts"].append({"key": key, "sku": a["sku"], "field": label(field), "current": prev,
                                         "response": val, "detail": f"Earlier response {cur['source'][9:]} said "
                                                                    f"'{prev}'. Earlier value kept until reviewed."})
            cur.setdefault("conflicts", []).append(answer)
            cur["status"] = "blocker"
            cur["reason"] = (cur.get("reason", "") + f" CONFLICT: {name} says '{val}'.").strip()
            summary["touched"].add(key)
            if q:
                q["status"] = "conflict"
            continue
        if cur and as_text(cur.get("value")) == val and not field.startswith("attr:"):
            summary["confirmed_same"] += 1
            cur.setdefault("confirmations", []).append(answer)
            continue
        set_field(p, field, fv(val, original=val, source=f"response:{name}", cls="directly_sourced",
                               transform=transform, reason=f"Vendor response {name} ({loc}). {note}".strip(),
                               status=status, response_at=at), at)
        summary["fields_updated"] += 1
        summary["touched"].add(key)
        if q and q["status"] != "conflict":
            q["status"] = "answered"
            q["history"].append({"at": at, "event": f"Answered in {name}"})
    summary["skus_matched"] = len(summary["skus_matched"])
    summary["touched"] = sorted(summary["touched"])
    summary["exceptions"] = exceptions
    return summary
