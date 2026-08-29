"""GraphStore — the Grounded Relationship Graph over the canonical topic registry.

Spec: learnings/knowledgegraph.md (v2 + A9). Typed edges between `roster_topic` labels,
every edge carrying provenance (curated | harvested | seed), an honesty label
(established | supported | hypothesized), a lifecycle status (shadow | active | demoted),
and — for harvested edges — span-verified evidence rows in a separate, indexed table
(A2: invalidation must be an index lookup, never a jsonb scan).

SPEED DESIGN (the graph sits on the answer path via A9's evidence legs, so lookups must be
fast): the ACTIVE edge set is tiny — hundreds to low thousands of rows — so the hot-path
query (`neighbors of these topics`) is served from an IN-PROCESS ADJACENCY SNAPSHOT built
from one indexed SELECT, refreshed on a TTL (default 60s) and invalidated on every write.
Steady state: zero DB round trips per question, O(1) dict lookup per topic. `narrower_than`
up-traversal is ONE level, resolved in memory — no recursive SQL anywhere. Cold paths
(admin lists, invalidation) are index-backed (subject_norm/object_norm btrees; evidence
document_id btree).

Ledger ethos (matches CurrencyStore): writes never destroy audit state — `sync_curated`
never resurrects a manually demoted edge; demotion flips status, never deletes; evidence
rows are append-only.
"""
from __future__ import annotations

import hashlib
import time

LABELS = ("established", "supported", "hypothesized")
STATUSES = ("shadow", "active", "demoted")
PROVENANCES = ("curated", "harvested", "seed")

_DDL = """
CREATE TABLE IF NOT EXISTS roster_topic_edge (
    id            text PRIMARY KEY,           -- sha(subject_norm|relation|object_norm|context_norm)
    subject       text NOT NULL,              -- canonical topic label (registry form)
    subject_norm  text NOT NULL,
    relation      text NOT NULL,              -- vertical vocabulary (validated on write)
    object        text NOT NULL,
    object_norm   text NOT NULL,
    context_topic text NOT NULL DEFAULT '',   -- A6 qualifier (setting/population), not a new node
    distinguished_by text NOT NULL DEFAULT '',-- v3/C-2: the discriminating finding on a mimic/
    --                                           underlies edge (identity-EXCLUDED — two drafts of
    --                                           the same pair with different discriminators must
    --                                           dedup onto one edge)
    label         text NOT NULL DEFAULT 'hypothesized',
    provenance    text NOT NULL,              -- curated | harvested | seed
    status        text NOT NULL DEFAULT 'shadow',   -- shadow | active | demoted
    needs_review  boolean NOT NULL DEFAULT false,   -- A5: evidence superseded → re-harvest flag
    confidence    real NOT NULL DEFAULT 0,
    seen_count    integer NOT NULL DEFAULT 0,       -- DISTINCT evidence document_ids (A2.4)
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- v3 ALTER-ensure: CREATE TABLE IF NOT EXISTS never migrates an existing prod table (C-2)
ALTER TABLE roster_topic_edge ADD COLUMN IF NOT EXISTS distinguished_by text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS roster_topic_edge_subj ON roster_topic_edge (subject_norm, status);
CREATE INDEX IF NOT EXISTS roster_topic_edge_obj  ON roster_topic_edge (object_norm, status);
CREATE TABLE IF NOT EXISTS roster_edge_evidence (
    edge_id     text NOT NULL,
    document_id text NOT NULL,
    quote       text NOT NULL DEFAULT '',
    session_id  text NOT NULL DEFAULT '',
    label       text NOT NULL DEFAULT 'hypothesized',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, document_id)
);
CREATE INDEX IF NOT EXISTS roster_edge_evidence_doc ON roster_edge_evidence (document_id);
"""


def _norm(label: str) -> str:
    return " ".join((label or "").lower().split())


def edge_identity(subject: str, relation: str, object_: str, context: str = "") -> str:
    """Direction-sensitive identity on NORMALIZED endpoints + context (panel A2.3): `causes`
    A→B and B→A are different edges; the same relation in a different context is too."""
    key = f"{_norm(subject)}|{relation}|{_norm(object_)}|{_norm(context)}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def build_adjacency(edges: list[dict]) -> dict:
    """Pure: active-edge rows → {"by_norm": norm → [(edge, direction)], "up": child_norm →
    parent label} adjacency maps. `narrower_than` edges feed ONLY the `up` map (they are
    topic hierarchy, not clinical relations to surface as neighbors)."""
    by_norm: dict[str, list] = {}
    up: dict[str, str] = {}
    for e in edges:
        if e["relation"] == "narrower_than":
            up[e["subject_norm"]] = e["object"]
            continue
        by_norm.setdefault(e["subject_norm"], []).append((e, "out"))
        by_norm.setdefault(e["object_norm"], []).append((e, "in"))
    return {"by_norm": by_norm, "up": up}


