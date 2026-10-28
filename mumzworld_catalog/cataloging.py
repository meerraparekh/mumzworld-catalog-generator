"""Section 4: categorization, attribute mapping, content and target-field mapping.

Order of authority for every decision: locked reviewer values, approved rules
(knowledge base), approved reference mappings (ValueMap, dropdown lists,
category list), deterministic matching, then the optional assistant.  Anything
that cannot be decided from evidence is left unresolved and classified as a
vendor query or manual review.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import knowledge
from .fields import fv, label, resolve_field_name, set_field, value
from .reference import DEFAULT_CLAIM_TERMS, Refs
from .sheets import norm

SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with",
               "from", "x"}
STOP = {"and", "the", "for", "with", "of", "to", "a", "an", "in", "on", "by", "set", "pcs", "pc", "pack",
        "new", "&", "x", "other", "others", "accessories", "misc", "all"}
EVIDENCE_FIELDS = ["sku", "brand", "product_name", "product_type", "vendor_category", "colour", "size",
                   "dimensions", "length", "width", "height", "weight", "material", "age_group", "gender",
                   "description", "features", "specifications", "safety_info", "country_of_origin",
                   "pack_quantity", "contents", "model_number", "warranty"]


def stem(tok: str) -> str:
    t = tok.lower()
    for suf, rep in (("ies", "y"), ("ses", "s"), ("es", "e"), ("s", "")):
        if len(t) > 4 and t.endswith(suf) and not t.endswith("ss"):
            return t[: -len(suf)] + rep
    return t


def toks(text: str) -> list[str]:
    return [stem(t) for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if t not in STOP and len(t) > 1]


def evidence(p: dict) -> dict:
    ev = {label(f): value(p, f) for f in EVIDENCE_FIELDS if value(p, f)}
    for f, v in p.get("fields", {}).items():
        if f.startswith("attr:") and v.get("value"):
            ev[f[5:]] = v["value"]
    obs = [e for e in p.get("evidence", []) if e.get("type") == "image_inspection"]
    if obs:
        ev["Image observations (assistant, unverified)"] = [o.get("observed") for o in obs]
    return ev


# -- categories ------------------------------------------------------------------------------
class CategoryIndex:
    def __init__(self, categories: list[dict]):
        self.cats = categories
        df = Counter()
        self.leaf, self.parent, self.kw = {}, {}, {}
        for c in categories:
            leaf = set(toks(c["path"][-1]))
            par = set(t for lvl in c["path"][:-1] for t in toks(lvl)) - leaf
            kws = [set(toks(k)) for k in c["keywords"] if toks(k)]
            self.leaf[c["key"]], self.parent[c["key"]], self.kw[c["key"]] = leaf, par, kws
            for t in leaf | par | set().union(*kws) if kws else leaf | par:
                df[t] += 1
        n = max(1, len(categories))
        self.idf = {t: math.log(1 + n / d) for t, d in df.items()}

    def score(self, strong: set[str], weak: set[str]) -> list[tuple[float, dict, list[str]]]:
        """Score categories against product tokens.  ``strong`` come from product type
        and vendor category, ``weak`` from the name and description start."""
        allp = strong | weak
        out = []
        for c in self.cats:
            k = c["key"]
            leaf = self.leaf[k]
            hits = leaf & allp
            s, why = 0.0, []
            if leaf and hits:
                cover = len(hits) / len(leaf)
                s += sum(self.idf.get(t, 1) * (3 if t in strong else 2) for t in hits) * cover
                why.append("name: " + ", ".join(sorted(hits)))
            ph = self.parent[k] & allp
            if ph:
                s += sum(self.idf.get(t, 1) * 0.6 for t in ph)
                why.append("parent: " + ", ".join(sorted(ph)))
            for kw in self.kw[k]:
                if kw and kw <= allp:
                    s += sum(self.idf.get(t, 1) for t in kw) * 2.5
                    why.append("keyword: " + " ".join(sorted(kw)))
            if s > 0:
                out.append((round(s, 3), c, why))
        out.sort(key=lambda x: -x[0])
        return out


def assign_category(p: dict, refs: Refs, index: CategoryIndex | None, ai, at: str, notes: list) -> None:
    cur = p["fields"].get("category")
    if cur and cur.get("locked"):
        return
    if not refs.categories:
        set_field(p, "category", fv(None, source="", cls="manual_review", status="blocker",
                                    reason="No approved category list is available."), at)
        return
    brand = value(p, "brand")
    vcat, ptype = value(p, "vendor_category"), value(p, "product_type")
    by_key = {norm(c["key"]): c for c in refs.categories}
    leaves = {}
    for c in refs.categories:
        leaves.setdefault(norm(c["path"][-1]), []).append(c)
    chosen, cls, src, reason, status, cands = None, "", "", "", "ok", []
    # 1. approved rule for the vendor's category / product type
    for basis, val in (("vendor category", vcat), ("product type", ptype)):
        r = knowledge.applicable("category", val, brand=brand) if val else None
        if r and refs.category(r["to"]):
            chosen, cls, src = refs.category(r["to"]), "rule_based", f"rule:{r['id']}"
            reason = f"Approved rule {r['id']} for {basis} '{val}'."
            break
    # 2. vendor category is itself an approved category
    if not chosen:
        for val in (vcat, ptype):
            if not val:
                continue
            same_leaf = leaves.get(norm(val), [])
            hit = by_key.get(norm(val)) or (same_leaf[0] if len(same_leaf) == 1 else None)
            if hit:
                chosen, cls, src = hit, "mapped", "categories:exact"
                reason = f"Vendor value '{val}' is an approved category."
                break
    # 3. evidence scoring against approved category names and keywords
    if not chosen and index:
        strong = set(toks(vcat)) | set(toks(ptype))
        weak = set(toks(value(p, "product_name"))) | set(toks(" ".join(str(value(p, "description")).split()[:25])))
        scored = index.score(strong, weak)
        cands = [{"category": c["key"], "score": s, "why": "; ".join(w)} for s, c, w in scored[:6]]
        if scored and (len(scored) == 1 or scored[0][0] >= 1.5 * scored[1][0]):
            chosen, cls, src = scored[0][1], "judgment", "categories:evidence"
            reason = f"Best match on approved category names/keywords ({cands[0]['why']})."
            status = "review"
        elif scored and ai:
            names = [c["category"] for c in cands]
            try:
                out = ai.choose_category(evidence(p), names, refs.sop_text)
                if not out.get("insufficient"):
                    chosen, cls, src = refs.category(out["category"]), "judgment", "assistant"
                    reason = f"Assistant ({out.get('confidence')} confidence): {out.get('rationale', '')}"
                    status = "review"
            except Exception as e:  # recorded; the product falls back to manual review
                notes.append(f"{value(p, 'sku')}: assistant not used for category ({e}).")
        if not chosen:
            cls, status = "manual_review", "blocker"
            reason = ("Several approved categories fit equally: " + ", ".join(c["category"] for c in cands[:3])
                      if cands else "No approved category matches the product name, type or vendor category.")
    # 4. approved correction rules on the proposed category
    if chosen and cls != "rule_based":
        r = knowledge.applicable("category", chosen["key"], brand=brand)
        if r and refs.category(r["to"]):
            reason += f" Corrected to '{r['to']}' by approved rule {r['id']}."
            chosen, cls, src, status = refs.category(r["to"]), "rule_based", f"rule:{r['id']}", "ok"
    extra = {"candidates": cands}
    if chosen and chosen.get("special"):
        extra["special"] = chosen.get("rule") or "Special category: check its exception rules."
    set_field(p, "category", fv(chosen["key"] if chosen else None, source=src, cls=cls, reason=reason,
                                status=status, **extra), at)
    if chosen:
        set_field(p, "category_id", fv(chosen.get("id") or None, source="categories", cls="rule_based",
                                       reason="ID of the chosen category in the approved list"), at)
        set_field(p, "tags", fv(" | ".join(chosen["path"]), source="categories", cls="rule_based",
                                reason="Category hierarchy of the chosen category"), at)


# -- attributes ------------------------------------------------------------------------------
def applies(attr: dict, category: str) -> bool:
    if not attr["categories"]:
        return True
    return bool(category) and any(category == c or category.startswith(c + " >") for c in attr["categories"])


def _source_value(p: dict, attr: dict) -> tuple[str, str]:
    src = attr["source"]
    if src:
        f = resolve_field_name(src)
        if f and not f.startswith("attr:"):
            return value(p, f), f"field:{f}"
        col = next((c for c in p["original"] if norm(c) == norm(src)), None)
        if col:
            return p["original"][col], f"vendor:{col}"
        return "", f"field:{src}"
    f = resolve_field_name(attr["attribute"])
    if f and value(p, f):
        return value(p, f), f"field:{f}"
    col = next((c for c in p["original"] if norm(c) == norm(attr["attribute"])), None)
    return (p["original"][col], f"vendor:{col}") if col else ("", "")


def map_attributes(p: dict, refs: Refs, ai, at: str, notes: list) -> None:
    m = refs.mapping
    if not m:
        return
    category = value(p, "category")
    brand = value(p, "brand")
    for attr in m["attributes"]:
        field = f"attr:{attr['attribute']}"
        cur = p["fields"].get(field)
        if cur and cur.get("locked"):
            continue
        if not applies(attr, category):
            if cur and not cur.get("locked"):
                p["fields"].pop(field, None)
            continue
        raw, src = _source_value(p, attr)
        allowed = refs.approved_values(attr["list"])
        resp = cur if cur and cur.get("source", "").startswith("response:") else None
        if resp:  # a vendor answer is mapped like vendor data, keeping its provenance
            raw, src = resp.get("original") or resp.get("value") or "", resp["source"]
        if not raw:
            if attr["mandatory"]:
                set_field(p, field, fv(None, source=src, cls="vendor_query", status="blocker",
                                       reason="Mandatory attribute: no value in the vendor data."), at)
            elif cur:
                p["fields"].pop(field, None)
            continue
        if allowed is None:
            set_field(p, field, fv(raw, original=raw, source=src, cls="manual_review", status="blocker",
                                   reason=f"Approved list '{attr['list']}' is missing."), at)
            continue
        parts = [x.strip() for x in re.split(re.escape(attr["separator"]), raw)] if attr["separator"] else [raw]
        mapped, unresolved, classes, reasons = [], [], [], []
        vmap = m["value_maps"].get(attr["attribute"]) or m["value_maps"].get(attr["list"]) or {}
        lower = {v.casefold(): v for v in allowed}
        loose = {}
        for v in allowed:
            loose.setdefault(norm(v), []).append(v)
        for part in [x for x in parts if x]:
            r = knowledge.applicable(field, part, category, brand)
            if r and r["to"] in allowed:
                mapped.append(r["to"]), classes.append("rule_based"), reasons.append(f"rule {r['id']}")
                continue
            e = vmap.get(norm(part))
            if e and e["target"] in allowed:
                mapped.append(e["target"]), classes.append("mapped"), reasons.append(f"ValueMap row {e['row']}")
                continue
            if part.casefold() in lower:
                mapped.append(lower[part.casefold()]), classes.append("mapped"), reasons.append("approved value")
                continue
            if len(loose.get(norm(part), [])) == 1:
                mapped.append(loose[norm(part)][0]), classes.append("mapped")
                reasons.append("approved value ignoring spaces/punctuation")
                continue
            unresolved.append(part)
        if unresolved and ai:
            try:
                out = ai.map_value(attr["attribute"], ", ".join(unresolved), allowed, evidence(p))
                if out.get("equivalent"):
                    mapped += out["values"]
                    classes.append("judgment")
                    reasons.append(f"assistant: {out.get('rationale', '')}")
                    unresolved = []
            except Exception as e:
                notes.append(f"{value(p, 'sku')}: assistant not used for {attr['attribute']} ({e}).")
        sep = attr["separator"] or ", "
        if unresolved:
            set_field(p, field, fv(sep.join(mapped) or None, original=raw, source=src, cls="manual_review",
                                   status="blocker" if attr["mandatory"] else "warning",
                                   reason=f"No approved {attr['list']} value is equivalent to: "
                                          + ", ".join(unresolved), unresolved=unresolved), at)
            continue
        cls = next((c for c in ("judgment", "rule_based") if c in classes), "mapped")
        mapped = list(dict.fromkeys(mapped))
        set_field(p, field, fv(sep.join(mapped), original=raw, source=src, cls=cls,
                               transform="" if sep.join(mapped) == raw else f"'{raw}' -> '{sep.join(mapped)}'",
                               reason="; ".join(dict.fromkeys(reasons)),
                               status="review" if cls == "judgment" else "ok"), at)


# -- content ---------------------------------------------------------------------------------
def smart_case(text: str, mode: str) -> str:
    if mode == "sentence":
        words = text.split(" ")
        return " ".join(w if (i and (w.isupper() or any(ch.isdigit() for ch in w)))
                        else (w[:1].upper() + w[1:] if i == 0 else w) for i, w in enumerate(words))
    if mode != "title":
        return text
    out = []
    for i, w in enumerate(text.split(" ")):
        if not w:
            continue
        if (w.isupper() and len(w) > 1) or any(ch.isdigit() for ch in w) or re.search(r"[a-z][A-Z]", w):
            out.append(w)
        elif i and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append("-".join(x[:1].upper() + x[1:].lower() for x in w.split("-")))
    return " ".join(out)


def fill_pattern(pattern: str, p: dict) -> tuple[str, list[str]]:
    """Fill {placeholders}.  When a placeholder has no value, the separator after it
    is dropped, so 'A - {b}, C' with no b gives 'A - C'.
    Returns (text, placeholders that had no value)."""
    out, pending, missing, skip = "", "", [], False
    brand = value(p, "brand")
    for part in re.split(r"(\{[^}]+\})", pattern):
        if not (part.startswith("{") and part.endswith("}")):
            if skip and not re.search(r"[A-Za-z0-9]", part):
                skip = False
                continue
            skip = False
            pending += part
            continue
        name = part[1:-1].strip()
        f = resolve_field_name(name) or name
        v = str(value(p, f)).strip()
        if f == "product_name" and brand and v.lower().startswith(brand.lower() + " ") and "{brand}" in pattern:
            v = v[len(brand):].strip()  # the brand is already in the title
        if not v:
            missing.append(name)
            skip = True
            continue
        if out or re.search(r"[A-Za-z0-9]", pending):
            out += pending
        pending = ""
        out += v
    if re.search(r"[A-Za-z0-9]", pending):
        out += pending
    return re.sub(r"\s+", " ", out).strip(" -,|/"), missing


def _split_bullets(text: str) -> list[str]:
    items = re.split(r"\n+|•|•|(?:^|\s)[-*]\s+|;\s+", text)
    return [re.sub(r"^\W+", "", x).strip() for x in items if x and x.strip(" -*•")]


def build_content(p: dict, refs: Refs, ai, at: str, notes: list, doc_text: str = "",
                  example_titles: list[str] | None = None) -> None:
    cat = value(p, "category")
    rule = lambda k: refs.content_rule(k, cat)  # noqa: E731
    case = (rule("title_case") or "as_is").lower()
    f = p["fields"]
    # title
    if not (f.get("title") or {}).get("locked"):
        pattern = rule("title_pattern")
        if pattern:
            text, missing = fill_pattern(pattern, p)
            text = smart_case(text, case)
            reason = f"ContentRules title_pattern '{pattern}'" + (f"; no value for {', '.join(missing)}" if missing
                                                                  else "")
            set_field(p, "title", fv(text or None, source="rule:ContentRules title_pattern", cls="rule_based",
                                     reason=reason, status="warning" if missing else "ok", missing=missing), at)
        else:
            name = value(p, "product_name")
            set_field(p, "title", fv(smart_case(name, case) or None, source="vendor:product_name",
                                     cls="manual_review", status="review",
                                     reason="No approved title_pattern rule: vendor name proposed as is."), at)
    desc, feats = str(value(p, "description")), str(value(p, "features"))
    drafted = None
    if ai and (desc or feats):
        rules = {k: rule(k) for k in ("title_pattern", "title_max_length", "title_case",
                                      "short_description_max_length", "bullets_max", "bullet_max_length",
                                      "forbidden_terms") if rule(k)}
        try:
            drafted = ai.write_content(evidence(p), rules, example_titles or [], refs.sop_text)
        except Exception as e:
            notes.append(f"{value(p, 'sku')}: assistant not used for content ({e}).")
    if drafted:
        for field, v in (("short_description", drafted.get("short_description")),
                         ("long_description", drafted.get("long_description")),
                         ("bullet_points", "\n".join(drafted.get("bullet_points") or [])),
                         ("search_terms", ", ".join(drafted.get("search_terms") or []))):
            if not (f.get(field) or {}).get("locked"):
                set_field(p, field, fv(v or None, source="assistant", cls="judgment", status="review",
                                       reason="Drafted by the assistant from vendor evidence; review required."
                                       + (" Not supported by evidence: " + "; ".join(drafted["unsupported_requests"])
                                          if drafted.get("unsupported_requests") else "")), at)
        if not rule("title_pattern") and drafted.get("title") and not (f.get("title") or {}).get("locked"):
            set_field(p, "title", fv(drafted["title"], source="assistant", cls="judgment", status="review",
                                     reason="No approved title_pattern rule; assistant draft."), at)
    else:
        maxlen = _int(rule("short_description_max_length"))
        if not (f.get("short_description") or {}).get("locked"):
            if desc:
                first = desc.split("\n\n")[0].replace("\n", " ")
                if maxlen and len(first) > maxlen:
                    sentences = re.split(r"(?<=[.!?])\s+", first)
                    first = ""
                    for s in sentences:
                        if len(first) + len(s) + 1 > maxlen:
                            break
                        first = (first + " " + s).strip()
                set_field(p, "short_description", fv(first or None, source="vendor:description",
                                                     cls="directly_sourced", transform="first paragraph"
                                                     + (f", whole sentences within {maxlen} characters" if maxlen else ""),
                                                     reason="Taken from the vendor description",
                                                     status="ok" if first else "warning"), at)
            else:
                set_field(p, "short_description", fv(None, source="", cls="vendor_query", status="warning",
                                                     reason="No vendor description."), at)
        if not (f.get("long_description") or {}).get("locked"):
            set_field(p, "long_description", fv(desc or None, source="vendor:description" if desc else "",
                                                cls="directly_sourced" if desc else "vendor_query",
                                                status="ok" if desc else "warning",
                                                reason="Vendor description" if desc else "No vendor description."), at)
        if not (f.get("bullet_points") or {}).get("locked"):
            bullets = _split_bullets(feats) if feats else []
            mx = _int(rule("bullets_max"))
            extra = ""
            if mx and len(bullets) > mx:
                extra = f"; vendor gave {len(bullets)} features, first {mx} kept"
                bullets = bullets[:mx]
            set_field(p, "bullet_points", fv("\n".join(bullets) or None, source="vendor:features" if feats else "",
                                             cls="directly_sourced" if feats else "vendor_query",
                                             transform="split into bullets" + extra if feats else "",
                                             reason="Vendor features" if feats else "No vendor features.",
                                             status="ok" if bullets else "warning"), at)
    check_claims(p, refs, doc_text)


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def check_claims(p: dict, refs: Refs, doc_text: str = "") -> None:
    """Flag claim words in content that the vendor data and documents do not contain,
    and forbidden terms."""
    cat = value(p, "category")
    claim_terms = [t.strip() for t in refs.content_rule("claim_terms", cat).split("|") if t.strip()] \
        or DEFAULT_CLAIM_TERMS
    forbidden = [t.strip() for t in refs.content_rule("forbidden_terms", cat).split("|") if t.strip()]
    source = (" ".join(p.get("original", {}).values()) + " " + doc_text).lower()
    for f in ("title", "short_description", "long_description", "bullet_points", "search_terms"):
        fvd = p["fields"].get(f)
        if not fvd or not fvd.get("value"):
            continue
        text = str(fvd["value"]).lower()
        unsupported = [t for t in claim_terms if re.search(rf"(?<!\w){re.escape(t.lower())}(?!\w)", text)
                       and not re.search(rf"(?<!\w){re.escape(t.lower())}(?!\w)", source)]
        bad = [t for t in forbidden if re.search(rf"(?<!\w){re.escape(t.lower())}(?!\w)", text)]
        fvd["claims"] = unsupported
        fvd["forbidden"] = bad


# -- target mapping --------------------------------------------------------------------------
def _field_text(p: dict, name: str) -> str:
    f = resolve_field_name(name) or name
    v = value(p, f)
    return "" if v is None else str(v)


def target_values(p: dict, refs: Refs, system: str) -> dict:
    """{template column: {"value", "rule", "source", "issue", "text"}} for one system."""
    m = refs.mapping
    out = {}
    if not m:
        return out
    cat = refs.category(value(p, "category")) if value(p, "category") else None
    for row in [r for r in m["mappings"] if r["system"] == system]:
        rule, src, par = row["rule"], row["source"], row["param"]
        v, issue = "", ""
        if rule == "copy":
            v = _field_text(p, src)
        elif rule == "constant":
            v = par
        elif rule == "template":
            v = re.sub(r"\{([^}]+)\}", lambda mm: _field_text(p, mm.group(1)), par or src)
        elif rule == "lookup":
            vm = m["value_maps"].get(par or row["target"], {})
            sv = _field_text(p, src)
            e = vm.get(norm(sv)) if sv else None
            if e:
                v = e["target"]
            elif sv:
                issue = f"'{sv}' is not in value map '{par or row['target']}'"
        elif rule == "concat":
            sep = {"(space)": " ", "\\n": "\n"}.get(par, par if par else " ")
            v = sep.join(x for x in (_field_text(p, s.strip()) for s in src.split("|")) if x)
        elif rule == "category_path":
            v = (par or " > ").join(cat["path"]) if cat else ""
        elif rule == "category_level":
            n = _int(par) or 1
            v = cat["path"][n - 1] if cat and len(cat["path"]) >= n else ""
        elif rule == "category_id":
            v = cat.get("id", "") if cat else ""
            if cat and not v:
                issue = "the chosen category has no ID in the category list"
        elif rule == "category_field":
            v = cat.get("extra", {}).get(par, "") if cat else ""
        elif rule == "attribute_code":
            attr = src[5:] if src.lower().startswith("attr:") else src
            a = next((x for x in m["attributes"] if x["attribute"] == attr), None)
            val = _field_text(p, f"attr:{attr}")
            v = refs.option_code(a["list"] if a else attr, val) if val else ""
            if val and not v:
                issue = f"no option code for '{val}'"
        elif rule == "images":
            v = (par or ",").join(i["file"] for i in p.get("images", []) if i.get("confidence") == "confirmed")
        elif rule in ("blank", ""):
            v = ""
            if not rule:
                issue = "no rule given in the mapping row"
        out[row["target"]] = {"value": v, "rule": rule, "source": src, "issue": issue, "row": row["row"],
                              "type": row["type"],
                              "text": (resolve_field_name(src) in ("sku", "barcode", "model_number"))
                              or row["type"] in ("text", "string")}
    return out


def catalog_product(p: dict, refs: Refs, index, ai, at: str, notes: list, doc_text: str = "",
                    example_titles=None) -> None:
    if not p.get("eligible"):
        return
    assign_category(p, refs, index, ai, at, notes)
    map_attributes(p, refs, ai, at, notes)
    build_content(p, refs, ai, at, notes, doc_text, example_titles)
    p["targets"] = {s: target_values(p, refs, s) for s in ("m2", "ns")}
