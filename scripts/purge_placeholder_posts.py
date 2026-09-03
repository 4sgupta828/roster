#!/usr/bin/env python
"""Remove TEMPLATE-FILLER 'post' artifacts already written before `placeholder_post` existed
(demo hosts like astro-nano-demo.vercel.app, 'Blog Post number 2', off-site links from a personal
feed) plus posts whose URL is shared by several people (a starter theme's sample content). Dry by
default; --live deletes and re-counts rs_artifact_scan for the 'site' source.

    python scripts/purge_placeholder_posts.py          # report only
    python scripts/purge_placeholder_posts.py --live   # delete
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "apps"))

from api.artifacts import placeholder_post  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    import asyncpg
    conn = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    try:
        rows = await conn.fetch(
            "SELECT entity_id, artifact_key, title, url, detail->>'feed' AS feed FROM rs_person_artifact WHERE kind = 'post'")
        bad = [(r, placeholder_post({"title": r["title"], "url": r["url"]}, r["feed"] or "")) for r in rows]
        bad = [(r, w) for r, w in bad if w]
        shared = [r["url"] for r in await conn.fetch(
            "SELECT url FROM rs_person_artifact WHERE kind = 'post' GROUP BY url HAVING count(DISTINCT entity_id) > 1")]
        print(f"posts: {len(rows)} | filler: {len(bad)} {dict(Counter(w for _, w in bad))} | shared-url posts: {len(shared)}")
        for r, w in bad[:8]:
            print(f"  {w:16s} {r['entity_id']:28s} {r['title'][:40]!r:44s} {r['url'][:60]}")
        if not args.live:
            print("dry run — pass --live to delete")
            return
        n = 0
        for r, _ in bad:
            n += int((await conn.execute(
                "DELETE FROM rs_person_artifact WHERE entity_id = $1 AND kind = 'post' AND artifact_key = $2",
                r["entity_id"], r["artifact_key"])).split()[-1])
        n2 = int((await conn.execute(
            "DELETE FROM rs_person_artifact WHERE kind = 'post' AND url = ANY($1)", shared)).split()[-1])
        await conn.execute(
            """UPDATE rs_artifact_scan s SET n_found = (SELECT count(*) FROM rs_person_artifact a
                                                        WHERE a.entity_id = s.entity_id AND a.kind = 'post')
               WHERE s.source = 'site'""")
        left = await conn.fetchval("SELECT count(*) FROM rs_person_artifact WHERE kind = 'post'")
        print(f"deleted {n} filler + {n2} shared-url posts; {left} posts remain")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
