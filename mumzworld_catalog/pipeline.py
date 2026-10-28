"""Running a ticket through the workflow and keeping its state.

``process`` can be run any number of times (after more files, a changed column
mapping or new reference versions).  Work already done is kept: products are
matched to their previous version by vendor file + row, and reviewer-locked
values, vendor responses, review decisions, image links, evidence and match
decisions survive the re-run.
"""
from __future__ import annotations

import shutil
import time
from collections import Counter

from . import ai as ai_mod
from . import cataloging, exports, images, knowledge, matching, normalize, queries, validation, website
from . import cache, config
from .fields import fv, label, propose_column_map, resolve_field_name, set_field, value
from .reference import Refs, extract_text
from .sheets import SheetError, inventory
from .storage import Ticket, now, stamp

KEEP_KEYS = ("review", "review_log", "evidence", "images_removed", "match_override")


class WorkflowError(ValueError):
    pass


# -- stage 1: inventory --------------------------------------------------------------------------
def vendor_tables(ticket: Ticket) -> list[dict]:
    meta = ticket.meta
    out = []
    for f in ticket.files("vendor_input"):
        try:
            sheet = meta.get("vendor_sheets", {}).get(f["id"])
            reader = (lambda p, sh_=sheet: normalize.sh.read_table(p, sheet=sh_)) if sheet else \
                normalize.detect_vendor_sheet
            t = dict(cache.get(f"vendor-{sheet or ''}", f["sha1"], ticket.path(f["stored"]), reader))
        except (SheetError, OSError) as e:
            out.append({"file_id": f["id"], "file": f["name"], "error": str(e), "columns": [], "records": [],
                        "sheet": ""})
            continue
        t.update(file_id=f["id"], file=f["name"])
        out.append(t)
    return out


def ensure_column_maps(ticket: Ticket, tables: list[dict]) -> dict:
    meta = ticket.meta
    maps = dict(meta.get("column_map") or {})
    changed = False
    for t in tables:
        if t["file_id"] not in maps and t["columns"]:
            maps[t["file_id"]] = propose_column_map(t["columns"])
            changed = True
    if changed:
        ticket.update(lambda m: m.update(column_map=maps))
    return maps


def file_inventory(ticket: Ticket) -> dict:
    tables = vendor_tables(ticket)
    maps = ensure_column_maps(ticket, tables)
    files = []
    for f in ticket.meta["files"]:
        info = {"readable": True, "sheets": [], "error": ""}
        if f["kind"] != "images":
            info = cache.get("inventory", f["sha1"], ticket.path(f["stored"]), inventory)
        files.append({**f, **info})
    vendor = []
    for t in tables:
        cmap = maps.get(t["file_id"], {})
        sku_col = next((c for c, fld in cmap.items() if fld == "sku"), None)
        skus = {str(r["values"].get(sku_col)).strip() for r in t["records"]
                if sku_col and r["values"].get(sku_col) not in (None, "")}
        vendor.append({"file_id": t["file_id"], "file": t["file"], "sheet": t.get("sheet"), "error": t.get("error"),
                       "records": len(t["records"]), "skus": len(skus), "columns": t["columns"], "map": cmap,
                       "unmapped": [c for c in t["columns"] if not cmap.get(c)]})
    refs = Refs.for_ticket(ticket)
    return {"files": files, "vendor": vendor, "missing_refs": refs.missing(), "ref_errors": refs.errors,
            "ns_pending": refs.ns_master_pending,
            "images": len(list(ticket.path("images").glob("*"))),
            "unreadable": [f["name"] for f in files if not f.get("readable", True)]}


# -- processing ----------------------------------------------------------------------------------
def _snapshot_refs(ticket: Ticket, refs: Refs) -> dict:
    used = refs.used()
    for kind, entries in refs.sources.items():
        for e in entries:
            if e["origin"] == "library":
                dest = ticket.path("refs", f"{kind}__{e['id']}__{e['path'].name.split('__', 1)[-1]}")
                if not dest.exists():
                    shutil.copyfile(e["path"], dest)
    return used


def _doc_text(ticket: Ticket) -> str:
    parts = []
    for f in ticket.files("documents"):
        text, _ = extract_text(ticket.path(f["stored"]), 100_000)
        parts.append(text)
    return "\n".join(parts)


