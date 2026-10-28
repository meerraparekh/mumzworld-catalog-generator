"""Section 9: validate the working catalog and both target outputs.

Every finding has a severity (blocker / warning / info), the affected SKU and
field, the current value, the requirement, the reason and a suggested action.
Waived findings stay in the list, marked with who waived them and why.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .fields import label, value
from .normalize import gtin_check
from .reference import Refs, setup_check
from .sheets import norm

SEVERITIES = ["blocker", "warning", "info"]


def issue(sev, code, key="", sku="", field="", current="", expected="", reason="", action="", system=""):
    return {"id": "|".join([code, key, system, field]), "severity": sev, "code": code, "key": key, "sku": sku,
            "field": field, "system": system, "current": "" if current is None else str(current)[:300],
            "expected": expected, "reason": reason, "action": action}


def _num(v) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except ValueError:
        return False


def validate(catalog: dict, refs: Refs, queries: dict, meta: dict, images_uploaded: bool) -> dict:
    out = []
    products, order = catalog["products"], catalog["order"]
    # -- ticket level -------------------------------------------------------------------
    for s in setup_check(refs):
        if s["severity"] != "info":
            out.append(issue(s["severity"], "SETUP", field=s["area"], reason=s["detail"],
                             action="Fix the reference file in the Reference Library (or upload a ticket-specific "
                                    "version) and re-run."))
    for sys_, kind in (("m2", "Magento 2"), ("ns", "NetSuite")):
        if sys_ not in refs.templates:
            out.append(issue("blocker", "TEMPLATE_MISSING", system=sys_, reason=f"The {kind} template is not "
                             "available or could not be read; its import file cannot be produced.",
                             action=f"Upload the approved {kind} template."))
    # -- identifiers across the products that would be exported --------------------------
    eligible = [k for k in order if products[k].get("eligible")]
    by_sku, by_bc = defaultdict(list), defaultdict(list)
    for k in order:
        p = products[k]
        if value(p, "sku"):
            by_sku[norm(value(p, "sku"))].append(k)
        if value(p, "barcode"):
            by_bc[str(value(p, "barcode")).lstrip("0")].append(k)
    for k in order:
        p = products[k]
        sku = value(p, "sku")
        m = p.get("match", {})
        if m.get("status") in ("possible_match", "conflict") and not m.get("override"):
            out.append(issue("blocker", "UNRESOLVED_MATCH", k, sku, "sku", current=m.get("match_type"),
                             expected="Decision: create as new item or exclude",
                             reason=m.get("reason", ""), action="Investigate and record a decision on the product "
                             "page (the product is held out of the outputs until then)."))
        if not p.get("eligible"):
            continue
        if not sku:
            out.append(issue("blocker", "MISSING_SKU", k, "", "sku", reason="No vendor SKU.",
                             action="Ask the vendor for the SKU or remove the row from the vendor file."))
        if len(by_sku.get(norm(sku), [])) > 1:
            out.append(issue("blocker", "DUPLICATE_SKU", k, sku, "sku", current=sku,
                             reason=f"{len(by_sku[norm(sku)])} rows have this SKU.",
                             action="Investigate; do not change the SKU without vendor confirmation."))
        bc = str(value(p, "barcode"))
        if bc:
            if len(by_bc.get(bc.lstrip("0"), [])) > 1:
                out.append(issue("blocker", "DUPLICATE_BARCODE", k, sku, "barcode", current=bc,
                                 reason=f"{len(by_bc[bc.lstrip('0')])} rows have this barcode.",
                                 action="Investigate with the vendor; the barcode is not changed automatically."))
            why = gtin_check(bc)
            if re.search(r"e\+?\d+$", bc, re.I):
                out.append(issue("blocker", "BARCODE_SCIENTIFIC", k, sku, "barcode", bc, "Full barcode digits",
                                 "Barcode is in scientific notation; digits may be lost.", "Query the vendor."))
            elif why:
                out.append(issue("warning", "BARCODE_INVALID", k, sku, "barcode", bc, "Valid GTIN-8/12/13/14",
                                 f"Barcode {why}.", "Confirm with the vendor."))
    # findings from reading the vendor file
    for fd in catalog.get("findings", []):
        if fd["code"] in ("SOURCE_CONFLICT", "SKU_WHITESPACE", "BARCODE_LEADING_ZERO") and products.get(
                fd["key"], {}).get("eligible"):
            p = products[fd["key"]]
            out.append(issue(fd["severity"], fd["code"], fd["key"], value(p, "sku"), fd["field"],
                             value(p, fd["field"]), reason=fd["detail"], action="Check the vendor value."))
    # -- product fields -------------------------------------------------------------------
    for k in eligible:
        p = products[k]
        sku = value(p, "sku")
        cat = value(p, "category")
        for f, d in p["fields"].items():
            st = d.get("status")
            if d.get("locked"):
                st = "ok" if st in ("review", "warning") else st
            if st == "blocker":
                code = {"vendor_query": "MISSING_VALUE", "manual_review": "NEEDS_DECISION"}.get(d["class"], "BLOCKED")
                if d.get("conflicts") and d.get("source", "").startswith("response:"):
                    code = "RESPONSE_CONFLICT"
                out.append(issue("blocker", code, k, sku, f, d.get("value"), expected=_expected(refs, f),
                                 reason=d.get("reason", ""), action=_action(d)))
            elif st == "warning":
                out.append(issue("warning", "CHECK_VALUE", k, sku, f, d.get("value"), reason=d.get("reason", ""),
                                 action=_action(d)))
            elif st == "review":
                out.append(issue("warning", "REVIEW_JUDGMENT", k, sku, f, d.get("value"),
                                 reason=f"{d.get('reason', '')}",
                                 action="Approve, edit or reject on the product page."))
            for t in d.get("claims") or []:
                out.append(issue("warning", "UNSUPPORTED_CLAIM", k, sku, f, d.get("value"),
                                 "Claims supported by vendor evidence",
                                 f"'{t}' appears in the content but not in the vendor data or documents.",
                                 "Remove the claim or add the supporting vendor evidence."))
            for t in d.get("forbidden") or []:
                out.append(issue("blocker", "FORBIDDEN_TERM", k, sku, f, d.get("value"), "No forbidden terms",
                                 f"'{t}' is a forbidden term in the content rules.", "Edit the content."))
        if cat and refs.category(cat) is None and not str(cat).count(";"):
            out.append(issue("blocker", "INVALID_CATEGORY", k, sku, "category", cat, "A category from the approved "
                             "list", "The category is not in the approved list.", "Choose an approved category."))
        if ";" in str(cat):
            cats = [c.strip() for c in str(cat).split(";") if c.strip()]
            allowed = refs.content_rule("multi_category").lower() in ("y", "yes", "true", "1")
            bad = [c for c in cats if not refs.category(c)]
            if bad:
                out.append(issue("blocker", "INVALID_CATEGORY", k, sku, "category", cat, "Approved categories",
                                 "Not in the approved list: " + ", ".join(bad), "Choose approved categories."))
            if not allowed or not all((refs.category(c) or {}).get("multi") for c in cats if refs.category(c)):
                out.append(issue("blocker", "MULTI_CATEGORY", k, sku, "category", cat, "One category",
                                 "More than one category is only allowed where the content rules and category "
                                 "list permit it.", "Keep one category."))
        special = (p["fields"].get("category") or {}).get("special")
        if special:
            out.append(issue("warning", "SPECIAL_CATEGORY", k, sku, "category", cat, reason=special,
                             action="Check the special-category exception rules before approval."))
        # content limits
        title = str(value(p, "title"))
        mx = _int(refs.content_rule("title_max_length", cat))
        if title and mx and len(title) > mx:
            out.append(issue("blocker", "TITLE_TOO_LONG", k, sku, "title", title, f"At most {mx} characters",
                             f"Title has {len(title)} characters.", "Shorten the title."))
        if title and (title != title.strip() or "  " in title):
            out.append(issue("warning", "TITLE_SPACING", k, sku, "title", title, reason="Extra spaces in title.",
                             action="Edit the title."))
        if title and re.search(r"\b(\w+) \1\b", title, re.I):
            out.append(issue("warning", "TITLE_REPEAT", k, sku, "title", title, reason="A word is repeated.",
                             action="Edit the title."))
        sd = str(value(p, "short_description"))
        mx = _int(refs.content_rule("short_description_max_length", cat))
        if sd and mx and len(sd) > mx:
            out.append(issue("blocker", "SHORT_DESC_TOO_LONG", k, sku, "short_description", sd[:80],
                             f"At most {mx} characters", f"{len(sd)} characters.", "Shorten it."))
        ld = str(value(p, "long_description"))
        mn = _int(refs.content_rule("long_description_min_length", cat))
        if ld and mn and len(ld) < mn:
            out.append(issue("warning", "LONG_DESC_SHORT", k, sku, "long_description", ld[:80],
                             f"At least {mn} characters", f"{len(ld)} characters.", "Expand from vendor evidence."))
        bl = _int(refs.content_rule("bullet_max_length", cat))
        if bl:
            for b in str(value(p, "bullet_points")).split("\n"):
                if len(b) > bl:
                    out.append(issue("warning", "BULLET_TOO_LONG", k, sku, "bullet_points", b[:80],
                                     f"At most {bl} characters", f"A bullet has {len(b)} characters.", "Shorten it."))
        # images
        conf = [i for i in p.get("images", []) if i.get("confidence") == "confirmed"]
        unsure = [i for i in p.get("images", []) if i.get("confidence") == "uncertain"]
        need = refs.content_rule("images_required", cat).lower() in ("y", "yes", "true", "1")
        mn = _int(refs.content_rule("min_images", cat)) or (1 if need else 0)
        if not images_uploaded:
            out.append(issue("info", "IMAGES_PENDING", k, sku, "images", reason="No images uploaded to the ticket "
                             "yet; image checks are pending.", action="Upload images when available."))
        elif mn and len(conf) < mn:
            out.append(issue("blocker", "IMAGES_MISSING", k, sku, "images", f"{len(conf)} confirmed",
                             f"At least {mn}", "Not enough images matched to this SKU.",
                             "Upload images named with the SKU or link them on the product page."))
        for i in unsure:
            out.append(issue("warning", "IMAGE_UNCERTAIN", k, sku, "images", i["file"], reason=i["how"],
                             action="Confirm or remove the association on the product page."))
        for i in conf:
            if i.get("duplicates"):
                out.append(issue("warning", "IMAGE_DUPLICATE", k, sku, "images", i["file"],
                                 reason="Identical image file(s): " + ", ".join(i["duplicates"]),
                                 action="Check that the right image is used."))
        for e in p.get("evidence", []):
            if e.get("type") == "image_inspection" and (e.get("observed") or {}).get("mismatches"):
                out.append(issue("warning", "IMAGE_MISMATCH", k, sku, "images", e.get("file"),
                                 reason="Assistant noticed: " + "; ".join(e["observed"]["mismatches"]),
                                 action="Compare the image with the vendor data."))
        # target systems
        for system in ("m2", "ns"):
            t = refs.templates.get(system)
            if not t:
                continue

            def tissue(*a, _system=system, **kw):
                return issue(*a, system=_system, **kw)
            tv = (p.get("targets") or {}).get(system, {})
            rows = {r["target"]: r for r in (refs.mapping or {}).get("mappings", []) if r["system"] == system}
            for col in t["columns"]:
                name = col["name"]
                r = rows.get(name)
                cell = tv.get(name, {})
                v = cell.get("value", "")
                required = name in t["required"] or (r and r["required"])
                if required and not r:
                    out.append(tissue("blocker", "UNMAPPED_REQUIRED", k, sku, name, "", "A confirmed mapping",
                                     "Required template column has no mapping row.",
                                     "Add the mapping to the Mapping workbook."))
                    continue
                if cell.get("issue"):
                    out.append(tissue("blocker" if required else "warning", "MAPPING_ISSUE", k, sku, name, v,
                                     reason=cell["issue"], action="Correct the source value or the value map."))
                if required and str(v).strip() == "":
                    out.append(tissue("blocker", "REQUIRED_EMPTY", k, sku, name, "", "A value",
                                     f"Required {system.upper()} column is empty"
                                     + (f" (from {r['source'] or r['rule']})" if r else "") + ".",
                                     "Fill the source field or answer the vendor query."))
                    continue
                if str(v).strip() == "" or not r:
                    continue
                allowed = refs.approved_values(r["list"]) if r["list"] else t["dropdowns"].get(name)
                if allowed is not None:
                    parts = [x.strip() for x in str(v).split(r["separator"])] if r["separator"] else [str(v)]
                    bad = [x for x in parts if x not in allowed]
                    if bad:
                        out.append(tissue("blocker", "NOT_APPROVED_VALUE", k, sku, name, v,
                                         f"Value from {'list ' + r['list'] if r['list'] else 'the template drop-down'}",
                                         "Not an approved value: " + ", ".join(bad), "Map to an approved value."))
                typ = r["type"]
                if typ in ("number", "decimal", "integer", "int", "price", "float") and not _num(v):
                    out.append(tissue("blocker", "TYPE_ERROR", k, sku, name, v, f"A {typ}",
                                     "Not a number.", "Correct the source value."))
                elif typ in ("integer", "int") and _num(v) and float(str(v).replace(",", "")) % 1:
                    out.append(tissue("blocker", "TYPE_ERROR", k, sku, name, v, "A whole number",
                                     "Not a whole number.", "Correct the source value."))
                if r["max_length"] and len(str(v)) > r["max_length"]:
                    out.append(tissue("blocker", "TOO_LONG", k, sku, name, str(v)[:80],
                                     f"At most {r['max_length']} characters", f"{len(str(v))} characters.",
                                     "Shorten the value."))
                if r["validation"]:
                    try:
                        if not re.fullmatch(r["validation"], str(v)):
                            out.append(tissue("blocker", "PATTERN", k, sku, name, v,
                                             f"Matches {r['validation']}", "Does not match the validation rule.",
                                             "Correct the value."))
                    except re.error:
                        out.append(tissue("warning", "BAD_RULE", k, sku, name,
                                         reason=f"Mappings row {r['row']}: invalid validation pattern.",
                                         action="Fix the Mapping workbook."))
        # cross-system: columns fed from the same identifier must agree
        for f in ("sku", "barcode"):
            vals = {}
            for system in ("m2", "ns"):
                for col, cell in ((p.get("targets") or {}).get(system) or {}).items():
                    if cell.get("rule") == "copy" and norm(cell.get("source")) in (norm(f), norm(label(f))):
                        vals.setdefault(system, set()).add(str(cell["value"]))
            if len(vals) == 2 and vals["m2"] != vals["ns"]:
                out.append(issue("blocker", "CROSS_SYSTEM", k, sku, f, f"{vals['m2']} / {vals['ns']}",
                                 "The same value in Magento 2 and NetSuite",
                                 "The two outputs carry different values for the same identifier.",
                                 "Check the mapping rules."))
        if p.get("targets") and value(p, "sku"):
            for system in ("m2", "ns"):
                if system in refs.templates and not any(norm(c.get("source")) == "sku" or c.get("value") == value(
                        p, "sku") for c in p["targets"].get(system, {}).values()):
                    out.append(issue("warning", "NO_SKU_COLUMN", k, sku, "sku", system=system,
                                     reason=f"No {system.upper()} column carries the SKU.",
                                     action="Check the mapping rules."))
    # outstanding queries
    for q in queries.get("queries", []):
        if q["status"] in ("open", "partially", "conflict") and products.get(q["key"], {}).get("eligible"):
            sev = "blocker" if q["priority"] == "High" else "warning"
            out.append(issue(sev, "OPEN_QUERY", q["key"], q["sku"], q["field"], q.get("current"), q.get("expected"),
                             f"Vendor query {q['id']} is {q['status']}: {q['reason']}",
                             "Wait for the vendor response, or set the value on the product page."))
    # de-duplicate and apply waivers
    seen, uniq = set(), []
    for i in out:
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        uniq.append(i)
    waivers = {w["issue"]: w for w in meta.get("waivers", []) if w.get("active", True)}
    for i in uniq:
        w = waivers.get(i["id"])
        if w and i["severity"] == "blocker":
            i["waived"] = {"by": w["by"], "at": w["at"], "reason": w["reason"]}
    counts = {s: sum(1 for i in uniq if i["severity"] == s and not i.get("waived")) for s in SEVERITIES}
    counts["waived"] = sum(1 for i in uniq if i.get("waived"))
    blocked = {i["key"] for i in uniq if i["severity"] == "blocker" and not i.get("waived") and i["key"]}
    ticket_blockers = [i for i in uniq if i["severity"] == "blocker" and not i["key"] and not i.get("waived")]
    return {"issues": uniq, "counts": counts, "blocked_keys": sorted(blocked),
            "ticket_blockers": len(ticket_blockers)}


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _expected(refs, field):
    if field.startswith("attr:"):
        a = next((x for x in (refs.mapping or {}).get("attributes", []) if x["attribute"] == field[5:]), None)
        if a:
            vals = refs.approved_values(a["list"]) or []
            return f"One of {len(vals)} approved {a['list']} values"
    if field == "category":
        return "An approved category"
    return "A value"


def _action(d):
    return {"vendor_query": "See the vendor query sheet.",
            "manual_review": "Decide on the product page.",
            "judgment": "Review the proposal on the product page."}.get(d.get("class"), "Check the value.")
