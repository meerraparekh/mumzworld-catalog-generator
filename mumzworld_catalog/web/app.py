"""Flask interface for the Mumzworld catalog generator."""
from __future__ import annotations

import io
import os
import tempfile
import traceback
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request, send_file, session, url_for)

from .. import __version__, config, exports, knowledge, pipeline, queries
from ..fields import CLASSES, LABELS, SOURCE_FIELDS, label, value
from ..reference import CONTENT_RULE_KEYS, MAPPING_RULES, Refs, setup_check, starter_mapping_workbook
from ..storage import IMAGE_EXT, MULTI_KINDS, REF_KINDS, REQUIRED_KINDS, STATES, TICKET_FILE_KINDS, Library, Ticket, now

PAGE_SIZE = 100
STAGES = [("overview", "1. Ticket & files"), ("catalog", "2. Working catalog"), ("queries", "3. Vendor queries"),
          ("responses", "4. Responses"), ("validation", "5. Validation"), ("review", "6. Review & export"),
          ("history", "History")]


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def create_app(ai_client=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.load().get("secret_key") or "local-only-change-me"
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
    app.config["AI_CLIENT"] = ai_client

    @app.context_processor
    def globals_():
        return {"app_version": __version__, "STATES": STATES, "CLASSES": CLASSES, "label": label,
                "value": value, "who": session.get("by", ""), "REF_KINDS": REF_KINDS}

    @app.errorhandler(500)
    def server_error(err):
        original = getattr(err, "original_exception", None) or err
        details = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(config.DATA_DIR / "error.log", "a", encoding="utf-8") as fh:
                fh.write(f"--- {now()} {request.method} {request.path}\n{details}\n")
        except OSError:
            pass
        return render_template("error.html", details=details), 500

    def ticket_or_404(tid) -> Ticket:
        try:
            return Ticket(tid)
        except KeyError:
            abort(404)

    def who() -> str:
        by = (request.form.get("by") or "").strip()
        if by:
            session["by"] = by
        return by or session.get("by", "")

    def uploads(field: str) -> list[tuple[Path, str]]:
        out = []
        for f in request.files.getlist(field):
            if not f or not f.filename:
                continue
            fd, tmp = tempfile.mkstemp(suffix=Path(f.filename).suffix)
            os.close(fd)
            tmp = Path(tmp)
            f.save(tmp)
            out.append((tmp, f.filename))
        return out

    def back(tid, page="overview", **kw):
        return redirect(url_for(page, tid=tid, **kw))

    # -- dashboard -----------------------------------------------------------------------------
    @app.get("/")
    def index():
        refs = Refs.from_library()
        lib = Library().index()
        missing = [k for k in REQUIRED_KINDS if not any(v.get("active") for v in lib.get(k, []))]
        tickets = Ticket.list_all()
        for t in tickets:
            try:
                tk = Ticket(t["id"])
                t["dash"] = pipeline.dashboard(tk)
            except KeyError:
                t["dash"] = {}
        return render_template("dashboard.html", tickets=tickets, missing=missing, first_run=not lib,
                               refs_errors=refs.errors)

    # -- settings ----------------------------------------------------------------------------
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            reviewers = [x.strip() for x in request.form.get("authorized_reviewers", "").split(",") if x.strip()]
            config.save({"ai_enabled": bool(request.form.get("ai_enabled")),
                         "ai_model": request.form.get("ai_model") or config.DEFAULTS["ai_model"],
                         "ai_effort": request.form.get("ai_effort") or "medium",
                         "website_lookup": bool(request.form.get("website_lookup")),
                         "website_search_url": request.form.get("website_search_url")
                         or config.DEFAULTS["website_search_url"],
                         "authorized_reviewers": reviewers})
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        return render_template("settings.html", cfg=config.load(), future=config.FUTURE_WORKFLOWS)

    # -- reference library ------------------------------------------------------------------
    @app.get("/library")
    def library():
        refs = Refs.from_library()
        lib = Library().index()
        summary = _ref_summary(refs)
        return render_template("library.html", lib=lib, kinds=REF_KINDS, multi=MULTI_KINDS, required=REQUIRED_KINDS,
                               checks=setup_check(refs), summary=summary, rules_help=MAPPING_RULES,
                               content_help=CONTENT_RULE_KEYS, fields=list(SOURCE_FIELDS), labels=LABELS)

    @app.post("/library/upload")
    def library_upload():
        kind = request.form.get("kind", "")
        if kind not in REF_KINDS:
            flash("Choose what kind of reference file this is.", "error")
            return redirect(url_for("library"))
        files = uploads("file")
        if not files:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("library"))
        for tmp, name in files:
            try:
                Library().add(kind, tmp, name, request.form.get("note", ""), who())
            finally:
                _remove(tmp)
        if kind == "ns_master":
            Refs.from_library()  # starts reading a large master in the background
        flash(f"Added {len(files)} file(s) as {REF_KINDS[kind]}.", "ok")
        return redirect(url_for("library") + f"#{kind}")

    @app.post("/library/<kind>/<vid>/active")
    def library_active(kind, vid):
        Library().set_active(kind, vid, request.form.get("active") == "1")
        return redirect(url_for("library") + f"#{kind}")

    @app.get("/library/<kind>/<vid>/download")
    def library_download(kind, vid):
        lib = Library()
        e = next((v for v in lib.index().get(kind, []) if v["id"] == vid), None)
        if not e:
            abort(404)
        return send_file(lib.path(e), as_attachment=True, download_name=e["name"])

    @app.get("/library/starter-mapping.xlsx")
    def starter_mapping():
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        starter_mapping_workbook(Path(tmp), Refs.from_library())
        data = Path(tmp).read_bytes()
        _remove(Path(tmp))
        return send_file(io.BytesIO(data), as_attachment=True, download_name="mapping_rules_starter.xlsx")

    @app.route("/library/reviews", methods=["GET", "POST"])
    def review_lessons():
        refs = Refs.from_library()
        attrs = [a["attribute"] for a in (refs.mapping or {}).get("attributes", [])]
        result = knowledge.analyse_reviews(refs.reviews, attrs)
        if request.method == "POST":
            n = knowledge.suggest_from_lessons(result["lessons"], "library review sheets")
            flash(f"{n} rule candidate(s) stored as suggestions. Approve them on the Rules page.", "ok")
            return redirect(url_for("rules"))
        return render_template("lessons.html", result=result, records=len(refs.reviews), ticket=None)

    # -- knowledge base ------------------------------------------------------------------------
    @app.get("/rules")
    def rules():
        status = request.args.get("status", "")
        return render_template("rules.html", rules=knowledge.rules(status or None), status=status,
                               classes=knowledge.CLASSIFICATIONS)

    @app.post("/rules/<rid>")
    def rule_decide(rid):
        try:
            knowledge.decide(rid, request.form.get("status", ""), who(), request.form.get("notes", ""),
                             {"to": request.form.get("to"), "description": request.form.get("description")})
            flash(f"Rule {rid} updated.", "ok")
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
        return redirect(url_for("rules", status=request.args.get("status", "")))

    # -- tickets -------------------------------------------------------------------------------
    @app.get("/tickets/new")
    def new_ticket():
        return render_template("new_ticket.html", workflows=config.WORKFLOWS, future=config.FUTURE_WORKFLOWS,
                               kinds=TICKET_FILE_KINDS, today=now()[:10])

    @app.post("/tickets")
    def create_ticket():
        f = request.form
        if not f.get("ticket_number", "").strip() or not f.get("vendor", "").strip():
            flash("Ticket number and vendor name are required.", "error")
            return redirect(url_for("new_ticket"))
        t = Ticket.create(f["ticket_number"].strip(), f["vendor"].strip(), f.get("region", "").strip(),
                          f.get("processing_date") or now()[:10], f.get("instructions", ""),
                          f.get("workflow", "basic_new_items"), who())
        n = _save_ticket_uploads(t)
        flash(f"Ticket created with {n} file(s). Check the file inventory and column mapping, then process.", "ok")
        return back(t.id)

    @app.post("/tickets/import")
    def import_ticket():
        files = uploads("package")
        if not files:
            flash("Choose a ticket package (.zip).", "error")
            return redirect(url_for("index"))
        tmp, _ = files[0]
        try:
            t = Ticket.import_package(tmp.read_bytes())
        except (ValueError, OSError) as e:
            flash(f"Could not import: {e}", "error")
            return redirect(url_for("index"))
        finally:
            _remove(tmp)
        flash("Ticket package imported.", "ok")
        return back(t.id)

    def _save_ticket_uploads(t: Ticket) -> int:
        n = 0
        for kind in TICKET_FILE_KINDS:
            if kind == "vendor_response":
                continue
            for tmp, name in uploads(f"file_{kind}"):
                try:
                    t.add_file(kind, tmp, name, who())
                    n += 1
                finally:
                    _remove(tmp)
        if t.files("ns_master"):
            Refs.for_ticket(t)  # starts reading a large master in the background
        return n

    @app.get("/t/<tid>")
    def overview(tid):
        t = ticket_or_404(tid)
        inv = pipeline.file_inventory(t)
        return render_template("ticket_overview.html", t=t, meta=t.meta, inv=inv, kinds=TICKET_FILE_KINDS,
                               fields=list(SOURCE_FIELDS), labels=LABELS, stages=STAGES, stage="overview",
                               dash=pipeline.dashboard(t), checks=setup_check(Refs.for_ticket(t)))

    @app.post("/t/<tid>/upload")
    def ticket_upload(tid):
        t = ticket_or_404(tid)
        n = _save_ticket_uploads(t)
        flash(f"Added {n} file(s). Process the ticket again to use them; existing work is kept.", "ok" if n else "error")
        return back(tid)

    @app.post("/t/<tid>/columns")
    def ticket_columns(tid):
        t = ticket_or_404(tid)
        fid = request.form.get("file_id", "")
        sheet = request.form.get("sheet", "")
        cmap = {}
        for k, v in request.form.items():
            if k.startswith("col::") and v:
                cmap[k[5:]] = v
        used = [v for v in cmap.values()]
        dup = {v for v in used if used.count(v) > 1}

        def upd(m):
            m.setdefault("column_map", {})[fid] = cmap
            if sheet:
                m.setdefault("vendor_sheets", {})[fid] = sheet
        t.update(upd)
        t.log("Column mapping updated", who())
        if dup:
            flash("Several columns map to " + ", ".join(label(d) for d in dup) + ": the first non-empty value is "
                  "used and differences are flagged.", "error")
        else:
            flash("Column mapping saved.", "ok")
        return back(tid)

    @app.post("/t/<tid>/process")
    def ticket_process(tid):
        t = ticket_or_404(tid)
        try:
            s = pipeline.process(t, who(), app.config["AI_CLIENT"])
            flash(f"Processed {s['records']} records: {s['eligible']} new-item candidates, {s['excluded']} existing "
                  f"excluded, {s['held']} held for investigation, {s['validation']['blocker']} blockers.", "ok")
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
            return back(tid)
        return back(tid, "catalog")

    @app.post("/t/<tid>/delete")
    def ticket_delete(tid):
        t = ticket_or_404(tid)
        if request.form.get("confirm") != t.meta["ticket_number"]:
            flash("Type the ticket number to confirm deletion.", "error")
            return back(tid, "history")
        t.delete()
        flash("Ticket deleted.", "ok")
        return redirect(url_for("index"))

    @app.get("/t/<tid>/file/<fid>")
    def ticket_file(tid, fid):
        t = ticket_or_404(tid)
        f = next((x for x in t.meta["files"] if x["id"] == fid), None)
        if not f:
            abort(404)
        return send_file(t.path(f["stored"]), as_attachment=True, download_name=f["name"])

    @app.get("/t/<tid>/image/<name>")
    def ticket_image(tid, name):
        t = ticket_or_404(tid)
        p = t.path("images", name)
        if not p.exists() or p.suffix.lower() not in IMAGE_EXT:
            abort(404)
        return send_file(p)

    # -- working catalog ------------------------------------------------------------------------
    @app.get("/t/<tid>/catalog")
    def catalog(tid):
        t = ticket_or_404(tid)
        cat = t.catalog()
        val = t.load("validation", {"issues": [], "blocked_keys": []})
        blocked = set(val.get("blocked_keys", []))
        issues_by = {}
        for i in val["issues"]:
            if i["key"]:
                issues_by.setdefault(i["key"], []).append(i)
        flt, q = request.args.get("filter", "candidates"), request.args.get("q", "").strip().lower()
        keys = cat["order"]
        P = cat["products"]
        filters = {
            "candidates": lambda p: p.get("eligible"),
            "blocked": lambda p: p.get("eligible") and p["key"] in blocked,
            "ready": lambda p: p.get("eligible") and p["key"] not in blocked,
            "judgment": lambda p: p.get("eligible") and any(d.get("status") == "review" and not d.get("locked")
                                                            for d in p["fields"].values()),
            "approved": lambda p: p.get("review", {}).get("status") == "approved",
            "excluded": lambda p: not p.get("eligible"),
            "all": lambda p: True,
        }
        keys = [k for k in keys if filters.get(flt, filters["all"])(P[k])]
        if q:
            keys = [k for k in keys if q in " ".join(str(d.get("value") or "") for d in P[k]["fields"].values()).lower()]
        page = max(1, int(request.args.get("page", 1) or 1))
        pages = max(1, (len(keys) + PAGE_SIZE - 1) // PAGE_SIZE)
        shown = keys[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        refs = Refs.for_ticket(t)
        attrs = [a["attribute"] for a in (refs.mapping or {}).get("attributes", [])]
        return render_template("catalog.html", t=t, meta=t.meta, cat=cat, keys=shown, total=len(keys), page=page,
                               pages=pages, flt=flt, q=q, blocked=blocked, issues_by=issues_by, attrs=attrs,
                               stages=STAGES, stage="catalog", dash=pipeline.dashboard(t))

    @app.get("/t/<tid>/p/<key>")
    def product(tid, key):
        t = ticket_or_404(tid)
        cat = t.catalog()
        p = cat["products"].get(key)
        if not p:
            abort(404)
        refs = Refs.for_ticket(t)
        val = t.load("validation", {"issues": []})
        issues = [i for i in val["issues"] if i["key"] == key]
        qs = [q for q in t.load("queries", {"queries": []})["queries"] if q["key"] == key]
        all_images = sorted(x.name for x in t.path("images").iterdir() if x.suffix.lower() in IMAGE_EXT) \
            if t.path("images").exists() else []
        attrs = (refs.mapping or {}).get("attributes", [])
        options = {f"attr:{a['attribute']}": refs.approved_values(a["list"]) or [] for a in attrs}
        options["category"] = [c["key"] for c in refs.categories]
        derived = ["category", "category_id", "tags", "title", "short_description", "long_description",
                   "bullet_points", "search_terms"]
        field_order = [f for f in derived if f in p["fields"]] + sorted(f for f in p["fields"] if f.startswith("attr:")) \
            + [f for f in SOURCE_FIELDS if f in p["fields"]]
        field_order += [f for f in p["fields"] if f not in field_order]
        order = cat["order"]
        i = order.index(key)
        return render_template("product.html", t=t, meta=t.meta, p=p, key=key, refs=refs, issues=issues, qs=qs,
                               all_images=all_images, options=options, stages=STAGES, stage="catalog",
                               prev_key=order[i - 1] if i else None,
                               next_key=order[i + 1] if i + 1 < len(order) else None,
                               field_order=field_order, source_fields=list(SOURCE_FIELDS), cfg=config.load(),
                               dash=pipeline.dashboard(t))

    @app.post("/t/<tid>/p/<key>/<action>")
    def product_action(tid, key, action):
        t = ticket_or_404(tid)
        f = request.form
        by = who()
        try:
            if action == "edit":
                field = f.get("field", "")
                pipeline.edit_field(t, key, field, f.get("value", ""), by, f.get("reason", ""))
                flash(f"{label(field)} updated and locked.", "ok")
            elif action == "accept":
                pipeline.accept_field(t, key, f.get("field", ""), by)
                flash(f"{label(f.get('field', ''))} accepted.", "ok")
            elif action == "unlock":
                pipeline.unlock_field(t, key, f.get("field", ""), by)
                flash("Unlocked; the field is decided automatically again.", "ok")
            elif action == "review":
                n = pipeline.review_product(t, [key], f.get("status", ""), by, f.get("note", ""))
                flash("Saved." if n else "Not approved: the product still has unresolved blockers.", "ok" if n else "error")
            elif action == "match":
                pipeline.decide_match(t, key, f.get("decision", ""), by, f.get("reason", ""))
                flash("Match decision recorded and the ticket re-processed.", "ok")
            elif action == "image":
                pipeline.link_image(t, key, f.get("file", ""), by, remove=f.get("remove") == "1")
                flash("Image association updated.", "ok")
            elif action == "evidence":
                pipeline.add_evidence(t, key, {"type": "reference", "url": f.get("url", ""),
                                               "supports": f.get("supports", ""), "fields": f.get("fields", ""),
                                               "exact": f.get("exact", "comparable")}, by)
                flash("Reference recorded.", "ok")
            elif action == "lookup":
                c = pipeline.website_lookup(t, [key], by)
                flash("Mumzworld.com lookup: " + ", ".join(f"{k}" for k in c), "ok")
            elif action == "inspect":
                r = pipeline.inspect_images(t, [key], by, app.config["AI_CLIENT"])
                flash(f"Inspected {r['inspected']} image(s)." + (" " + "; ".join(r["failed"]) if r["failed"] else ""),
                      "ok")
            else:
                abort(404)
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
        return redirect(url_for("product", tid=tid, key=key) + (f"#{f.get('field', '')}" if f.get("field") else ""))

    # -- vendor queries -------------------------------------------------------------------------
    @app.get("/t/<tid>/queries")
    def queries_page(tid):
        t = ticket_or_404(tid)
        data = t.load("queries", {"queries": []})
        st = request.args.get("status", "outstanding")
        qs = data["queries"]
        if st == "outstanding":
            qs = queries.outstanding(data)
        elif st != "all":
            qs = [q for q in qs if q["status"] == st]
        return render_template("queries.html", t=t, meta=t.meta, qs=qs, status=st, all=data["queries"],
                               stages=STAGES, stage="queries", dash=pipeline.dashboard(t))

    @app.get("/t/<tid>/download/<what>")
    def download(tid, what):
        t = ticket_or_404(tid)
        refs = Refs.for_ticket(t)
        num = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in t.meta["ticket_number"]) or "ticket"
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        tmp = Path(tmp)
        try:
            if what == "queries":
                exports.query_sheet(tmp, t.load("queries", {"queries": []}), refs, t.meta)
                name = f"{num}_vendor_queries.xlsx"
            elif what == "working":
                exports.working_catalog(tmp, t.catalog(), refs, t.load("validation", {}))
                name = f"{num}_working_catalog.xlsx"
            elif what == "reports":
                exports.reports(tmp, t.meta, t.catalog(), refs, t.load("validation", {}), t.load("queries", {}),
                                t.catalog().get("exclusions", []), pipeline.lessons(t))
                name = f"{num}_reports_DRAFT.xlsx"
            elif what == "package":
                data = t.package()
                return send_file(io.BytesIO(data), as_attachment=True, download_name=f"{num}_ticket_package.zip")
            else:
                abort(404)
            data = tmp.read_bytes()
        finally:
            _remove(tmp)
        t.log(f"Downloaded {name}", session.get("by", ""))
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name)

    @app.get("/t/<tid>/outputs/<folder>/<name>")
    def output_file(tid, folder, name):
        t = ticket_or_404(tid)
        p = t.path("outputs", folder, name)
        if not p.exists():
            abort(404)
        return send_file(p, as_attachment=True, download_name=name)

    # -- responses ------------------------------------------------------------------------------
    @app.route("/t/<tid>/responses", methods=["GET", "POST"])
    def responses_page(tid):
        t = ticket_or_404(tid)
        result = None
        if request.method == "POST":
            files = uploads("file")
            if not files:
                flash("Choose the vendor response file(s).", "error")
            if not t.catalog()["order"]:
                flash("Process the ticket before importing responses.", "error")
                files = []
            results = []
            for tmp, name in files:
                try:
                    results.append(pipeline.import_response(t, tmp, name, who()))
                except Exception as e:  # a bad file must not lose the others
                    flash(f"{name}: could not be imported ({e}).", "error")
                finally:
                    _remove(tmp)
            result = results
        return render_template("responses.html", t=t, meta=t.meta, results=result, stages=STAGES,
                               stage="responses", dash=pipeline.dashboard(t),
                               exceptions=t.catalog().get("exceptions", []))

    # -- validation -----------------------------------------------------------------------------
    @app.get("/t/<tid>/validation")
    def validation_page(tid):
        t = ticket_or_404(tid)
        val = t.load("validation", {"issues": [], "counts": {}})
        sev, code = request.args.get("severity", ""), request.args.get("code", "")
        issues = [i for i in val["issues"] if (not sev or i["severity"] == sev) and (not code or i["code"] == code)]
        codes = sorted({i["code"] for i in val["issues"]})
        return render_template("validation.html", t=t, meta=t.meta, val=val, issues=issues[:2000],
                               total=len(issues), sev=sev, code=code, codes=codes, stages=STAGES,
                               stage="validation", dash=pipeline.dashboard(t))

    @app.post("/t/<tid>/revalidate")
    def revalidate(tid):
        t = ticket_or_404(tid)
        pipeline.revalidate(t)
        flash("Validation re-run.", "ok")
        return back(tid, "validation")

    @app.post("/t/<tid>/waive")
    def waive(tid):
        t = ticket_or_404(tid)
        try:
            pipeline.waive(t, request.form.get("issue", ""), who(), request.form.get("reason", ""))
            flash("Waiver recorded.", "ok")
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
        return redirect(request.referrer or url_for("validation_page", tid=tid))

    # -- review & export ------------------------------------------------------------------------
    @app.get("/t/<tid>/review")
    def review(tid):
        t = ticket_or_404(tid)
        cat = t.catalog()
        val = t.load("validation", {"blocked_keys": []})
        blocked = set(val.get("blocked_keys", []))
        P = cat["products"]
        candidates = [k for k in cat["order"] if P[k].get("eligible") and k not in blocked
                      and P[k].get("review", {}).get("status") != "approved"]
        preview = request.args.get("preview") == "1"
        return render_template("review.html", t=t, meta=t.meta, dash=pipeline.dashboard(t), cat=cat,
                               candidates=candidates, preview=preview, stages=STAGES, stage="review",
                               cfg=config.load(), state=pipeline.compute_state(t))

    @app.post("/t/<tid>/review/bulk")
    def review_bulk(tid):
        t = ticket_or_404(tid)
        keys = request.form.getlist("key")
        status = request.form.get("status", "approved")
        try:
            n = pipeline.review_product(t, keys, status, who(), request.form.get("note", ""))
            flash(f"{n} product(s) marked {status}.", "ok")
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
        return redirect(request.form.get("next") or url_for("review", tid=tid))

    @app.post("/t/<tid>/approve")
    def approve(tid):
        t = ticket_or_404(tid)
        try:
            ap = pipeline.approve_ticket(t, who(), request.form.get("note", ""))
            flash(f"Ticket approved by {ap['by']} for {ap['products']} product(s).", "ok")
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
        return back(tid, "review")

    @app.post("/t/<tid>/export")
    def export(tid):
        t = ticket_or_404(tid)
        try:
            out = pipeline.export(t, who())
            flash(f"Exported to {out['folder']}.", "ok")
        except pipeline.WorkflowError as e:
            flash(str(e), "error")
        return back(tid, "review")

    # -- history & lessons ----------------------------------------------------------------------
    @app.route("/t/<tid>/history", methods=["GET", "POST"])
    def history(tid):
        t = ticket_or_404(tid)
        result = pipeline.lessons(t)
        if request.method == "POST":
            n = knowledge.suggest_from_lessons(result["lessons"], f"ticket {t.meta['ticket_number']}")
            flash(f"{n} rule candidate(s) stored as suggestions (not yet approved).", "ok")
            return redirect(url_for("rules"))
        return render_template("history.html", t=t, meta=t.meta, result=result, stages=STAGES, stage="history",
                               dash=pipeline.dashboard(t))

    return app


