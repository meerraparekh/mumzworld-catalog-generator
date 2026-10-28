"""On-disk storage: the reference library and one workspace per ticket.

::

    <DATA_DIR>/library/index.json            versions of every reference file
    <DATA_DIR>/library/<kind>/<vid>__<name>  the reference files, never modified
    <DATA_DIR>/knowledge/rules.json          approved rules and unapproved suggestions
    <DATA_DIR>/tickets/<id>/ticket.json      metadata, files, column map, state, history
    <DATA_DIR>/tickets/<id>/files/           original uploads, never modified
    <DATA_DIR>/tickets/<id>/images/          images extracted from uploads / ZIPs
    <DATA_DIR>/tickets/<id>/refs/            copies of the reference versions used
    <DATA_DIR>/tickets/<id>/catalog.json     working catalog (field level, with history)
    <DATA_DIR>/tickets/<id>/queries.json     vendor queries and their status
    <DATA_DIR>/tickets/<id>/validation.json  latest validation findings
    <DATA_DIR>/tickets/<id>/outputs/         exported workbooks and reports

JSON writes are atomic (temp file, then rename), so an interrupted process never
leaves a half-written state file.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import config

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def lock(name: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(name, threading.RLock())


def write_json(path: Path, data) -> None:
    """Write JSON atomically.  On Windows the rename is briefly refused while another
    program has the file open, so retry for a few seconds, then write in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, "utf-8")
    delay = 0.02
    for _ in range(30):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 1.5, 0.2)
    try:
        path.write_text(text, "utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path, default=None):
    """Read JSON, retrying briefly if the file is being replaced right now."""
    if not path.exists():
        return default
    for attempt in range(20):
        try:
            return json.loads(path.read_text("utf-8"))
        except (PermissionError, ValueError):
            if attempt == 19:
                raise
            time.sleep(0.05)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name or "file").name).strip(" .")
    return name or "file"

REF_KINDS = {
    "sop": "Cataloging SOP",
    "categories": "Approved category & subcategory list",
    "dropdowns": "Attribute definitions & dropdowns",
    "mapping": "Mapping & content rules workbook",
    "m2_template": "Magento 2 output template",
    "ns_template": "NetSuite output template",
    "ns_master": "NetSuite product master",
    "review_sheet": "Team review sheet",
    "completed_ticket": "Completed catalog ticket (example)",
}
# Kinds where every active version is used together rather than only the latest.
MULTI_KINDS = {"review_sheet", "completed_ticket", "sop"}
REQUIRED_KINDS = ["categories", "dropdowns", "mapping", "m2_template", "ns_template", "ns_master"]

TICKET_FILE_KINDS = {
    "vendor_input": "Vendor product input",
    "images": "Product images / ZIP",
    "documents": "Vendor documents / specifications",
    "vendor_response": "Vendor response",
    **REF_KINDS,
}

STATES = ["draft", "processing", "awaiting_vendor", "awaiting_review", "blocked", "approved", "exported"]
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _root(name: str) -> Path:
    d = config.DATA_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- reference library ------------------------------------------------------------
class Library:
    def __init__(self):
        self.dir = _root("library")
        self.index_path = self.dir / "index.json"

    def index(self) -> dict:
        return read_json(self.index_path, {})

    def add(self, kind: str, src: Path, name: str, note: str = "", by: str = "") -> dict:
        if kind not in REF_KINDS:
            raise ValueError(f"Unknown reference kind: {kind}")
        vid = uuid.uuid4().hex[:8]
        dest = self.dir / kind / f"{vid}__{safe_filename(name)}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        entry = {"id": vid, "kind": kind, "name": name, "stored": str(dest.relative_to(self.dir)),
                 "uploaded_at": now(), "sha1": sha1(dest), "size": dest.stat().st_size, "note": note,
                 "by": by, "active": True}
        with lock("library"):
            idx = self.index()
            versions = idx.setdefault(kind, [])
            if kind not in MULTI_KINDS:
                for v in versions:
                    v["active"] = False
            versions.append(entry)
            write_json(self.index_path, idx)
        return entry

    def set_active(self, kind: str, vid: str, active: bool) -> None:
        with lock("library"):
            idx = self.index()
            for v in idx.get(kind, []):
                if v["id"] == vid:
                    v["active"] = active
                elif active and kind not in MULTI_KINDS:
                    v["active"] = False
            write_json(self.index_path, idx)

    def active(self, kind: str) -> list[dict]:
        return [v for v in self.index().get(kind, []) if v.get("active")]

    def path(self, entry: dict) -> Path:
        return self.dir / entry["stored"]


# -- tickets ------------------------------------------------------------------------
def tickets_dir() -> Path:
    return _root("tickets")


