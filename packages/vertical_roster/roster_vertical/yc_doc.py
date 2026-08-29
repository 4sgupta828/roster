"""Turn a Y Combinator directory record into a company-profile document + facets.

The YC company directory is the POPULATION SEED of startups — the roster of YC-backed companies
with their batch, one-liner, description, industry/tags, location, status, team size and (when the
detail page is enriched) FOUNDERS. It is a curated public directory, not an audited filing, so it
grades `source_kind=reference` → `evidence_kind.classify` returns `verified_structured` (the same
neutral-structured tier as Wikidata: retrievable + span-grounded, never authority-controlling). The
`## Founders` section (names + titles, straight off the YC profile) is the team/execution signal —
who is actually building the company.

STRUCTURAL only (Rule 18): every field here is read verbatim off the Algolia/detail record (name,
batch, status, industries, founder names/titles) — no semantic judgment, no inference. Fields differ
slightly between the Algolia search record and the detail page, so we read whichever the record
carries (e.g. status from `status` OR `ycdc_status`, location from `all_locations` OR `location`).
"""
from __future__ import annotations


def company_id(rec: dict) -> str:
    """Stable native id: the YC slug (human-readable, also the detail-page key); fallbacks to ids."""
    for k in ("slug", "objectID", "id"):
        v = str(rec.get(k) or "").strip()
        if v:
            return v
    return ""


def title(rec: dict) -> str:
    return str(rec.get("name") or rec.get("title") or "").strip()


def _status(rec: dict) -> str:
    return str(rec.get("status") or rec.get("ycdc_status") or "").strip()


def _location(rec: dict) -> str:
    loc = str(rec.get("all_locations") or rec.get("location") or "").strip()
    if not loc:
        parts = [str(rec.get(k) or "").strip() for k in ("city", "country")]
        loc = ", ".join(p for p in parts if p)
    return loc


def _industries(rec: dict) -> list[str]:
    """Ordered, de-duped industry labels read off the record (industry + industries[] + subindustry)."""
    out: list[str] = []
    seen: set[str] = set()
    raw = [rec.get("industry")]
    ind = rec.get("industries")
    if isinstance(ind, list):
        raw += ind
    raw.append(rec.get("subindustry"))
    for v in raw:
        s = str(v or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _tags(rec: dict) -> list[str]:
    raw = rec.get("tags")
    return [str(t).strip() for t in raw if str(t).strip()] if isinstance(raw, list) else []


def _founders(rec: dict) -> list[dict]:
    raw = rec.get("founders")
    return [f for f in (raw or []) if isinstance(f, dict)] if isinstance(raw, list) else []


def facets(rec: dict) -> dict:
    loc = _location(rec)
    us = ("USA" in loc) or ("United States" in loc) or (str(rec.get("country") or "").strip() in ("US", "USA"))
    f = {
        "source_kind": "reference",        # → verified_structured tier (Wikidata precedent)
        "entity_type": "company",
        "source_country": "US" if us else "global",
        "batch": str(rec.get("batch") or "").strip(),
        "yc_status": _status(rec).lower(),
    }
    yf = str(rec.get("year_founded") or "").strip()
    if yf[:4].isdigit() and len(yf[:4]) == 4:
        f["year"] = yf[:4]
    ind = _industries(rec)
    if ind:
        f["industry"] = ind[0].lower()
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    name = title(rec) or "(unnamed company)"
    parts: list[str] = [f"# {name}", ""]

    meta: list[str] = []
    batch = str(rec.get("batch") or "").strip()
    if batch:
        meta.append(f"YC {batch}")
    batch_name = str(rec.get("batch_name") or "").strip()
    if batch_name and batch_name.lower() != batch.lower():
        meta.append(batch_name)
    st = _status(rec)
    if st:
        meta.append(f"status: {st}")
    yf = str(rec.get("year_founded") or "").strip()
    if yf:
        meta.append(f"founded {yf}")
    loc = _location(rec)
    if loc:
        meta.append(loc)
    ts = str(rec.get("team_size") or "").strip()
    if ts and ts != "0":
        meta.append(f"team size {ts}")
    lead = " · ".join(meta) if meta else "Y Combinator company"
    parts += [f"{lead}  (Y Combinator company directory profile — the YC-backed startup population)", ""]

    if rec.get("one_liner"):
        parts += [str(rec["one_liner"]).strip(), ""]

    founders = _founders(rec)
    if founders:
        parts += ["## Founders", ""]
        for fdr in founders:
            fname = str(fdr.get("full_name") or fdr.get("name") or "").strip()
            if not fname:
                continue
            role = str(fdr.get("title") or fdr.get("founder_role") or "").strip()
            parts.append(f"- {fname}" + (f" — {role}" if role else ""))
        parts += [""]

    if rec.get("long_description"):
        parts += ["## About", str(rec["long_description"]).strip(), ""]

    ind = _industries(rec)
    if ind:
        parts += ["## Industry", ", ".join(ind), ""]
    tags = _tags(rec)
    if tags:
        parts += ["## Tags", ", ".join(tags), ""]

    site = str(rec.get("website") or "").strip()
    if site:
        parts += [f"Website: {site}", ""]

    return "\n".join(parts).strip() + "\n"
