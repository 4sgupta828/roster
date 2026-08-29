"""Turn a GDELT news article record into a small citable markdown document + facets.

GDELT gives article METADATA (title, domain, date, country) — not the body — so a news "document" is
a headline-level signal. It grades as `analysis` (reputable press) via evidence_kind, and the persona
treats news as non-primary — useful for detecting activity, launches, and market moves, never as an
audited fact. The article URL is the document id, so the citation links straight to the source.
"""
from __future__ import annotations


def article_url(rec: dict) -> str:
    return str(rec.get("url") or "").strip()


def title(rec: dict) -> str:
    # GDELT tokenizes titles with spaces around punctuation; collapse for readability.
    return " ".join(str(rec.get("title") or "").split()).replace(" .", ".").replace(" ,", ",").strip()


def _date(rec: dict) -> str:
    d = str(rec.get("seendate") or "")
    return d[:8] if len(d) >= 8 else d   # YYYYMMDD


def facets(rec: dict) -> dict:
    d = _date(rec)
    f = {
        "source_kind": "news",
        "domain": str(rec.get("domain") or "").lower(),
        "sourcecountry": str(rec.get("sourcecountry") or "").lower(),
        "language": str(rec.get("language") or "").lower(),
    }
    if d[:4].isdigit():
        f["year"] = d[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    d = _date(rec)
    date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
    parts = [f"# {title(rec)}", ""]
    parts += [f"News headline reported by {rec.get('domain','')} "
              f"({rec.get('sourcecountry','')}), {date_str}.", ""]
    parts += ["This is a news signal (headline-level), not an audited fact.", ""]
    return "\n".join(parts).strip() + "\n"
