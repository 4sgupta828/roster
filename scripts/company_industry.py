"""COMPANY INDUSTRY from LOOKUP DATA (owner, 2026-09-07: "why are fintech jobs not captured?") — never a model guess.

The schema had no way to say what an employer DOES: a brief could name a role, a level, a place and a skill, but
"fintech" could only ride along as free text. This writes the company entity's own `industry` facet from the curated
map in `roster_vertical/data/company_industry.json`; jobs and people then read it through the via-key
`company_industry`, exactly as they read `company_type`.

Matching is deliberate and narrow: a company slug matches by its normalized form (lowercase, non-alphanumerics
removed) or by that form with a trailing legal suffix removed (inc / corp / llc / plc / ltd / group / holdings …).
An employer the map does not name gets NO industry facet — never a guess.

Idempotent; no spend. Runs in the API container:  python scripts/company_industry.py --live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, "/app/apps" if os.path.isdir("/app/apps") else os.path.join(os.path.dirname(__file__), "..", "apps"))

_SUFFIXES = ("inc", "incorporated", "corp", "corporation", "llc", "lp", "plc", "ltd", "limited", "co", "company",
             "group", "holdings", "holding", "sa", "se", "nv", "ag", "gmbh", "bv", "srl", "pte", "pty", "technologies",
             "technology", "labs", "lab", "software", "systems", "solutions", "global", "international", "usa")


def norm(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(slug or "").lower())


def core(slug: str) -> str:
    """The slug without a trailing legal / generic suffix: 'coupang_inc' → 'coupang', 'wpp plc' → 'wpp'."""
    parts = [p for p in re.split(r"[^a-z0-9]+", str(slug or "").lower()) if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return "".join(parts)


def load_derived() -> tuple[dict, int]:
    """({employee label: our industry}, min_support) — the free second source (see the data file's note)."""
    data = _data()
    d = data.get("derived_from_employee_labels") or {}
    return {str(k): str(v) for k, v in (d.get("map") or {}).items()}, int(d.get("min_support") or 2)


def _data() -> dict:
    try:
        from roster_vertical.data import company_industry as mod   # type: ignore
        return mod.DATA
    except Exception:   # noqa: BLE001
        p = pathlib.Path(__file__).resolve().parents[1] / "packages" / "vertical_roster" / "roster_vertical" / "data" / "company_industry.json"
        return json.loads(p.read_text()) if p.exists() else {"industries": {}, "as_of": ""}


def load_map() -> tuple[dict, str]:
    """{normalized slug: industry} from the curated file, with its as_of date."""
    data = _data()
    out: dict[str, str] = {}
    for industry, slugs in (data.get("industries") or {}).items():
        for s in slugs:
            out.setdefault(norm(s), industry)
            out.setdefault(core(s), industry)
    return out, str(data.get("as_of") or "")


def industry_of(slug: str, table: dict) -> str | None:
    return table.get(norm(slug)) or table.get(core(slug))


async def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--live", action="store_true"); args = ap.parse_args()
    import asyncpg
    from roster_vertical.facet_schema import FACET_SCHEMA
    from api.facet_store import FacetSQLStore, company_entity_id
    table, as_of = load_map()
    pool = await asyncpg.create_pool(os.environ["ROSTER_CORPUS_DSN"], min_size=1, max_size=2)

    async def getter():
        return pool
    store = FacetSQLStore(getter, FACET_SCHEMA)
    await store.ensure_schema()
    async with pool.acquire() as conn:
        job_cos = {r["c"] for r in await conn.fetch("SELECT DISTINCT lower(replace(company, ' ', '_')) AS c FROM rs_job WHERE closed_at IS NULL AND company <> ''")}
        ppl_cos = {r["c"] for r in await conn.fetch("SELECT DISTINCT facet_value_norm AS c FROM roster_entity_facet WHERE facet_key = 'company' AND entity_kind = 'person'")}
        postings = {r["c"]: r["n"] for r in await conn.fetch("SELECT lower(replace(company, ' ', '_')) AS c, count(*) n FROM rs_job WHERE closed_at IS NULL AND company <> '' GROUP BY 1")}
        # SECOND SOURCE: an employer whose employees carry one pre-schema `industry` label (never `sector`, which
        # describes the person's work, not the employer) takes that label when the curated map does not name it
        dmap, min_support = load_derived()
        derived: dict[str, str] = {}
        if dmap:
            rows = await conn.fetch("""SELECT co.facet_value_norm AS company, f.facet_value_norm AS label, count(*) AS n
                                         FROM roster_entity_facet f
                                         JOIN roster_entity_facet co ON co.entity_id = f.entity_id AND co.facet_key = 'company'
                                        WHERE f.entity_kind = 'person' AND f.facet_key = 'industry' AND f.facet_value_norm = ANY($1::text[])
                                        GROUP BY 1, 2 HAVING count(*) >= $2""", list(dmap), int(min_support))
            best: dict[str, tuple[int, str]] = {}
            for r in rows:                                   # one label per company: the one most employees carry
                cur = best.get(r["company"])
                if cur is None or r["n"] > cur[0]:
                    best[r["company"]] = (int(r["n"]), dmap[r["label"]])
            derived = {c: v for c, (_n, v) in best.items()}
    slugs = sorted(job_cos | ppl_cos)
    ver = FACET_SCHEMA.version()
    hit, written, covered_postings, by_industry, n_derived = 0, 0, 0, {}, 0
    for slug in slugs:
        ind = industry_of(slug, table)
        prov_kind = "curated"
        if not ind:
            ind = derived.get(slug) or derived.get(core(slug))
            prov_kind = "employees"
        if not ind:
            continue
        hit += 1
        n_derived += 1 if prov_kind == "employees" else 0
        covered_postings += int(postings.get(slug) or 0)
        by_industry[ind] = by_industry.get(ind, 0) + 1
        if args.live:
            prov = f"lookup:curated {as_of}".strip() if prov_kind == "curated" else "derived:employee_industry_labels"
            written += await store.project("company", company_entity_id(slug), {"schema_version": ver, "facets": {
                "industry": [{"value": ind, "confidence": 1.0 if prov_kind == "curated" else 0.7, "provenance": prov}]}})
    await pool.close()
    top = ", ".join(f"{k} {v}" for k, v in sorted(by_industry.items(), key=lambda kv: -kv[1])[:12])
    print(f"companies seen {len(slugs)} · matched {hit} (curated {hit - n_derived}, from employee labels {n_derived}) · rows written {written} · open postings covered {covered_postings}"
          f"{'' if args.live else ' (dry — nothing written)'}\n  {top}")
    if args.live and hit and not written:
        # `project` drops a key the schema does not declare: this pass once reported 1,342 matches and wrote 0 rows
        raise SystemExit("matched companies but wrote NO rows — is `industry` declared as a company key in the schema?")


if __name__ == "__main__":
    asyncio.run(main())
