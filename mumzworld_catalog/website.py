"""Looking up comparable listings on Mumzworld.com.

The site is a reference for presentation and comparable products only.  Every
lookup is recorded with what happened: when the page cannot be reached (or
lists nothing readable) that is recorded as such, and no listing is ever
claimed as inspected unless its page was actually read.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request

from . import config
from .storage import now


def search(query: str, opener=None) -> dict:
    cfg = config.load()
    url = cfg["website_search_url"].format(query=urllib.parse.quote_plus(query))
    rec = {"type": "website_search", "query": query, "url": url, "at": now(), "results": []}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (catalog reference lookup)"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=cfg.get("website_timeout_s", 15)) as resp:
            html = resp.read(3_000_000).decode("utf-8", errors="replace")
    except Exception as e:  # network, HTTP and TLS errors all mean "not accessed"
        rec["status"] = "inaccessible"
        rec["detail"] = f"Could not open the search page: {e}"
        return rec
    seen = set()
    for m in re.finditer(r'<a[^>]+href="(/en/[^"#?]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, text = m.group(1), re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip()
        if len(text) < 8 or href in seen or re.search(r"/(search|category|brands?|cart|account|help)\b", href):
            continue
        seen.add(href)
        rec["results"].append({"url": "https://www.mumzworld.com" + href, "title": text[:200]})
        if len(rec["results"]) >= 8:
            break
    if rec["results"]:
        rec["status"] = "listed"
        rec["detail"] = (f"{len(rec['results'])} listing link(s) read from the search page. Listings are "
                         "candidates only until a reviewer marks one as an exact or comparable match.")
    else:
        rec["status"] = "no_readable_results"
        rec["detail"] = ("The search page opened but no product links could be read (the site may render "
                         "results with JavaScript).")
    return rec
