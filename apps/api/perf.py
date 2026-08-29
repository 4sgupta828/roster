"""Performance-metrics store — one compact row per answered Q&A/Panel run, distilled from the
diagnostics trace at save time, so the admin perf dashboard aggregates with fast SQL (percentiles,
failure counts) instead of scanning big JSONB blobs. Best-effort: a perf write never breaks /research.
Vertical-isolated like the other app stores."""
from __future__ import annotations

import json
import uuid
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_perf_event (
    id            TEXT PRIMARY KEY,
    vertical      TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'qa',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_ms      INTEGER,
    anthropic_ms  INTEGER,
    judge_ms      INTEGER,
    retrieval_ms  INTEGER,
    embed_ms      INTEGER,
    compose_ms    INTEGER,      -- slowest LLM call (usually compose)
    extract_ms    INTEGER,      -- 2nd slowest (usually the answer/extraction step)
    anthropic_calls INTEGER,
    tokens        INTEGER,
    claims        INTEGER,
    rejected      INTEGER,
    atoms         INTEGER,
    grounded      BOOLEAN,
    compose_failed BOOLEAN,
    stopped_reason TEXT,
    congruence_drops INTEGER,
    failures      JSONB NOT NULL DEFAULT '[]'::jsonb,
    effort        DOUBLE PRECISION,
    audience      TEXT,
    modality      TEXT
);
CREATE INDEX IF NOT EXISTS idx_perf_vertical_created ON roster_perf_event (vertical, created_at DESC);
"""


def event_from_diagnostics(diag: dict, *, kind: str = "qa", grounded: bool | None = None,
                           claims: int = 0, rejected: int = 0, stopped_reason: str = "",
                           effort: float | None = None, audience: str = "",
                           modality: str = "") -> dict:
    """Distill a compact perf row from the research diagnostics dict (+ a few top-level result fields).
    Structural extraction only — no interpretation."""
    t = (diag or {}).get("timing", {}) or {}
    calls = sorted(((diag or {}).get("llm_calls_detail", []) or []), key=lambda c: c.get("ms", 0), reverse=True)
    funnel = (diag or {}).get("funnel", {}) or {}
    cong = (diag or {}).get("congruence", {}) or {}
    fails = [f.get("stage", "?") for f in ((diag or {}).get("failures", []) or [])]
    return {
        "kind": kind,
        "total_ms": t.get("total_ms") or (diag or {}).get("duration_ms"),
        "anthropic_ms": t.get("anthropic_ms"),
        "judge_ms": t.get("judge_ms"),
        "retrieval_ms": t.get("retrieval_ms"),
        "embed_ms": t.get("embed_ms"),
        "compose_ms": calls[0].get("ms") if calls else None,
        "extract_ms": calls[1].get("ms") if len(calls) > 1 else None,
        "anthropic_calls": t.get("anthropic_calls") or len(calls),
        "tokens": sum(c.get("out", 0) + c.get("in", 0) for c in calls) or (diag or {}).get("budget", {}).get("tokens"),
        "claims": claims or funnel.get("verified", 0),
        "rejected": rejected or funnel.get("rejected", 0),
        "atoms": funnel.get("atoms_gathered", 0),
        "grounded": grounded,
        "compose_failed": bool((diag or {}).get("compose_failed")),
        "stopped_reason": (stopped_reason or (diag or {}).get("stopped_reason") or "")[:80],
        "congruence_drops": cong.get("off_subject", 0) + cong.get("not_entailed", 0),
        "failures": fails,
        "effort": effort,
        "audience": audience[:20],
        "modality": modality[:20],
    }


class PerfStore:
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

    async def record(self, ev: dict) -> None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_perf_event
                   (id, vertical, kind, total_ms, anthropic_ms, judge_ms, retrieval_ms, embed_ms,
                    compose_ms, extract_ms, anthropic_calls, tokens, claims, rejected, atoms,
                    grounded, compose_failed, stopped_reason, congruence_drops, failures,
                    effort, audience, modality)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20::jsonb,$21,$22,$23)""",
                uuid.uuid4().hex[:16], self._vertical, ev.get("kind", "qa"),
                ev.get("total_ms"), ev.get("anthropic_ms"), ev.get("judge_ms"), ev.get("retrieval_ms"),
                ev.get("embed_ms"), ev.get("compose_ms"), ev.get("extract_ms"), ev.get("anthropic_calls"),
                ev.get("tokens"), ev.get("claims"), ev.get("rejected"), ev.get("atoms"),
                ev.get("grounded"), ev.get("compose_failed"), ev.get("stopped_reason") or "",
                ev.get("congruence_drops"), json.dumps(ev.get("failures", [])),
                ev.get("effort"), ev.get("audience") or "", ev.get("modality") or "")

    async def stats(self, *, hours: int = 168, kind: str | None = None) -> dict[str, Any]:
        """Aggregate metrics over a window. Percentiles via percentile_cont; failure + stopped-reason
        distributions via separate rollups. Returns a dashboard-ready dict."""
        await self._ensure()
        pool = await self._get_pool()
        args = [self._vertical, str(int(hours))]
        w = "vertical=$1 AND created_at > now() - ($2||' hours')::interval"
        if kind:
            args.append(kind)
            w += f" AND kind=${len(args)}"

        def _pctl(col):
            return (f"avg({col})::int AS {col}_avg, "
                    f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {col})::int AS {col}_p50, "
                    f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {col})::int AS {col}_p95, "
                    f"percentile_cont(0.99) WITHIN GROUP (ORDER BY {col})::int AS {col}_p99, "
                    f"max({col}) AS {col}_max")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT count(*) AS n,
                        {_pctl('total_ms')},
                        avg(anthropic_ms)::int AS anthropic_ms_avg,
                        avg(judge_ms)::int AS judge_ms_avg,
                        avg(retrieval_ms)::int AS retrieval_ms_avg,
                        avg(embed_ms)::int AS embed_ms_avg,
                        avg(compose_ms)::int AS compose_ms_avg,
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY compose_ms)::int AS compose_ms_p95,
                        avg(extract_ms)::int AS extract_ms_avg,
                        avg(anthropic_calls)::numeric(10,1) AS calls_avg,
                        avg(tokens)::int AS tokens_avg,
                        avg(claims)::numeric(10,1) AS claims_avg,
                        avg(rejected)::numeric(10,2) AS rejected_avg,
                        avg(atoms)::numeric(10,1) AS atoms_avg,
                        sum(CASE WHEN grounded IS FALSE THEN 1 ELSE 0 END) AS ungrounded,
                        sum(CASE WHEN compose_failed THEN 1 ELSE 0 END) AS compose_fails,
                        sum(congruence_drops) AS congruence_drops
                    FROM roster_perf_event WHERE {w}""", *args)
            failure_rows = await conn.fetch(
                f"SELECT f AS stage, count(*) AS n FROM roster_perf_event, "
                f"jsonb_array_elements_text(failures) AS f WHERE {w} GROUP BY 1 ORDER BY 2 DESC", *args)
            stop_rows = await conn.fetch(
                f"SELECT COALESCE(NULLIF(stopped_reason,''),'(none)') AS reason, count(*) AS n "
                f"FROM roster_perf_event WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 12", *args)
            recent = await conn.fetch(
                f"SELECT created_at, kind, total_ms, compose_ms, judge_ms, retrieval_ms, anthropic_calls, "
                f"claims, grounded, stopped_reason FROM roster_perf_event WHERE {w} "
                f"ORDER BY created_at DESC LIMIT 20", *args)
        d = dict(row) if row else {}
        d["window_hours"] = hours
        d["failures"] = [{"stage": r["stage"], "n": r["n"]} for r in failure_rows]
        d["stopped_reasons"] = [{"reason": r["reason"], "n": r["n"]} for r in stop_rows]
        d["recent"] = [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}}
                       for r in recent]
        # jsonify numerics
        return json.loads(json.dumps(d, default=str))
