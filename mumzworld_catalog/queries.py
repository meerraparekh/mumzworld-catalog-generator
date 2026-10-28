"""Section 7B: the vendor query sheet.

Queries are derived from the working catalog after each cataloging pass: only
for information that is required (by a Required mapping, a mandatory attribute
or a content/image rule) and not reliably available.  Each query is one SKU +
one field so answers can be merged at field level; the export also groups
them per SKU into one consolidated message.
"""
from __future__ import annotations

import re

from .fields import SOURCE_FIELDS, label, resolve_field_name, value
from .reference import Refs
from .storage import now

DERIVED_SOURCES = {"short_description": ["description"], "long_description": ["description"],
                   "bullet_points": ["features"], "category": [], "category_id": [], "tags": []}
EXPECTED = {
    "barcode": "Barcode as text: 8, 12, 13 or 14 digits including the check digit (keep leading zeros)",
    "weight": "Number with unit, e.g. 1.2 kg",
    "dimensions": "L x W x H with unit, e.g. 30 x 20 x 15 cm",
    "length": "Number with unit (cm)", "width": "Number with unit (cm)", "height": "Number with unit (cm)",
    "retail_price": "Number with currency", "cost_price": "Number with currency",
    "age_group": "Age range as stated on the packaging", "description": "Product description (plain text)",
    "features": "One feature per line", "country_of_origin": "Country name",
    "images": "Image files named with the SKU (e.g. SKU_1.jpg), at least the required number",
    "product_type": "What the product is and what it is used for",
}


def required_fields(refs: Refs, p: dict) -> dict[str, str]:
    """{internal field: why it is required} for this product."""
    req = {}
    m = refs.mapping or {}
    for r in m.get("mappings", []):
        if not r["required"]:
            continue
        srcs = [s.strip() for s in r["source"].split("|") if s.strip()] if r["rule"] in (
            "copy", "concat", "lookup", "attribute_code") else re.findall(r"\{([^}]+)\}", r["param"] or "")
        if r["rule"] in ("category_path", "category_level", "category_id", "category_field"):
            srcs = ["category"]
        for s in srcs:
            f = resolve_field_name(s)
            if f:
                req.setdefault(f, f"required by {r['system'].upper()} column '{r['target']}'")
    cat = value(p, "category")
    pattern = refs.content_rule("title_pattern", cat)
    if "title" in req and pattern:
        for name in re.findall(r"\{([^}]+)\}", pattern):
            f = resolve_field_name(name)
            if f:
                req.setdefault(f, f"used in the title pattern (title is {req['title']})")
    for f, srcs in DERIVED_SOURCES.items():
        if f in req:
            for s in srcs:
                req.setdefault(s, f"needed for {label(f).lower()} ({req[f]})")
    return req


