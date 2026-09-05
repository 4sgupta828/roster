"""COMPANY facets from LOOKUP DATA (docs/specs/facet-contract-evaluator.md §2.3, step 5) — never a model guess.

For every company slug the index knows (open postings + people's employers) write the company entity's own
facets: `type` ∈ fortune500 | public | big_tech | startup | other and `stage` when a record states it.
Sources, in order of authority: the curated Fortune-500 set in the vertical, the curated big-tech list
(`roster_vertical/data/company_sets.json`, dated), then the accelerator / stage facets people carry.
Idempotent; no spend. Runs in the API container:  python scripts/company_facets.py --live"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, "/app/apps" if os.path.isdir("/app/apps") else os.path.join(os.path.dirname(__file__), "..", "apps"))


def _key(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(slug or "").lower())


async def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--live", action="store_true"); args = ap.parse_args()
    import asyncpg
    from roster_vertical.facet_schema import FACET_SCHEMA
    from api.facet_store import FacetSQLStore, company_entity_id
    from api.people_population import _F500
    try:
        from roster_vertical.data import company_sets  # type: ignore
        sets = company_sets.SETS
    except Exception:   # noqa: BLE001
        import pathlib
        p = pathlib.Path(__file__).resolve().parents[1] / "packages" / "vertical_roster" / "roster_vertical" / "data" / "company_sets.json"
        sets = json.loads(p.read_text()) if p.exists() else {"big_tech": [], "as_of": ""}
    big = {_key(x) for x in sets.get("big_tech", [])}
    f500 = {_key(x) for x in _F500}
    pool = await asyncpg.create_pool(os.environ["ROSTER_CORPUS_DSN"], min_size=1, max_size=2)

    async def getter():
        return pool
    store = FacetSQLStore(getter, FACET_SCHEMA)
    await store.ensure_schema()
    async with pool.acquire() as conn:
        job_cos = {r["c"] for r in await conn.fetch("SELECT DISTINCT lower(replace(company, ' ', '_')) AS c FROM rs_job WHERE closed_at IS NULL AND company <> ''")}
        ppl_cos = {r["c"] for r in await conn.fetch("SELECT DISTINCT facet_value_norm AS c FROM roster_entity_facet WHERE facet_key = 'company' AND entity_kind = 'person'")}
        startup = {r["c"] for r in await conn.fetch("""SELECT DISTINCT f.facet_value_norm AS c FROM roster_entity_facet f
                                                        WHERE f.facet_key = 'company' AND f.entity_id IN (
                                                          SELECT entity_id FROM roster_entity_facet WHERE facet_key = 'accelerator' OR (facet_key = 'stage' AND facet_value_norm = 'startup'))""")}
        public = {r["c"] for r in await conn.fetch("""SELECT DISTINCT f.facet_value_norm AS c FROM roster_entity_facet f
                                                       WHERE f.facet_key = 'company' AND f.entity_id IN (SELECT entity_id FROM roster_entity_facet WHERE facet_key = 'stage' AND facet_value_norm = 'public')""")}
    slugs = sorted(job_cos | ppl_cos)
    ver = FACET_SCHEMA.version(); n = {"fortune500": 0, "big_tech": 0, "public": 0, "startup": 0, "other": 0}
    for slug in slugs:
        k = _key(slug)
        if k in f500:
            t, prov = "fortune500", "lookup:fortune500"
        elif k in big:
            t, prov = "big_tech", f"lookup:big_tech {sets.get('as_of', '')}".strip()
        elif slug in public or k in {_key(x) for x in public}:
            t, prov = "public", "registry:stage"
        elif slug in startup or k in {_key(x) for x in startup}:
            t, prov = "startup", "registry:accelerator_or_stage"
        else:
            t, prov = "other", "lookup:none"
        n[t] += 1
        if not args.live:
            continue
        env = {"schema_version": ver, "facets": {"type": [{"value": t, "confidence": 1.0, "provenance": prov}]}}
        if t == "public":
            env["facets"]["stage"] = [{"value": "public", "confidence": 1.0, "provenance": "registry:stage"}]
        await store.project("company", company_entity_id(slug), env)
    await pool.close()
    print(f"companies: {len(slugs)} · {n}{'' if args.live else ' (dry — nothing written)'}")


if __name__ == "__main__":
    asyncio.run(main())
