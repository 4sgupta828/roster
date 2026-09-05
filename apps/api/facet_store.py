"""The Postgres facet STORE adapter (docs/specs/facet-contract-evaluator.md §1, §2.2, §5) — implements the
kernel's FacetStore protocol over the facet read model (`roster_entity_facet`, now for every entity kind) and
the entity tables (`rs_job`, `rs_entity` + `rs_person_vec`). Musts are applied IN SQL on every leg; counts are a
GROUP BY over the must-filtered slice; `project` is the ONLY writer of facet rows from an extraction envelope.

Semantics are the kernel's (`matches_must` / `count_rows`); the parity test runs both over the same data."""
from __future__ import annotations

import json
from typing import Any

from roster_kernel.facets import UNKNOWN, FacetSchema, FacetType

_DDL = (
    "ALTER TABLE roster_entity_facet ADD COLUMN IF NOT EXISTS entity_kind text NOT NULL DEFAULT 'person'",
    "ALTER TABLE roster_entity_facet ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT ''",
    "ALTER TABLE roster_entity_facet ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT ''",
    "ALTER TABLE roster_entity_facet ADD COLUMN IF NOT EXISTS numeric_value double precision",
    "CREATE INDEX IF NOT EXISTS ix_roster_facet_kind_kv ON roster_entity_facet (tenant_id, entity_kind, facet_key, facet_value_norm)",
    "CREATE INDEX IF NOT EXISTS ix_roster_facet_entity ON roster_entity_facet (entity_id, facet_key)",
    "ALTER TABLE rs_job ADD COLUMN IF NOT EXISTS facets_projected text",
)

_JOB_COLS = "j.id, j.company, j.title, j.location, j.department, j.url, j.source, j.skills, j.posted_at, j.updated_at"


_CTRL = __import__("re").compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(v) -> str:
    return _CTRL.sub("", str(v if v is not None else ""))


def job_entity_id(job_id: int | str) -> str:
    return f"job:{job_id}"


def company_entity_id(slug: str) -> str:
    return f"company:{(slug or '').strip().lower().replace(' ', '_')}"


def via_target_key(key: str, via: str) -> str:
    """Convention: a via-key `company_type` reads the related company's `type`; `company_stage` → `stage`."""
    return key[len(via) + 1:] if key.startswith(via + "_") else key


def closed_vocab_pairs(schema: FacetSchema, kind: str) -> tuple[list[str], list[str], list[str]]:
    """(open_keys, closed_keys, closed_values) for the kind's navigable own keys: a closed key (categorical /
    ordinal vocabulary, numeric bands) only ever counts or attaches values the schema names — rows an older
    vocabulary wrote under the same key name are invisible to the read model, never a chip."""
    open_keys, ck, cv = [], [], []
    for k in schema.for_kind(kind):
        if k.via:
            continue
        vals = tuple(k.values) if k.values else tuple(b[0] for b in k.bands) if k.bands else ()
        if vals:
            for v in vals:
                ck.append(k.key); cv.append(v)
        else:
            open_keys.append(k.key)
    return open_keys, ck, cv