def _ref_summary(refs: Refs) -> dict:
    out = {}
    if refs.categories:
        out["categories"] = (f"{len(refs.categories)} categories; columns: "
                             + ", ".join(f"{k}={v}" for k, v in refs.category_columns.items() if v))
    if refs.dropdowns:
        out["dropdowns"] = f"{len(refs.dropdowns)} lists: " + ", ".join(
            f"{k} ({len(v['values'])})" for k, v in list(refs.dropdowns.items())[:30])
    if refs.mapping:
        m = refs.mapping
        out["mapping"] = (f"{sum(1 for x in m['mappings'] if x['system'] == 'm2')} Magento 2 and "
                          f"{sum(1 for x in m['mappings'] if x['system'] == 'ns')} NetSuite mapping rows; "
                          f"{len(m['attributes'])} attributes; {len(m['content_rules'])} content rules; "
                          f"{len(m['value_maps'])} value maps")
    for k in ("m2", "ns"):
        t = refs.templates.get(k)
        if t:
            out[f"{k}_template"] = (f"Sheet '{t['sheet']}', header row {t['header_row']}, {len(t['columns'])} columns, "
                                    f"{len(t['required'])} marked required, {len(t['dropdowns'])} with drop-downs")
    if refs.ns_master:
        out["ns_master"] = (f"{len(refs.ns_master['rows']):,} items from sheet(s) {refs.ns_master['sheet']}; columns: "
                            + ", ".join(f"{k}={v}" for k, v in refs.ns_master["columns"].items() if v))
    elif refs.ns_master_pending:
        j = refs.ns_master_pending
        out["ns_master"] = (f"Still reading {j['name']} ({j.get('size_mb', '?')} MB): {j.get('rows') or 0:,} rows so far. "
                            "Refresh this page to update.")
    if refs.reviews:
        out["review_sheet"] = f"{len(refs.reviews)} review decisions"
    if refs.sop_text:
        out["sop"] = f"{len(refs.sop_text):,} characters of SOP text (reference only)"
    return out