def needed(p: dict, refs: Refs, findings: list[dict], images_uploaded: bool) -> list[dict]:
    """The open information needs of one eligible product."""
    out = []
    sku = value(p, "sku")
    req = required_fields(refs, p)
    fields = p["fields"]
    for f, why in req.items():
        if f.startswith("attr:") or f in DERIVED_SOURCES or f in ("title",):
            continue
        if f in SOURCE_FIELDS and not value(p, f):
            out.append({"field": f, "current": "", "reason": f"Missing; {why}.", "priority": "High",
                        "expected": EXPECTED.get(f, ""), "question": f"Please provide the {label(f).lower()} for "
                        f"SKU {sku}."})
    m = refs.mapping or {}
    for a in m.get("attributes", []):
        f = f"attr:{a['attribute']}"
        fvd = fields.get(f)
        if not fvd:
            continue
        allowed = refs.approved_values(a["list"]) or []
        opts = "; ".join(allowed[:40]) + (f" (+{len(allowed) - 40} more, see Allowed Values sheet)"
                                          if len(allowed) > 40 else "")
        if fvd["class"] == "vendor_query" and not fvd.get("value"):
            out.append({"field": f, "current": "", "priority": "High" if a["mandatory"] else "Medium",
                        "reason": "Mandatory attribute with no value in the vendor data.",
                        "expected": a["format"] or f"One of: {opts}",
                        "question": f"Please provide the {a['attribute']} of SKU {sku}"
                                    + (", choosing from the allowed values." if allowed else ".")})
        elif fvd.get("unresolved") and a["mandatory"]:
            out.append({"field": f, "current": fvd.get("original", ""), "priority": "High",
                        "reason": f"The value '{', '.join(fvd['unresolved'])}' has no approved equivalent.",
                        "expected": f"One of: {opts}",
                        "question": f"For SKU {sku} the {a['attribute']} is given as "
                                    f"'{', '.join(fvd['unresolved'])}'. Which of the allowed values applies?"})
    cat = fields.get("category") or {}
    if cat.get("status") == "blocker" and not value(p, "product_type") and not cat.get("candidates"):
        out.append({"field": "product_type", "current": "", "priority": "High",
                    "reason": "The product could not be placed in an approved category from its name alone.",
                    "expected": EXPECTED["product_type"],
                    "question": f"What type of product is SKU {sku} ({value(p, 'product_name')}) and what is it "
                                "used for?"})
    for fd in findings:
        if fd["key"] != p["key"] or fd["code"] not in ("BARCODE_INVALID", "BARCODE_SCIENTIFIC",
                                                       "BARCODE_LEADING_ZERO"):
            continue
        out.append({"field": "barcode", "current": value(p, "barcode"), "priority": "High",
                    "reason": fd.get("detail") or fd.get("reason", ""), "expected": EXPECTED["barcode"],
                    "question": f"Please confirm the barcode of SKU {sku}: we received '{value(p, 'barcode')}'."})
    cat_key = value(p, "category")
    need_img = refs.content_rule("images_required", cat_key).lower() in ("y", "yes", "true", "1")
    try:
        min_img = int(float(refs.content_rule("min_images", cat_key) or 0))
    except ValueError:
        min_img = 0
    confirmed = [i for i in p.get("images", []) if i.get("confidence") == "confirmed"]
    if (need_img or min_img) and images_uploaded and len(confirmed) < max(1, min_img):
        out.append({"field": "images", "current": f"{len(confirmed)} image(s)", "priority": "High",
                    "reason": f"At least {max(1, min_img)} image(s) required; {len(confirmed)} matched to this SKU.",
                    "expected": EXPECTED["images"],
                    "question": f"Please send product images for SKU {sku} named with the SKU."})
    seen, uniq = set(), []
    for q in out:
        if q["field"] not in seen:
            seen.add(q["field"])
            uniq.append(q)
    return uniq


def refresh(data: dict, products: dict, order: list[str], refs: Refs, findings: list[dict],
            images_uploaded: bool) -> dict:
    """Update the stored queries from the current catalog.  Returns counts."""
    data.setdefault("queries", [])
    data.setdefault("next", 1)
    by_key = {(q["key"], q["field"]): q for q in data["queries"]}
    active = set()
    counts = {"new": 0, "updated": 0, "closed": 0}
    for k in order:
        p = products[k]
        if not p.get("eligible"):
            continue
        for need in needed(p, refs, findings, images_uploaded):
            active.add((k, need["field"]))
            q = by_key.get((k, need["field"]))
            if q is None:
                q = {"id": f"Q{data['next']:05d}", "key": k, "sku": value(p, "sku"),
                     "product": value(p, "product_name"), "field": need["field"], "field_label": label(need["field"]),
                     "status": "open", "created_at": now(), "answers": [], "history": []}
                data["next"] += 1
                data["queries"].append(q)
                by_key[(k, need["field"])] = q
                counts["new"] += 1
            elif q["status"] in ("resolved", "closed", "answered"):
                q["history"].append({"at": now(), "event": "Still needed after the answer" if q["status"] ==
                                     "answered" else "Reopened: information still needed"})
                q["status"] = "partially" if q["answers"] else "open"
                counts["updated"] += 1
            q.update({k2: need[k2] for k2 in ("current", "question", "reason", "expected", "priority")})
    for q in data["queries"]:
        if (q["key"], q["field"]) not in active and q["status"] in ("open", "partially", "conflict", "answered"):
            p = products.get(q["key"])
            src = (p or {}).get("fields", {}).get(q["field"], {}).get("source", "")
            q["status"] = "resolved" if q["answers"] else "closed"
            q["history"].append({"at": now(), "event": "No longer needed" + (f" (value now from {src})" if src
                                                                               else "")})
            counts["closed"] += 1
    return counts


def outstanding(data: dict) -> list[dict]:
    return [q for q in data.get("queries", []) if q["status"] in ("open", "partially", "conflict")]
