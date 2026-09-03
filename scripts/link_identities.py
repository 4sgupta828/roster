#!/usr/bin/env python
"""Cross-source IDENTITY LINKER (worker leg): github ↔ openalex/theorg/yc/sec by name + shared
employer, unique-name guarded. Idempotent; prints what it linked. `--dry` writes nothing."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps"))
from api.identity_links import find_and_write_links  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--max-dupes", type=int, default=3)
    args = ap.parse_args()
    import asyncpg
    conn = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    try:
        for src in ("github", "openalex"):
            others = ("openalex", "theorg", "yc", "sec") if src == "github" else ("theorg", "yc", "sec")
            res = await find_and_write_links(conn, src=src, others=others, max_dupes=args.max_dupes, dry=args.dry)
            print(f"{src}: candidates {res['candidates']} → linked {res['linked']} (rejected {res['rejected']})"
                  f"{' [dry]' if args.dry else ''}; e.g. {res['sample'][:3]}", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
