"""Turn a Hacker News story (Algolia HN Search API) into a labeled market-SIGNAL document + facets.

HN is developer/market SENTIMENT — the LOWEST authority tier. `source_kind=social` +
`sk="hackernews"` → `evidence_kind.classify` returns `sentiment_signal`, which the authority policy
NEVER lets be controlling. It is perception, not fact: the persona/answer format must present it as a
labeled market signal, never as an established finding. STRUCTURAL only (Rule 18): points/comments are
engagement counts we read off, not a judgment of truth.
"""
from __future__ import annotations


def story_id(rec: dict) -> str:
    return str(rec.get("objectID") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or rec.get("story_title") or "").strip()


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "social",
        "source_country": "global",
        "entity_type": "discussion",
        "points": str(rec.get("points") if rec.get("points") is not None else ""),
        "num_comments": str(rec.get("num_comments") if rec.get("num_comments") is not None else ""),
    }
    created = str(rec.get("created_at") or "")
    if len(created) >= 4 and created[:4].isdigit():
        f["year"] = created[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = [f"Hacker News story {story_id(rec)}"]
    if rec.get("points") is not None:
        meta.append(f"{rec['points']} points")
    if rec.get("num_comments") is not None:
        meta.append(f"{rec['num_comments']} comments")
    if rec.get("author"):
        meta.append(f"by {rec['author']}")
    if rec.get("created_at"):
        meta.append(str(rec["created_at"]))
    parts += [" · ".join(meta) + "  (market signal — community sentiment, not a verified fact)", ""]
    if rec.get("url"):
        parts += [f"URL: {rec['url']}", ""]
    txt = rec.get("story_text") or rec.get("comment_text") or ""
    if isinstance(txt, str) and txt.strip():
        import re
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt)).strip()   # strip HTML from self-posts
        if clean:
            parts += ["## Post", clean, ""]
    return "\n".join(parts).strip() + "\n"