class FacetSQLStore:
    def __init__(self, pool_getter, schema: FacetSchema, *, tenant_id: str = "demo", embed=None, baseline=None):
        self._pool_getter = pool_getter
        self.schema = schema
        self.tenant = tenant_id
        self._embed = embed              # text → pgvector literal (app-supplied; None → no semantic leg)
        self._baseline = baseline        # async (qvec) → float | None  (noise floor)
        self._ready = False

    async def _conn(self):
        return await self._pool_getter()

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        pool = await self._conn()
        async with pool.acquire() as conn:
            for ddl in _DDL:
                await conn.execute(ddl)
        self._ready = True

    # ---------------- musts → SQL ----------------
    def _must_sql(self, ent: str, must: dict, args: list) -> list[str]:
        """One EXISTS per must key over the facet rows of entity `ent` (a SQL expression yielding the
        entity_id). Within a key OR (= ANY), across keys AND (one clause each). Unknown never matches
        because a missing row cannot satisfy EXISTS. Via keys go through the company relation."""
        clauses = []
        for key, want in (must or {}).items():
            k = self.schema.key(key)
            if k is None:
                clauses.append("FALSE"); continue
            args.append(self.tenant); ti = len(args)
            if k.via:
                tkey = via_target_key(key, k.via)
                args.append(k.via); vi = len(args)
                args.append(tkey); tk = len(args)
                args.append([str(x) for x in (want or [])] if not isinstance(want, dict) else []); vv = len(args)
                clauses.append(f"""EXISTS (SELECT 1 FROM roster_entity_facet r JOIN roster_entity_facet c
                                    ON c.tenant_id = r.tenant_id AND c.entity_kind = 'company' AND c.entity_id = 'company:' || r.facet_value_norm AND c.facet_key = ${tk}
                                  WHERE r.tenant_id = ${ti} AND r.entity_id = {ent} AND r.facet_key = ${vi} AND c.facet_value_norm = ANY(${vv}))""")
                continue
            args.append(key); ki = len(args)
            if isinstance(want, dict):
                lo, hi = want.get("min"), want.get("max")
                conds = []
                if lo is not None:
                    args.append(float(lo)); conds.append(f"f.numeric_value >= ${len(args)}")
                if hi is not None:
                    args.append(float(hi)); conds.append(f"f.numeric_value <= ${len(args)}")
                extra = (" AND " + " AND ".join(conds)) if conds else ""
                clauses.append(f"EXISTS (SELECT 1 FROM roster_entity_facet f WHERE f.tenant_id = ${ti} AND f.entity_id = {ent} AND f.facet_key = ${ki} AND f.numeric_value IS NOT NULL{extra})")
                continue
            vals = [str(x) for x in (want or []) if str(x) and str(x) != UNKNOWN]
            if not vals:
                continue
            args.append(vals); vi = len(args)
            if k.type is FacetType.hierarchical:
                pats = [v + "%" for v in vals] + list(vals)
                args[-1] = pats
                clauses.append(f"EXISTS (SELECT 1 FROM roster_entity_facet f WHERE f.tenant_id = ${ti} AND f.entity_id = {ent} AND f.facet_key = ${ki} AND f.facet_value_norm LIKE ANY(${vi}))")
            else:
                clauses.append(f"EXISTS (SELECT 1 FROM roster_entity_facet f WHERE f.tenant_id = ${ti} AND f.entity_id = {ent} AND f.facet_key = ${ki} AND f.facet_value_norm = ANY(${vi}))")
        return clauses

    # ---------------- rows ----------------
    async def _attach_facets(self, conn, kind: str, rows: list[dict]) -> list[dict]:
        ids = [r["_eid"] for r in rows]
        if not ids:
            return rows
        fr = await conn.fetch("SELECT entity_id, facet_key, facet_value_norm, display_value, numeric_value, provenance FROM roster_entity_facet "
                              "WHERE tenant_id = $1 AND entity_id = ANY($2::text[])", self.tenant, ids)
        by: dict[str, dict] = {eid: {"facets": {}, "numeric": {}, "display": {}, "provenance": {}} for eid in ids}
        _ok, _ck, _cv = closed_vocab_pairs(self.schema, kind)
        _closed: dict[str, set] = {}
        for k_, v_ in zip(_ck, _cv):
            _closed.setdefault(k_, set()).add(v_)
        for f in fr:
            if f["facet_key"] in _closed and f["facet_value_norm"] not in _closed[f["facet_key"]]:
                continue                                     # an older vocabulary's value under a schema key name
            d = by[f["entity_id"]]
            d["facets"].setdefault(f["facet_key"], []).append(f["facet_value_norm"])
            if f["numeric_value"] is not None:
                d["numeric"][f["facet_key"]] = float(f["numeric_value"])
            if f["display_value"]:
                d["display"].setdefault(f["facet_key"], f["display_value"])
            if f["provenance"]:
                d["provenance"].setdefault(f["facet_key"], f["provenance"])
        # via keys: the company's own facets, read through the entity's company slug
        via_keys = [k for k in self.schema.for_kind(kind) if k.via]
        if via_keys:
            slugs = sorted({v for eid in ids for v in by[eid]["facets"].get("company", [])})
            if slugs:
                cr = await conn.fetch("SELECT entity_id, facet_key, facet_value_norm FROM roster_entity_facet WHERE tenant_id = $1 AND entity_kind = 'company' AND entity_id = ANY($2::text[])",
                                      self.tenant, [company_entity_id(s) for s in slugs])
                cf: dict[str, dict] = {}
                for c in cr:
                    cf.setdefault(c["entity_id"], {}).setdefault(c["facet_key"], []).append(c["facet_value_norm"])
                for eid in ids:
                    for k in via_keys:
                        tkey = via_target_key(k.key, k.via)
                        vals = [v for s in by[eid]["facets"].get(k.via, []) for v in cf.get(company_entity_id(s), {}).get(tkey, [])]
                        if vals:
                            by[eid]["facets"][k.key] = sorted(set(vals))
        for r in rows:
            r.update(by[r["_eid"]])
        return rows

    def _job_row(self, r) -> dict:
        return {"id": r["id"], "_eid": job_entity_id(r["id"]), "kind": "job", "company": r["company"], "title": r["title"], "location": r["location"],
                "department": r["department"], "url": r["url"], "source": r["source"], "skills": list(r["skills"] or []),
                "posted_at": r["posted_at"], "updated_at": (r["updated_at"].isoformat() if r["updated_at"] else None), "sim": (float(r["sim"]) if "sim" in r.keys() and r["sim"] is not None else None)}

    def _person_row(self, r) -> dict:
        return {"id": r["entity_id"], "_eid": r["entity_id"], "entity_id": r["entity_id"], "kind": "person", "name": r["name"],
                "sim": (float(r["sim"]) if "sim" in r.keys() and r["sim"] is not None else None)}

    async def enumerate(self, kind: str, must: dict, *, cap: int = 400) -> list[dict]:
        await self.ensure_schema()
        pool = await self._conn()
        args: list = []
        async with pool.acquire() as conn:
            if kind == "job":
                cl = self._must_sql("('job:' || j.id::text)", must, args)
                args.append(int(cap))
                rows = await conn.fetch(f"SELECT {_JOB_COLS} FROM rs_job j WHERE j.closed_at IS NULL" + "".join(" AND " + c for c in cl)
                                        + f" ORDER BY j.updated_at DESC LIMIT ${len(args)}", *args)
                out = [self._job_row(r) for r in rows]
            else:
                cl = self._must_sql("e.entity_id", must, args)
                args.append(int(cap))
                rows = await conn.fetch("SELECT e.entity_id, e.name FROM rs_entity e WHERE e.kind = 'person' AND e.status = 'active'" + "".join(" AND " + c for c in cl)
                                        + f" ORDER BY e.entity_id LIMIT ${len(args)}", *args)
                out = [self._person_row(r) for r in rows]
            return await self._attach_facets(conn, kind, out)

    async def semantic(self, kind: str, text: str, must: dict, *, cap: int = 400) -> list[dict]:
        if self._embed is None:
            return []
        qv = self._embed(text)
        if not qv:
            return []
        await self.ensure_schema()
        pool = await self._conn()
        args: list = [qv]
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.ef_search = 200")
                if kind == "job":
                    cl = self._must_sql("('job:' || j.id::text)", must, args)
                    args.append(int(cap))
                    rows = await conn.fetch(f"SELECT {_JOB_COLS}, 1 - (j.embedding <=> $1::vector) AS sim FROM rs_job j WHERE j.embedding IS NOT NULL AND j.closed_at IS NULL"
                                            + "".join(" AND " + c for c in cl) + f" ORDER BY j.embedding <=> $1::vector LIMIT ${len(args)}", *args)
                    out = [self._job_row(r) for r in rows]
                else:
                    cl = self._must_sql("e.entity_id", must, args)
                    args.append(int(cap))
                    rows = await conn.fetch("SELECT e.entity_id, e.name, 1 - (v.embedding <=> $1::vector) AS sim FROM rs_person_vec v JOIN rs_entity e ON e.entity_id = v.entity_id "
                                            "WHERE e.kind = 'person' AND e.status = 'active'" + "".join(" AND " + c for c in cl)
                                            + f" ORDER BY v.embedding <=> $1::vector LIMIT ${len(args)}", *args)
                    out = [self._person_row(r) for r in rows]
            return await self._attach_facets(conn, kind, out)

    # ---------------- counts ----------------
    async def counts(self, kind: str, must: dict, schema: FacetSchema, *, depth: dict | None = None) -> dict:
        await self.ensure_schema()
        pool = await self._conn()
        args: list = []
        ent = "('job:' || j.id::text)" if kind == "job" else "e.entity_id"
        cl = self._must_sql(ent, must, args)
        if kind == "job":
            slice_sql = "SELECT ('job:' || j.id::text) AS entity_id FROM rs_job j WHERE j.closed_at IS NULL" + "".join(" AND " + c for c in cl)
        else:
            slice_sql = "SELECT e.entity_id FROM rs_entity e WHERE e.kind = 'person' AND e.status = 'active'" + "".join(" AND " + c for c in cl)
        nav = [k for k in schema.for_kind(kind) if k.navigable]
        _open, _ck, _cv = closed_vocab_pairs(schema, kind)
        nav_keys = {x.key for x in nav}
        args.append([k for k in _open if k in nav_keys]); oi = len(args)
        args.append([k for k in _ck if k in nav_keys]); cki = len(args)
        args.append([v for k, v in zip(_ck, _cv) if k in nav_keys]); cvi = len(args)
        ki = oi                                  # the slice's own params end before ours
        # index-friendly: two ANY() conditions the (tenant, kind, key, value) index can serve; the exact (key, value)
        # legality is re-checked in Python below (a value legal for another closed key is a rounding error in `have`)
        legal = f"(f.facet_key = ANY(${oi}) OR (f.facet_key = ANY(${cki}) AND f.facet_value_norm = ANY(${cvi})))"
        _legal_pairs = set(zip(args[cki - 1], args[cvi - 1]))
        _closed_keys = set(args[cki - 1])
        out: dict = {}
        async with pool.acquire() as conn:
            total = int(await conn.fetchval(f"SELECT count(*) FROM ({slice_sql}) s", *args[: ki - 1]) or 0)
            rows = await conn.fetch(f"""WITH s AS ({slice_sql})
                                        SELECT f.facet_key, f.facet_value_norm AS v, count(DISTINCT f.entity_id) AS n
                                        FROM roster_entity_facet f JOIN s ON s.entity_id = f.entity_id
                                        WHERE {legal} GROUP BY 1, 2""", *args)
            have = await conn.fetch(f"""WITH s AS ({slice_sql})
                                        SELECT f.facet_key, count(DISTINCT f.entity_id) AS n
                                        FROM roster_entity_facet f JOIN s ON s.entity_id = f.entity_id
                                        WHERE {legal} GROUP BY 1""", *args)
            have_n = {r["facet_key"]: int(r["n"]) for r in have}
            for k in nav:
                if k.via:
                    tkey = via_target_key(k.key, k.via)
                    vargs = list(args[: ki - 1]) + [tkey, k.via]          # the slice's params, then the two of ours
                    vr = await conn.fetch(f"""WITH s AS ({slice_sql})
                                              SELECT c.facet_value_norm AS v, count(DISTINCT f.entity_id) AS n
                                              FROM roster_entity_facet f JOIN s ON s.entity_id = f.entity_id
                                              JOIN roster_entity_facet c ON c.tenant_id = f.tenant_id AND c.entity_kind = 'company'
                                                   AND c.entity_id = 'company:' || f.facet_value_norm AND c.facet_key = ${len(vargs) - 1}
                                              WHERE f.facet_key = ${len(vargs)} GROUP BY 1""", *vargs)
                    d = {r["v"]: int(r["n"]) for r in vr}
                    known = sum(d.values())
                    if total - known > 0:
                        d[UNKNOWN] = total - known
                    out[k.key] = d
                    continue
                d = {r["v"]: int(r["n"]) for r in rows if r["facet_key"] == k.key and (k.key not in _closed_keys or (k.key, r["v"]) in _legal_pairs)}
                if k.type is FacetType.set:
                    d = dict(sorted(d.items(), key=lambda kv: -kv[1])[: k.top_n])
                else:
                    unknown = total - have_n.get(k.key, 0)
                    if unknown > 0:
                        d[UNKNOWN] = unknown
                out[k.key] = d
        return out

    async def noise_floor(self, kind: str, text: str) -> float | None:
        if self._embed is None or self._baseline is None:
            return None
        qv = self._embed(text)
        return await self._baseline(qv, kind) if qv else None

    # ---------------- projection (the only writer) ----------------
    async def project(self, kind: str, entity_id: str, envelope: dict, *, replace_keys: list[str] | None = None) -> int:
        """Envelope → facet rows for one entity (idempotent: the envelope's keys are replaced, other keys
        are left alone). Numeric keys store the band as the value and the number in `numeric_value`.
        Returns rows written."""
        await self.ensure_schema()
        facets = (envelope or {}).get("facets") or {}
        version = str((envelope or {}).get("schema_version") or "")
        keys = list(replace_keys or facets.keys())
        rows = []
        for key, items in facets.items():
            k = self.schema.key(key)
            if k is None:
                continue
            for it in (items or []):
                if not isinstance(it, dict):
                    it = {"value": it}
                num = it.get("number")
                if k.type is FacetType.numeric:
                    band = self.schema.band_of(key, num) if num is not None else (self.schema.validate_value(key, it.get("value")) or UNKNOWN)
                    if band == UNKNOWN and num is None:
                        continue
                    val = band
                else:
                    val = self.schema.validate_value(key, it.get("value"))
                    if val is None:
                        continue
                rows.append((self.tenant, entity_id, kind, key, val, _clean(it.get("display"))[:300], float(it.get("confidence") or 0.0),
                             str(it.get("provenance") or "")[:40], version, (float(num) if num is not None else None)))
        pool = await self._conn()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if keys:
                    await conn.execute("DELETE FROM roster_entity_facet WHERE tenant_id = $1 AND entity_id = $2 AND facet_key = ANY($3::text[])", self.tenant, entity_id, keys)
                for r in rows:
                    await conn.execute("""INSERT INTO roster_entity_facet (tenant_id, entity_id, entity_kind, facet_key, facet_value_norm, display_value, confidence, provenance, schema_version, numeric_value)
                                          VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                                          ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO UPDATE SET
                                            entity_kind = EXCLUDED.entity_kind, display_value = EXCLUDED.display_value, confidence = EXCLUDED.confidence,
                                            provenance = EXCLUDED.provenance, schema_version = EXCLUDED.schema_version, numeric_value = EXCLUDED.numeric_value""", *r)
        return len(rows)


