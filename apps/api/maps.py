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
ALTER TABLE rs_map_review ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';
ALTER TABLE rs_map_review ADD COLUMN IF NOT EXISTS reviewer_key text NOT NULL DEFAULT '';
ALTER TABLE rs_map_review ADD COLUMN IF NOT EXISTS reviewer_name text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS rs_map_revision (
    map_id            text NOT NULL REFERENCES rs_map(id) ON DELETE CASCADE,
    revision_id       int  NOT NULL,
    reason            text NOT NULL DEFAULT 'initial',
    brief_snapshot    text NOT NULL DEFAULT '',
    filters_snapshot  jsonb NOT NULL DEFAULT '{}'::jsonb,
    coverage_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    row_snapshot      jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (map_id, revision_id)
);
CREATE TABLE IF NOT EXISTS rs_map_review (
    map_id        text NOT NULL REFERENCES rs_map(id) ON DELETE CASCADE,
    entity_id     text NOT NULL,
    state         text NOT NULL DEFAULT 'unreviewed',   -- unreviewed | shortlisted | needs more evidence | reviewed
    note          text NOT NULL DEFAULT '',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (map_id, entity_id)
);
"""

REVIEW_STATES = ("unreviewed", "shortlist", "maybe", "needs more evidence", "not relevant")
_LEGACY_STATE = {"shortlisted": "shortlist", "reviewed": "maybe"}     # older rows read as the new vocabulary
FEEDBACK_TAGS = ("more_like_this", "less_like_this", "wrong_domain", "wrong_seniority", "wrong_location",
                 "wrong_company_target", "evidence_too_weak", "needs_artifact_evidence",
                 "self_stated_is_enough", "private_company_talent")
REVISION_REASONS = ("initial", "hiring_manager_feedback", "manual_filter_change", "evidence_refresh", "corpus_expansion")
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
            await conn.execute(
                """INSERT INTO rs_map_revision (map_id, revision_id, reason, brief_snapshot, filters_snapshot,
                                                coverage_snapshot, row_snapshot)
                   VALUES ($1, 0, 'initial', $2, $3::jsonb, $4::jsonb, $5::jsonb)""",
                mid, (brief or "").strip()[:2000], json.dumps(filters or {}), json.dumps(coverage or {}), json.dumps(rows))
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
                "SELECT entity_id, state, note, tags, reviewer_key, reviewer_name, updated_at FROM rs_map_review WHERE map_id = $1", map_id)
            revs = await conn.fetch(
                "SELECT revision_id, reason, created_at FROM rs_map_revision WHERE map_id = $1 ORDER BY revision_id", map_id)
        d = dict(r)
        for k in ("filters", "coverage", "rows"):
            v = d.get(k)
            d[k] = json.loads(v) if isinstance(v, str) else (v or ({} if k != "rows" else []))
        d["reviews"] = {}
        d["feedback"] = []                       # every reviewer's row-level feedback (tags + note), by entity
        for x in reviews:
            st = _LEGACY_STATE.get(x["state"], x["state"])
            rec = {"state": st, "note": x["note"], "tags": list(x["tags"] or []), "reviewer_key": x["reviewer_key"] or "",
                   "reviewer_name": x["reviewer_name"] or "", "updated_at": str(x["updated_at"])}
            if not x["reviewer_key"]:            # the owner's own review is the row's headline state
                d["reviews"][x["entity_id"]] = rec
            d["feedback"].append({"entity_id": x["entity_id"].split("##", 1)[0], **rec})
        d["revisions"] = [{"revision_id": r["revision_id"], "reason": r["reason"], "created_at": str(r["created_at"])} for r in revs]
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

    async def review(self, map_id: str, *, owner_id: str | None, entity_id: str, state: str,
                     note: str | None = None, tags: list[str] | None = None,
                     share_token: str | None = None, reviewer_key: str = "", reviewer_name: str = "") -> bool:
        """Set a row's HUMAN review state, feedback tags and note (never a verdict — no 'reject').
        The OWNER writes the row's headline review (reviewer_key ''); a NAMED REVIEWER on the private
        link (share token + their own key/name) writes their own row alongside — reviewers never
        overwrite each other. Feedback edits the next map's contract, never the evidence."""
        await self._ensure()
        state = _LEGACY_STATE.get(state, state)
        if state not in REVIEW_STATES:
            return False
        tags_given = tags is not None
        tags = [t for t in (tags or []) if t in FEEDBACK_TAGS][:6]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT owner_id, share_token FROM rs_map WHERE id = $1", map_id)
            if r is None:
                return False
            is_owner = bool(owner_id and r["owner_id"] == owner_id)
            via_link = bool(share_token and secrets.compare_digest(share_token, r["share_token"]) and reviewer_key)
            if not (is_owner or via_link):
                return False
            rkey = "" if is_owner else reviewer_key[:64]
            rname = ("" if is_owner else (reviewer_name or "reviewer")[:80])
            # one row per (map, entity, reviewer) — the table's PK is (map, entity) for the owner's row;
            # reviewer rows key on reviewer_key inside entity_id's namespace to stay additive
            eid = entity_id if is_owner else f"{entity_id}##{rkey}"
            await conn.execute(
                """INSERT INTO rs_map_review (map_id, entity_id, state, note, tags, reviewer_key, reviewer_name)
                   VALUES ($1,$2,$3,$4,$5::text[],$6,$7)
                   ON CONFLICT (map_id, entity_id) DO UPDATE SET
                     state = EXCLUDED.state,
                     note = CASE WHEN $8 THEN EXCLUDED.note ELSE rs_map_review.note END,
                     tags = CASE WHEN $9 THEN EXCLUDED.tags ELSE rs_map_review.tags END,
                     reviewer_name = EXCLUDED.reviewer_name,
                     updated_at = now()""",
                map_id, eid, state, (note or "")[:2000], tags, rkey, rname, note is not None, tags_given)
            await conn.execute("UPDATE rs_map SET updated_at = now() WHERE id = $1", map_id)
        return True