def _carry_over(old: dict, new: dict) -> None:
    """Keep reviewer and response work from the previous version of a product."""
    for k in KEEP_KEYS:
        if k in old:
            new[k] = old[k]
    new["images"] = [i for i in old.get("images", []) if i.get("how", "").startswith("reviewer")]
    for f, d in old.get("fields", {}).items():
        if d.get("locked") or d.get("source", "").startswith("response:"):
            new["fields"][f] = d
        elif f in new["fields"]:
            if old["fields"][f].get("history"):
                new["fields"][f]["history"] = old["fields"][f]["history"]
        else:
            if f in ("category", "category_id", "tags", "title", "short_description", "long_description",
                     "bullet_points", "search_terms") or f.startswith("attr:"):
                new["fields"][f] = d  # recomputed below; history kept


def process(ticket: Ticket, by: str = "", ai_client=None) -> dict:
    if ticket.meta.get("processing"):
        raise WorkflowError("This ticket is already being processed.")
    if not ticket.files("vendor_input"):
        raise WorkflowError("Upload the vendor product input file first.")
    ns_master_wait(Refs.for_ticket(ticket))
    ticket.update(lambda m: m.update(processing=True, state="processing"))
    try:
        return _process(ticket, by, ai_client)
    finally:
        ticket.update(lambda m: m.update(processing=False))
        ticket.update(lambda m: m.update(state=compute_state(ticket)))


def ns_master_wait(refs: Refs) -> None:
    j = refs.ns_master_pending
    if j:
        mins = (time.time() - j["started"]) / 60
        raise WorkflowError(f"The NetSuite master ({j['name']}, {j.get('size_mb', '?')} MB) is still being read: "
                            f"{j.get('rows') or 0:,} rows so far, {mins:.0f} min in. This happens once per file; "
                            "process the ticket when it is done (the ticket page shows progress).")


def _process(ticket: Ticket, by: str, ai_client) -> dict:
    at = now()
    refs = Refs.for_ticket(ticket)
    ns_master_wait(refs)
    used = _snapshot_refs(ticket, refs)
    tables = [t for t in vendor_tables(ticket) if not t.get("error")]
    maps = ensure_column_maps(ticket, tables)
    old = ticket.catalog()
    old_by_src = {(p["source"]["file_id"], p["source"]["row"]): p for p in old["products"].values()}
    built, border, findings = normalize.build_records(tables, maps, at)
    products, order = {}, []
    nxt = old.get("next_key", len(old["products"]) + 1)
    for k in border:
        p = built[k]
        prev = old_by_src.get((p["source"]["file_id"], p["source"]["row"]))
        if prev:
            key = prev["key"]
            _carry_over(prev, p)
        else:
            key = f"P{nxt:05d}"
            nxt += 1
        for fd in findings:
            if fd["key"] == k:
                fd["key"] = key
        p["key"] = key
        products[key] = p
        order.append(key)
    for k in order:  # duplicate links refer to build keys; recompute on final keys
        products[k].pop("duplicate_sku", None)
        products[k].pop("duplicate_barcode", None)
    overrides = {k: products[k]["match_override"] for k in order if products[k].get("match_override")}
    exclusions = matching.match_all(products, order, refs.ns_master, overrides)
    img_summary = images.associate(products, order, ticket.path("images"))
    images_uploaded = img_summary["total"] > 0
    if ai_client is not None:
        assistant, ai_note = ai_mod.Assistant(ai_client), ""
    else:
        assistant, ai_note = ai_mod.get()
    notes = [] if assistant else [ai_note]
    index = cataloging.CategoryIndex(refs.categories) if refs.categories else None
    doc_text = _doc_text(ticket)
    examples = [r["corrected"] for r in refs.reviews if r.get("field", "").lower() == "title"
                and knowledge.norm(r.get("status")) in knowledge.APPROVED_WORDS][:30]
    for k in order:
        cataloging.catalog_product(products[k], refs, index, assistant, at, notes, doc_text, examples)
    catalog = {"products": products, "order": order, "findings": findings, "exceptions": old.get("exceptions", []),
               "next_key": nxt, "exclusions": exclusions, "images": img_summary, "notes": notes,
               "revision": old.get("revision", 0) + 1, "processed_at": at}
    qdata = ticket.load("queries", {"queries": [], "next": 1})
    first = validation.validate(catalog, refs, {"queries": []}, ticket.meta, images_uploaded)
    qcounts = queries.refresh(qdata, products, order, refs, first["issues"], images_uploaded)
    val = validation.validate(catalog, refs, qdata, ticket.meta, images_uploaded)
    ticket.save_catalog(catalog)
    ticket.save("queries", qdata)
    ticket.save("validation", val)
    summary = {"at": at, "records": len(order), "eligible": sum(1 for k in order if products[k]["eligible"]),
               "excluded": sum(1 for e in exclusions if e["match_type"] == matching.LABELS[matching.EXISTING]),
               "held": sum(1 for k in order if products[k]["match"].get("status") in ("possible_match", "conflict")
                           and not products[k].get("match_override")),
               "queries": qcounts, "validation": val["counts"], "images": img_summary["total"], "notes": notes}

    def upd(m):
        m["refs_used"] = used
        m["processed_at"] = at
        m["last_run"] = summary
    ticket.update(upd)
    ticket.log(f"Processed: {summary['records']} records, {summary['eligible']} new-item candidates, "
               f"{val['counts']['blocker']} blockers, {len(queries.outstanding(qdata))} open queries", by)
    return summary