def legacy_job_envelope(flat: dict, *, schema_version: str, provenance: str = "posting") -> dict:
    """The 2026-09-05 extraction record {field, level, role_family} → the spec envelope shape."""
    flat = flat or {}
    facets: dict[str, list] = {}
    if flat.get("field"):
        facets["field"] = [{"value": flat["field"], "confidence": 0.6, "provenance": provenance}]
    if flat.get("level") and flat["level"] != UNKNOWN:
        facets["level"] = [{"value": flat["level"], "confidence": 0.6, "provenance": provenance}]
    if flat.get("role_family"):
        facets["role_family"] = [{"value": flat["role_family"], "confidence": 0.6, "provenance": provenance}]
    return {"schema_version": schema_version, "facets": facets}


def structural_job_facets(job: dict, *, schema_version: str, now_days: float | None = None) -> dict:
    """Facets that come from the row itself, not from the model: the company slug, the skills the body
    named, and the posting age (derived, provenance 'ats_field')."""
    facets: dict[str, list] = {}
    co = str(job.get("company") or "").strip().lower().replace(" ", "_")
    if co:
        facets["company"] = [{"value": co, "display": str(job.get("company") or ""), "confidence": 1.0, "provenance": "ats_field"}]
    sk = [str(s) for s in (job.get("skills") or []) if str(s).strip()]
    if sk:
        facets["skill"] = [{"value": s, "confidence": 0.8, "provenance": "posting"} for s in sk[:12]]
    if now_days is not None:
        facets["posted"] = [{"number": float(now_days), "display": f"{int(now_days)} days ago", "confidence": 1.0, "provenance": "ats_field"}]
    return {"schema_version": schema_version, "facets": facets}


