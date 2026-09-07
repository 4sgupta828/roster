"""THE EMPLOYER'S OWN WEBSITE, harvested from the board page each company publishes on (owner, 2026-09-07: "the company
link is there on the boards"). One request per COMPANY (never per posting), politely paced, cached forever in the
company entity's `link_website` row, resumable: a company already known is skipped.

    python scripts/company_sites.py --live --limit 400        # the biggest employers first
    python scripts/company_sites.py                           # dry run: what it would fetch

No model spend. Stored as a plain row (facet_key `link_website`, provenance `board:<ats>`) — it is a link, not a facet
value the search filters on, so it never enters the schema."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, "/app/apps" if os.path.isdir("/app/apps") else os.path.join(os.path.dirname(__file__), "..", "apps"))

UA = "Mozilla/5.0 (compatible; roster-jobs/1.0; +https://roster-api-production-3405.up.railway.app)"


def fetch(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(400_000).decode("utf-8", "replace")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true"); ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()
    import asyncpg
    from roster_vertical.employer_link import employer_page
    from roster_vertical.employer_site import company_site
    pool = await asyncpg.create_pool(os.environ["ROSTER_CORPUS_DSN"], min_size=1, max_size=2)
    async with pool.acquire() as conn:
        known = {r["entity_id"] for r in await conn.fetch(
            "SELECT entity_id FROM roster_entity_facet WHERE entity_kind='company' AND facet_key='link_website'")}
        rows = await conn.fetch("""SELECT lower(replace(company,' ','_')) AS slug, company, count(*) AS n,
                                          min(url) AS url, min(source) AS source
                                     FROM rs_job WHERE closed_at IS NULL AND company <> '' AND url <> ''
                                    GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 4000""")
    # one board per company, biggest employers first, skipping those we already know
    todo, seen = [], set()
    for r in rows:
        slug = r["slug"]
        if slug in seen or f"company:{slug}" in known:
            continue
        board = employer_page(r["url"], r["source"] or "", r["company"] or "")
        if not board:
            continue
        seen.add(slug)
        todo.append((slug, board, int(r["n"])))
        if len(todo) >= args.limit:
            break
    print(f"companies to try: {len(todo)} (already known: {len(known)})")
    got, failed, by_ats = 0, 0, defaultdict(int)
    for slug, board, n in todo:
        if not args.live:
            continue
        try:
            site = company_site(board, fetch(board))
        except Exception as e:   # noqa: BLE001 — a board that 404s or blocks is simply unknown
            site = None; failed += 1
            if failed <= 3:
                print(f"   {slug}: {type(e).__name__} {str(e)[:60]}")
        if site:
            got += 1
            by_ats[board.split("/")[2]] += 1
            async with pool.acquire() as conn:
                await conn.execute("""INSERT INTO roster_entity_facet (tenant_id, entity_id, facet_key, facet_value_norm,
                                          display_value, entity_kind, provenance, confidence)
                                      VALUES ('demo', $1, 'link_website', $2, $2, 'company', $3, 1.0)
                                      ON CONFLICT DO NOTHING""",
                                   f"company:{slug}", site, f"board:{board.split('/')[2]}")
        time.sleep(args.pause)
    await pool.close()
    print(f"sites found {got} · not found {len(todo) - got - failed} · fetch failures {failed}"
          f"{'' if args.live else ' (dry — nothing fetched)'}\n  by board: {dict(by_ats)}")


if __name__ == "__main__":
    asyncio.run(main())