def revalidate(ticket: Ticket) -> dict:
    """Re-run cataloging of unlocked derived fields, queries and validation after edits."""
    refs = Refs.for_ticket(ticket)
    catalog = ticket.catalog()
    at = now()
    images_uploaded = any(ticket.path("images").iterdir())
    doc_text = _doc_text(ticket)
    for k in catalog["order"]:
        p = catalog["products"][k]
        if p.get("eligible"):
            p["targets"] = {s: cataloging.target_values(p, refs, s) for s in ("m2", "ns")}
            cataloging.check_claims(p, refs, doc_text)
    qdata = ticket.load("queries", {"queries": [], "next": 1})
    first = validation.validate(catalog, refs, {"queries": []}, ticket.meta, images_uploaded)
    queries.refresh(qdata, catalog["products"], catalog["order"], refs, first["issues"], images_uploaded)
    val = validation.validate(catalog, refs, qdata, ticket.meta, images_uploaded)
    catalog["revision"] = catalog.get("revision", 0) + 1
    catalog["validated_at"] = at
    ticket.save_catalog(catalog)
    ticket.save("queries", qdata)
    ticket.save("validation", val)
    ticket.update(lambda m: m.update(state=compute_state(ticket)))
    return val


def recatalog(ticket: Ticket, keys: list[str], ai_client=None) -> None:
    refs = Refs.for_ticket(ticket)
    catalog = ticket.catalog()
    at = now()
    assistant = ai_mod.Assistant(ai_client) if ai_client is not None else ai_mod.get()[0]
    index = cataloging.CategoryIndex(refs.categories) if refs.categories else None
    notes = catalog.setdefault("notes", [])
    doc_text = _doc_text(ticket)
    for k in keys:
        p = catalog["products"].get(k)
        if p:
            cataloging.catalog_product(p, refs, index, assistant, at, notes, doc_text)
    ticket.save_catalog(catalog)


# -- state ---------------------------------------------------------------------------------------
def compute_state(ticket: Ticket) -> str:
    meta = ticket.meta
    if meta.get("processing"):
        return "processing"
    catalog = ticket.catalog()
    if not catalog["order"]:
        return "draft"
    if meta.get("exports") and meta["exports"][-1].get("revision") == catalog.get("revision"):
        return "exported"
    ap = meta.get("approval")
    if ap and ap.get("revision") == catalog.get("revision"):
        return "approved"
    val = ticket.load("validation", {})
    if val.get("ticket_blockers"):
        return "blocked"
    q = ticket.load("queries", {"queries": []})
    if any(x["priority"] == "High" for x in queries.outstanding(q)):
        return "awaiting_vendor"
    eligible = [k for k in catalog["order"] if catalog["products"][k].get("eligible")]
    if eligible and set(eligible) <= set(val.get("blocked_keys", [])):
        return "blocked"
    return "awaiting_review"


