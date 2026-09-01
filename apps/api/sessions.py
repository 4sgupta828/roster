"""Research-session persistence — save each Q&A (and any generated video) like factra's
`rs_research_session`. App-level (NOT kernel): keeps the kernel generic. Backed by the same
Postgres the corpus uses (ROSTER_CORPUS_DSN); a no-op store is used when no DSN is set so the
API still runs against the fixture corpus.

VERTICAL-ISOLATED: the store is bound to ONE vertical (the deployment's active vertical) and
every query is scoped by it, so sessions never cross verticals even if a DB were shared. This
composes with tenant isolation (both are filtered).

Saving is best-effort: a persistence failure must never break the /research response.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_research_session (
    id             TEXT PRIMARY KEY,
    vertical       TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    workspace_id   TEXT,
    question       TEXT NOT NULL,
    answer         TEXT NOT NULL DEFAULT '',
    grounded       BOOLEAN NOT NULL DEFAULT FALSE,
    claims         JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_stats   JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage_gaps  JSONB NOT NULL DEFAULT '[]'::jsonb,
    rejected       INTEGER NOT NULL DEFAULT 0,
    sources        JSONB NOT NULL DEFAULT '[]'::jsonb,
    video_filename TEXT,
    video_title    TEXT,
    video_duration DOUBLE PRECISION,
    user_name      TEXT,
    user_email     TEXT,
    visual_observation TEXT,
    attachments    JSONB NOT NULL DEFAULT '[]'::jsonb,
    layman_answer  TEXT,
    deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    thread         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- additive columns for pre-existing tables (expand-only; safe to re-run)
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS user_name  TEXT;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS user_email TEXT;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS visual_observation TEXT;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS attachments JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS real_patient BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS layman_answer TEXT;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS thread JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS audience TEXT NOT NULL DEFAULT 'clinician';
ALTER TABLE roster_research_session ADD COLUMN IF NOT EXISTS terms JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS idx_nrs_vertical_tenant_created
    ON roster_research_session (vertical, tenant_id, created_at DESC);
"""


