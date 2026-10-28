"""Stage 3: compare incoming products with the NetSuite master.

* Exact SKU match (and no barcode contradiction) -> existing product confirmed, excluded.
* SKU match but a different barcode, or several master rows with the SKU -> conflict.
* No SKU match but the barcode or vendor SKU matches a master row -> possible match,
  kept for investigation (never removed on a barcode alone).
* Otherwise -> new product candidate.
"""
from __future__ import annotations

from collections import defaultdict

from .fields import value
from .sheets import norm

EXISTING, NEW, POSSIBLE, CONFLICT = "existing", "new", "possible_match", "conflict"
LABELS = {EXISTING: "Existing product confirmed", NEW: "New product candidate",
          POSSIBLE: "Possible match - investigate", CONFLICT: "Duplicate or conflicting record"}


def _index(records, field):
    idx = defaultdict(list)
    for r in records:
        if r.get(field):
            idx[norm(r[field]) if field != "barcode" else r[field].lstrip("0")].append(r)
    return idx


def match_all(products: dict, order: list[str], master: dict | None, overrides: dict | None = None) -> list[dict]:
    """Set ``product["match"]`` and ``product["eligible"]``; return the exclusion /
    exception report rows.  ``overrides`` maps product key -> decision made by a
    user ({"decision": "new"|"existing", "by", "reason"})."""
    overrides = overrides or {}
    report = []
    if not master:
        for k in order:
            p = products[k]
            p["match"] = {"status": "unchecked", "label": "Not checked - NetSuite master missing",
                          "decision": "Pending", "reason": "Upload the NetSuite master to check existing products."}
            p["eligible"] = True
        return report
    recs = master["records"]
    by_sku, by_bc, by_vsku = _index(recs, "sku"), _index(recs, "barcode"), _index(recs, "vendor_sku")
    for k in order:
        p = products[k]
        sku, bc = value(p, "sku"), value(p, "barcode")
        m = {"status": NEW, "match_type": "", "ns_record": None, "reason": "No master row has this SKU, barcode "
             "or vendor SKU."}
        sku_hits = by_sku.get(norm(sku), []) if sku else []
        bc_hits = by_bc.get(bc.lstrip("0"), []) if bc else []
        vsku_hits = by_vsku.get(norm(sku), []) if sku else []
        if len(sku_hits) > 1:
            m = {"status": CONFLICT, "match_type": "SKU", "ns_record": sku_hits[0],
                 "reason": f"{len(sku_hits)} master rows have SKU {sku} (rows "
                           + ", ".join(str(r["row"]) for r in sku_hits) + ")."}
        elif sku_hits:
            r = sku_hits[0]
            if bc and r.get("barcode") and r["barcode"].lstrip("0") != bc.lstrip("0"):
                m = {"status": CONFLICT, "match_type": "SKU (barcode differs)", "ns_record": r,
                     "reason": f"SKU matches master row {r['row']} but its barcode is {r['barcode']} and the vendor "
                               f"sent {bc}."}
            else:
                m = {"status": EXISTING, "match_type": "SKU" + (" + barcode" if bc and r.get("barcode") else ""),
                     "ns_record": r, "reason": f"Exact SKU match with master row {r['row']}."}
        elif bc_hits or vsku_hits:
            hits = bc_hits or vsku_hits
            m = {"status": POSSIBLE, "match_type": "barcode" if bc_hits else "vendor SKU", "ns_record": hits[0],
                 "reason": ("Barcode" if bc_hits else "Vendor SKU") + f" matches master SKU {hits[0]['sku']} (row "
                           f"{hits[0]['row']}) but the SKU differs. Could be a data error or a legitimate exception."}
        ov = overrides.get(k)
        m["label"] = LABELS[m["status"]]
        if ov:
            m["override"] = ov
            m["decision"] = "Create as new item" if ov["decision"] == "new" else "Exclude (existing)"
            p["eligible"] = ov["decision"] == "new"
        else:
            m["decision"] = {EXISTING: "Excluded from new-item creation", NEW: "Included",
                             POSSIBLE: "Held for investigation", CONFLICT: "Held for investigation"}[m["status"]]
            p["eligible"] = m["status"] == NEW
        p["match"] = m
        if m["status"] != NEW or ov:
            r = m.get("ns_record") or {}
            report.append({"key": k, "sku": sku, "barcode": bc, "match_identifier": m["match_type"],
                           "ns_record": " / ".join(x for x in (r.get("internal_id"), r.get("sku"), r.get("name"))
                                                   if x),
                           "ns_row": r.get("row", ""), "match_type": m["label"], "decision": m["decision"],
                           "reason": m["reason"] + (f" Decision by {ov['by']}: {ov.get('reason', '')}" if ov else "")})
    return report
