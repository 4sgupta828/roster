"""Turn a company ENGINEERING-BLOG feed item (RSS/Atom) into a labeled, self-reported document + facets.

A company engineering blog (Netflix Tech Blog, Cloudflare, Meta Engineering, …) is a CORPORATE
artifact: a genuinely technical account of how an org builds — but written BY that org ABOUT its own
technology, so it carries a structural self-promotion bias ("why our architecture/product is great").
It is therefore SELF-REPORTED signal, not independent analysis: `source_kind="corp_eng"` maps to the
`technical_signal` tier (rank 2 — the same self-reported tier as GitHub traction / preprints), so it is
useful and retrievable but can NEVER outrank fact-checked press or a filing on a claim about the org's
own tech, and is never controlling. This is deliberately a LOWER tier than a NAMED individual's essay
(`expert_feed` → `expert_analysis`, rank 3), whose reputational incentive is to be right.

STRUCTURAL only (Rule 18): publication (the blog/company name)/author/year are metadata the feed
published about itself — read off, judged not at all. A parsed feed item is a plain dict with
(best-effort across RSS+Atom shapes): id/guid/link, title, author, publication, published, summary
(description), content (content:encoded / full content).
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
        "source_kind": "corp_eng",        # → technical_signal tier (rank 2, self-reported)
        "source_country": "global",
        "entity_type": "corp_eng",
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

    # Byline: WHICH company blog, WHO, WHEN — plus the explicit self-reported framing so a claim about
    # the company's own technology is never read as an independently verified fact.
    lead = " — ".join(x for x in [publication, author] if x) or "Company engineering blog"
    byline = lead + (f", {published}" if published else "")
    parts += [
        byline
        + "  (company engineering blog — a self-reported technical account by the organization "
        + "about its own technology, not independently verified press or a primary record)",
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
            parts += ["## Engineering write-up", clean, ""]

    return "\n".join(parts).strip() + "\n"
