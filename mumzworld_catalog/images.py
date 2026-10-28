"""Stage 4: associate uploaded images with products.

An image is associated only on clear evidence: its file name is listed in the
vendor's image column, or the SKU / barcode appears in the file name as a whole
token.  A SKU found inside a longer token is an *uncertain* match shown for
review.  Image URLs in the vendor file are recorded but not downloaded.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from .fields import value
from .sheets import norm
from .storage import IMAGE_EXT


def associate(products: dict, order: list[str], images_dir: Path, keep_manual: bool = True) -> dict:
    files = sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT) \
        if images_dir.exists() else []
    hashes = {f.name: hashlib.sha1(f.read_bytes()).hexdigest() for f in files}
    by_hash = defaultdict(list)
    for name, h in hashes.items():
        by_hash[h].append(name)
    lower = {f.name.lower(): f.name for f in files}
    stems = {f.stem.lower(): f.name for f in files}
    claims = defaultdict(list)  # image -> product keys (confirmed)
    for k in order:
        p = products[k]
        manual = [i for i in p.get("images", []) if i.get("how", "").startswith("reviewer")] if keep_manual else []
        removed = set(p.get("images_removed", []))
        found, refs = {i["file"]: i for i in manual}, []
        for ref in re.split(r"[,;|\n]+", value(p, "image_refs")):
            ref = ref.strip()
            if not ref:
                continue
            if re.match(r"https?://", ref, re.I):
                refs.append({"ref": ref, "status": "URL listed by vendor - not downloaded or inspected"})
                continue
            name = lower.get(Path(ref).name.lower()) or stems.get(Path(ref).stem.lower())
            if name:
                found.setdefault(name, {"file": name, "how": "listed in vendor image column", "confidence": "confirmed"})
            else:
                refs.append({"ref": ref, "status": "listed by vendor but no uploaded file has this name"})
        ids = [(str(x).lower(), lab) for x, lab in ((value(p, "sku"), "SKU"), (value(p, "barcode"), "barcode")) if x]
        for f in files:
            if f.name in found:
                continue
            stem = f.stem.lower()
            for ident, lab in ids:
                bounded = re.search(rf"(^|[^a-z0-9]){re.escape(ident)}([^a-z0-9]|$)", stem)
                if bounded or norm(stem) == norm(ident):
                    found[f.name] = {"file": f.name, "how": f"{lab} in file name", "confidence": "confirmed"}
                    break
                if len(norm(ident)) >= 5 and norm(ident) in norm(stem):
                    found[f.name] = {"file": f.name, "how": f"{lab} inside a longer file-name token",
                                     "confidence": "uncertain"}
                    break
        imgs = [i for n, i in found.items() if n not in removed]
        for i in imgs:
            i["sha1"] = hashes.get(i["file"], "")
            if i["confidence"] == "confirmed":
                claims[i["file"]].append(k)
        p["images"] = imgs
        p["image_refs"] = refs
    for name, keys in claims.items():
        if len(keys) > 1:
            for k in keys:
                for i in products[k]["images"]:
                    if i["file"] == name:
                        i["confidence"] = "uncertain"
                        i["how"] += f" - also matches {len(keys) - 1} other product(s)"
    used = {i["file"] for k in order for i in products[k]["images"]}
    for k in order:
        p = products[k]
        for i in p["images"]:
            dups = [n for n in by_hash.get(i["sha1"], []) if n != i["file"]]
            i["duplicates"] = dups
    return {"total": len(files), "unassociated": sorted(n for n in hashes if n not in used),
            "duplicates": [v for v in by_hash.values() if len(v) > 1]}


def media_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp"}.get(ext, "")
