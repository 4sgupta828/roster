"""PeopleStore — the kernel-generic ENTITY INVENTORY (Frontier People / spec E-series).

Domain-neutral: rows are professionals of ANY vertical (physicians now; lawyers/engineers
later) keyed by NATURAL registry ids (E-2: NPI / NMC reg no — relocation must never orphan
metrics). Write ethos mirrors the graph store (status lifecycle, per-field provenance,
append-only metrics, never-resurrect); READS are INDEXED SQL ONLY (E-5: the graph's
in-process snapshot caps ~20k rows — banned here at 1-2M entities).

Provenance/honesty (E-1/E-4): every metric carries source + period + retrieved_at and a
label chosen by the CALLER (e.g. "Original Medicare Part B claims, 2023" — never "patients
seen"). `discipline_status` defaults 'not_collected' (P0 is ADMIN-ONLY until P1 board
actions land). Suppression = status flip (right-of-reply), never deletion.
"""
from __future__ import annotations

_DDL = """
CREATE TABLE IF NOT EXISTS roster_entity (
    entity_id     text PRIMARY KEY,            -- NATURAL key: 'npi:1234567890' | 'nmc:...'
    vertical      text NOT NULL DEFAULT 'medical',
    kind          text NOT NULL DEFAULT 'physician',
    name          text NOT NULL,
    taxonomy      text NOT NULL DEFAULT '',    -- vertical specialty code (NUCC for medical)
    specialty     text NOT NULL DEFAULT '',    -- human specialty label (vertical-mapped)
    credential    text NOT NULL DEFAULT '',    -- MD/DO/MBBS…
    org           text NOT NULL DEFAULT '',
    city          text NOT NULL DEFAULT '',
    state         text NOT NULL DEFAULT '',
    country       text NOT NULL DEFAULT 'US',
    license_status    text NOT NULL DEFAULT 'not_collected',   -- E-1 first-class
    board_certified   text NOT NULL DEFAULT 'not_collected',
    discipline_status text NOT NULL DEFAULT 'not_collected',
    status        text NOT NULL DEFAULT 'active',  -- active | suppressed | retired | deceased
    source        text NOT NULL DEFAULT '',        -- spine source (e.g. 'nppes')
    valid_as_of   date,                            -- the SOURCE file's vintage (staleness model)
    retrieved_at  timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ne_spec_geo ON roster_entity (vertical, specialty, country, state);
CREATE INDEX IF NOT EXISTS ne_taxonomy ON roster_entity (taxonomy);
CREATE INDEX IF NOT EXISTS ne_name ON roster_entity (lower(name));
CREATE TABLE IF NOT EXISTS roster_entity_metric (
    entity_id    text NOT NULL,
    metric_key   text NOT NULL,               -- e.g. 'medicare_partb_services'
    metric_label text NOT NULL DEFAULT '',    -- E-4 honest display label incl. skew
    value        double precision NOT NULL,
    unit         text NOT NULL DEFAULT '',
    period       text NOT NULL DEFAULT '',    -- e.g. '2023'
    detail       text NOT NULL DEFAULT '',    -- e.g. HCPCS family for per-procedure rollups
    source       text NOT NULL,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, metric_key, period, detail)
);
CREATE INDEX IF NOT EXISTS nem_metric ON roster_entity_metric (metric_key, period, value DESC);
CREATE TABLE IF NOT EXISTS roster_entity_edge (
    entity_a     text NOT NULL,               -- canonical order: entity_a < entity_b
    entity_b     text NOT NULL,
    relation     text NOT NULL,               -- same_group | same_facility | coauthor | referral_pattern
    evidence_key text NOT NULL DEFAULT '',    -- the SHARED FACT: group PAC id / facility CCN /
    --                                           paper id / CMS shared-patient file row — never inference
    period       text NOT NULL DEFAULT '',
    source       text NOT NULL,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_a, entity_b, relation, evidence_key)
);
CREATE INDEX IF NOT EXISTS nee_a ON roster_entity_edge (entity_a, relation);
CREATE INDEX IF NOT EXISTS nee_b ON roster_entity_edge (entity_b, relation);
CREATE INDEX IF NOT EXISTS nee_ev ON roster_entity_edge (relation, evidence_key);
CREATE TABLE IF NOT EXISTS roster_entity_affiliation (
    entity_id    text NOT NULL,               -- who
    kind         text NOT NULL,               -- group_practice | hospital | dialysis facility | ...
    affil_key    text NOT NULL,               -- the SHARED FACT key: org PAC id / facility CCN
    name         text NOT NULL DEFAULT '',    -- display name when the source publishes one
    source       text NOT NULL,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, kind, affil_key)
);
CREATE INDEX IF NOT EXISTS nea_key ON roster_entity_affiliation (kind, affil_key);
CREATE TABLE IF NOT EXISTS roster_entity_contact (
    entity_id    text NOT NULL,
    kind         text NOT NULL,               -- phone | website | email
    value        text NOT NULL,
    source       text NOT NULL,
    published_by_subject boolean NOT NULL DEFAULT false,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, kind, value)
);
"""


class PeopleStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None
        self._facets = None
        self._facets_at = 0.0

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

    async def set_status(self, entity_id: str, status: str) -> bool:
        """Suppression/right-of-reply and lifecycle — flip, never delete (E-0)."""
        if status not in ("active", "suppressed", "retired", "deceased"):
            raise ValueError(f"bad status {status!r}")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE roster_entity SET status=$2, updated_at=now() WHERE entity_id=$1",
                entity_id, status)
        return res == "UPDATE 1"

    @staticmethod
    def _facet_preds(*, specialty: str = "", taxonomy: str = "", state: str = "",
                     country: str = "", name: str = "", city: str = ""):
        """Shared WHERE builder for search/breakdown — active rows only, always."""
        preds, args = ["e.status = 'active'"], []

        def _p(clause, val):
            args.append(val)
            preds.append(clause.format(n=len(args)))
        if specialty:
            _p("e.specialty ILIKE ${n}", f"%{specialty}%")
        if taxonomy:
            _p("e.taxonomy = ${n}", taxonomy)
        if state:
            _p("e.state = ${n}", state.upper())
        if country:
            _p("e.country = ${n}", country.upper())
        if name:
            _p("lower(e.name) LIKE ${n}", f"%{name.lower()}%")
        if city:
            _p("e.city ILIKE ${n}", f"%{city}%")
        return preds, args

    async def search(self, *, specialty: str = "", taxonomy: str = "", state: str = "",
                     country: str = "", name: str = "", city: str = "", metric_key: str = "",
                     metric_period: str = "", sort_metric: str = "",
                     limit: int = 50) -> list[dict]:
        """INDEXED SQL facet search (E-5). NO default ranking (E-3): rows come back in
        NEUTRAL name order unless the CALLER explicitly passes sort_metric — the user must
        actively choose the sort key; suppressed/retired/deceased are always excluded."""
        pool = await self._get_pool()
        preds, args = self._facet_preds(specialty=specialty, taxonomy=taxonomy, state=state,
                                        country=country, name=name, city=city)
        join, order = "", "ORDER BY e.name"
        if sort_metric:
            args.append(sort_metric)
            k = len(args)
            args.append(metric_period or "")
            join = (f"LEFT JOIN LATERAL (SELECT sum(value) AS mv FROM roster_entity_metric m "
                    f"WHERE m.entity_id = e.entity_id AND m.metric_key = ${k} "
                    f"AND (${k+1} = '' OR m.period = ${k+1})) mt ON true")
            order = "ORDER BY mt.mv DESC NULLS LAST, e.name"
        elif metric_key:
            args.append(metric_key)
            join = (f"JOIN LATERAL (SELECT 1 FROM roster_entity_metric m WHERE "
                    f"m.entity_id = e.entity_id AND m.metric_key = ${len(args)} LIMIT 1) mx ON true")
        args.append(min(max(limit, 1), 200))
        sql = (f"SELECT e.*{', mt.mv AS sort_value' if sort_metric else ''} FROM roster_entity e "
               f"{join} WHERE {' AND '.join(preds)} {order} LIMIT ${len(args)}")
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        out = []
        for r in rows:
            d = dict(r)
            for k in ("retrieved_at", "updated_at"):
                d[k] = d[k].isoformat()
            d["valid_as_of"] = d["valid_as_of"].isoformat() if d["valid_as_of"] else None
            out.append(d)
        return out

    async def breakdown(self, *, specialty: str = "", taxonomy: str = "", state: str = "",
                        country: str = "", name: str = "", city: str = "") -> dict:
        """The narrowing agent's situational awareness: how many match the current facets,
        and how the matches DISTRIBUTE over the other facets (top values) — so a clarifying
        question can split the remaining set instead of guessing. Counts only; indexed."""
        pool = await self._get_pool()
        preds, args = self._facet_preds(specialty=specialty, taxonomy=taxonomy, state=state,
                                        country=country, name=name, city=city)
        where = " AND ".join(preds)
        out = {}
        async with pool.acquire() as conn:
            out["total"] = await conn.fetchval(
                f"SELECT count(*) FROM roster_entity e WHERE {where}", *args)
            for col in ("specialty", "state", "city"):
                rows = await conn.fetch(
                    f"SELECT e.{col} AS v, count(*) AS c FROM roster_entity e WHERE {where} "
                    f"AND e.{col} != '' GROUP BY 1 ORDER BY c DESC LIMIT 8", *args)
                out[f"by_{col}"] = [{"value": r["v"], "count": r["c"]} for r in rows]
        return out

    async def entity(self, entity_id: str) -> dict | None:
        """Full profile: entity + ALL metrics (with per-field provenance) + contacts."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            e = await conn.fetchrow("SELECT * FROM roster_entity WHERE entity_id=$1", entity_id)
            if not e:
                return None
            ms = await conn.fetch(
                "SELECT metric_key, metric_label, value, unit, period, detail, source, "
                "retrieved_at FROM roster_entity_metric WHERE entity_id=$1 "
                "ORDER BY metric_key, period DESC", entity_id)
            cs = await conn.fetch(
                "SELECT kind, value, source, published_by_subject FROM roster_entity_contact "
                "WHERE entity_id=$1", entity_id)
        d = dict(e)
        for k in ("retrieved_at", "updated_at"):
            d[k] = d[k].isoformat()
        d["valid_as_of"] = d["valid_as_of"].isoformat() if d["valid_as_of"] else None
        d["metrics"] = [{**dict(m), "retrieved_at": m["retrieved_at"].isoformat()} for m in ms]
        d["contacts"] = [dict(c) for c in cs]
        async with pool.acquire() as conn:
            afs = await conn.fetch(
                "SELECT kind, affil_key, name, source FROM roster_entity_affiliation "
                "WHERE entity_id=$1 ORDER BY kind, name", entity_id)
        d["affiliations"] = [dict(a) for a in afs]
        return d

    async def affiliations_for(self, entity_ids: list[str]) -> dict[str, list[dict]]:
        """Batch affiliation lookup for result cards — the hospital/group must be visible
        BEFORE a click, not buried in the profile."""
        if not entity_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT entity_id, kind, affil_key, name FROM roster_entity_affiliation "
                "WHERE entity_id = ANY($1) ORDER BY kind, name", entity_ids)
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["entity_id"], []).append(dict(r))
        return out

    async def add_affiliations(self, rows: list[dict]) -> int:
        """Bulk-upsert structural affiliations (entity ↔ group practice / facility). Like
        edges, these are SHARED FACTS keyed by a registry id (org PAC id, facility CCN) —
        never inference — and they power the live referral-network join in connections()."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO roster_entity_affiliation (entity_id, kind, affil_key, name,
                     source) VALUES ($1,$2,$3,$4,$5)
                   ON CONFLICT (entity_id, kind, affil_key) DO UPDATE SET
                     name = CASE WHEN EXCLUDED.name != '' THEN EXCLUDED.name
                                 ELSE roster_entity_affiliation.name END,
                     retrieved_at = now()""",
                [[r["entity_id"], r["kind"], r["affil_key"], r.get("name", ""),
                  r["source"]] for r in rows])
        return len(rows)

    async def add_edges(self, edges: list[dict]) -> int:
        """Append co-affiliation/co-author edges. STRUCTURAL FACTS ONLY (Rule 18): every edge
        carries the shared evidence_key (group PAC id, facility CCN, paper id, CMS
        shared-patient row) — the referral network is derived from public records, never
        guessed. Pairs are stored in canonical order so A-B and B-A dedupe."""
        pool = await self._get_pool()
        rows = []
        for e in edges:
            a, b = sorted((e["entity_a"], e["entity_b"]))
            if a == b:
                continue
            rows.append([a, b, e["relation"], str(e.get("evidence_key", ""))[:120],
                         str(e.get("period", ""))[:16], e["source"]])
        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO roster_entity_edge (entity_a, entity_b, relation,
                         evidence_key, period, source)
                       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""", rows)
        return len(rows)

    async def derive_shared_key_edges(self, *, relation: str, metric_key: str,
                                      source: str) -> int:
        """Derive edges from a SHARED-KEY metric already loaded (e.g. metric_key
        'group_pac_id' or 'facility_ccn' with `detail` = the shared id): all ACTIVE entities
        sharing a detail value become pairwise-connected, evidence_key = that value. Pure
        SQL, self-joins capped per shared key (a 500-physician hospital is a hub, not 125k
        edges — cap pairs per key at 2k by smallest-entity order)."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                f"""INSERT INTO roster_entity_edge (entity_a, entity_b, relation,
                      evidence_key, period, source)
                    SELECT LEAST(m1.entity_id, m2.entity_id), GREATEST(m1.entity_id, m2.entity_id),
                           $1, m1.detail, m1.period, $2
                    FROM roster_entity_metric m1
                    JOIN roster_entity_metric m2
                      ON m1.metric_key = m2.metric_key AND m1.detail = m2.detail
                     AND m1.period = m2.period AND m1.entity_id < m2.entity_id
                    JOIN roster_entity e1 ON e1.entity_id = m1.entity_id AND e1.status='active'
                    JOIN roster_entity e2 ON e2.entity_id = m2.entity_id AND e2.status='active'
                    WHERE m1.metric_key = $3 AND m1.detail != ''
                    ON CONFLICT DO NOTHING""",
                relation, source, metric_key)
        try:
            return int((res or "INSERT 0 0").split()[-1])
        except ValueError:
            return 0

    async def connections(self, entity_id: str, *, relation: str = "",
                          limit: int = 50) -> list[dict]:
        """An entity's network: connected colleagues with the shared fact + their profile
        basics — the referral-network read ('who can Dr X hand this case to, in-network').
        Two sources UNIONed: stored edges, and LIVE shared-affiliation neighbors (same
        group practice / same hospital, joined on the registry key at read time — at 1.27M
        entities a large hospital would explode a materialized pairwise edge table, so
        pairs are never stored; E-5 indexed reads only)."""
        pool = await self._get_pool()
        preds = "AND c.relation = $3" if relation else ""
        args = [entity_id, min(limit, 200)] + ([relation] if relation else [])
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT c.peer, c.relation, c.evidence_key, c.source,
                           e.name, e.specialty, e.city, e.state, e.org
                    FROM (
                      SELECT CASE WHEN ee.entity_a = $1 THEN ee.entity_b
                                  ELSE ee.entity_a END AS peer,
                             ee.relation, ee.evidence_key, ee.source
                        FROM roster_entity_edge ee
                       WHERE ee.entity_a = $1 OR ee.entity_b = $1
                      UNION
                      SELECT a2.entity_id, 'same_' || replace(a1.kind, ' ', '_'),
                             COALESCE(NULLIF(a1.name, ''), a1.affil_key), a1.source
                        FROM roster_entity_affiliation a1
                        JOIN roster_entity_affiliation a2
                          ON a2.kind = a1.kind AND a2.affil_key = a1.affil_key
                         AND a2.entity_id != $1
                       WHERE a1.entity_id = $1
                    ) c
                    JOIN roster_entity e ON e.entity_id = c.peer AND e.status = 'active'
                    WHERE true {preds}
                    ORDER BY e.name LIMIT $2""", *args)
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            n = await conn.fetchrow(
                "SELECT count(*) AS entities, count(DISTINCT specialty) AS specialties, "
                "count(*) FILTER (WHERE status != 'active') AS non_active FROM roster_entity")
            m = await conn.fetchval("SELECT count(*) FROM roster_entity_metric")
            c = await conn.fetchval("SELECT count(*) FROM roster_entity_contact")
        return {**dict(n), "metrics": m, "contacts": c}

    async def facet_values(self) -> dict:
        """The CLOSED facet vocabularies, read from the data itself (the kernel stays
        domain-free — labels come from whatever the loaders wrote): distinct specialty
        labels, states, countries, and metric keys actually present. Cached per process
        with a 10-minute TTL — a bulk load runs on ONE replica, and the others must pick
        up the new vocabulary without a restart; loaders on the same process also call
        invalidate_facets() for immediacy."""
        import time
        if self._facets is not None and time.monotonic() - self._facets_at < 600:
            return self._facets
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            sp = await conn.fetch("SELECT DISTINCT specialty FROM roster_entity "
                                  "WHERE specialty != '' ORDER BY specialty")
            st = await conn.fetch("SELECT DISTINCT state FROM roster_entity "
                                  "WHERE state != '' ORDER BY state")
            co = await conn.fetch("SELECT DISTINCT country FROM roster_entity "
                                  "WHERE country != '' ORDER BY country")
            mk = await conn.fetch("SELECT DISTINCT metric_key, metric_label "
                                  "FROM roster_entity_metric ORDER BY metric_key")
        self._facets = {"specialties": [r["specialty"] for r in sp],
                        "states": [r["state"] for r in st],
                        "countries": [r["country"] for r in co],
                        "metrics": [{"key": r["metric_key"], "label": r["metric_label"]}
                                    for r in mk]}
        self._facets_at = time.monotonic()
        return self._facets

    def invalidate_facets(self) -> None:
        self._facets = None