def dashboard(ticket: Ticket) -> dict:
    catalog = ticket.catalog()
    val = ticket.load("validation", {"issues": [], "counts": {}, "blocked_keys": []})
    q = ticket.load("queries", {"queries": []})
    products = catalog["products"]
    eligible = [k for k in catalog["order"] if products[k].get("eligible")]
    blocked = set(val.get("blocked_keys", []))
    approved = [k for k in eligible if products[k].get("review", {}).get("status") == "approved"]
    return {
        "total": len(catalog["order"]),
        "excluded_existing": sum(1 for k in catalog["order"] if products[k].get("match", {}).get("status") ==
                                 matching.EXISTING and not products[k].get("eligible")),
        "held": sum(1 for k in catalog["order"] if products[k].get("match", {}).get("status") in
                    ("possible_match", "conflict") and not products[k].get("eligible")),
        "candidates": len(eligible),
        "ready": sum(1 for k in eligible if k not in blocked),
        "blocked": sum(1 for k in eligible if k in blocked),
        "approved": len(approved),
        "approved_ready": sum(1 for k in approved if k not in blocked),
        "rejected": sum(1 for k in eligible if products[k].get("review", {}).get("status") == "rejected"),
        "open_queries": len(queries.outstanding(q)),
        "unresolved_review": sum(1 for i in val.get("issues", []) if i["code"] in ("REVIEW_JUDGMENT", "NEEDS_DECISION",
                                                                                 "UNRESOLVED_MATCH")),
        "counts": val.get("counts", {}),
        "ticket_blockers": val.get("ticket_blockers", 0),
    }


# -- review actions ------------------------------------------------------------------------------
def _authorized(by: str) -> None:
    if not by.strip():
        raise WorkflowError("Enter your name: reviewer decisions are recorded with it.")
    allowed = config.load().get("authorized_reviewers") or []
    if allowed and by not in allowed:
        raise WorkflowError(f"{by} is not in the list of authorized reviewers (Settings).")


def edit_field(ticket: Ticket, key: str, field: str, new_value: str, by: str, reason: str = "") -> None:
    if not by.strip():
        raise WorkflowError("Enter your name: edits are recorded with it.")
    catalog = ticket.catalog()
    p = catalog["products"][key]
    at = now()
    cur = p["fields"].get(field) or {}
    nv = fv(new_value.strip() or None, original=cur.get("original"), source=f"reviewer:{by}", cls="manual",
            reason=reason or "Set by reviewer", status="ok", previous=cur.get("value"),
            previous_class=cur.get("class"))
    nv["locked"] = True
    set_field(p, field, nv, at, force=True)
    p.setdefault("review_log", []).append({"at": at, "by": by, "event": f"{label(field)} set to '{new_value}'"
                                                                         + (f" ({reason})" if reason else "")})
    if p.get("review", {}).get("status") == "approved":
        p["review"] = {"status": "pending", "note": "Reset after an edit"}
    ticket.save_catalog(catalog)
    if field == "category" or field.startswith("attr:") or field in ("brand", "product_name", "colour", "size"):
        recatalog(ticket, [key])
    revalidate(ticket)


def accept_field(ticket: Ticket, key: str, field: str, by: str) -> None:
    """Approve a proposed value as it stands (it becomes locked)."""
    _authorized(by)
    catalog = ticket.catalog()
    p = catalog["products"][key]
    d = p["fields"][field]
    d["locked"] = True
    d["approved_by"], d["approved_at"] = by, now()
    if d.get("status") == "review":
        d["status"] = "ok"
    p.setdefault("review_log", []).append({"at": now(), "by": by, "event": f"Accepted {label(field)}: "
                                                                            f"'{d.get('value')}'"})
    ticket.save_catalog(catalog)
    revalidate(ticket)


def unlock_field(ticket: Ticket, key: str, field: str, by: str) -> None:
    catalog = ticket.catalog()
    p = catalog["products"][key]
    d = p["fields"].get(field)
    if d:
        d["locked"] = False
        if d.get("class") == "manual":
            p["fields"].pop(field)
        p.setdefault("review_log", []).append({"at": now(), "by": by, "event": f"Unlocked {label(field)}"})
    ticket.save_catalog(catalog)
    recatalog(ticket, [key])
    revalidate(ticket)


