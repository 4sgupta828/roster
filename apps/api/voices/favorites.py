"""Favourites — the moments a reader wants to keep.

Stored per ACCOUNT, not per browser, because the point of keeping something is finding it again from
another device. A favourite carries a SNAPSHOT of the card as well as its id: feeds roll, and an
episode that scrolls out of its show's feed would otherwise leave a reader with a saved item that
renders as an empty row. The snapshot is what was true when they saved it; the id is how it rejoins
the live corpus when the piece is still there.
"""
from __future__ import annotations

import json

DDL = """
CREATE TABLE IF NOT EXISTS vo_favorite (
    user_id     text NOT NULL,
    moment_id   text NOT NULL,
    snapshot    jsonb NOT NULL DEFAULT '{}'::jsonb,
    note        text NOT NULL DEFAULT '',
    saved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, moment_id));
CREATE INDEX IF NOT EXISTS vo_favorite_user ON vo_favorite (user_id, saved_at DESC);
"""

KEEP = ("id", "kind", "text", "title", "show", "speaker", "role", "published", "url", "t_start",
        "image", "media", "register", "quotable", "company_id")


def snapshot_of(moment: dict) -> dict:
    """Only the fields a card renders. Storing the whole row would freeze facets we may re-derive."""
    return {k: moment.get(k) for k in KEEP if moment.get(k) not in (None, "", [], {})}


async def ensure(conn) -> None:
    await conn.execute(DDL)


async def add(conn, user_id: str, moment: dict, note: str = "") -> dict:
    await ensure(conn)
    mid = str(moment.get("id") or "").strip()
    if not mid:
        raise ValueError("a moment id is required")
    await conn.execute(
        "INSERT INTO vo_favorite (user_id, moment_id, snapshot, note) VALUES ($1,$2,$3::jsonb,$4) "
        "ON CONFLICT (user_id, moment_id) DO UPDATE SET snapshot = EXCLUDED.snapshot, "
        "    note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note ELSE vo_favorite.note END",
        user_id, mid, json.dumps(snapshot_of(moment)), (note or "")[:500])
    return {"id": mid, "saved": True}


async def remove(conn, user_id: str, moment_id: str) -> dict:
    await ensure(conn)
    await conn.execute("DELETE FROM vo_favorite WHERE user_id = $1 AND moment_id = $2",
                       user_id, moment_id)
    return {"id": moment_id, "saved": False}


async def listing(conn, user_id: str, *, limit: int = 100) -> list[dict]:
    """Newest first, each rendered from its snapshot. A saved moment always renders."""
    await ensure(conn)
    rows = await conn.fetch(
        "SELECT moment_id, snapshot, note, saved_at FROM vo_favorite WHERE user_id = $1 "
        "ORDER BY saved_at DESC LIMIT $2", user_id, int(max(1, min(limit, 500))))
    out = []
    for r in rows:
        snap = r["snapshot"]
        snap = json.loads(snap) if isinstance(snap, str) else (snap or {})
        snap["id"] = r["moment_id"]
        snap["note"] = r["note"]
        snap["saved_at"] = r["saved_at"].isoformat() if r["saved_at"] else ""
        out.append(snap)
    return out


async def ids(conn, user_id: str) -> list[str]:
    """Just the ids, so a result list can show which cards are already kept."""
    await ensure(conn)
    return [r["moment_id"] for r in
            await conn.fetch("SELECT moment_id FROM vo_favorite WHERE user_id = $1", user_id)]
