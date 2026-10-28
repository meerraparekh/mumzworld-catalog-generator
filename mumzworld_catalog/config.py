"""Application settings for the Mumzworld catalog generator.

Defaults below are overridden by ``settings.json`` in the data directory (written
from the Settings page) and by environment variables for deployment values.
Business rules (categories, dropdowns, mappings, content rules) are NOT kept
here: they come only from the uploaded reference library.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("MUMZ_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# Workflows the app knows about.  Only the first one is implemented; the others
# are listed so the ticket form and storage already carry a workflow id.
WORKFLOWS = {
    "basic_new_items": "Basic Catalog Creation (new products)",
}
FUTURE_WORKFLOWS = [
    "Regional counterparts from existing products",
    "Update existing product listings",
    "Configurable products (parent-child SKUs)",
    "Size / colour variant structures",
    "Bulk corrections and catalog maintenance",
]

DEFAULTS = {
    # Optional cataloging assistant (Claude).  Off until switched on and an API
    # credential is available; every suggestion it makes is a "Cataloging
    # judgment" that needs review.
    "ai_enabled": False,
    "ai_model": "claude-opus-5-5",
    "ai_effort": "medium",
    # Look up comparable listings on Mumzworld.com.  Failures are recorded as
    # "inaccessible" - the app never pretends to have searched.
    "website_lookup": False,
    "website_search_url": "https://www.mumzworld.com/en/search?q={query}",
    "website_timeout_s": 15,
    # People allowed to give final approval and waive blockers.  Empty = any
    # named reviewer (the name is still recorded).
    "authorized_reviewers": [],
}


def settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = settings_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text("utf-8")))
        except ValueError:
            pass
    return cfg


def save(updates: dict) -> dict:
    cfg = load()
    cfg.update(updates)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(cfg, indent=2), "utf-8")
    return cfg
