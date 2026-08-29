"""CurrencyStore — the auditable change-event ledger + derived block stamps.

Design (spec v2, amendments A2/A4):
- `roster_change_event` is the SOURCE OF TRUTH. Block facet stamps (`superseded_by`,
  `retracted`) are a derived cache: re-ingest overwrites facets, so stamps are re-derivable
  at any time from the approved events (`apply_stamps` is idempotent — run it after ingests).
- Events are idempotent on (relation, old, new) and carry a status:
    shadow   — recorded, no effect (future LLM-detected events start here);
    approved — stamps applied; declared (curator) lineage starts here (A4: high-confidence only);
    retracted_event — a mistake, undone (stamps removed, event kept for audit).
- Stamps DEMOTE (or, for retracted sources, exclude) — they never delete: the old edition is the
  evidence a change brief cites, and may be the only source for an unrevised topic.
"""
from __future__ import annotations

import hashlib
import json

RELATIONS = ("superseded_by", "retracted", "amended_by", "clarified_by")

_DDL = """
CREATE TABLE IF NOT EXISTS roster_change_event (
    id               text PRIMARY KEY,
    relation         text NOT NULL,
    old_document_id  text NOT NULL DEFAULT '',
    new_document_id  text NOT NULL DEFAULT '',
    subjects         jsonb NOT NULL DEFAULT '[]'::jsonb,
    materiality      text NOT NULL DEFAULT 'major',
    confidence       text NOT NULL DEFAULT 'declared',
    brief_md         text NOT NULL DEFAULT '',
    brief_claims     jsonb NOT NULL DEFAULT '[]'::jsonb,
    status           text NOT NULL DEFAULT 'shadow',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS roster_change_event_status ON roster_change_event (status, created_at DESC);
CREATE TABLE IF NOT EXISTS roster_watch (
    user_id     text NOT NULL,
    topic       text NOT NULL,
    source      text NOT NULL DEFAULT 'manual',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, topic)
);
CREATE TABLE IF NOT EXISTS roster_watch_seen (
    user_id  text NOT NULL,
    event_id text NOT NULL,
    seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, event_id)
);
CREATE TABLE IF NOT EXISTS roster_pulse_state (
    key        text PRIMARY KEY,          -- e.g. retraction_scan | detect_scan | last_retraction_sweep
    value      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS roster_topic (
    label      text PRIMARY KEY,          -- canonical form; lowercase-unique via norm below
    norm       text NOT NULL UNIQUE,
    source     text NOT NULL DEFAULT 'seed',   -- seed | llm
    created_at timestamptz NOT NULL DEFAULT now()
);
-- v3 kind-as-ROLE (KG spec C-5): condition | drug | finding | exposure | population.
-- norm UNIQUE keeps one row per label; overlap labels (anemia = condition AND finding)
-- keep kind='condition' — condition WINS the row; manifests_as may target condition rows.
-- ALTER-ensure because CREATE TABLE IF NOT EXISTS never migrates the existing prod table.
ALTER TABLE roster_topic ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'condition';
"""


def _topic_norm(label: str) -> str:
    return " ".join((label or "").lower().split())


def _event_id(relation: str, old: str, new: str) -> str:
    return hashlib.sha256(f"{relation}|{old}|{new}".encode()).hexdigest()[:32]