def review_product(ticket: Ticket, keys: list[str], status: str, by: str, note: str = "") -> int:
    if status not in ("approved", "rejected", "pending"):
        raise WorkflowError("Unknown review status.")
    _authorized(by)
    catalog = ticket.catalog()
    val = ticket.load("validation", {"blocked_keys": []})
    blocked = set(val.get("blocked_keys", []))
    n = 0
    for k in keys:
        p = catalog["products"].get(k)
        if not p or not p.get("eligible"):
            continue
        if status == "approved" and k in blocked:
            continue
        p["review"] = {"status": status, "by": by, "at": now(), "note": note}
        p.setdefault("review_log", []).append({"at": now(), "by": by, "event": f"Product {status}"
                                               + (f": {note}" if note else "")})
        n += 1
    catalog["revision"] = catalog.get("revision", 0) + 1
    ticket.save_catalog(catalog)
    ticket.log(f"{n} product(s) marked {status}", by)
    ticket.update(lambda m: m.update(state=compute_state(ticket)))
    return n


def decide_match(ticket: Ticket, key: str, decision: str, by: str, reason: str) -> None:
    _authorized(by)
    if decision not in ("new", "existing", "clear"):
        raise WorkflowError("Unknown decision.")
    if decision != "clear" and not reason.strip():
        raise WorkflowError("Give the reason for the decision.")
    catalog = ticket.catalog()
    p = catalog["products"][key]
    if decision == "clear":
        p.pop("match_override", None)
    else:
        p["match_override"] = {"decision": decision, "by": by, "at": now(), "reason": reason}
    p.setdefault("review_log", []).append({"at": now(), "by": by, "event": f"Match decision: {decision} ({reason})"})
    ticket.save_catalog(catalog)
    ticket.log(f"Match decision for {value(p, 'sku')}: {decision}", by)
    process(ticket, by)


def waive(ticket: Ticket, issue_id: str, by: str, reason: str) -> None:
    _authorized(by)
    if not reason.strip():
        raise WorkflowError("A waiver needs a reason.")
    ticket.update(lambda m: m["waivers"].append({"issue": issue_id, "by": by, "at": now(), "reason": reason,
                                                 "active": True}))
    ticket.log(f"Waived {issue_id}: {reason}", by)
    revalidate(ticket)


def link_image(ticket: Ticket, key: str, filename: str, by: str, remove: bool = False) -> None:
    catalog = ticket.catalog()
    p = catalog["products"][key]
    at = now()
    if remove:
        p["images"] = [i for i in p.get("images", []) if i["file"] != filename]
        p.setdefault("images_removed", []).append(filename)
        ev = f"Removed image {filename}"
    else:
        if not ticket.path("images", filename).exists():
            raise WorkflowError("No such image in this ticket.")
        p["images"] = [i for i in p.get("images", []) if i["file"] != filename] + [
            {"file": filename, "how": f"reviewer {by}", "confidence": "confirmed"}]
        p["images_removed"] = [x for x in p.get("images_removed", []) if x != filename]
        ev = f"Linked image {filename}"
    p.setdefault("review_log", []).append({"at": at, "by": by, "event": ev})
    ticket.save_catalog(catalog)
    revalidate(ticket)


def add_evidence(ticket: Ticket, key: str, entry: dict, by: str) -> None:
    catalog = ticket.catalog()
    p = catalog["products"][key]
    entry.update(at=now(), by=by)
    p.setdefault("evidence", []).append(entry)
    ticket.save_catalog(catalog)


def website_lookup(ticket: Ticket, keys: list[str], by: str, opener=None) -> dict:
    catalog = ticket.catalog()
    counts = Counter()
    for k in keys:
        p = catalog["products"].get(k)
        if not p:
            continue
        q = " ".join(x for x in (value(p, "brand"), value(p, "product_name")) if x) or value(p, "sku")
        rec = website.search(q, opener)
        rec["by"] = by
        p.setdefault("evidence", []).append(rec)
        counts[rec["status"]] += 1
    ticket.save_catalog(catalog)
    ticket.log("Mumzworld.com lookup: " + ", ".join(f"{v} {k}" for k, v in counts.items()), by)
    return dict(counts)


