"""Turn a Lobsters (lobste.rs) story into a labeled sentiment-SIGNAL document + facets.

Lobsters is a technical-community discussion board — a cleaner, more expert alternative to Reddit
for deep-tech. It is practitioner PERCEPTION, the LOWEST authority tier. `source_kind=social` +
`sk="lobsters"` → `evidence_kind.classify` returns `sentiment_signal`, which the authority policy
NEVER lets be controlling. It is a labeled market/community signal, never an established finding.
STRUCTURAL only (Rule 18): score/comment_count/tags are engagement metadata we read off, not a
judgment of truth.
"""
from __future__ import annotations

import re


def story_id(rec: dict) -> str:
    return str(rec.get("short_id") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or "").strip()


def _tags(rec: dict) -> list[str]:
    return [str(t).strip() for t in (rec.get("tags") or []) if str(t).strip()]


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "social",
        "source_country": "global",
        "entity_type": "discussion",
        "score": str(rec.get("score") if rec.get("score") is not None else ""),
        "comment_count": str(rec.get("comment_count") if rec.get("comment_count") is not None else ""),
        "tags": ", ".join(_tags(rec)),
    }
    created = str(rec.get("created_at") or "")
    if len(created) >= 4 and created[:4].isdigit():
        f["year"] = created[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = ["Lobsters story"]
    if rec.get("score") is not None:
        meta.append(f"{rec['score']} points")
    if rec.get("comment_count") is not None:
        meta.append(f"{rec['comment_count']} comments")
    tags = _tags(rec)
    if tags:
        meta.append("tags: " + ", ".join(tags))
    parts += [
        " · ".join(meta)
        + "  (technical-community discussion — practitioner perception, not a verified fact)",
        "",
    ]
    if rec.get("url"):
        parts += [f"URL: {rec['url']}", ""]
    if rec.get("comments_url"):
        parts += [f"Discussion: {rec['comments_url']}", ""]
    txt = rec.get("description") or ""
    if isinstance(txt, str) and txt.strip():
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt)).strip()   # strip HTML/markup
        if clean:
            parts += ["## Description", clean, ""]
    return "\n".join(parts).strip() + "\n"
