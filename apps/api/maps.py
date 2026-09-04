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
import hashlib
import hmac
import io
import json
import os
import re
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
ALTER TABLE rs_map_revision ADD COLUMN IF NOT EXISTS delta jsonb NOT NULL DEFAULT '{}'::jsonb;
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


def derive_state(tags: list[str] | None) -> str:
    """The review state a set of feedback tags implies (ONE control in the UI: the chips; the state
    is never asked separately). Mirrors the FE's deriveReviewState."""
    t = set(tags or [])
    if "more_like_this" in t:
        return "shortlist"
    if "less_like_this" in t:
        return "not relevant"
    if t & {"evidence_too_weak", "needs_artifact_evidence"}:
        return "needs more evidence"
    return "maybe" if t else "unreviewed"


REVISION_REASONS = ("initial", "hiring_manager_feedback", "manual_filter_change", "evidence_refresh", "corpus_expansion")
_MAX_ROWS = 400


_MAX_REVIEWERS_PER_MAP = int(os.environ.get("ROSTER_MAX_REVIEWERS_PER_MAP", "10") or 10)
_CTRL_RX = re.compile(r"[\x00-\x1f\x7f<>]")


def sanitize_text(v: str | None, limit: int) -> str:
    """Reviewer-supplied text at the API boundary: control characters and angle brackets stripped,
    whitespace collapsed, length-capped (output is escaped too; this keeps the stored value clean)."""
    return re.sub(r"\s+", " ", _CTRL_RX.sub("", str(v or ""))).strip()[:limit]


def _review_secret() -> bytes:
    return (os.environ.get("ROSTER_REVIEW_SECRET") or os.environ.get("ROSTER_ADMIN_TOKEN") or "roster-review-dev").encode()


def sign_reviewer(map_id: str, reviewer_key: str, name: str) -> str:
    """A SERVER-SIGNED reviewer token: HMAC over (map, key, name). A guest gets one by registering a
    name on the private link; review writes must present it — the read-only share token alone never
    authorizes a write, and a key cannot be spoofed without the secret."""
    msg = f"{map_id}|{reviewer_key}|{name}".encode()
    return hmac.new(_review_secret(), msg, hashlib.sha256).hexdigest()[:40]


