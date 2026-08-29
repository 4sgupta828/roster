"""Turn a Reddit post (OAuth API "t3" link record) into a labeled market-SIGNAL document + facets.

Reddit is community/developer SENTIMENT — the LOWEST authority tier, exactly like Hacker News.
`source_kind=social` + `sk="reddit"` → `evidence_kind.classify` returns `sentiment_signal`, which the
authority policy NEVER lets be controlling. It is perception and momentum from the broader community,
never an established fact: the persona / answer format must present it as a labeled market signal.
STRUCTURAL only (Rule 18): score/comments are engagement counts we read off, not a judgment of truth.
"""
from __future__ import annotations

import re

_REDDIT = "https://www.reddit.com"


def _data(rec: dict) -> dict:
    """Accept either a raw t3 `data` dict or the `{kind, data}` envelope."""
    d = rec.get("data") if isinstance(rec.get("data"), dict) else rec
    return d or {}


def post_id(rec: dict) -> str:
    return str(_data(rec).get("id") or "").strip()


def title(rec: dict) -> str:
    return str(_data(rec).get("title") or "").strip()


def facets(rec: dict) -> dict:
    d = _data(rec)
    f = {
        "source_kind": "social",
        "source_country": "global",
        "entity_type": "discussion",
        "subreddit": str(d.get("subreddit") or "").strip(),
        "score": str(d.get("score") if d.get("score") is not None else ""),
        "num_comments": str(d.get("num_comments") if d.get("num_comments") is not None else ""),
    }
    created = d.get("created_utc")
    if isinstance(created, (int, float)) and created > 0:
        # structural: epoch-seconds → UTC year (timezone-aware; no naive utcfromtimestamp)
        import datetime
        f["year"] = str(datetime.datetime.fromtimestamp(int(created), datetime.timezone.utc).year)
    return {k: v for k, v in f.items() if v}


def permalink(rec: dict) -> str:
    p = str(_data(rec).get("permalink") or "").strip()
    return (_REDDIT + p) if p.startswith("/") else p


def to_markdown(rec: dict) -> str:
    d = _data(rec)
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = [f"Reddit post r/{d.get('subreddit','')} · {post_id(rec)}"]
    if d.get("score") is not None:
        meta.append(f"{d['score']} points")
    if d.get("num_comments") is not None:
        meta.append(f"{d['num_comments']} comments")
    if d.get("author"):
        meta.append(f"by u/{d['author']}")
    parts += [" · ".join(meta) + "  (market signal — community sentiment, not a verified fact)", ""]
    link = permalink(rec)
    if link:
        parts += [f"URL: {link}", ""]
    ext = str(d.get("url") or "").strip()
    if ext and ext != link:
        parts += [f"Links to: {ext}", ""]
    body = str(d.get("selftext") or "").strip()
    if body:
        clean = re.sub(r"\s+", " ", body).strip()
        if clean:
            parts += ["## Post", clean, ""]
    return "\n".join(parts).strip() + "\n"
