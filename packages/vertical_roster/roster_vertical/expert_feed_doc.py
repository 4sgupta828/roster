"""Turn an expert-newsletter/blog feed item (RSS/Atom) into a labeled EXPERT-ANALYSIS document + facets.

Expert newsletters and technical blogs are a NAMED expert's interpretation / foresight — the
product's most distinctive gap (curated tech-trend opinion). `source_kind="essay"` maps to a new
"expert_analysis" tier the orchestrator wires separately. It is INTERPRETATION, not a peer-reviewed
or primary fact: the byline/answer format must present it as a named expert's opinion, never as an
established finding. STRUCTURAL only (Rule 18): author/publication/year are metadata the feed
published about itself — read off, not judged.

A parsed feed item is a plain dict with (best-effort across RSS+Atom shapes):
    id / guid / link, title, author, publication, published (pubDate/updated string),
    summary (description), content (content:encoded / full content).
"""
from __future__ import annotations

import re

_MAX_BODY = 16000


def item_id(rec: dict) -> str:
    # Stable native id: prefer the feed's own guid/id, else the permalink.
    return str(rec.get("guid") or rec.get("id") or rec.get("link") or "").strip()


def title(rec: dict) -> str:
    return " ".join(str(rec.get("title") or "").split()).strip()


def _year(rec: dict) -> str:
    published = str(rec.get("published") or "")
    m = re.search(r"(19|20)\d{2}", published)   # structural: 4-digit year from pubDate/updated
    return m.group(0) if m else ""


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "essay",           # → NEW "expert_analysis" tier (wired separately)
        "source_country": "global",
        "entity_type": "essay",
        "author": " ".join(str(rec.get("author") or "").split()).strip(),
        "publication": " ".join(str(rec.get("publication") or "").split()).strip(),
        "year": _year(rec),
    }
    return {k: v for k, v in f.items() if v}


def _strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()


def to_markdown(rec: dict) -> str:
    author = " ".join(str(rec.get("author") or "").split()).strip()
    publication = " ".join(str(rec.get("publication") or "").split()).strip()
    published = str(rec.get("published") or "").strip()

    parts: list[str] = [f"# {title(rec)}", ""]

    # Byline: WHO said it, WHERE, WHEN — plus the explicit expert-opinion framing.
    lead = " — ".join(x for x in [publication, author] if x) or "Expert analysis"
    byline = lead + (f", {published}" if published else "")
    parts += [
        byline
        + "  (expert analysis / opinion — a named expert's interpretation, "
        + "not a peer-reviewed or primary fact)",
        "",
    ]

    if rec.get("link"):
        parts += [f"URL: {rec['link']}", ""]

    body = rec.get("content") or rec.get("summary") or rec.get("description") or ""
    if isinstance(body, str) and body.strip():
        clean = _strip_html(body)
        if clean:
            if len(clean) > _MAX_BODY:
                clean = clean[:_MAX_BODY].rstrip() + " …"
            parts += ["## Analysis", clean, ""]

    return "\n".join(parts).strip() + "\n"
