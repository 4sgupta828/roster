"""Voices routes — the mode's API.

Public: search moments, and list the sources with the measurement that admitted each one (a reader is
entitled to know what this corpus is made of before weighing anything in it). Admin: ingest and the
boilerplate pass, both free of model spend.

The mode is flag-gated (ROSTER_VOICES). OFF is a true no-op: no routes, no tables touched.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from . import favorites
from .ingest import VOICE_CONNECTORS, ingest_voices, mark_boilerplate
from .search import AUDIENCES, build_query, dedupe, moment, terms, tsqueries
from .summarize import cached, store, summarize


def voices_enabled() -> bool:
    return os.environ.get("ROSTER_VOICES", "").lower() in ("1", "true", "yes", "on")


class SearchIn(BaseModel):
    q: str = ""
    kinds: list[str] = []          # essay | podcast | video | chapter
    audience: str = ""             # job_seeker | hiring_team | "" (everything)
    speaker: str = ""
    limit: int = 30
    days: int = 0                  # browse window: 7, 30, 90 … 0 means no window
    order: str = "recent"          # recent | watched


class SummaryIn(BaseModel):
    id: str                        # "<document_id>::<block_id>" as a moment card carries it
    refresh: bool = False          # re-read it: an extractive summary written during a model outage
    #                                is cached, and without this the outage's result outlives it


class FavoriteIn(BaseModel):
    moment: dict = {}              # the card as rendered, so a kept item survives the feed rolling
    note: str = ""


class JobIn(BaseModel):
    kind: str = "ingest"
    limit: int = 60


def _window(days: int) -> str:
    """The ISO date a browse window starts at, or "" for no window."""
    days = int(days or 0)
    if days <= 0:
        return ""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=min(days, 3650))).isoformat()


def build_router(pool_of, *, manifest=None, pg_source_of=None, tenant_id: str = "default",
                 admin_token: str = "", llm_json=None, user_of=None) -> APIRouter:
    router = APIRouter()

    async def _user(token: str) -> str:
        """The signed-in account id, or "" — a kept list follows the ACCOUNT, never the browser."""
        if user_of is None or not token:
            return ""
        try:
            u = await user_of(token)
        except Exception:      # noqa: BLE001 — a broken session must not break a search
            return ""
        return str((u or {}).get("id") or "") if isinstance(u, dict) else ""

    async def _rows(sql: str, params: list) -> list[dict]:
        pool = await pool_of()
        async with pool.acquire() as conn:
            out = await conn.fetch(sql, *params)

        def shape(rec) -> dict:
            d = dict(rec)
            d.pop("created_at", None)          # an internal column, never part of a card
            if "facets" in d:
                d["facets"] = json.loads(d["facets"]) if isinstance(d["facets"], str) else (d["facets"] or {})
            return d
        return [shape(r) for r in out]

    @router.post("/voices/search")
    async def voices_search(body: SearchIn, x_roster_token: str = Header(default="")) -> dict:
        """Moments matching the question. Keyword-ranked, so it works with no embedding provider."""
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        want = max(1, min(int(body.limit or 30), 60))
        since = _window(body.days)
        widened = False
        audience = body.audience if body.audience in AUDIENCES else ""
        # A BROWSE shows one moment per piece: a feed of "what landed" should list distinct pieces,
        # not two chapters of the same episode. A SEARCH may show two, because a second passage from
        # the same piece is often the better answer.
        browse = not body.q.strip()

        async def _fetch(tsquery: str = "", since_: str = "") -> list[dict]:
            sql, params = build_query(q=body.q, kinds=tuple(body.kinds), audience=audience,
                                      speaker=body.speaker, limit=want * 6, since=since_,
                                      order=body.order, per_document=browse, tsquery=tsquery)
            return await _rows(sql, params)

        # A question is answered STRICTLY first and relaxed only when the strict answer is thin.
        matched = ""
        if browse:
            rows = await _fetch(since_=since)
        else:
            rows = []
            for label, tq in tsqueries(terms(body.q)) or [("any word", "")]:
                rows = await _fetch(tsquery=tq)
                matched = label
                # Stop at the strictest rung that answers with enough, from more than one voice. A
                # page of five pieces by the same publisher is technically the best match and reads
                # as if the corpus knows one person; widening one rung finds the others.
                voices = {str((r.get("facets") or {}).get("publication") or "") for r in rows}
                if len(rows) >= 6 and len(voices) >= 2:
                    break
        # A window that returns almost nothing is worse than a wider one: widen rather than show an
        # empty week, and say which window the reader is actually looking at.
        if since and len(rows) < 6:
            since, widened = "", True
            rows = await _fetch()
        moments = dedupe([moment(r) for r in rows], limit=want, per_document=1 if browse else 2)
        uid = await _user(x_roster_token)
        if uid:
            pool = await pool_of()
            async with pool.acquire() as conn:
                kept = set(await favorites.ids(conn, uid))
            for m in moments:
                m["saved"] = m["id"] in kept
        return {
            "moments": moments,
            "counts": {"total": len(moments),
                       "quotable": sum(1 for m in moments if m["quotable"]),
                       "pointers": sum(1 for m in moments if not m["quotable"])},
            # Said plainly so the surface never has to guess or imply otherwise.
            "ranking": "keyword",
            "matched_on": matched,
            "window": {"since": since, "widened": widened},
            "audience": audience,
            # The one sentence this mode may never drop. It is not a disclaimer bolted on at the end:
            # the corpus is opinion, and a surface that forgets to say so has misrepresented it.
            "register": ("Practitioner advice and pointers — what these people argue and where they "
                         "said it. Not verified outcomes, and not Roster's recommendation."),
        }

    @router.get("/voices/sources")
    async def voices_sources() -> dict:
        """What this corpus is made of, and the measurement that admitted each source. A reader
        weighing advice is entitled to know whose advice, and why it is here at all."""
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        from roster_vertical import voices_sources as reg
        return {
            "sources": [{"label": s.label, "kind": s.kind, "audience": s.audience,
                         "author": s.author, "role": s.role, "yield_pct": s.yield_pct,
                         "on_topic_pct": s.on_topic_pct, "measured": s.measured}
                        for s in reg.SOURCES],
            "bars": {**reg.BARS, "on_topic": reg.ON_TOPIC_BAR},
            "rejected": [{"label": lbl, "why": why} for lbl, why in reg.REJECTED],
        }

    @router.post("/voices/summary")
    async def voices_summary(body: SummaryIn) -> dict:
        """What one piece says, read once and stored. Lazy by design: nothing is summarised until a
        reader opens that card, so spend follows attention rather than corpus size."""
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        doc = str(body.id or "").split("::", 1)[0]
        if not doc:
            raise HTTPException(status_code=400, detail="a moment id is required")
        pool = await pool_of()
        async with pool.acquire() as conn:
            hit = None if body.refresh else await cached(conn, doc)
            if hit:
                return {**hit, "cached": True}
            rows = await conn.fetch(
                "SELECT text, document_title, source_key, facets FROM rs_block "
                "WHERE document_id = $1 ORDER BY block_id LIMIT 400", doc)
            if not rows:
                raise HTTPException(status_code=404, detail="that piece is not in the corpus")
            facets = rows[0]["facets"]
            if isinstance(facets, str):
                facets = json.loads(facets)
            facets = facets or {}
            is_chapter = str(facets.get("source_kind") or "") == "chapter_pointer"
            text = "\n".join(str(r["text"] or "") for r in rows)
            out = await summarize(title=str(rows[0]["document_title"] or ""),
                                  author=str(facets.get("author") or facets.get("writer")
                                             or facets.get("publication") or ""),
                                  text=text, is_chapter=is_chapter,
                                  llm_json=(None if is_chapter else llm_json))
            await store(conn, doc, out)
        return {**out, "cached": False}

    @router.get("/voices/favorites")
    async def voices_favorites(x_roster_token: str = Header(default="")) -> dict:
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        uid = await _user(x_roster_token)
        if not uid:
            raise HTTPException(status_code=401, detail="sign in to keep moments")
        pool = await pool_of()
        async with pool.acquire() as conn:
            return {"moments": await favorites.listing(conn, uid)}

    @router.post("/voices/favorites")
    async def voices_favorite_add(body: FavoriteIn, x_roster_token: str = Header(default="")) -> dict:
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        uid = await _user(x_roster_token)
        if not uid:
            raise HTTPException(status_code=401, detail="sign in to keep moments")
        pool = await pool_of()
        async with pool.acquire() as conn:
            return await favorites.add(conn, uid, body.moment or {}, body.note or "")

    @router.delete("/voices/favorites/{moment_id:path}")
    async def voices_favorite_del(moment_id: str, x_roster_token: str = Header(default="")) -> dict:
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        uid = await _user(x_roster_token)
        if not uid:
            raise HTTPException(status_code=401, detail="sign in to keep moments")
        pool = await pool_of()
        async with pool.acquire() as conn:
            await favorites.remove(conn, uid, moment_id)
        return {"ok": True}

    @router.post("/admin/voices/jobs")
    async def voices_job(body: JobIn, x_admin_token: str = Header(default="")) -> dict:
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(status_code=403, detail="admin token required")
        if body.kind == "ingest":
            if manifest is None or pg_source_of is None:
                raise HTTPException(status_code=503, detail="ingest is unavailable here")
            src = pg_source_of()
            out = await ingest_voices(manifest, src, tenant_id=tenant_id,
                                      limit=max(1, min(int(body.limit or 60), 300)))
            return {"ok": True, "kind": "ingest", "blocks": out, "connectors": list(VOICE_CONNECTORS)}
        if body.kind == "mark_boilerplate":
            return {"ok": True, "kind": body.kind, "stamped": await mark_boilerplate(await pool_of())}
        raise HTTPException(status_code=400, detail=f"unknown job kind: {body.kind}")

    return router