class CurrencyStore:
    def __init__(self, dsn: str, *, block_table: str = "rs_block"):
        self._dsn = dsn
        self._block_table = block_table
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            async with self._pool.acquire() as conn:
                await conn.execute(_DDL)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---- events (source of truth) -------------------------------------------------------------

    async def record(self, *, relation: str, old_document_id: str = "", new_document_id: str = "",
                     subjects: list[str] | None = None, materiality: str = "major",
                     confidence: str = "declared", status: str = "shadow") -> str:
        """Idempotent on (relation, old, new): re-recording NEVER downgrades an existing event's
        status or overwrites its audit trail — a swept declared relation that was manually
        retracted stays retracted. Returns the event id."""
        if relation not in RELATIONS:
            raise ValueError(f"unknown relation {relation!r}")
        eid = _event_id(relation, old_document_id, new_document_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_change_event
                     (id, relation, old_document_id, new_document_id, subjects,
                      materiality, confidence, status)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8)
                   ON CONFLICT (id) DO UPDATE SET
                     -- enrich empty subjects on re-record (e.g. a later sweep learned the title);
                     -- NEVER touch status/audit fields
                     subjects = CASE WHEN roster_change_event.subjects = '[]'::jsonb
                                     THEN EXCLUDED.subjects ELSE roster_change_event.subjects END,
                     updated_at = now()""",
                eid, relation, old_document_id, new_document_id,
                json.dumps(list(subjects or [])), materiality, confidence, status)
        return eid

    async def list_events(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        pool = await self._get_pool()
        where = "WHERE status=$2" if status else ""
        args = [min(limit, 500)] + ([status] if status else [])
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, relation, old_document_id, new_document_id, subjects, materiality,
                           confidence, brief_md, status, created_at, updated_at
                    FROM roster_change_event {where}
                    ORDER BY created_at DESC LIMIT $1""", *args)
        out = []
        for r in rows:
            d = dict(r)
            d["subjects"] = json.loads(d["subjects"]) if isinstance(d["subjects"], str) else d["subjects"]
            d["created_at"] = d["created_at"].isoformat()
            d["updated_at"] = d["updated_at"].isoformat()
            out.append(d)
        return out

    async def set_status(self, event_id: str, status: str) -> bool:
        """approve | retract an event. Retraction un-stamps its blocks (kept for audit)."""
        if status not in ("shadow", "approved", "retracted_event"):
            raise ValueError(f"bad status {status!r}")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE roster_change_event SET status=$2, updated_at=now() WHERE id=$1 "
                "RETURNING relation, old_document_id, new_document_id", event_id, status)
        if row is None:
            return False
        if status == "approved":
            await self._stamp(row["relation"], row["old_document_id"], row["new_document_id"])
        elif status == "retracted_event":
            await self._unstamp(row["relation"], row["old_document_id"])
        return True

    # ---- derived stamps (re-derivable cache on the block table) --------------------------------

    async def apply_stamps(self) -> int:
        """Idempotently (re-)derive block stamps from ALL approved events — run after any ingest
        (re-ingest overwrites facets, erasing stamps; the ledger restores them). Returns events
        applied."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT relation, old_document_id, new_document_id FROM roster_change_event "
                "WHERE status='approved'")
        for r in rows:
            await self._stamp(r["relation"], r["old_document_id"], r["new_document_id"])
        return len(rows)

    async def _stamp(self, relation: str, old_doc: str, new_doc: str) -> None:
        if not old_doc:
            return
        pool = await self._get_pool()
        patch = json.dumps({"retracted": "true"} if relation == "retracted"
                           else {relation: new_doc})
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._block_table} SET facets = facets || $2::jsonb WHERE document_id=$1",
                old_doc, patch)

    async def _unstamp(self, relation: str, old_doc: str) -> None:
        if not old_doc:
            return
        key = "retracted" if relation == "retracted" else relation
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._block_table} SET facets = facets - $2 WHERE document_id=$1",
                old_doc, key)

    async def list_document_ids(self, *, prefix: str, limit: int = 50000) -> list[str]:
        """Distinct corpus document ids under a source prefix (e.g. 'europepmc:') — the input a
        detector sweeps. Structural read on the block table."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT DISTINCT document_id FROM {self._block_table}
                    WHERE document_id LIKE $1 LIMIT $2""", prefix + "%", limit)
        return [r["document_id"] for r in rows]

    # ---- canonical topic registry (P1) ---------------------------------------------------------
    # STABILITY CONTRACT: LLM runs never mint variants of an existing topic — suggesters/
    # canonicalizers are shown this registry and must prefer exact reuse; a genuinely novel topic
    # is inserted ONCE and becomes the stable form for every later run and user.

    async def list_topics(self, *, limit: int = 500, kind: str | None = None) -> list[str]:
        """kind filter (v3 C-5): Pulse watch/canonize/suggest prompts pass kind='condition' so
        drug/finding rows minted for the graph never contaminate the watchable-topic prompts."""
        pool = await self._get_pool()
        where, args = "", [limit]
        if kind:
            args.append(kind)
            where = "WHERE kind=$2"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT label FROM roster_topic {where} ORDER BY created_at LIMIT $1", *args)
        return [r["label"] for r in rows]

    async def ensure_topics(self, labels: list[str], *, source: str = "llm",
                            kind: str = "condition") -> list[str]:
        """Insert missing topics (case/whitespace-insensitive) and return the CANONICAL label for
        each input — an existing registry entry wins over the incoming variant, INCLUDING its
        kind (condition wins the row per C-5: minting 'anemia' as a finding never downgrades
        the existing condition row)."""
        pool = await self._get_pool()
        out: list[str] = []
        async with pool.acquire() as conn:
            for lb in labels or []:
                lb = (lb or "").strip()[:120]
                if not lb:
                    continue
                row = await conn.fetchrow(
                    """INSERT INTO roster_topic (label, norm, source, kind) VALUES ($1,$2,$3,$4)
                       ON CONFLICT (norm) DO UPDATE SET norm=EXCLUDED.norm
                       RETURNING label""", lb, _topic_norm(lb), source, kind)
                out.append(row["label"] if row else lb)
        return out

    # ---- watches + inbox (P1) ------------------------------------------------------------------

    async def add_watch(self, *, user_id: str, topic: str, source: str = "manual") -> None:
        t = (topic or "").strip()[:200]
        if not t:
            raise ValueError("empty topic")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO roster_watch (user_id, topic, source) VALUES ($1,$2,$3) "
                "ON CONFLICT DO NOTHING", user_id, t, source)

    async def remove_watch(self, *, user_id: str, topic: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM roster_watch WHERE user_id=$1 AND topic=$2",
                               user_id, (topic or "").strip()[:200])

    async def list_watches(self, *, user_id: str) -> list[dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT topic, source, created_at FROM roster_watch WHERE user_id=$1 "
                "ORDER BY created_at DESC", user_id)
        return [{"topic": r["topic"], "source": r["source"],
                 "created_at": r["created_at"].isoformat()} for r in rows]

    async def inbox(self, *, user_id: str, limit: int = 50) -> list[dict]:
        """Approved events matching the user's watches — P1 matching is STRUCTURAL and
        precision-biased: case-insensitive containment between watch topic and an event's subject
        strings (LLM matching is a later layer). Each item carries `seen`."""
        watches = [w["topic"].lower() for w in await self.list_watches(user_id=user_id)]
        if not watches:
            return []
        events = await self.list_events(status="approved", limit=300)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            seen_rows = await conn.fetch(
                "SELECT event_id FROM roster_watch_seen WHERE user_id=$1", user_id)
        seen = {r["event_id"] for r in seen_rows}
        out = []
        for e in events:
            subj = " ".join(str(s) for s in (e.get("subjects") or [])).lower()
            if any((t in subj or (subj and subj in t)) for t in watches if t):
                out.append({**e, "seen": e["id"] in seen})
            if len(out) >= limit:
                break
        return out

    # ---- shared state (replica-safe: scan statuses, sweep clocks) ------------------------------

    async def set_state(self, key: str, value: dict) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_pulse_state (key, value) VALUES ($1, $2::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                key, json.dumps(value or {}))

    async def get_state(self, key: str) -> dict | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM roster_pulse_state WHERE key=$1", key)
        if row is None:
            return None
        v = row["value"]
        return v if isinstance(v, dict) else json.loads(v)

    async def list_documents_meta(self, *, facet_key: str, facet_values: tuple[str, ...],
                                  limit: int = 2000) -> list[dict]:
        """Per-document metadata for detector candidate generation (structural): distinct documents
        whose facets[facet_key] is one of facet_values, with issuer/year/conditions/title. Generic —
        the caller (vertical/app) decides which facet marks its 'versioned-document' tier."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT document_id, MAX(document_title) AS title,
                           MAX(facets->>'issuer') AS issuer, MAX(facets->>'year') AS year,
                           MAX(facets->>'conditions') AS conditions
                    FROM {self._block_table}
                    WHERE facets->>$1 = ANY($2::text[])
                    GROUP BY document_id LIMIT $3""",
                facet_key, list(facet_values), limit)
        out = []
        for r in rows:
            try:
                conds = json.loads(r["conditions"]) if r["conditions"] else []
            except Exception:   # noqa: BLE001
                conds = []
            out.append({"document_id": r["document_id"], "title": r["title"] or "",
                        "issuer": r["issuer"] or "", "year": r["year"] or "",
                        "conditions": conds if isinstance(conds, list) else []})
        return out

    def _topic_matches(self, topic: str, subjects: list) -> bool:
        t = (topic or "").lower().strip()
        subj = " ".join(str(s) for s in (subjects or [])).lower()
        return bool(t and (t in subj or (subj and subj in t)))

    async def events_for_topic(self, topic: str, *, days: int | None = None,
                              limit: int = 50) -> list[dict]:
        """Approved relational events matching a topic (containment), optionally windowed."""
        events = await self.list_events(status="approved", limit=300)
        out = []
        for e in events:
            if not self._topic_matches(topic, e.get("subjects")):
                continue
            if days is not None:
                import datetime as _dt
                cut = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
                if _dt.datetime.fromisoformat(e["created_at"]) < cut:
                    continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    async def inbox_summary(self, *, user_id: str, days: int = 30) -> list[dict]:
        """Per-watched-topic rollup — question (1) 'anything I haven't seen?' and the headline of
        question (2) 'movement in the window'. Structural only: relational events here; new-source
        activity is composed by the caller (topic-as-query needs the retrieval engine)."""
        watches = await self.list_watches(user_id=user_id)
        if not watches:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            seen_rows = await conn.fetch(
                "SELECT event_id FROM roster_watch_seen WHERE user_id=$1", user_id)
        seen = {r["event_id"] for r in seen_rows}
        out = []
        for w in watches:
            evs = await self.events_for_topic(w["topic"], days=days)
            all_evs = await self.events_for_topic(w["topic"])
            out.append({"topic": w["topic"], "source": w["source"],
                        "unseen": sum(1 for e in all_evs if e["id"] not in seen),
                        "events_window": len(evs),
                        "events": [{**e, "seen": e["id"] in seen} for e in evs[:10]]})
        return out

    async def docs_first_seen(self, document_ids: list[str], *, days: int = 30) -> list[dict]:
        """Which of these documents FIRST landed in the corpus within the window (structural: min
        block created_at; pre-time-axis rows are NULL = unknown and honestly excluded). The caller
        supplies candidate ids from a topic retrieval — topic-as-query composition."""
        if not document_ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT document_id, MIN(created_at) AS first_seen,
                           MAX(document_title) AS title, MAX(source_key) AS source_key
                    FROM {self._block_table}
                    WHERE document_id = ANY($1::text[])
                    GROUP BY document_id
                    HAVING MIN(created_at) IS NOT NULL
                       AND MIN(created_at) >= now() - ($2 || ' days')::interval
                    ORDER BY MIN(created_at) DESC""",
                list(document_ids)[:200], str(int(days)))
        return [{"document_id": r["document_id"], "first_seen": r["first_seen"].isoformat(),
                 "title": r["title"] or r["document_id"], "source_key": r["source_key"] or ""}
                for r in rows]

    async def mark_seen(self, *, user_id: str, event_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO roster_watch_seen (user_id, event_id) VALUES ($1,$2) "
                "ON CONFLICT DO NOTHING", user_id, event_id)

    # ---- P0 sweep: curator-declared lineage → approved events + stamps -------------------------

    async def sweep_declared(self, lineage: list[dict]) -> dict:
        """Record vertical-declared relations (highest confidence → status approved, A4) and apply
        stamps. Idempotent; safe to run on every ingest completion or admin trigger."""
        recorded = 0
        for rel in lineage or []:
            relation = rel.get("relation", "")
            if relation not in RELATIONS:
                continue
            await self.record(
                relation=relation,
                old_document_id=rel.get("old_document_id", ""),
                new_document_id=rel.get("new_document_id", ""),
                subjects=rel.get("subjects") or [],
                confidence="declared", status="shadow")   # record first; approve applies stamps
            eid = _event_id(relation, rel.get("old_document_id", ""), rel.get("new_document_id", ""))
            # approve ONLY if still shadow (a manually-retracted event stays retracted)
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM roster_change_event WHERE id=$1", eid)
            if row and row["status"] == "shadow":
                await self.set_status(eid, "approved")
            recorded += 1
        stamped = await self.apply_stamps()
        return {"declared": recorded, "stamps_applied": stamped}
