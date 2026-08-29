"""Turn a Stack Overflow question (Stack Exchange API) into a labeled developer-SIGNAL document + facets.

Stack Overflow is developer-adoption / community PERCEPTION — the LOWEST authority tier. `source_kind=social`
+ `sk="stackoverflow"` → `evidence_kind.classify` returns `sentiment_signal`, which the authority policy
NEVER lets be controlling. It is community perception, not fact: the persona/answer format must present it
as a labeled developer-adoption signal, never as an established finding. STRUCTURAL only (Rule 18): votes /
answer counts / tags are engagement figures we read off, not a judgment of truth.
"""
from __future__ import annotations

import datetime
import re


def question_id(rec: dict) -> str:
    return str(rec.get("question_id") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or "").strip()


def _tags(rec: dict) -> list[str]:
    tags = rec.get("tags") or []
    return [str(t).strip() for t in tags if str(t).strip()]


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "social",
        "source_country": "global",
        "entity_type": "qa",
        "score": str(rec.get("score") if rec.get("score") is not None else ""),
        "answer_count": str(rec.get("answer_count") if rec.get("answer_count") is not None else ""),
        "is_answered": str(rec.get("is_answered") if rec.get("is_answered") is not None else ""),
        "tags": ", ".join(_tags(rec)),
    }
    ts = rec.get("creation_date")
    if ts is not None and str(ts).strip():
        try:
            year = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).year
            f["year"] = str(year)
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = ["Stack Overflow question"]
    if rec.get("score") is not None:
        meta.append(f"{rec['score']} votes")
    if rec.get("answer_count") is not None:
        meta.append(f"{rec['answer_count']} answers")
    tags = _tags(rec)
    if tags:
        meta.append("tags: " + ", ".join(tags))
    parts += [" · ".join(meta)
              + "  (developer-adoption signal — community perception, not a verified fact)", ""]
    if rec.get("link"):
        parts += [f"URL: {rec['link']}", ""]
    body = rec.get("body") or ""
    if isinstance(body, str) and body.strip():
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()   # strip HTML from the question body
        if clean:
            parts += ["## Question", clean[:8000], ""]
    return "\n".join(parts).strip() + "\n"
