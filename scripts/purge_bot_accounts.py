#!/usr/bin/env python
"""Retire NON-PEOPLE minted as people: AI systems credited as paper authors on OpenAlex ('ChatGPT',
'OpenAI(ChatGPT)', 'Codex', 'A. ChatGPT'…) and GitHub bot / automation accounts ('AL Bot',
'dependabot', 'github-actions', '…[bot]'). DRY BY DEFAULT; `--live` sets rs_entity.status='retired'
(soft — every read path filters status='active'; nothing is deleted, reversible by hand) and removes
their vectors so they never rank. Name words that are also real names (Claude, Gemini, Bard, Cody)
are deliberately NOT matched.

    python scripts/purge_bot_accounts.py          # list + count
    python scripts/purge_bot_accounts.py --live   # retire
"""
from __future__ import annotations

import argparse
import asyncio
import os

# whole-word / anchored patterns — a person named "Roberto Botti" or "Claude Shannon" must not match
NAME_RX = (
    r"(^|[^a-z])(chatgpt|chat gpt|codex|copilot|dependabot|renovate|github-actions|actions-user|"
    r"openai ?\(|\(openai\)|gpt-?[0-9o]|gpt-?o?bot|llm assistant|ai assistant)([^a-z]|$)"
    r"|(^|\s)bot(\s|$)|\[bot\]$|^bot\b"
)
# login patterns only where the login itself DECLARES automation — a login merely ending in 'bot'
# is a person often enough ('paleolimbot' = Dewey Dunnington, 'KadoBOT' = Ricardo Ambrogi)
ID_RX = r"^github:(.*\[bot\]|.*-bot|bot-.*|chatgpt.*|codex.*|dependabot.*|renovate.*|github-actions.*|copilot.*|actions-user)$"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    import asyncpg
    conn = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    try:
        rows = await conn.fetch(
            "SELECT entity_id, name FROM rs_entity WHERE kind='person' AND status='active' "
            "AND (name ~* $1 OR entity_id ~* $2) ORDER BY name", NAME_RX, ID_RX)
        print(f"non-person accounts matched: {len(rows)}")
        from collections import Counter
        print("  by name:", Counter(r["name"] for r in rows).most_common(12))
        print("  by source:", Counter(r["entity_id"].split(":", 1)[0] for r in rows))
        if not args.live:
            print("dry run — pass --live to retire them"); return
        ids = [r["entity_id"] for r in rows]
        r1 = await conn.execute("UPDATE rs_entity SET status='retired' WHERE entity_id = ANY($1)", ids)
        r2 = await conn.execute("DELETE FROM rs_person_vec WHERE entity_id = ANY($1)", ids)
        r3 = await conn.execute("UPDATE rs_person_link SET status='revoked' WHERE a_id = ANY($1) OR b_id = ANY($1)", ids)
        print("applied:", r1, "|", r2, "|", r3)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