def neighbors_from(adj: dict, topics: list[str], *, limit: int = 12) -> list[dict]:
    """Pure: topic labels → adjacent edges, deduped, confidence-desc. Each input topic is
    also lifted ONE level up the `narrower_than` hierarchy (A6) so "anemia in pregnancy"
    reaches edges written against "anemia"."""
    norms: list[tuple[str, str, int]] = []          # (norm, via-topic, input rank)
    seen_norms: set[str] = set()
    for i, t in enumerate(topics or []):
        n = _norm(t)
        if n and n not in seen_norms:
            seen_norms.add(n)
            norms.append((n, t, i))
        parent = adj["up"].get(n)
        if parent and _norm(parent) not in seen_norms:
            seen_norms.add(_norm(parent))
            norms.append((_norm(parent), t, i))
    out, seen_ids = [], set()
    for n, via, rank in norms:
        for e, direction in adj["by_norm"].get(n, ()):
            if e["id"] in seen_ids:
                continue
            seen_ids.add(e["id"])
            out.append({"subject": e["subject"], "relation": e["relation"],
                        "object": e["object"], "context_topic": e.get("context_topic", ""),
                        "distinguished_by": e.get("distinguished_by", ""),
                        "label": e["label"], "confidence": e.get("confidence", 0),
                        "provenance": e.get("provenance", ""), "direction": direction,
                        "via": via, "id": e["id"], "_rank": rank})
    # Deterministic order that decides which edges win the A9 two-leg cap: the FIRST input
    # topic's edges before later topics' (callers pass the primary/asked subject first),
    # then confidence desc, then CASE-INSENSITIVE lexical (capital-letter subjects must not
    # jump the queue — the Parkinson-before-anemia prod bug).
    out.sort(key=lambda d: (d["_rank"], -float(d.get("confidence") or 0),
                            d["subject"].lower(), d["relation"], d["object"].lower()))
    for d in out:
        d.pop("_rank", None)
    return out[:limit]


def edges_fully_dead(evidence_by_edge: dict[str, list[str]], dead_docs: set[str]) -> list[str]:
    """Pure (A5 demotion decision): edge ids whose EVERY evidence document is dead. An edge
    with any surviving evidence keeps its status; an edge with no evidence rows (curated)
    is never in the input."""
    return [eid for eid, docs in evidence_by_edge.items()
            if docs and all(d in dead_docs for d in docs)]


