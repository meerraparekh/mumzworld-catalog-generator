"""Reusable rules learned from review decisions.

Review corrections become *suggestions*.  A suggestion is applied to products
only after an authorized user approves it, and every applied rule is named in
the field's provenance (``rule:R0007``).  Nothing is "learned" except what is
saved here.
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .fields import resolve_field_name
from .sheets import norm
from .storage import lock, now, read_json, write_json

APPROVED_WORDS = {"approved", "yes", "y", "done", "final", "ok", "accepted", "correct", "corrected", "closed"}
CLASSIFICATIONS = {
    "general": "General rule candidate",
    "category": "Category-specific rule candidate",
    "brand": "Brand-specific rule candidate",
    "one_off": "One-off correction",
    "conflict": "Unclear or conflicting example",
}
# Fields whose corrections can be applied as a value rule (original -> corrected).
CONTENT_FIELDS = {"title", "short_description", "long_description", "bullet_points", "search_terms"}


def path():
    d = config.DATA_DIR / "knowledge"
    d.mkdir(parents=True, exist_ok=True)
    return d / "rules.json"


def load() -> dict:
    return read_json(path(), {"rules": [], "next": 1})


def _save(data: dict) -> None:
    write_json(path(), data)


def rules(status: str | None = None) -> list[dict]:
    return [r for r in load()["rules"] if status is None or r["status"] == status]


def _key(field, frm, scope) -> str:
    return "|".join([field, norm(frm), norm(scope.get("category", "")), norm(scope.get("brand", ""))])


def add_suggestion(field: str, frm: str, to: str, scope: dict, classification: str, examples: list[dict],
                   source: str, description: str = "", kind: str = "value") -> dict:
    """Add (or extend) an unapproved suggestion.  Approved or rejected rules with the
    same key are not changed; new examples are attached for the reviewer."""
    with lock("knowledge"):
        data = load()
        k = _key(field, frm, scope)
        for r in data["rules"]:
            if r["key"] == k and r["to"] == to:
                seen = {(e.get("file"), e.get("row")) for e in r["examples"]}
                r["examples"] += [e for e in examples if (e.get("file"), e.get("row")) not in seen]
                r["updated_at"] = now()
                _save(data)
                return r
        rid = f"R{data['next']:04d}"
        data["next"] += 1
        r = {"id": rid, "key": k, "kind": kind, "field": field, "from": frm, "to": to, "scope": scope,
             "classification": classification, "description": description or _describe(field, frm, to, scope),
             "examples": examples, "source": source, "status": "suggested", "added_at": now(),
             "updated_at": now(), "notes": "", "approved_by": "", "approved_at": ""}
        data["rules"].append(r)
        _save(data)
        return r


def _describe(field, frm, to, scope) -> str:
    where = []
    if scope.get("category"):
        where.append(f"category '{scope['category']}'")
    if scope.get("brand"):
        where.append(f"brand '{scope['brand']}'")
    s = f"{field}: '{frm}' becomes '{to}'"
    return s + (" for " + " and ".join(where) if where else "")


def decide(rule_id: str, status: str, by: str, notes: str = "", edits: dict | None = None) -> dict:
    if status not in ("approved", "rejected", "suggested"):
        raise ValueError("status must be approved, rejected or suggested")
    allowed = config.load().get("authorized_reviewers") or []
    if status == "approved":
        if not by:
            raise ValueError("Enter your name to approve a rule.")
        if allowed and by not in allowed:
            raise ValueError(f"{by} is not an authorized reviewer.")
    with lock("knowledge"):
        data = load()
        r = next((x for x in data["rules"] if x["id"] == rule_id), None)
        if not r:
            raise KeyError(rule_id)
        if edits:
            for k in ("to", "description"):
                if edits.get(k) is not None and edits[k] != "":
                    r[k] = edits[k]
            if "category" in edits or "brand" in edits:
                r["scope"] = {"category": edits.get("category", r["scope"].get("category", "")),
                              "brand": edits.get("brand", r["scope"].get("brand", ""))}
                r["key"] = _key(r["field"], r["from"], r["scope"])
        r["status"], r["notes"], r["updated_at"] = status, notes or r.get("notes", ""), now()
        r["history"] = r.get("history", []) + [{"at": now(), "status": status, "by": by, "notes": notes}]
        if status == "approved":
            r["approved_by"], r["approved_at"] = by, now()
        _save(data)
        return r


def applicable(field: str, value: str, category: str = "", brand: str = "") -> dict | None:
    """The most specific approved value rule for ``field`` whose 'from' equals
    ``value`` and whose scope matches."""
    best, score = None, -1
    for r in rules("approved"):
        if r["kind"] != "value" or r["field"] != field or norm(r["from"]) != norm(value):
            continue
        sc, sb = r["scope"].get("category", ""), r["scope"].get("brand", "")
        if sc and not (category == sc or category.startswith(sc + " >")):
            continue
        if sb and norm(sb) != norm(brand):
            continue
        s = (2 if sb else 0) + (1 if sc else 0)
        if s > score:
            best, score = r, s
    return best


# -- review sheets -> suggestions ---------------------------------------------------------
def _approved(status: str) -> bool:
    return norm(status) in APPROVED_WORDS


def analyse_reviews(records: list[dict], attributes: list[str]) -> dict:
    """Group review decisions into lessons.  Returns {"lessons": [...], "skipped": n}."""
    groups = defaultdict(list)
    skipped = 0
    attr_norm = {norm(a): a for a in attributes}
    for rec in records:
        if not rec.get("corrected") or rec.get("corrected") == rec.get("original"):
            skipped += 1
            continue
        f = norm(rec["field"])
        field = f"attr:{attr_norm[f]}" if f in attr_norm else resolve_field_name(rec["field"]) or rec["field"]
        groups[(field, norm(rec.get("original", "")))].append(rec)
    lessons = []
    for (field, _), recs in groups.items():
        approved = [r for r in recs if _approved(r.get("status", ""))]
        unapproved = len(recs) - len(approved)
        base = approved or recs
        targets = defaultdict(list)
        for r in base:
            targets[r["corrected"]].append(r)
        lesson = {"field": field, "from": base[0].get("original", ""), "unapproved": unapproved,
                  "examples": [{"file": r.get("file"), "sheet": r.get("sheet"), "row": r.get("row"),
                                "sku": r.get("sku"), "comment": r.get("comment"), "reason": r.get("reason"),
                                "status": r.get("status"), "date": r.get("date"), "version": r.get("version"),
                                "category": r.get("category"), "brand": r.get("brand")} for r in base]}
        if len(targets) > 1:
            # Different corrections for the same original value: separable by category or brand?
            cats = {to: {r.get("category", "") for r in rs} for to, rs in targets.items()}
            brands = {to: {norm(r.get("brand", "")) for r in rs} for to, rs in targets.items()}
            if all(len(c) == 1 and "" not in c for c in cats.values()) and len(
                    {next(iter(c)) for c in cats.values()}) == len(cats):
                for to, rs in targets.items():
                    lessons.append({**lesson, "to": to, "classification": "category",
                                    "scope": {"category": rs[0].get("category", "")},
                                    "examples": [e for e in lesson["examples"] if e["category"] == rs[0].get(
                                        "category", "")]})
                continue
            if all(len(b) == 1 and "" not in b for b in brands.values()) and len(
                    {next(iter(b)) for b in brands.values()}) == len(brands):
                for to, rs in targets.items():
                    lessons.append({**lesson, "to": to, "classification": "brand",
                                    "scope": {"brand": rs[0].get("brand", "")},
                                    "examples": [e for e in lesson["examples"] if norm(e["brand"]) == norm(
                                        rs[0].get("brand", ""))]})
                continue
            versions = {r.get("version") or r.get("date") for r in base}
            lessons.append({**lesson, "to": " / ".join(targets), "classification": "conflict", "scope": {},
                            "note": "Different corrections for the same value"
                            + (f" across versions/dates {', '.join(sorted(v for v in versions if v))}"
                               if len(versions) > 1 else "") + "; needs a human decision."})
            continue
        to = next(iter(targets))
        if not approved:
            cls = "conflict"
            note = "No approved example (approval status blank or not approved)."
        elif len(approved) == 1:
            cls, note = "one_off", ""
        else:
            brands = {norm(r.get("brand", "")) for r in approved}
            cats = {r.get("category", "") for r in approved}
            if len(brands) == 1 and "" not in brands:
                cls = "brand"
            elif len(cats) == 1 and "" not in cats:
                cls = "category"
            else:
                cls = "general"
            note = ""
        scope = {}
        if cls == "brand":
            scope = {"brand": approved[0].get("brand", "")}
        elif cls == "category":
            scope = {"category": approved[0].get("category", "")}
        lessons.append({**lesson, "to": to, "classification": cls, "scope": scope, "note": note,
                        "content": field in CONTENT_FIELDS})
    lessons.sort(key=lambda x: (list(CLASSIFICATIONS).index(x["classification"]), -len(x["examples"])))
    return {"lessons": lessons, "skipped": skipped}


def suggest_from_lessons(lessons: list[dict], source: str) -> int:
    """Store rule candidates (not one-offs, conflicts or free-text content) as suggestions."""
    n = 0
    for x in lessons:
        if x["classification"] in ("one_off", "conflict") or x.get("content"):
            continue
        add_suggestion(x["field"], x["from"], x["to"], x["scope"], x["classification"], x["examples"], source)
        n += 1
    return n
