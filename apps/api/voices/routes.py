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
from .search import (AUDIENCES, VOICE_SOURCE_KEYS, build_query, build_vector_query, dedupe, fuse,
                     moment, terms, tsqueries)
from .summarize import cached, store, summarize


# The similarity a moment must reach to be shown UNDER A ROLE. Measured 2026-09-08 across five role
# probes: useful moments score 0.44-0.54 and the filler tail sits below 0.40. A strip is an aside on
# someone else's card, so its bar is higher than a search's — a reader did not ask for three rows.
MIN_ROLE_SCORE = 0.40


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


class RoleIn(BaseModel):
    """A job card asking what practitioners say about preparing for THIS role."""
    title: str = ""
    company: str = ""
    facets: dict = {}              # the posting's own facets: field, function, level, skill, specialty
    limit: int = 3


class JobIn(BaseModel):
    kind: str = "ingest"
    limit: int = 60
    dry_run: bool = False          # "embed": report what it would cost without spending it


def _window(days: int) -> str:
    """The ISO date a browse window starts at, or "" for no window."""
    days = int(days or 0)
    if days <= 0:
        return ""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=min(days, 3650))).isoformat()


def build_router(pool_of, *, manifest=None, pg_source_of=None, tenant_id: str = "default",
                 admin_token: str = "", llm_json=None, user_of=None, embed=None, embed_many=None) -> APIRouter:
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

        # THE SEMANTIC LEG runs first, because whether it is available CHANGES how the keyword leg is
        # asked. Words alone matched "Being Enthusiastic Is Not Being Fake" for "fake candidates" —
        # the words overlap, the meanings do not. Neither leg replaces the other: the vector leg
        # cannot see a block with no vector yet, and the keyword leg cannot see a paraphrase.
        matched, ranking, qvec = "", "keyword", None
        if not browse and embed is not None:
            try:
                qvec = await embed(body.q)
            except Exception:          # noqa: BLE001 — no vector is a degraded search, not a failed one
                qvec = None

        if browse:
            rows = await _fetch(since_=since)
        else:
            # With meaning available the words are taken LITERALLY: the generic-word stripping and
            # the loosest rung of the ladder are both compensations for having no semantics, and
            # leaving them on when the vector leg is running only adds noise back.
            rungs = tsqueries(terms(body.q, keep_generic=bool(qvec))) or [("any word", "")]
            if qvec and len(rungs) > 1:
                rungs = rungs[:-1]
            rows = []
            for label, tq in rungs:
                rows = await _fetch(tsquery=tq)
                matched = label
                # Stop at the strictest rung that answers with enough, from more than one voice. A
                # page of five pieces by the same publisher is technically the best match and reads
                # as if the corpus knows one person; widening one rung finds the others.
                voices = {str((r.get("facets") or {}).get("publication") or "") for r in rows}
                if len(rows) >= 6 and len(voices) >= 2:
                    break
            if qvec:
                sql, params = build_vector_query(qvec=qvec, kinds=tuple(body.kinds), audience=audience,
                                                 speaker=body.speaker, limit=want * 3, per_document=browse)
                vrows = await _rows(sql, params)
                if vrows:
                    rows = fuse(rows, vrows, limit=want * 6)
                    ranking = "hybrid"
                    if not matched:
                        matched = "meaning"
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
            "ranking": ranking,
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

    @router.post("/voices/for_role")
    async def voices_for_role(body: RoleIn) -> dict:
        """PREPARE FOR THIS ONE — the moments a candidate for this specific posting would want.

        This is the reason the corpus lives inside a job product rather than in a reader: the question
        "how do I prepare for this" is asked ON a role, not in a search box. The role's own words are
        the query, which is what the semantic leg is for — a posting title is a phrase, not keywords.

        Job-seeker material only. A hiring team's take on running the loop is not preparation advice,
        and mixing them would be the surface answering a question nobody asked.
        """
        if not voices_enabled():
            raise HTTPException(status_code=404, detail="voices mode is not enabled")
        want = max(1, min(int(body.limit or 3), 8))
        f = body.facets if isinstance(body.facets, dict) else {}

        def vals(key: str, n: int = 2) -> list[str]:
            v = f.get(key)
            v = v if isinstance(v, list) else ([v] if v else [])
            return [str(x).replace("_", " ") for x in v[:n] if x]

        # The probe reads as the question a candidate would actually ask, because that is what the
        # vector leg matches against — not a bag of facet tokens.
        bits = ["preparing for a"] + vals("level", 1) + [str(body.title or "").strip()] + ["interview"]
        extra = vals("specialty", 2) + vals("skill", 3) + vals("function", 1) + vals("field", 1)
        probe = " ".join(x for x in bits if x) + (" — " + ", ".join(extra) if extra else "")

        qvec = None
        if embed is not None:
            try:
                qvec = await embed(probe)
            except Exception:      # noqa: BLE001
                qvec = None
        # MEANING ONLY, and a floor. A role probe is a PHRASE, so the vector leg is the right
        # instrument; OR-ing its words was what put "Economics of building software" under an
        # account-executive posting. Measured 2026-09-08 over five role probes: a genuinely useful
        # moment scores 0.44-0.54 against this corpus, and the tail below 0.40 is filler. Three
        # weak rows are worse than one good one, so the strip returns FEWER rather than padding —
        # the same fail-safe the rest of the product uses: abstain, never "close enough".
        rows: list[dict] = []
        if qvec:
            sql, params = build_vector_query(qvec=qvec, audience="job_seeker", limit=want * 6,
                                             per_document=True)
            rows = [r for r in await _rows(sql, params) if float(r.get("score") or 0) >= MIN_ROLE_SCORE]
        else:
            # no embedder: a STRICT keyword query, never a loose one. Nothing beats noise here.
            kw = terms(str(body.title or "") + " " + " ".join(extra))
            if kw:
                sql, params = build_query(q=" ".join(kw), audience="job_seeker", limit=want * 4,
                                          per_document=True, tsquery=" & ".join(kw[:4]))
                rows = await _rows(sql, params)
        # ONE moment per piece and one per publisher: three chapters of the same episode is not
        # three answers, and three takes from one channel is not a range of views.
        moments = dedupe([moment(r) for r in rows], limit=want, per_document=1, per_source=1)
        return {"moments": moments, "probe": probe,
                "ranking": "meaning" if qvec else "keyword",
                "register": ("What practitioners say about preparing for a role like this — advice, "
                             "not a guide to this employer's process.")}

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
        if body.kind == "embed":
            # Give vectors to the blocks that have none. Idempotent and resumable; the cost is
            # reported first so a run is never launched blind.
            from roster_kernel.retrieval.backfill import count_missing, embed_missing
            pool = await pool_of()
            n, chars = await count_missing(pool, source_keys=list(VOICE_SOURCE_KEYS))
            if embed_many is None:
                return {"ok": False, "kind": "embed", "missing": n, "chars": chars,
                        "detail": "no embedder is configured here"}
            if body.dry_run:
                return {"ok": True, "kind": "embed", "dry_run": True, "missing": n, "chars": chars,
                        "projected_usd": round(chars / 3.7 / 1e6 * 0.02, 4)}
            out = await embed_missing(pool, embed_many, source_keys=list(VOICE_SOURCE_KEYS),
                                      batch=128, limit=max(1, min(int(body.limit or 2000), 20000)))
            return {"ok": True, "kind": "embed", "missing_before": n, **out}
        if body.kind == "mark_boilerplate":
            return {"ok": True, "kind": body.kind, "stamped": await mark_boilerplate(await pool_of())}
        raise HTTPException(status_code=400, detail=f"unknown job kind: {body.kind}")

    return router