class GraphStore:
    def __init__(self, dsn: str, *, relations: tuple[str, ...] = (), cache_ttl: float = 60.0):
        self._dsn = dsn
        self._relations = tuple(relations) + (("narrower_than",) if "narrower_than" not in relations else ())
        self._cache_ttl = cache_ttl
        self._pool = None
        self._snap: dict | None = None              # {"at": monotonic, "adj": adjacency}
        self._snap_edge_count = 0

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

    # ---- writes (all invalidate the snapshot) --------------------------------------------------

    async def upsert_edge(self, *, subject: str, relation: str, object_: str,
                          context_topic: str = "", distinguished_by: str = "",
                          label: str = "hypothesized",
                          provenance: str = "harvested", status: str = "shadow",
                          confidence: float = 0.0, note: str = "") -> str:
        """Idempotent on identity. Re-upsert refreshes label/confidence/note but NEVER
        resurrects a demoted edge and never downgrades active→shadow (audit ethos)."""
        if relation not in self._relations:
            raise ValueError(f"relation {relation!r} not in vocabulary {self._relations}")
        if label not in LABELS or status not in STATUSES or provenance not in PROVENANCES:
            raise ValueError(f"bad label/status/provenance {label!r}/{status!r}/{provenance!r}")
        eid = edge_identity(subject, relation, object_, context_topic)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_topic_edge
                     (id, subject, subject_norm, relation, object, object_norm, context_topic,
                      distinguished_by, label, provenance, status, confidence, note)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                   ON CONFLICT (id) DO UPDATE SET
                     -- CURATED rows are immune to non-curated overwrites (a harvested draft
                     -- colliding on identity must never dilute curator label/confidence or
                     -- get the row demoted on ITS weak evidence — the Wilson-edge incident)
                     label=CASE WHEN roster_topic_edge.provenance='curated'
                                     AND EXCLUDED.provenance != 'curated'
                                THEN roster_topic_edge.label ELSE EXCLUDED.label END,
                     confidence=CASE WHEN roster_topic_edge.provenance='curated'
                                          AND EXCLUDED.provenance != 'curated'
                                THEN roster_topic_edge.confidence ELSE EXCLUDED.confidence END,
                     distinguished_by=CASE WHEN EXCLUDED.distinguished_by != ''
                                                AND NOT (roster_topic_edge.provenance='curated'
                                                         AND EXCLUDED.provenance != 'curated')
                                           THEN EXCLUDED.distinguished_by
                                           ELSE roster_topic_edge.distinguished_by END,
                     note=CASE WHEN roster_topic_edge.provenance='curated'
                                    AND EXCLUDED.provenance != 'curated'
                               THEN roster_topic_edge.note ELSE EXCLUDED.note END,
                     updated_at=now(),
                     status=CASE WHEN roster_topic_edge.status='demoted'
                                 THEN roster_topic_edge.status
                                 WHEN roster_topic_edge.status='active' THEN 'active'
                                 ELSE EXCLUDED.status END
                   """,
                eid, subject.strip()[:200], _norm(subject), relation,
                object_.strip()[:200], _norm(object_), context_topic.strip()[:200],
                distinguished_by.strip()[:300], label, provenance, status,
                float(confidence), note[:500])
        self._snap = None
        return eid

    async def add_evidence(self, *, edge_id: str, document_id: str, quote: str = "",
                           session_id: str = "", label: str = "hypothesized") -> int:
        """Append one evidence row (idempotent per document) and refresh `seen_count` to the
        DISTINCT-document count (A2.4 — re-asking the same question over the same document
        never manufactures independent agreement). Returns the new seen_count."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_edge_evidence (edge_id, document_id, quote, session_id, label)
                   VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                edge_id, document_id, quote[:1000], session_id, label)
            row = await conn.fetchrow(
                """UPDATE roster_topic_edge SET seen_count =
                     (SELECT count(DISTINCT document_id) FROM roster_edge_evidence WHERE edge_id=$1),
                     updated_at=now()
                   WHERE id=$1 RETURNING seen_count""", edge_id)
        self._snap = None
        return int(row["seen_count"]) if row else 0

    async def set_status(self, edge_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"bad status {status!r}")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_topic_edge SET status=$2, needs_review=false, updated_at=now() "
                "WHERE id=$1", edge_id, status)
        self._snap = None
        return res == "UPDATE 1"

    async def sync_curated(self, edges: list[dict]) -> dict:
        """Upsert the vertical's curated edges (born ACTIVE — curator trust, A4 analog of
        declared lineage). Idempotent; never resurrects a manually demoted edge."""
        n = 0
        for e in edges or []:
            await self.upsert_edge(
                subject=e["subject"], relation=e["relation"], object_=e["object"],
                context_topic=e.get("context_topic", ""),
                distinguished_by=e.get("distinguished_by", ""),
                label=e.get("label", "established"),
                provenance="curated", status="active",
                confidence=float(e.get("confidence", 1.0)), note=e.get("note", ""))
            n += 1
        return {"curated_synced": n}

    # ---- A5: evidence invalidation --------------------------------------------------------------

    async def invalidate_from_events(self, events: list[dict]) -> dict:
        """Idempotent sweep over approved change events: edges whose evidence is ENTIRELY
        retracted documents → demoted (consumers stop reading them); edges citing a
        superseded document → flagged needs_review (re-harvest against the successor).
        Curated edges are exempt from demotion (curator trust outlives any one document)."""
        dead = list({e["old_document_id"] for e in events or []
                     if e.get("relation") == "retracted" and e.get("old_document_id")})
        stale = list({e["old_document_id"] for e in events or []
                      if e.get("relation") == "superseded_by" and e.get("old_document_id")})
        demoted = flagged = 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if dead:
                res = await conn.execute(
                    """UPDATE roster_topic_edge e SET status='demoted', updated_at=now()
                       WHERE e.status != 'demoted' AND e.provenance != 'curated'
                         AND EXISTS (SELECT 1 FROM roster_edge_evidence v
                                     WHERE v.edge_id=e.id AND v.document_id = ANY($1::text[]))
                         AND NOT EXISTS (SELECT 1 FROM roster_edge_evidence v
                                         WHERE v.edge_id=e.id
                                           AND NOT (v.document_id = ANY($1::text[])))""", dead)
                demoted = int((res or "UPDATE 0").split()[-1])
            if stale:
                res = await conn.execute(
                    """UPDATE roster_topic_edge e SET needs_review=true, updated_at=now()
                       WHERE e.status='active' AND NOT e.needs_review
                         AND EXISTS (SELECT 1 FROM roster_edge_evidence v
                                     WHERE v.edge_id=e.id AND v.document_id = ANY($1::text[]))""",
                    stale)
                flagged = int((res or "UPDATE 0").split()[-1])
        if demoted or flagged:
            self._snap = None
        return {"edges_demoted": demoted, "edges_flagged": flagged,
                "dead_docs": len(dead), "superseded_docs": len(stale)}

    # ---- reads ----------------------------------------------------------------------------------

    async def neighbors(self, topics: list[str], *, limit: int = 12) -> list[dict]:
        """HOT PATH: adjacent active edges for these topics, from the in-process snapshot."""
        adj = await self._adjacency()
        return neighbors_from(adj, topics, limit=limit)

    async def edge_topics(self, *, limit: int = 300) -> list[str]:
        """Distinct LABELS of active-edge endpoints (+ hierarchy children) — the closed
        vocabulary the LLM mapping layer maps INTO (verbatim membership is code-validated,
        so the model can never mint a topic). Snapshot-served."""
        adj = await self._adjacency()
        labels: set[str] = set()
        for lst in adj["by_norm"].values():
            for e, _d in lst:
                labels.add(e["subject"])
                labels.add(e["object"])
        return sorted(labels)[:limit]

    async def match_topics(self, text: str, *, limit: int = 3) -> list[str]:
        """Which EDGE-BEARING topic labels appear verbatim in the text — precision-biased
        STRUCTURAL containment (the Pulse-inbox P1 pattern), word-boundary safe, longest
        match first. Misses synonyms BY DESIGN (fail-safe: no match → no expansion); the
        LLM-owned mapping layer (reasoned-scaffold piggyback) is the semantic upgrade.
        Served from the snapshot — no DB round trip at steady state.
        NOTE: needle AND haystack go through the SAME punctuation-stripping normalize —
        norms keep punctuation ("NASH / MASH", "low-voltage ECG") while question text is
        stripped, so an asymmetric compare can never match those labels (v3 panel bug)."""
        import re

        def _flat(s: str) -> str:
            return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())

        adj = await self._adjacency()
        hay = " " + _flat(text) + " "
        cands = set(adj["by_norm"].keys()) | set(adj["up"].keys())
        found = [n for n in cands if f" {_flat(n)} " in hay]
        found.sort(key=len, reverse=True)
        return found[:limit]

    async def _adjacency(self) -> dict:
        if self._snap is not None and (time.monotonic() - self._snap["at"]) < self._cache_ttl:
            return self._snap["adj"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, subject, subject_norm, relation, object, object_norm,
                          context_topic, distinguished_by, label, provenance, confidence
                   FROM roster_topic_edge WHERE status='active' LIMIT 20000""")
        edges = [dict(r) for r in rows]
        self._snap = {"at": time.monotonic(), "adj": build_adjacency(edges)}
        self._snap_edge_count = len(edges)
        return self._snap["adj"]

    async def list_edges(self, *, status: str | None = None, needs_review: bool | None = None,
                         limit: int = 200) -> list[dict]:
        pool = await self._get_pool()
        preds, args = [], []
        if status:
            args.append(status)
            preds.append(f"status=${len(args)}")
        if needs_review is not None:
            args.append(needs_review)
            preds.append(f"needs_review=${len(args)}")
        where = ("WHERE " + " AND ".join(preds)) if preds else ""
        args.append(min(limit, 1000))
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, subject, relation, object, context_topic, distinguished_by,
                           label, provenance, status, needs_review, confidence, seen_count,
                           note, created_at, updated_at
                    FROM roster_topic_edge {where}
                    ORDER BY updated_at DESC LIMIT ${len(args)}""", *args)
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat()
            d["updated_at"] = d["updated_at"].isoformat()
            out.append(d)
        return out

    async def evidence_for(self, edge_id: str) -> list[dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT document_id, quote, session_id, label, created_at
                   FROM roster_edge_evidence WHERE edge_id=$1 ORDER BY created_at""", edge_id)
        return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]

    async def stats(self) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) AS n FROM roster_topic_edge GROUP BY status")
            ev = await conn.fetchrow("SELECT count(*) AS n FROM roster_edge_evidence")
        return {"edges": {r["status"]: r["n"] for r in rows},
                "evidence_rows": ev["n"] if ev else 0,
                "snapshot_edges": self._snap_edge_count,
                "cache_ttl_seconds": self._cache_ttl}