def inspect_images(ticket: Ticket, keys: list[str], by: str, ai_client=None) -> dict:
    assistant, why = ai_mod.get() if ai_client is None else (ai_mod.Assistant(ai_client), "")
    if not assistant:
        raise WorkflowError(f"Image inspection needs the assistant: {why}")
    catalog = ticket.catalog()
    done, failed = 0, []
    for k in keys:
        p = catalog["products"].get(k)
        imgs = [i for i in (p or {}).get("images", []) if i.get("confidence") == "confirmed"
                and images.media_type(i["file"])]
        if not imgs:
            continue
        i = imgs[0]
        try:
            obs = assistant.inspect_image(ticket.path("images", i["file"]), images.media_type(i["file"]),
                                          cataloging.evidence(p))
        except Exception as e:
            failed.append(f"{value(p, 'sku')}: {e}")
            continue
        p.setdefault("evidence", []).append({"type": "image_inspection", "file": i["file"], "observed": obs,
                                             "at": now(), "by": f"assistant (requested by {by})",
                                             "note": "Visual observation only; not used for technical claims."})
        done += 1
    ticket.save_catalog(catalog)
    ticket.log(f"Image inspection: {done} product(s) inspected" + (f", {len(failed)} failed" if failed else ""), by)
    revalidate(ticket)
    return {"inspected": done, "failed": failed}


# -- vendor responses ----------------------------------------------------------------------------
def import_response(ticket: Ticket, tmp, name: str, by: str) -> dict:
    from . import responses
    entry = ticket.add_file("vendor_response", tmp, name, by)  # original kept with the ticket files
    refs = Refs.for_ticket(ticket)
    catalog = ticket.catalog()
    qdata = ticket.load("queries", {"queries": [], "next": 1})
    before = Counter(q["status"] for q in qdata.get("queries", []))
    attrs = {a["attribute"]: (resolve_field_name(a["source"] or a["attribute"]) or "")
             for a in (refs.mapping or {}).get("attributes", [])}
    attrs = {a: (f if f and not f.startswith("attr:") else "") for a, f in attrs.items()}
    summary = responses.merge(catalog, qdata, ticket.path(entry["stored"]), name, attrs)
    for x in summary["exceptions"]:
        x["file"] = name
    catalog.setdefault("exceptions", []).extend(summary["exceptions"])
    ticket.save_catalog(catalog)
    ticket.save("queries", qdata)
    recatalog(ticket, summary["touched"])
    old_issues = {i["id"] for i in ticket.load("validation", {"issues": []})["issues"]}
    val = revalidate(ticket)
    qdata = ticket.load("queries", {"queries": []})
    after = Counter(q["status"] for q in qdata["queries"])
    summary.update({
        "queries_resolved": after["resolved"] - before["resolved"],
        "queries_partial": after["partially"],
        "queries_outstanding": len(queries.outstanding(qdata)),
        "unmatched_skus": sum(1 for x in summary["exceptions"] if x["type"] in ("UNKNOWN SKU", "AMBIGUOUS SKU")),
        "new_issues": [i for i in val["issues"] if i["id"] not in old_issues and i["severity"] != "info"][:200],
        "file_id": entry["id"], "by": by,
    })
    ticket.update(lambda m: m["responses"].append({k: v for k, v in summary.items() if k != "new_issues"}
                                                  | {"new_issue_count": len(summary["new_issues"])}))
    ticket.log(f"Vendor response {name}: {summary['fields_updated']} field(s) updated, "
               f"{len(summary['conflicts'])} conflict(s), {summary['unmatched_skus']} unmatched SKU(s)", by)
    return summary


# -- approval and export -------------------------------------------------------------------------
def approve_ticket(ticket: Ticket, by: str, note: str = "") -> dict:
    _authorized(by)
    revalidate(ticket)
    catalog = ticket.catalog()
    val = ticket.load("validation")
    if val["ticket_blockers"]:
        raise WorkflowError(f"{val['ticket_blockers']} ticket-level blocker(s) must be resolved or waived first.")
    approved = [k for k in catalog["order"] if catalog["products"][k].get("eligible")
                and catalog["products"][k].get("review", {}).get("status") == "approved"]
    if not approved:
        raise WorkflowError("Approve at least one product first.")
    bad = [k for k in approved if k in set(val["blocked_keys"])]
    if bad:
        raise WorkflowError(f"{len(bad)} approved product(s) now have unresolved blockers; resolve, waive or "
                            "un-approve them.")
    ap = {"by": by, "at": now(), "note": note, "revision": catalog.get("revision"), "products": len(approved)}
    ticket.update(lambda m: m.update(approval=ap))
    ticket.log(f"Ticket approved for export ({len(approved)} products)", by)
    ticket.update(lambda m: m.update(state=compute_state(ticket)))
    return ap