class Ticket:
    def __init__(self, ticket_id: str):
        if not re.fullmatch(r"[a-f0-9]{12}", ticket_id or ""):
            raise KeyError(ticket_id)
        self.id = ticket_id
        self.dir = tickets_dir() / ticket_id
        if not (self.dir / "ticket.json").exists():
            raise KeyError(ticket_id)

    @classmethod
    def create(cls, ticket_number: str, vendor: str, region: str, processing_date: str, instructions: str = "",
               workflow: str = "basic_new_items", by: str = "") -> "Ticket":
        if workflow not in config.WORKFLOWS:
            raise ValueError(f"Workflow '{workflow}' is not available yet.")
        tid = uuid.uuid4().hex[:12]
        d = tickets_dir() / tid
        for sub in ("files", "images", "refs", "responses", "outputs"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        write_json(d / "ticket.json", {
            "id": tid, "ticket_number": ticket_number, "vendor": vendor, "region": region,
            "processing_date": processing_date, "instructions": instructions, "workflow": workflow,
            "created_at": now(), "created_by": by, "state": "draft", "files": [], "column_map": {},
            "vendor_sheets": {}, "refs_used": {}, "responses": [], "approval": None, "waivers": [],
            "exports": [], "processed_at": None,
            "history": [{"at": now(), "event": f"Ticket created ({config.WORKFLOWS[workflow]})", "by": by}],
        })
        return cls(tid)

    @staticmethod
    def list_all() -> list[dict]:
        out = []
        for d in sorted(tickets_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = read_json(d / "ticket.json")
            if meta:
                out.append(meta)
        return out

    @property
    def meta(self) -> dict:
        return read_json(self.dir / "ticket.json")

    def update(self, fn) -> dict:
        with lock(self.id):
            meta = self.meta
            fn(meta)
            write_json(self.dir / "ticket.json", meta)
            return meta

    def log(self, event: str, by: str = "") -> None:
        self.update(lambda m: m["history"].append({"at": now(), "event": event, "by": by}))

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts).resolve()
        if self.dir.resolve() not in p.parents and p != self.dir.resolve():
            raise PermissionError("path outside ticket")
        return p

    # -- json documents ------------------------------------------------------------
    def load(self, name: str, default=None):
        return read_json(self.dir / f"{name}.json", default)

    def save(self, name: str, data) -> None:
        with lock(self.id):
            write_json(self.dir / f"{name}.json", data)

    def catalog(self) -> dict:
        return self.load("catalog", {"products": {}, "order": [], "exceptions": []})

    def save_catalog(self, data: dict) -> None:
        self.save("catalog", data)

    # -- files ---------------------------------------------------------------------
    def add_file(self, kind: str, src: Path, name: str, by: str = "") -> dict:
        if kind not in TICKET_FILE_KINDS:
            raise ValueError(f"Unknown file kind: {kind}")
        fid = uuid.uuid4().hex[:8]
        dest = self.dir / "files" / f"{fid}__{safe_filename(name)}"
        shutil.copyfile(src, dest)
        entry = {"id": fid, "kind": kind, "name": name, "stored": f"files/{dest.name}", "uploaded_at": now(),
                 "by": by, "sha1": sha1(dest), "size": dest.stat().st_size}
        if kind == "images":
            entry["images"] = self._extract_images(dest, name)
        self.update(lambda m: m["files"].append(entry))
        self.log(f"Uploaded {TICKET_FILE_KINDS[kind].lower()}: {name}", by)
        return entry

    def _extract_images(self, stored: Path, name: str) -> list[str]:
        """Copy an image, or every image inside a ZIP, into images/.  Paths inside
        the ZIP are flattened; nothing is written outside images/."""
        out_dir = self.dir / "images"
        saved = []

        def put(fname: str, data: bytes):
            base = safe_filename(Path(fname).name)
            target = out_dir / base
            n = 1
            while target.exists():
                if hashlib.sha1(target.read_bytes()).hexdigest() == hashlib.sha1(data).hexdigest():
                    saved.append(target.name)
                    return
                target = out_dir / f"{Path(base).stem}~{n}{Path(base).suffix}"
                n += 1
            target.write_bytes(data)
            saved.append(target.name)

        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(stored) as zf:
                    for info in zf.infolist():
                        fn = info.filename
                        if info.is_dir() or "__MACOSX" in fn or Path(fn).name.startswith("."):
                            continue
                        if fn.lower().endswith(IMAGE_EXT) and info.file_size < 50 * 1024 * 1024:
                            put(fn, zf.read(info))
            except zipfile.BadZipFile:
                return []
        elif name.lower().endswith(IMAGE_EXT):
            put(name, stored.read_bytes())
        return saved

    def files(self, kind: str | None = None) -> list[dict]:
        return [f for f in self.meta["files"] if kind is None or f["kind"] == kind]

    # -- package -------------------------------------------------------------------
    def package(self) -> bytes:
        """The whole workspace as a ZIP that can be re-imported elsewhere."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in self.dir.rglob("*"):
                if p.is_file() and not p.name.endswith(".tmp"):
                    zf.write(p, p.relative_to(self.dir).as_posix())
        return buf.getvalue()

    @classmethod
    def import_package(cls, data: bytes) -> "Ticket":
        tid = uuid.uuid4().hex[:12]
        d = tickets_dir() / tid
        d.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "ticket.json" not in zf.namelist():
                shutil.rmtree(d)
                raise ValueError("Not a ticket package (ticket.json missing).")
            for info in zf.infolist():
                target = (d / info.filename).resolve()
                if d.resolve() not in target.parents or info.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
        meta = read_json(d / "ticket.json")
        old = meta.get("id")
        meta["id"] = tid
        meta["history"].append({"at": now(), "event": f"Imported from ticket package (was {old})", "by": ""})
        write_json(d / "ticket.json", meta)
        return cls(tid)

    def delete(self) -> None:
        with lock(self.id):
            shutil.rmtree(self.dir, ignore_errors=True)
