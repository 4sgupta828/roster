"""EVIDENCE MAP artifacts — Phase 3 of docs/specs/talent-intelligence-redesign.md.

A saved map is a DURABLE deliverable: the brief, the compiled filters, a snapshot of the rows (each
with its code-derived evidence packet), the coverage statement, map-level notes, per-row review
states + notes, and a private share token. Schema uses the broad map/entity/evidence/coverage
vocabulary (the Talent Map is the first package; a Company Map reuses the same tables). Recruiting
labels live in the app/FE layer only.

Exports preserve source links and evidence strength (spec acceptance) — CSV is built by code from
the snapshot; nothing is model-generated.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import uuid

_DDL = """
CREATE TABLE IF NOT EXISTS rs_map (
    id            text PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'demo',
    vertical      text NOT NULL,
    map_type      text NOT NULL,                 -- talent | company | expert | ...
    title         text NOT NULL DEFAULT '',
    brief         text NOT NULL,
    filters       jsonb NOT NULL DEFAULT '{}'::jsonb,
    coverage      jsonb NOT NULL DEFAULT '{}'::jsonb,
    rows          jsonb NOT NULL DEFAULT '[]'::jsonb,   -- entity row snapshots incl. evidence packets
    notes         text NOT NULL DEFAULT '',
    owner_id      text,
    share_token   text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rs_map_owner ON rs_map (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS rs_map_review (
    map_id        text NOT NULL REFERENCES rs_map(id) ON DELETE CASCADE,
    entity_id     text NOT NULL,
    state         text NOT NULL DEFAULT 'unreviewed',   -- unreviewed | shortlisted | needs more evidence | reviewed
    note          text NOT NULL DEFAULT '',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (map_id, entity_id)
);
"""

REVIEW_STATES = ("unreviewed", "shortlisted", "needs more evidence", "reviewed")
_MAX_ROWS = 400


class MapStore:
    def __init__(self, dsn: str, *, vertical: str):
        self._dsn = dsn
        self._vertical = vertical
        self._pool = None
        self._ready = False

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        return self._pool

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def create(self, *, tenant_id: str, map_type: str, brief: str, rows: list[dict],
                     coverage: dict | None, filters: dict | None, title: str = "",
                     owner_id: str | None = None) -> dict:
        await self._ensure()
        mid = uuid.uuid4().hex
        token = secrets.token_urlsafe(18)
        rows = list(rows or [])[:_MAX_ROWS]
        title = (title or "").strip()[:160] or (brief or "").strip()[:80]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_map (id, tenant_id, vertical, map_type, title, brief, filters,
                                       coverage, rows, owner_id, share_token)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11)""",
                mid, tenant_id, self._vertical, map_type, title, (brief or "").strip()[:2000],
                json.dumps(filters or {}), json.dumps(coverage or {}), json.dumps(rows),
                owner_id, token)
        return {"id": mid, "share_token": token, "title": title, "rows": len(rows)}

    async def get(self, map_id: str, *, owner_id: str | None = None,
                  share_token: str | None = None) -> dict | None:
        """Owner OR anyone holding the share token may read; others get None (not found)."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM rs_map WHERE id = $1", map_id)
            if r is None:
                return None
            if not ((owner_id and r["owner_id"] == owner_id) or
                    (share_token and secrets.compare_digest(share_token, r["share_token"]))):
                return None
            reviews = await conn.fetch(
                "SELECT entity_id, state, note, updated_at FROM rs_map_review WHERE map_id = $1", map_id)
        d = dict(r)
        for k in ("filters", "coverage", "rows"):
            v = d.get(k)
            d[k] = json.loads(v) if isinstance(v, str) else (v or ({} if k != "rows" else []))
        d["reviews"] = {x["entity_id"]: {"state": x["state"], "note": x["note"],
                                         "updated_at": str(x["updated_at"])} for x in reviews}
        d["is_owner"] = bool(owner_id and r["owner_id"] == owner_id)
        d["created_at"] = str(d["created_at"]); d["updated_at"] = str(d["updated_at"])
        if not d["is_owner"]:
            d.pop("share_token", None)      # a viewer must not learn the token from the payload
        return d

    async def list_mine(self, owner_id: str, *, limit: int = 50) -> list[dict]:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, map_type, title, brief, jsonb_array_length(rows) AS n_rows,
                          created_at, updated_at, share_token
                   FROM rs_map WHERE owner_id = $1 ORDER BY created_at DESC LIMIT $2""",
                owner_id, int(limit))
        return [{**dict(r), "created_at": str(r["created_at"]), "updated_at": str(r["updated_at"])}
                for r in rows]

    async def update(self, map_id: str, *, owner_id: str, title: str | None = None,
                     notes: str | None = None) -> bool:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """UPDATE rs_map SET title = COALESCE($3, title), notes = COALESCE($4, notes),
                                     updated_at = now()
                   WHERE id = $1 AND owner_id = $2""",
                map_id, owner_id,
                (title.strip()[:160] if title is not None else None),
                (notes[:8000] if notes is not None else None))
        return res.endswith("1")

    async def review(self, map_id: str, *, owner_id: str, entity_id: str, state: str,
                     note: str | None = None) -> bool:
        """Owner-only: set a row's HUMAN review state + note (never a verdict — no 'reject')."""
        await self._ensure()
        if state not in REVIEW_STATES:
            return False
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            own = await conn.fetchval("SELECT 1 FROM rs_map WHERE id = $1 AND owner_id = $2",
                                      map_id, owner_id)
            if not own:
                return False
            await conn.execute(
                """INSERT INTO rs_map_review (map_id, entity_id, state, note)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (map_id, entity_id) DO UPDATE SET
                     state = EXCLUDED.state,
                     note = CASE WHEN $5 THEN EXCLUDED.note ELSE rs_map_review.note END,
                     updated_at = now()""",
                map_id, entity_id, state, (note or "")[:2000], note is not None)
            await conn.execute("UPDATE rs_map SET updated_at = now() WHERE id = $1", map_id)
        return True


def build_csv(m: dict) -> str:
    """The export: one row per entity, preserving links and evidence strength (spec acceptance).
    Columns follow the Talent Map table: person, company, role, location, evidence strength,
    evidence types, sources, why surfaced, profile links, review state, note, entity id."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "current_company", "role", "location", "evidence_strength",
                "evidence_types", "source_families", "corroborated_claims", "gaps",
                "why_surfaced", "profile_links", "review_state", "review_note", "entity_id"])
    reviews = m.get("reviews") or {}
    for p in m.get("rows") or []:
        attrs = p.get("attributes") or []

        def _a(key):
            return next((a.get("display") for a in attrs if a.get("key") == key and a.get("display")), "")
        ev = p.get("evidence") or {}
        rv = reviews.get(p.get("entity_id") or "", {})
        links = " ".join((l.get("url") or "") for l in (p.get("links") or [])
                         if str(l.get("url") or "").startswith("http"))
        cite = (p.get("citation") or {}).get("document_id") or ""
        if str(cite).startswith("http") and cite not in links:
            links = (cite + " " + links).strip()
        w.writerow([
            p.get("name") or "", _a("company"), _a("role") or _a("title"),
            _a("metro") or _a("country"), ev.get("strength") or "",
            "|".join(ev.get("types") or []), "|".join(ev.get("families") or []),
            "|".join(ev.get("corroborated_keys") or []), "|".join(ev.get("gaps") or []),
            p.get("blurb") or "", links, rv.get("state") or "unreviewed", rv.get("note") or "",
            p.get("entity_id") or ""])
    return out.getvalue()