def lessons(ticket: Ticket) -> dict:
    """Potential new rules from this ticket's reviewer corrections, plus recurring patterns."""
    catalog = ticket.catalog()
    refs = Refs.for_ticket(ticket)
    recs = []
    for k in catalog["order"]:
        p = catalog["products"][k]
        for f, d in p["fields"].items():
            if d.get("class") == "manual" and d.get("previous") not in (None, "") and d.get("previous") != d.get("value"):
                recs.append({"sku": value(p, "sku"), "field": f[5:] if f.startswith("attr:") else f,
                             "original": str(d["previous"]), "corrected": str(d["value"]), "status": "approved",
                             "category": value(p, "category"), "brand": value(p, "brand"),
                             "comment": d.get("reason"), "file": f"ticket {ticket.meta['ticket_number']}",
                             "row": k, "sheet": ""})
    attrs = [a["attribute"] for a in (refs.mapping or {}).get("attributes", [])]
    out = knowledge.analyse_reviews(recs, attrs)
    val = ticket.load("validation", {"issues": []})
    codes = Counter(i["code"] for i in val["issues"] if i["severity"] != "info")
    qf = Counter(q["field_label"] for q in ticket.load("queries", {"queries": []})["queries"])
    obs = [(f"Recurring validation finding: {c}", n) for c, n in codes.most_common(8) if n > 1]
    obs += [(f"Frequent vendor query: {f}", n) for f, n in qf.most_common(5) if n > 1]
    out["observations"] = obs
    out["records"] = recs
    return out


def export(ticket: Ticket, by: str) -> dict:
    meta = ticket.meta
    catalog = ticket.catalog()
    ap = meta.get("approval")
    if not ap or ap.get("revision") != catalog.get("revision"):
        raise WorkflowError("The ticket must be approved (after its latest change) before final export.")
    refs = Refs.for_ticket(ticket)
    for s in ("m2", "ns"):
        if s not in refs.templates:
            raise WorkflowError(f"The {s.upper()} template cannot be read; export stopped.")
    approved = [catalog["products"][k] for k in catalog["order"] if catalog["products"][k].get("eligible")
                and catalog["products"][k].get("review", {}).get("status") == "approved"]
    folder = ticket.path("outputs", stamp())
    folder.mkdir(parents=True, exist_ok=True)
    num = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in meta["ticket_number"]) or "ticket"
    out = {"at": now(), "by": by, "revision": catalog.get("revision"), "folder": folder.name, "files": {}}
    try:
        for s, tag in (("m2", "Magento2"), ("ns", "NetSuite")):
            t = refs.templates[s]
            ext = t["format"] if t["format"] in ("xlsx", "xlsm", "csv") else "xlsx"
            fn = f"{num}_{tag}_import.{ext}"
            out["files"][s] = {"name": fn, **exports.final_output(folder / fn, refs, s, approved)}
    except exports.ExportError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise WorkflowError(str(e)) from e
    rep = f"{num}_supporting_reports.xlsx"
    exports.reports(folder / rep, ticket.meta, catalog, refs, ticket.load("validation"), ticket.load("queries", {}),
                    catalog.get("exclusions", []), lessons(ticket))
    out["files"]["reports"] = {"name": rep}
    wc = f"{num}_working_catalog.xlsx"
    exports.working_catalog(folder / wc, catalog, refs, ticket.load("validation"))
    out["files"]["working"] = {"name": wc}
    ticket.update(lambda m: m["exports"].append(out))
    ticket.log(f"Exported {len(approved)} product(s) to Magento 2 and NetSuite files", by)
    ticket.update(lambda m: m.update(state=compute_state(ticket)))
    return out
