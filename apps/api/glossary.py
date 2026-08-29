"""Glossary persistence — the accumulating web of explained domain terms.

Every time a user asks "explain the key terms" on an answer (or navigates to a related term),
the explanations are upserted here. Over time this becomes the deployment's own navigable
vocabulary corpus: All Terms, searchable, each term linking to its RELATED terms (edges of the
knowledge web). App-level (NOT kernel), same Postgres as the corpus (ROSTER_CORPUS_DSN).

VERTICAL-ISOLATED like SessionStore: bound to one vertical, every query scoped by it.
Upsert semantics: first explanation wins for the definition fields (stable entries — a term's
meaning doesn't churn per answer); `related` edges are UNIONED (the web only grows);
`times_seen` counts how often the term resurfaced. Saving is best-effort: a persistence
failure must never break the /terms response.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_glossary_term (
    id          TEXT PRIMARY KEY,
    vertical    TEXT NOT NULL,
    term_key    TEXT NOT NULL,
    term        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    plain       TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT '',
    application TEXT NOT NULL DEFAULT '',
    related     JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_session_id TEXT,
    times_seen  INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vertical, term_key)
);
CREATE INDEX IF NOT EXISTS idx_ngt_vertical_term ON roster_glossary_term (vertical, term_key);
CREATE INDEX IF NOT EXISTS idx_ngt_vertical_updated ON roster_glossary_term (vertical, updated_at DESC);
"""


def _norm(term: str) -> str:
    return " ".join((term or "").lower().split())


class GlossaryStore:
    """Async Postgres-backed store, BOUND to one vertical. Schema ensured lazily."""

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

    async def upsert_many(self, terms: list[dict], *, session_id: str | None = None) -> int:
        """Accumulate explanations. First write wins on definition fields; related edges are
        unioned; times_seen increments. Returns how many rows were touched."""
        await self._ensure()
        pool = await self._get_pool()
        n = 0
        async with pool.acquire() as conn:
            for t in terms:
                key = _norm(t.get("term", ""))
                if not key or not (t.get("plain") or "").strip():
                    continue
                await conn.execute(
                    """
                    INSERT INTO roster_glossary_term
                        (id, vertical, term_key, term, category, plain, purpose, application,
                         related, first_session_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
                    ON CONFLICT (vertical, term_key) DO UPDATE SET
                        times_seen = roster_glossary_term.times_seen + 1,
                        related = (
                            SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb)
                            FROM jsonb_array_elements_text(roster_glossary_term.related || EXCLUDED.related) AS v
                        ),
                        updated_at = now()
                    """,
                    uuid.uuid4().hex[:16], self._vertical, key,
                    (t.get("term") or "").strip(), (t.get("category") or "").strip(),
                    (t.get("plain") or "").strip(), (t.get("purpose") or "").strip(),
                    (t.get("application") or "").strip(),
                    json.dumps([s for s in (t.get("related") or []) if (s or "").strip()]),
                    session_id)
                n += 1
        return n

    async def list(self, *, q: str | None = None, letter: str | None = None,
                   limit: int = 500, offset: int = 0) -> dict[str, Any]:
        """Browse/search the accumulated glossary. Returns terms + total + per-letter counts
        (the A-Z rail) + which related terms already have entries (navigability)."""
        await self._ensure()
        pool = await self._get_pool()
        where, args = ["vertical = $1"], [self._vertical]
        if q and q.strip():
            args.append(f"%{q.strip().lower()}%")
            where.append(f"(term_key LIKE ${len(args)} OR lower(plain) LIKE ${len(args)})")
        if letter and letter.strip():
            args.append(letter.strip().lower()[:1] + "%")
            where.append(f"term_key LIKE ${len(args)}")
        w = " AND ".join(where)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT term_key, term, category, plain, purpose, application, related,
                           times_seen, updated_at
                    FROM roster_glossary_term WHERE {w}
                    ORDER BY term_key LIMIT {int(limit)} OFFSET {int(offset)}""", *args)
            total = await conn.fetchval(
                f"SELECT count(*) FROM roster_glossary_term WHERE {w}", *args)
            letters = await conn.fetch(
                "SELECT substr(term_key,1,1) AS l, count(*) AS n FROM roster_glossary_term "
                "WHERE vertical = $1 GROUP BY 1 ORDER BY 1", self._vertical)
            known = {r["term_key"] for r in await conn.fetch(
                "SELECT term_key FROM roster_glossary_term WHERE vertical = $1", self._vertical)}
        terms = []
        for r in rows:
            rel = r["related"]
            rel = json.loads(rel) if isinstance(rel, str) else (rel or [])
            terms.append({
                "term_key": r["term_key"], "term": r["term"], "category": r["category"],
                "plain": r["plain"], "purpose": r["purpose"], "application": r["application"],
                "related": [{"term": s, "known": _norm(s) in known} for s in rel],
                "times_seen": r["times_seen"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return {"terms": terms, "total": int(total or 0),
                "letters": {r["l"]: r["n"] for r in letters}}

    async def get(self, term: str) -> dict[str, Any] | None:
        """One term by (normalized) name, with related-term navigability flags."""
        key = _norm(term)
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT term_key, term, category, plain, purpose, application, related, times_seen "
                "FROM roster_glossary_term WHERE vertical = $1 AND term_key = $2",
                self._vertical, key)
            if not r:
                return None
            known = {x["term_key"] for x in await conn.fetch(
                "SELECT term_key FROM roster_glossary_term WHERE vertical = $1", self._vertical)}
        rel = r["related"]
        rel = json.loads(rel) if isinstance(rel, str) else (rel or [])
        return {"term_key": r["term_key"], "term": r["term"], "category": r["category"],
                "plain": r["plain"], "purpose": r["purpose"], "application": r["application"],
                "related": [{"term": s, "known": _norm(s) in known} for s in rel],
                "times_seen": r["times_seen"]}

    async def sanitize_markup(self) -> dict[str, int]:
        """One-time repair: strip stray XML/HTML-ish tags (e.g. `<r>metformin</r>`) that older
        writes let leak into `term`/`related` before the kernel cleaned them. Structural cleanup of
        markup, not a semantic edit (Rule 18). Idempotent — a clean row is left untouched."""
        from roster_kernel.research.terms import strip_markup
        await self._ensure()
        pool = await self._get_pool()
        scanned = changed = 0
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, term, related FROM roster_glossary_term WHERE vertical = $1",
                self._vertical)
            for r in rows:
                scanned += 1
                rel = r["related"]
                rel = json.loads(rel) if isinstance(rel, str) else (rel or [])
                new_term = strip_markup(r["term"])
                seen: set[str] = set()
                new_rel: list[str] = []
                for s in rel:
                    c = strip_markup(s)
                    k = c.lower()
                    if c and k not in seen:
                        seen.add(k)
                        new_rel.append(c)
                if new_term == r["term"] and new_rel == rel:
                    continue
                await conn.execute(
                    "UPDATE roster_glossary_term SET term = $2, related = $3::jsonb, "
                    "updated_at = now() WHERE id = $1",
                    r["id"], new_term, json.dumps(new_rel))
                changed += 1
        return {"scanned": scanned, "changed": changed}

    async def count(self) -> int:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT count(*) FROM roster_glossary_term WHERE vertical = $1",
                self._vertical) or 0)