async def project_legacy_people(pool, *, pairs: list[tuple[str, str, str, str]], artifact_map: dict[str, str],
                                schema_version: str, tenant_id: str = "demo") -> dict:
    """SET-BASED projection of the people index's pre-schema rows into schema rows (spec §8 step 4 bridge —
    no model call). For each (legacy_key, legacy_value → schema_key, schema_value) pair the matching rows
    gain a schema row with provenance `legacy`; an entity that already holds a NON-legacy row for that key
    (a real extraction) is left alone. The `evidence` key is derived from `rs_person_artifact` (provenance
    `artifact`). Idempotent (ON CONFLICT DO NOTHING); returns rows written per schema key."""
    out: dict[str, int] = {}
    by_key: dict[str, list[tuple[str, str, str]]] = {}
    for ok, ov, nk, nv in pairs:
        by_key.setdefault(nk, []).append((ok, ov, nv))
    async with pool.acquire() as conn:
        for nk, items in by_key.items():
            res = await conn.execute("""
                INSERT INTO roster_entity_facet (tenant_id, entity_id, entity_kind, facet_key, facet_value_norm, display_value, confidence, provenance, schema_version, numeric_value)
                SELECT DISTINCT f.tenant_id, f.entity_id, 'person', $2, m.nv, '', 0.5, 'legacy', $3, NULL::double precision
                FROM roster_entity_facet f
                JOIN unnest($4::text[], $5::text[], $6::text[]) AS m(ok, ov, nv) ON m.ok = f.facet_key AND m.ov = f.facet_value_norm
                WHERE f.tenant_id = $1 AND f.entity_kind = 'person'
                  AND NOT EXISTS (SELECT 1 FROM roster_entity_facet x WHERE x.tenant_id = f.tenant_id AND x.entity_id = f.entity_id
                                  AND x.facet_key = $2 AND x.provenance <> 'legacy' AND x.schema_version <> '')
                ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO NOTHING""",
                tenant_id, nk, schema_version, [i[0] for i in items], [i[1] for i in items], [i[2] for i in items])
            out[nk] = out.get(nk, 0) + int(str(res).split()[-1] or 0)
        if artifact_map:
            res = await conn.execute("""
                INSERT INTO roster_entity_facet (tenant_id, entity_id, entity_kind, facet_key, facet_value_norm, display_value, confidence, provenance, schema_version, numeric_value)
                SELECT DISTINCT $1::text, a.entity_id, 'person', 'evidence', m.nv, '', 1.0, 'artifact', $2::text, NULL::double precision
                FROM rs_person_artifact a JOIN unnest($3::text[], $4::text[]) AS m(ok, nv) ON m.ok = a.kind
                JOIN rs_entity e ON e.entity_id = a.entity_id AND e.kind = 'person'
                ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO NOTHING""",
                tenant_id, schema_version, list(artifact_map.keys()), list(artifact_map.values()))
            out["evidence"] = int(str(res).split()[-1] or 0)
    return out
