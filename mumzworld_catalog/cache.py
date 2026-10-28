"""Parsed reference files, kept so each file is read only once.

Reference files never change once uploaded (a new version is a new file), so
the parse result is keyed by the file's SHA-1.  Results are kept in memory and
in ``<DATA_DIR>/cache/`` so a restart does not re-read them either.

A large NetSuite master can take minutes to read.  It is read in a background
thread; until it is ready, callers see its progress instead of waiting.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config

VERSION = 2  # bump when a parser's output changes
BACKGROUND_BYTES = 20 * 1024 * 1024  # larger files are read in the background

_mem: dict[tuple, object] = {}
_mem_lock = threading.Lock()
_jobs: dict[str, dict] = {}  # sha1 -> progress of a background read


def _dir() -> Path:
    d = config.DATA_DIR / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(kind: str, sha1: str) -> Path:
    return _dir() / f"{kind}-{sha1}-v{VERSION}.json"


def get(kind: str, sha1: str, path: Path, parser):
    """The parse result, from memory, from disk, or by parsing now."""
    key = (str(config.DATA_DIR), kind, sha1, VERSION)
    with _mem_lock:
        if key in _mem:
            return _mem[key]
    f = _file(kind, sha1)
    data = None
    if f.exists():
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            data = None
    if data is None:
        data = parser(path)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str), "utf-8")
        tmp.replace(f)
    with _mem_lock:
        _mem[key] = data
    return data


def ready(kind: str, sha1: str) -> bool:
    key = (str(config.DATA_DIR), kind, sha1, VERSION)
    return key in _mem or _file(kind, sha1).exists()


def background(kind: str, sha1: str, path: Path, parser) -> dict:
    """Start (once) reading a large file in a thread.  Returns its progress:
    {"state": "reading"|"done"|"error", "rows": n, "started": t, "error": str}."""
    if ready(kind, sha1):
        return {"state": "done", "rows": None}
    with _mem_lock:
        job = _jobs.get(sha1)
        if job and job["state"] in ("reading", "done"):
            return job
        job = {"state": "reading", "rows": 0, "started": time.time(), "error": "",
               "size_mb": round(Path(path).stat().st_size / 1e6)}
        _jobs[sha1] = job

    def progress(n):
        job["rows"] = n

    def run():
        try:
            get(kind, sha1, path, lambda p: parser(p, progress))
            job["state"] = "done"
        except Exception as e:  # reported on the ticket page; a new attempt can be started
            job["state"], job["error"] = "error", str(e)

    threading.Thread(target=run, daemon=True, name=f"read-{kind}-{sha1[:8]}").start()
    return job


def status(sha1: str) -> dict | None:
    return _jobs.get(sha1)