def verify_reviewer(map_id: str, reviewer_key: str, name: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(sign_reviewer(map_id, reviewer_key, name), token)


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
            # MIGRATION (idempotent): one review row per (map, entity, reviewer) — the owner's row has
            # reviewer_key '' — instead of the old (map, entity) key that could hold a single review
            pk_cols = [r["attname"] for r in await conn.fetch(
                """SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = 'rs_map_review'::regclass AND i.indisprimary ORDER BY a.attnum""")]
            if "reviewer_key" not in pk_cols:
                async with conn.transaction():
                    await conn.execute("ALTER TABLE rs_map_review DROP CONSTRAINT IF EXISTS rs_map_review_pkey")
                    await conn.execute("ALTER TABLE rs_map_review ADD PRIMARY KEY (map_id, entity_id, reviewer_key)")
            # one-time vocabulary migration (the read path no longer needs the legacy map for new rows)
            await conn.execute("UPDATE rs_map_review SET state = 'shortlist' WHERE state = 'shortlisted'")
            await conn.execute("UPDATE rs_map_review SET state = 'maybe' WHERE state = 'reviewed'")
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
                "SELECT revision_id, reason, delta, created_at FROM rs_map_revision WHERE map_id = $1 ORDER BY revision_id", map_id)
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
            d["feedback"].append({"entity_id": x["entity_id"], **rec})
        d["revisions"] = [{"revision_id": r["revision_id"], "reason": r["reason"], "created_at": str(r["created_at"]),
                           "delta": (json.loads(r["delta"]) if isinstance(r["delta"], str) else (r["delta"] or {}))} for r in revs]
        # names for reviewed people who left the map in a later revision (feedback outlives the row)
        _present = {str(x.get("entity_id") or "") for x in d["rows"] if isinstance(x, dict)}
        _missing = sorted({f["entity_id"] for f in d["feedback"] if f["entity_id"] not in _present})
        d["row_names"] = {}
        if _missing:
            async with pool.acquire() as conn:
                for x in await conn.fetch(
                        """SELECT DISTINCT r->>'entity_id' AS eid, r->>'name' AS name
                           FROM rs_map_revision, jsonb_array_elements(row_snapshot) r
                           WHERE map_id = $1 AND r->>'entity_id' = ANY($2::text[])""", map_id, _missing):
                    if x["eid"] and x["name"]:
                        d["row_names"][x["eid"]] = x["name"]
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

    async def add_revision(self, map_id: str, *, owner_id: str, reason: str, brief: str, filters: dict | None,
                           coverage: dict | None, rows: list[dict], delta: dict | None = None) -> int | None:
        """A NEW REVISION of the map (owner only): the map's live brief/filters/coverage/rows move to the
        revised search's output; the previous state stays in its own revision row. `delta` is the
        code-computed diff + the plain-words edit log + the two-line summary. Returns the revision id."""
        await self._ensure()
        if reason not in REVISION_REASONS:
            reason = "manual_filter_change"
        rows = list(rows or [])[:_MAX_ROWS]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT owner_id FROM rs_map WHERE id = $1", map_id)
            if r is None or not (owner_id and r["owner_id"] == owner_id):
                return None
            async with conn.transaction():
                nxt = int(await conn.fetchval(
                    "SELECT COALESCE(MAX(revision_id), -1) + 1 FROM rs_map_revision WHERE map_id = $1", map_id) or 0)
                if nxt == 0:
                    # a map saved before revision tracking: its CURRENT state becomes revision 0 first, so
                    # the pre-revision list is kept and the new state is revision 1 (never overwrite 0)
                    cur = await conn.fetchrow("SELECT brief, filters, coverage, rows FROM rs_map WHERE id = $1", map_id)
                    await conn.execute(
                        """INSERT INTO rs_map_revision (map_id, revision_id, reason, brief_snapshot, filters_snapshot,
                                                        coverage_snapshot, row_snapshot)
                           VALUES ($1, 0, 'initial', $2, $3::jsonb, $4::jsonb, $5::jsonb)""",
                        map_id, cur["brief"] or "", json.dumps(cur["filters"] if not isinstance(cur["filters"], str) else json.loads(cur["filters"])),
                        json.dumps(cur["coverage"] if not isinstance(cur["coverage"], str) else json.loads(cur["coverage"])),
                        json.dumps(cur["rows"] if not isinstance(cur["rows"], str) else json.loads(cur["rows"])))
                    nxt = 1
                await conn.execute(
                    """INSERT INTO rs_map_revision (map_id, revision_id, reason, brief_snapshot, filters_snapshot,
                                                    coverage_snapshot, row_snapshot, delta)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb)""",
                    map_id, nxt, reason, (brief or "").strip()[:2000], json.dumps(filters or {}),
                    json.dumps(coverage or {}), json.dumps(rows), json.dumps(delta or {}))
                await conn.execute(
                    """UPDATE rs_map SET brief = $2, filters = $3::jsonb, coverage = $4::jsonb, rows = $5::jsonb,
                                         updated_at = now() WHERE id = $1""",
                    map_id, (brief or "").strip()[:2000], json.dumps(filters or {}), json.dumps(coverage or {}), json.dumps(rows))
        return nxt

    async def register_reviewer(self, map_id: str, *, share_token: str, name: str) -> dict | None:
        """A guest on the private link becomes a NAMED REVIEWER: returns {reviewer_key, reviewer_name,
        reviewer_token}. Capped at ROSTER_MAX_REVIEWERS_PER_MAP distinct reviewers per map. None when
        the link is wrong, the name is empty, or the map is full."""
        await self._ensure()
        name = sanitize_text(name, 80)
        if not name:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT share_token FROM rs_map WHERE id = $1", map_id)
            if r is None or not (share_token and secrets.compare_digest(share_token, r["share_token"])):
                return None
            n = await conn.fetchval(
                "SELECT count(DISTINCT reviewer_key) FROM rs_map_review WHERE map_id = $1 AND reviewer_key <> ''", map_id)
            if int(n or 0) >= _MAX_REVIEWERS_PER_MAP:
                return None
        key = secrets.token_urlsafe(12)
        return {"reviewer_key": key, "reviewer_name": name, "reviewer_token": sign_reviewer(map_id, key, name)}

    async def review(self, map_id: str, *, owner_id: str | None, entity_id: str, state: str,
                     note: str | None = None, tags: list[str] | None = None,
                     reviewer_key: str = "", reviewer_name: str = "", reviewer_token: str = "") -> bool:
        """Set a row's HUMAN review state, feedback tags and note (never a verdict — no 'reject').
        The OWNER writes the row's headline review (reviewer_key ''); a NAMED REVIEWER on the private
        link (share token + their own key/name) writes their own row alongside — reviewers never
        overwrite each other. Feedback edits the next map's contract, never the evidence."""
        await self._ensure()
        tags_given = tags is not None
        tags = [t for t in (tags or []) if t in FEEDBACK_TAGS][:6]
        if state == "auto" or (tags_given and not state):
            state = derive_state(tags)
        state = _LEGACY_STATE.get(state, state)
        if state not in REVIEW_STATES:
            return False
        reviewer_name = sanitize_text(reviewer_name, 80)
        note = sanitize_text(note, 2000) if note is not None else None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT owner_id FROM rs_map WHERE id = $1", map_id)
            if r is None:
                return False
            is_owner = bool(owner_id and r["owner_id"] == owner_id)
            via_token = bool(reviewer_key and reviewer_name and verify_reviewer(map_id, reviewer_key, reviewer_name, reviewer_token))
            if not (is_owner or via_token):
                return False
            rkey = "" if is_owner else reviewer_key[:64]
            rname = "" if is_owner else reviewer_name
            eid = entity_id
            await conn.execute(
                """INSERT INTO rs_map_review (map_id, entity_id, state, note, tags, reviewer_key, reviewer_name)
                   VALUES ($1,$2,$3,$4,$5::text[],$6,$7)
                   ON CONFLICT (map_id, entity_id, reviewer_key) DO UPDATE SET
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