def build_jobs_csv(m: dict) -> str:
    """JOB MAP export: one row per role — title, company, location, match, reasons, apply link,
    posting summary parameters when a summary was saved with the row, review state, note."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["title", "company", "location", "match_pct", "reasons", "apply_url", "source",
                "work_mode", "compensation", "employment_type", "review_state", "review_note", "job_ref"])
    reviews = m.get("reviews") or {}
    for j in m.get("rows") or []:
        ref = str(j.get("id") or j.get("key") or ((j.get("company") or "") + "|" + (j.get("title") or "")))
        rv = reviews.get(ref, {})
        sm = j.get("summary") or {}
        w.writerow([j.get("title") or "", (j.get("company") or "").replace("_", " "), j.get("location") or "",
                    j.get("match_pct") or "", "|".join(j.get("reasons") or []), j.get("url") or "", j.get("source") or "",
                    sm.get("work_mode") or "", sm.get("compensation") or "", sm.get("employment_type") or "",
                    rv.get("state") or "unreviewed", rv.get("note") or "", ref])
    return out.getvalue()


def build_csv(m: dict) -> str:
    """The export: one row per entity, preserving links and evidence strength (spec acceptance).
    Columns follow the Talent Map table: person, company, role, location, evidence strength,
    evidence types, sources, why surfaced, profile links, review state, note, entity id."""
    if (m.get("map_type") or "") == "jobs":
        return build_jobs_csv(m)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "current_company", "role", "location", "evidence_strength",
                "evidence_types", "source_families", "corroborated_claims", "gaps",
                "why_surfaced", "profile_links", "public_artifacts", "newest_artifact",
                "artifact_links", "review_state", "review_note", "entity_id"])
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
        art = p.get("artifacts") or {}
        art_counts = "|".join(f"{k}:{n}" for k, n in sorted((art.get("counts") or {}).items()))
        if not art_counts and art.get("scanned"):
            art_counts = "none found"
        art_links = " ".join((it.get("url") or "") for it in (art.get("items") or [])
                             if str(it.get("url") or "").startswith("http"))
        w.writerow([
            p.get("name") or "", _a("company"), _a("role") or _a("title"),
            _a("metro") or _a("country"), ev.get("strength") or "",
            "|".join(ev.get("types") or []), "|".join(ev.get("families") or []),
            "|".join(ev.get("corroborated_keys") or []), "|".join(ev.get("gaps") or []),
            p.get("blurb") or "", links, art_counts, art.get("newest") or "", art_links,
            rv.get("state") or "unreviewed", rv.get("note") or "", p.get("entity_id") or ""])
    return out.getvalue()
