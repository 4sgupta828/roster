#!/usr/bin/env python
"""OpenAlex identities: authorship is not a job title. One-off data fix, DRY BY DEFAULT.

OpenAlex-sourced people (`openalex:*`) were minted with role 'Researcher' and a seniority
('Researcher' / 'Senior' / 'Principal' / 'Distinguished Scientist') INFERRED from citation counts.
The source evidences only that the person AUTHORED works under an AFFILIATION — a software engineer
who co-authored one paper under Anthropic's affiliation is not "a researcher at Anthropic".

  role facet      : keep facet_value_norm='researcher' (the search key "researchers in X" relies on)
                    but display_value → 'Published author' (what the source actually shows)
  seniority facet : DELETED for openalex:* — a bibliometric guess presented as a career level

    python scripts/fix_openalex_roles.py          # counts only
    python scripts/fix_openalex_roles.py --live   # apply
"""
from __future__ import annotations

import argparse
import asyncio
import os


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    import asyncpg
    conn = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    try:
        n_role = await conn.fetchval(
            "SELECT count(*) FROM roster_entity_facet WHERE entity_id LIKE 'openalex:%' AND facet_key='role' "
            "AND facet_value_norm='researcher' AND display_value <> 'Published author'")
        n_sen = await conn.fetchval(
            "SELECT count(*) FROM roster_entity_facet WHERE entity_id LIKE 'openalex:%' AND facet_key='seniority'")
        print(f"role displays to relabel: {n_role} | inferred seniority facets to delete: {n_sen}")
        if not args.live:
            print("dry run — pass --live to apply"); return
        r1 = await conn.execute(
            "UPDATE roster_entity_facet SET display_value='Published author' WHERE entity_id LIKE 'openalex:%' "
            "AND facet_key='role' AND facet_value_norm='researcher' AND display_value <> 'Published author'")
        r2 = await conn.execute(
            "DELETE FROM roster_entity_facet WHERE entity_id LIKE 'openalex:%' AND facet_key='seniority'")
        print("applied:", r1, "|", r2)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