class SessionStore:
    """Async Postgres-backed store, BOUND to one vertical. Schema ensured lazily.

    Every read/write is scoped by `self._vertical` so a deployment can only ever see its
    own vertical's sessions.
    """

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

    async def save(self, *, tenant_id: str, workspace_id: str | None, question: str,
                   answer: str, grounded: bool, claims: list[dict], source_stats: dict,
                   coverage_gaps: list[str], rejected: int, sources: list[str] | None,
                   user_name: str | None = None, user_email: str | None = None,
                   visual_observation: str | None = None,
                   attachments: list[dict] | None = None,
                   audience: str = "clinician",
                   charts: list[dict] | None = None,
                   interpretation: list[dict] | None = None,
                   confidence: dict | None = None,
                   reasoning_purpose: str = "",
                   reasoning_conclusion: str = "",
                   diagnostics: dict | None = None,
                   question_contract: dict | None = None,
                   web_providers: dict | None = None,
                   kind: str = "research",
                   extra: dict | None = None) -> str:
        await self._ensure()
        sid = uuid.uuid4().hex
        # turn 0 also lives in `thread` so a conversation is one shareable row; the flat columns
        # keep the first turn for the list view + backward compatibility.
        turn0 = {"question": question, "answer": answer, "grounded": grounded, "claims": claims,
                 "source_stats": source_stats, "coverage_gaps": coverage_gaps, "rejected": rejected,
                 "visual_observation": visual_observation, "attachments": attachments or []}
        if kind and kind != "research":
            turn0["kind"] = kind             # e.g. "panel" — the reopen path renders it differently
        if extra:
            turn0.update(extra)              # kind-specific payload (e.g. panel takes / n_specialists)
        if audience and audience != "clinician":
            turn0["audience"] = audience     # per-turn tag; the flat column drives list segmentation
        if charts:
            turn0["charts"] = charts         # grounded charts persist in the shareable session (JSONB)
        if interpretation:
            turn0["interpretation"] = interpretation   # reasoning-read layer persists (JSONB)
        if confidence:
            turn0["confidence"] = confidence
        if reasoning_purpose:
            turn0["reasoning_purpose"] = reasoning_purpose
        if reasoning_conclusion:
            turn0["reasoning_conclusion"] = reasoning_conclusion
        if diagnostics:
            turn0["diagnostics"] = diagnostics
        if question_contract:
            # Schema-registry phase 0: the derived QuestionContract (mode/entities/axes) rides the
            # JSONB thread turn — additive field only, no schema migration needed.
            turn0["question_contract"] = question_contract
        if web_providers:
            turn0["web_providers"] = web_providers   # search-source attribution (JSONB thread turn; additive)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_research_session
                   (id, vertical, tenant_id, workspace_id, question, answer, grounded, claims,
                    source_stats, coverage_gaps, rejected, sources, user_name, user_email,
                    visual_observation, attachments, thread, audience)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12::jsonb,
                           $13,$14,$15,$16::jsonb,$17::jsonb,$18)""",
                sid, self._vertical, tenant_id, workspace_id, question, answer, grounded,
                json.dumps(claims), json.dumps(source_stats), json.dumps(coverage_gaps),
                rejected, json.dumps(sources or []),
                (user_name or None), (user_email or None),
                (visual_observation or None), json.dumps(attachments or []),
                json.dumps([turn0]), (audience or "clinician"),
            )
        return sid

    async def append_turn(self, session_id: str, turn: dict, *, audience: str = "clinician") -> bool:
        """Append a follow-up turn to a conversation thread (in place). Returns True if it matched.

        AUDIENCE-GUARDED: only appends when the session's audience matches `audience`. A mismatch
        (e.g. the asker toggled clinician→patient mid-thread) returns False, so the caller saves a
        FRESH session instead of corrupting a thread that mixes audiences."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_research_session SET thread = thread || $3::jsonb "
                "WHERE id=$1 AND vertical=$2 AND NOT deleted AND audience=$4",
                session_id, self._vertical, json.dumps([turn]), (audience or "clinician"))
        return res.endswith("1")

    async def save_layman(self, session_id: str, text: str) -> bool:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_research_session SET layman_answer=$3 WHERE id=$1 AND vertical=$2",
                session_id, self._vertical, text)
        return res.endswith("1")

    async def save_terms(self, session_id: str, terms: list[dict]) -> bool:
        """Persist the key-term explanations generated for this session (re-rendered on reopen)."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_research_session SET terms=$3::jsonb WHERE id=$1 AND vertical=$2",
                session_id, self._vertical, json.dumps(terms))
        return res.endswith("1")

    async def save_turn_artifact(self, session_id: str, turn_index: int, key: str,
                                 value: list[dict]) -> bool:
        """Persist a per-answer artifact (visuals / terms) onto a SPECIFIC thread turn, so a reopened
        multi-turn conversation re-renders it on the right answer. No-op if the turn index is out of
        range. `key` is a fixed identifier ('visuals'|'terms'), never user input."""
        if key not in ("visuals", "terms"):
            return False
        await self._ensure()
        pool = await self._get_pool()
        idx = max(0, int(turn_index or 0))
        async with pool.acquire() as conn:
            res = await conn.execute(
                f"UPDATE roster_research_session "
                f"SET thread = jsonb_set(thread, ARRAY[$3::text, '{key}'], $4::jsonb, true) "
                "WHERE id=$1 AND vertical=$2 AND NOT deleted "
                "AND jsonb_array_length(thread) > $5",
                session_id, self._vertical, str(idx), json.dumps(value), idx)
        return res.endswith("1")

    async def save_turn_visuals(self, session_id: str, turn_index: int, visuals: list[dict]) -> bool:
        return await self.save_turn_artifact(session_id, turn_index, "visuals", visuals)

    async def save_turn_terms(self, session_id: str, turn_index: int, terms: list[dict]) -> bool:
        return await self.save_turn_artifact(session_id, turn_index, "terms", terms)

    async def soft_delete(self, session_id: str) -> bool:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_research_session SET deleted=TRUE WHERE id=$1 AND vertical=$2",
                session_id, self._vertical)
        return res.endswith("1")

    async def attach_video(self, session_id: str, *, filename: str, title: str,
                           duration: float) -> bool:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """UPDATE roster_research_session
                   SET video_filename=$3, video_title=$4, video_duration=$5
                   WHERE id=$1 AND vertical=$2""",
                session_id, self._vertical, filename, title, duration)
        return res.endswith("1")   # "UPDATE 1" when a row matched

    async def list(self, *, tenant_id: str, limit: int = 50,
                   q: str | None = None, audience: str | None = None,
                   kind: str | None = None, user_email: str | None = None) -> list[dict[str, Any]]:
        await self._ensure()
        pool = await self._get_pool()
        # optional full-text-ish search over the question + asker (name/email)
        where = "vertical=$1 AND tenant_id=$2 AND NOT deleted"
        params: list[Any] = [self._vertical, tenant_id]
        if user_email and user_email.strip():          # PRIVATE per-account scoping
            params.append(user_email.strip().lower())
            where += f" AND lower(user_email)=${len(params)}"
        if q and q.strip():
            params.append(f"%{q.strip()}%")
            where += (f" AND (question ILIKE ${len(params)} OR user_name ILIKE ${len(params)}"
                      f" OR user_email ILIKE ${len(params)})")
        if audience in ("clinician", "patient"):
            params.append(audience)
            where += f" AND audience=${len(params)}"
        # optional session-kind filter (Past-Sessions tabs): "panel" isolates the specialist-panel
        # discussions; "research" isolates plain Q&A (kind is NULL in the DB for those).
        if kind == "panel":
            where += " AND thread->0->>'kind' = 'panel'"
        elif kind == "crossview":
            where += " AND thread->0->>'kind' = 'crossview'"
        elif kind == "jobs":
            where += " AND thread->0->>'kind' = 'jobs'"
        elif kind == "qa":               # Q&A mode (Eigen-powered research answers)
            where += " AND thread->0->>'kind' = 'qa'"
        elif kind == "people":           # everything that ISN'T a job/qa/panel/crossview search (people_population, research, …)
            where += " AND (thread->0->>'kind' IS NULL OR thread->0->>'kind' NOT IN ('jobs','qa','panel','crossview'))"
        elif kind == "research":
            where += " AND (thread->0->>'kind' IS NULL OR thread->0->>'kind' = 'research')"
        params.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, question, grounded, video_filename, user_name, user_email,
                           jsonb_array_length(attachments) AS n_attach, audience, created_at,
                           real_patient, thread->0->>'kind' AS kind
                    FROM roster_research_session
                    WHERE {where} ORDER BY created_at DESC LIMIT ${len(params)}""",
                *params)
        return [{
            "id": r["id"], "question": r["question"], "grounded": r["grounded"],
            "has_video": bool(r["video_filename"]), "n_attach": r["n_attach"] or 0,
            "user_name": r["user_name"], "user_email": r["user_email"],
            "audience": r["audience"] or "clinician",
            "kind": r["kind"] or "research",
            "real_patient": bool(r["real_patient"]),
            "created_at": r["created_at"].isoformat(),
        } for r in rows]

    async def list_videos(self, *, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """All sessions that have a briefing video (for the video catalogue)."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, question, video_filename, video_title, video_duration,
                          user_name, user_email, created_at
                   FROM roster_research_session
                   WHERE vertical=$1 AND tenant_id=$2 AND NOT deleted AND video_filename IS NOT NULL
                   ORDER BY created_at DESC LIMIT $3""",
                self._vertical, tenant_id, limit)
        return [{
            "id": r["id"], "question": r["question"],
            "video_filename": r["video_filename"], "video_title": r["video_title"],
            "video_duration": r["video_duration"],
            "user_name": r["user_name"], "user_email": r["user_email"],
            "created_at": r["created_at"].isoformat(),
        } for r in rows]

    async def set_real_patient(self, session_id: str, value: bool) -> bool:
        """Mark/unmark a session as a REAL-WORLD PATIENT case (the orange ◉ in the list)."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_research_session SET real_patient=$3 "
                "WHERE id=$1 AND vertical=$2 AND NOT deleted",
                session_id, self._vertical, bool(value))
        return res.endswith("1")

    async def graph_impact(self, *, days: int = 30, limit: int = 1000) -> dict[str, Any]:
        """The always-on graph-quality watch: compare answers WITH graph legs merged vs without,
        from the diagnostics already persisted on each session turn (no extra instrumentation).
        Structural aggregate only — grounded rate, claim counts, merged-atom volume."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT grounded, jsonb_array_length(claims) AS n_claims,
                          thread->0->'diagnostics'->'graph_legs' AS gl
                   FROM roster_research_session
                   WHERE vertical=$1 AND NOT deleted
                     AND created_at >= now() - ($2 || ' days')::interval
                   ORDER BY created_at DESC LIMIT $3""",
                self._vertical, str(int(days)), limit)
        import json as _json
        legs = {"answers": 0, "grounded": 0, "claims": 0, "atoms_merged": 0, "shadow_runs": 0}
        base = {"answers": 0, "grounded": 0, "claims": 0}
        top_queries: dict[str, int] = {}
        for r in rows:
            gl = r["gl"]
            if isinstance(gl, str):
                try:
                    gl = _json.loads(gl)
                except Exception:   # noqa: BLE001
                    gl = None
            merged = 0
            if isinstance(gl, dict):
                for leg in gl.get("legs") or []:
                    merged += int(leg.get("merged") or 0)
                    q = (leg.get("query") or "")[:80]
                    if q:
                        top_queries[q] = top_queries.get(q, 0) + 1
                if gl.get("shadow"):
                    legs["shadow_runs"] += 1
            if merged > 0:
                legs["answers"] += 1
                legs["grounded"] += 1 if r["grounded"] else 0
                legs["claims"] += r["n_claims"] or 0
            else:
                base["answers"] += 1
                base["grounded"] += 1 if r["grounded"] else 0
                base["claims"] += r["n_claims"] or 0
            legs["atoms_merged"] += merged
        def _rate(d):
            return round(d["grounded"] / d["answers"], 3) if d["answers"] else None
        def _avg(d):
            return round(d["claims"] / d["answers"], 1) if d["answers"] else None
        return {"days": days, "sampled": len(rows),
                "with_legs": {"answers": legs["answers"], "grounded_rate": _rate(legs),
                              "avg_claims": _avg(legs), "atoms_merged": legs["atoms_merged"],
                              "shadow_runs": legs["shadow_runs"]},
                "without_legs": {"answers": base["answers"], "grounded_rate": _rate(base),
                                 "avg_claims": _avg(base)},
                "top_leg_queries": sorted(top_queries.items(), key=lambda x: -x[1])[:10]}

    async def get(self, session_id: str) -> dict[str, Any] | None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM roster_research_session WHERE id=$1 AND vertical=$2 AND NOT deleted",
                session_id, self._vertical)
        if r is None:
            return None
        # asyncpg returns jsonb columns as text (no codec set) — decode them here.
        def _j(v, default):
            if v is None:
                return default
            return json.loads(v) if isinstance(v, str) else v
        return {
            "id": r["id"], "tenant_id": r["tenant_id"], "workspace_id": r["workspace_id"],
            "question": r["question"], "answer": r["answer"], "grounded": r["grounded"],
            "claims": _j(r["claims"], []), "source_stats": _j(r["source_stats"], {}),
            "coverage_gaps": _j(r["coverage_gaps"], []), "rejected": r["rejected"],
            "sources": _j(r["sources"], []),
            "video_filename": r["video_filename"], "video_title": r["video_title"],
            "video_duration": r["video_duration"],
            "user_name": r["user_name"], "user_email": r["user_email"],
            "visual_observation": r["visual_observation"],
            "attachments": _j(r["attachments"], []),
            "layman_answer": r["layman_answer"],
            "terms": _j(r["terms"], []),
            "thread": _j(r["thread"], []),
            "audience": r["audience"] or "clinician",
            "real_patient": bool(r["real_patient"]),
            "created_at": r["created_at"].isoformat(),
        }
