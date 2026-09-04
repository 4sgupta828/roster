"""Accounts + feedback persistence (adoption P0) — app-level, kernel-free, same Postgres as sessions.

Two tables, one store:
  - roster_user: registered users. Registration is upsert-on-email and
    issues a bearer token (sha256 stored, raw returned ONCE). NPI verification (US) is a structural
    lookup against the public CMS registry — a computable fact, not a semantic judgment (Rule 18).
  - roster_feedback: per-answer user feedback keyed to the SAME W1–W9 warrant taxonomy the eval and
    auditor use (docs/specs/answer-warrant-contract.md) — one contract, three uses. This is the
    accumulating ground-truth signal that eventually adjudicates the eval judges.

ALPHA HONESTY: there is no email-verification loop yet (no mail infra), so possession of an email is
self-declared; the token only proves "same registrant". Fine for a no-PHI research tool; add magic-link
verification before anything sensitive rides on identity.

Best-effort like SessionStore: a persistence failure must never break an answer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
import uuid
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_user (
    id            TEXT PRIMARY KEY,
    vertical      TEXT NOT NULL,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL,
    profession    TEXT NOT NULL DEFAULT '',
    country       TEXT NOT NULL DEFAULT '',
    npi           TEXT,
    npi_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    token_hash    TEXT NOT NULL,
    disclaimer_ack_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vertical, email)
);
CREATE INDEX IF NOT EXISTS idx_nu_token ON roster_user (token_hash);
CREATE TABLE IF NOT EXISTS roster_feedback (
    id          TEXT PRIMARY KEY,
    vertical    TEXT NOT NULL,
    user_id     TEXT,
    user_email  TEXT,
    session_id  TEXT NOT NULL DEFAULT '',
    turn_index  INTEGER NOT NULL DEFAULT 0,
    verdict     TEXT NOT NULL,
    modes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    claim_index INTEGER,
    note        TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nfb_vertical_created ON roster_feedback (vertical, created_at DESC);
-- PER-DEVICE TOKENS: multiple active tokens per user, so signing in on a second browser/device no
-- longer orphans the first (the old single token_hash column rotated on every register). The legacy
-- column keeps being written and checked as a FALLBACK so pre-existing sessions stay valid.
CREATE TABLE IF NOT EXISTS roster_user_token (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nut_user ON roster_user_token (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS roster_user_pref (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
-- PASSWORD auth (additive, nullable so pre-existing token-only users are untouched): PBKDF2-HMAC-
-- SHA256, per-user salt, both stored as hex text. Verified constant-time; never logged.
ALTER TABLE roster_user ADD COLUMN IF NOT EXISTS pw_hash TEXT;
ALTER TABLE roster_user ADD COLUMN IF NOT EXISTS pw_salt TEXT;
-- SAVED SEARCHES: a signed-in user's query history (auto-recorded by the FE on each search).
CREATE TABLE IF NOT EXISTS roster_saved_search (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    query       TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'research',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rss_user ON roster_saved_search (user_id, created_at DESC);
-- SHORTLIST BUCKETS: custom named buckets to collect people & jobs across searches.
CREATE TABLE IF NOT EXISTS roster_bucket (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rb_user ON roster_bucket (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS roster_bucket_item (
    id          BIGSERIAL PRIMARY KEY,
    bucket_id   BIGINT NOT NULL REFERENCES roster_bucket(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- 'person' | 'job'
    ref_id      TEXT NOT NULL,            -- entity_id (person) or job id
    label       TEXT,                     -- display name captured at save time
    payload     JSONB,                    -- snapshot (blurb/url/etc) so it survives source drift
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bucket_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_rbi_bucket ON roster_bucket_item (bucket_id, created_at DESC);
-- CANDIDATE PROFILE for "smooth apply": one superset profile per user (the union of what ATSes ask),
-- stored as JSONB so the field set can grow without migrations, plus an optional resume file. Sensitive
-- fields (work authorization, EEO/demographics) live inside `profile`; the FE keeps demographics OFF by
-- default and they are only surfaced to an ATS that explicitly asks. PII — never logged.
CREATE TABLE IF NOT EXISTS roster_candidate_profile (
    user_id       TEXT PRIMARY KEY,
    profile       JSONB NOT NULL DEFAULT '{}'::jsonb,
    resume_name   TEXT,
    resume_type   TEXT,
    resume_bytes  BYTEA,
    resume_at     TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- OUTREACH LOG: a record each time the user drafts/opens a message to someone via a channel (email/
-- linkedin/x/github/…). Roster never sends on their behalf — this just tracks who they contacted for
-- follow-up. person_ref = the person's entity_id.
CREATE TABLE IF NOT EXISTS roster_outreach (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    person_ref   TEXT NOT NULL,
    person_name  TEXT,
    channel      TEXT NOT NULL,
    message      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ro_user ON roster_outreach (user_id, created_at DESC);
-- RÉSUMÉ → PROFILE autofill: docling+LLM parse runs in a SEPARATE process; these columns hold its
-- status and the suggested fields (the FE prefills the form from parsed_profile for the user to review).
ALTER TABLE roster_candidate_profile ADD COLUMN IF NOT EXISTS parse_status TEXT;      -- pending|done|failed
ALTER TABLE roster_candidate_profile ADD COLUMN IF NOT EXISTS parsed_profile JSONB;
ALTER TABLE roster_candidate_profile ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ;
-- LINKEDIN CONNECTIONS (intro path): the user's OWN export (LinkedIn → Settings → Data privacy → Get a
-- copy of your data → Connections.csv), uploaded once, private to the account, deletable. Used only
-- to answer "who do I know at <company>" on a job card. Never scraped, never shared, never a search
-- signal for other users.
CREATE TABLE IF NOT EXISTS roster_connection (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    first_name    TEXT NOT NULL DEFAULT '',
    last_name     TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    company_norm  TEXT NOT NULL DEFAULT '',
    position      TEXT NOT NULL DEFAULT '',
    connected_on  TEXT NOT NULL DEFAULT '',
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rc_user_co ON roster_connection (user_id, company_norm);
-- APPLICATIONS (controlled auto-apply): one row per posting the user queued. The agent fills and
-- screenshots (status filled / needs_you); the user approves; only then it submits. Tracks what was
-- applied to, where, when, with what answers. Screenshot = the filled form as the user saw it.
CREATE TABLE IF NOT EXISTS roster_application (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    job_ref       TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL,
    ats           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued|filled|needs_you|approved|submitted|failed
    reason        TEXT NOT NULL DEFAULT '',
    filled        JSONB NOT NULL DEFAULT '[]'::jsonb,
    open_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    answers       JSONB NOT NULL DEFAULT '{}'::jsonb,
    drafts        JSONB NOT NULL DEFAULT '{}'::jsonb,
    screenshot    BYTEA,
    submitted_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ra_user ON roster_application (user_id, created_at DESC);
"""

_MAX_TOKENS_PER_USER = 10   # prune oldest beyond this (a lost device's token eventually ages out)



def _norm_company(name: str) -> str:
    """'Stripe, Inc.' / 'STRIPE' / 'Stripe Inc' → 'stripe' — the same loose key the job index uses for companies."""
    import re as _re
    s = _re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    s = _re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|sa|ag|the)\b", " ", s)
    return _re.sub(r"\s+", " ", s).strip().replace(" ", "_")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


import hmac as _hmac   # noqa: E402

_PBKDF2_ITER = 240_000


def hash_password(password: str) -> tuple[str, str]:
    """(pw_hash_hex, pw_salt_hex) via PBKDF2-HMAC-SHA256, 240k iters, 16-byte random salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return dk.hex(), salt.hex()


def verify_password(password: str, pw_hash_hex: str, pw_salt_hex: str) -> bool:
    """Constant-time verify. Always runs the KDF (caller passes a dummy salt for unknown users)."""
    try:
        salt = bytes.fromhex(pw_salt_hex or "00" * 16)
    except ValueError:
        salt = b"\x00" * 16
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return _hmac.compare_digest(dk.hex(), pw_hash_hex or "")


class AccountStore:
    """Async Postgres store for users + feedback, vertical-scoped like SessionStore."""

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

    async def email_exists(self, email: str) -> bool:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM roster_user WHERE vertical=$1 AND email=$2",
                self._vertical, email.lower().strip()))

    async def register(self, *, email: str, name: str, profession: str = "", country: str = "",
                       npi: str = "", npi_verified: bool = False, disclaimer_ack: bool = False,
                       pw_hash: str = "", pw_salt: str = "") -> tuple[dict[str, Any], str]:
        """Upsert-on-email; rotates the token every call (re-registering re-claims the account —
        acceptable at alpha, see module docstring). Password (optional) is stored only when supplied,
        and never overwritten with blanks on a later token-only re-register. Returns (public user
        dict, RAW token — shown once)."""
        await self._ensure()
        pool = await self._get_pool()
        token = secrets.token_urlsafe(32)
        uid = uuid.uuid4().hex
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO roster_user (id, vertical, email, name, profession, country, npi,
                                            npi_verified, token_hash, disclaimer_ack_at, pw_hash, pw_salt)
                   VALUES ($1,$2,$3,$4,$5,$6,NULLIF($7,''),$8,$9, CASE WHEN $10 THEN now() END,
                           NULLIF($11,''), NULLIF($12,''))
                   ON CONFLICT (vertical, email) DO UPDATE SET
                     name=EXCLUDED.name, profession=EXCLUDED.profession, country=EXCLUDED.country,
                     npi=COALESCE(EXCLUDED.npi, roster_user.npi),
                     npi_verified=(roster_user.npi_verified OR EXCLUDED.npi_verified),
                     token_hash=EXCLUDED.token_hash,
                     disclaimer_ack_at=COALESCE(roster_user.disclaimer_ack_at, EXCLUDED.disclaimer_ack_at),
                     pw_hash=COALESCE(EXCLUDED.pw_hash, roster_user.pw_hash),
                     pw_salt=COALESCE(EXCLUDED.pw_salt, roster_user.pw_salt),
                     last_seen=now()
                   RETURNING id, email, name, profession, country, npi_verified, created_at""",
                uid, self._vertical, email.lower().strip(), name.strip(), profession.strip(),
                country.strip(), npi.strip(), npi_verified, _hash(token), disclaimer_ack,
                pw_hash, pw_salt)
        # PER-DEVICE: this registration's token is ADDED to the user's active set (the legacy
        # column above still rotates for back-compat, but auth checks this table first — so the
        # previous device's token, if it's in the table, keeps working).
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO roster_user_token (token_hash, user_id) VALUES ($1,$2) "
                "ON CONFLICT DO NOTHING", _hash(token), row["id"])
            await conn.execute(
                """DELETE FROM roster_user_token WHERE user_id=$1 AND token_hash NOT IN (
                     SELECT token_hash FROM roster_user_token WHERE user_id=$1
                     ORDER BY created_at DESC LIMIT $2)""", row["id"], _MAX_TOKENS_PER_USER)
        user = {"id": row["id"], "email": row["email"], "name": row["name"],
                "profession": row["profession"], "country": row["country"],
                "verified": row["npi_verified"]}
        return user, token

    async def user_by_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        await self._ensure()
        pool = await self._get_pool()
        h = _hash(token)
        async with pool.acquire() as conn:
            # per-device table first; legacy single-column hash as fallback (pre-existing sessions)
            row = await conn.fetchrow(
                """SELECT u.id, u.email, u.name, u.profession, u.country, u.npi_verified
                   FROM roster_user_token t JOIN roster_user u ON u.id = t.user_id
                   WHERE t.token_hash=$2 AND u.vertical=$1""", self._vertical, h)
            if row:
                await conn.execute(
                    "UPDATE roster_user_token SET last_seen=now() WHERE token_hash=$1", h)
                await conn.execute("UPDATE roster_user SET last_seen=now() WHERE id=$1", row["id"])
            else:
                row = await conn.fetchrow(
                    """UPDATE roster_user SET last_seen=now()
                       WHERE vertical=$1 AND token_hash=$2
                       RETURNING id, email, name, profession, country, npi_verified""",
                    self._vertical, h)
        if not row:
            return None
        return {"id": row["id"], "email": row["email"], "name": row["name"],
                "profession": row["profession"], "country": row["country"],
                "verified": row["npi_verified"]}

    async def add_feedback(self, *, user: dict | None, session_id: str, turn_index: int,
                           verdict: str, modes: list[str], claim_index: int | None,
                           note: str, question: str) -> str:
        await self._ensure()
        pool = await self._get_pool()
        fid = uuid.uuid4().hex
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_feedback (id, vertical, user_id, user_email, session_id,
                                                turn_index, verdict, modes, claim_index, note, question)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)""",
                fid, self._vertical, (user or {}).get("id"), (user or {}).get("email"),
                session_id[:64], turn_index, verdict, json.dumps(modes), claim_index,
                note[:2000], question[:1000])
        return fid

    async def feedback_summary(self, *, limit: int = 25) -> dict[str, Any]:
        """The accumulating signal, aggregated: totals, per-verdict, per-W-mode, per-day, recent rows.
        This is what 'watch what feedback is building up over time' reads."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM roster_feedback WHERE vertical=$1", self._vertical)
            by_verdict = {r["verdict"]: r["n"] for r in await conn.fetch(
                "SELECT verdict, count(*) n FROM roster_feedback WHERE vertical=$1 GROUP BY 1",
                self._vertical)}
            by_mode = {r["mode"]: r["n"] for r in await conn.fetch(
                """SELECT jsonb_array_elements_text(modes) AS mode, count(*) n
                   FROM roster_feedback WHERE vertical=$1 GROUP BY 1 ORDER BY 2 DESC""",
                self._vertical)}
            by_day = [{"day": r["day"].isoformat(), "n": r["n"]} for r in await conn.fetch(
                """SELECT created_at::date AS day, count(*) n FROM roster_feedback
                   WHERE vertical=$1 GROUP BY 1 ORDER BY 1 DESC LIMIT 30""", self._vertical)]
            recent = [dict(r) for r in await conn.fetch(
                """SELECT id, user_email, session_id, turn_index, verdict, modes::text AS modes,
                          claim_index, note, question, created_at
                   FROM roster_feedback WHERE vertical=$1
                   ORDER BY created_at DESC LIMIT $2""", self._vertical, limit)]
        for r in recent:
            r["modes"] = json.loads(r["modes"] or "[]")
            r["created_at"] = r["created_at"].isoformat()
        n_users = None
        async with (await self._get_pool()).acquire() as conn:
            n_users = await conn.fetchval(
                "SELECT count(*) FROM roster_user WHERE vertical=$1", self._vertical)
            n_verified = await conn.fetchval(
                "SELECT count(*) FROM roster_user WHERE vertical=$1 AND npi_verified", self._vertical)
        return {"total": total, "by_verdict": by_verdict, "by_mode": by_mode,
                "by_day": by_day, "recent": recent,
                "users": {"registered": n_users, "npi_verified": n_verified}}

    async def list_users(self, *, limit: int = 500) -> list[dict]:
        """All registered users (newest first) — name, email, profession, country, NPI-verified,
        registered + last-seen timestamps. Admin-only (PII); the caller gates access."""
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                """SELECT name, email, profession, country, npi_verified,
                          created_at, last_seen
                   FROM roster_user WHERE vertical=$1 ORDER BY created_at DESC LIMIT $2""",
                self._vertical, int(limit))
        out = []
        for r in rows:
            d = dict(r)
            for k in ("created_at", "last_seen"):
                if d.get(k) is not None:
                    d[k] = d[k].isoformat()
            out.append(d)
        return out

    async def accounts_overview(self, *, limit: int = 500) -> list[dict]:
        """Registered accounts with what each has on file — résumé, saved Talent Maps, searches — for
        the admin listing. PII: the caller gates access (panel password AND an admin account)."""
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                """SELECT u.id, u.name, u.email, u.created_at, u.last_seen, (u.pw_hash IS NOT NULL) AS has_password,
                          EXISTS (SELECT 1 FROM roster_candidate_profile p WHERE p.user_id = u.id AND p.resume_bytes IS NOT NULL) AS has_resume,
                          (SELECT count(*) FROM rs_map m WHERE m.owner_id = u.id) AS maps,
                          (SELECT count(*) FROM roster_research_session s WHERE s.user_email = u.email) AS searches
                   FROM roster_user u WHERE u.vertical=$1 ORDER BY u.created_at DESC LIMIT $2""",
                self._vertical, int(limit))
        out = []
        for r in rows:
            d = dict(r)
            for k in ("created_at", "last_seen"):
                if d.get(k) is not None:
                    d[k] = d[k].isoformat()
            out.append(d)
        return out

    async def first_user_email(self) -> str:
        """The earliest-registered account (the owner) — the default admin when ROSTER_ADMIN_EMAILS is unset."""
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            return (await conn.fetchval(
                "SELECT email FROM roster_user WHERE vertical=$1 ORDER BY created_at ASC LIMIT 1", self._vertical)) or ""

    # ---- per-user preferences (Roster IN D-7: the server-authoritative profile substrate) ----

    async def get_pref(self, user_id: str, key: str) -> str:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            v = await conn.fetchval(
                "SELECT value FROM roster_user_pref WHERE user_id=$1 AND key=$2", user_id, key)
        return v or ""

    async def set_pref(self, user_id: str, key: str, value: str) -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_user_pref (user_id, key, value) VALUES ($1,$2,$3)
                   ON CONFLICT (user_id, key) DO UPDATE SET value=EXCLUDED.value,
                   updated_at=now()""", user_id, key[:64], (value or "")[:60000])

    # ---- password login (email + password → fresh per-device token) ----
    async def login(self, *, email: str, password: str) -> tuple[dict[str, Any], str] | None:
        """Verify email+password (constant-time) and issue a new token. None on bad credentials or
        an account with no password set (token-only legacy user). Never logs the password."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, email, name, profession, country, npi_verified, pw_hash, pw_salt
                   FROM roster_user WHERE vertical=$1 AND email=$2""",
                self._vertical, email.lower().strip())
        # run the KDF even when the user is unknown / passwordless to avoid timing enumeration
        ok = verify_password(password, (row["pw_hash"] if row else "") or "",
                             (row["pw_salt"] if row else "") or "")
        if not row or not row["pw_hash"] or not ok:
            return None
        token = secrets.token_urlsafe(32)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO roster_user_token (token_hash, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                _hash(token), row["id"])
            await conn.execute(
                """DELETE FROM roster_user_token WHERE user_id=$1 AND token_hash NOT IN (
                     SELECT token_hash FROM roster_user_token WHERE user_id=$1
                     ORDER BY created_at DESC LIMIT $2)""", row["id"], _MAX_TOKENS_PER_USER)
            await conn.execute("UPDATE roster_user SET last_seen=now() WHERE id=$1", row["id"])
        user = {"id": row["id"], "email": row["email"], "name": row["name"],
                "profession": row["profession"], "country": row["country"], "verified": row["npi_verified"]}
        return user, token

    async def logout(self, token: str) -> None:
        if not token:
            return
        await self._ensure()
        h = _hash(token)
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute("DELETE FROM roster_user_token WHERE token_hash=$1", h)
            # ALSO clear the legacy single-column token, or user_by_token's fallback keeps it valid
            await conn.execute("UPDATE roster_user SET token_hash='' WHERE token_hash=$1", h)

    # ---- saved searches ----
    async def add_search(self, user_id: str, query: str, mode: str = "research") -> None:
        q = (query or "").strip()
        if not q:
            return
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            last = await conn.fetchval(
                "SELECT query FROM roster_saved_search WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1",
                user_id)
            if last != q[:2000]:   # skip consecutive duplicate re-runs
                await conn.execute(
                    "INSERT INTO roster_saved_search (user_id, query, mode) VALUES ($1,$2,$3)",
                    user_id, q[:2000], (mode or "research")[:24])

    async def list_searches(self, user_id: str, *, limit: int = 200) -> list[dict]:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, query, mode, created_at FROM roster_saved_search "
                "WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2", user_id, int(limit))
        return [{"id": r["id"], "query": r["query"], "mode": r["mode"],
                 "at": r["created_at"].isoformat()} for r in rows]

    # ---- shortlist buckets ----
    async def list_buckets(self, user_id: str) -> list[dict]:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                """SELECT b.id, b.name, b.created_at, count(i.id) n
                   FROM roster_bucket b LEFT JOIN roster_bucket_item i ON i.bucket_id=b.id
                   WHERE b.user_id=$1 GROUP BY b.id ORDER BY b.created_at DESC""", user_id)
        return [{"id": r["id"], "name": r["name"], "count": r["n"],
                 "at": r["created_at"].isoformat()} for r in rows]

    async def create_bucket(self, user_id: str, name: str) -> dict:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            bid = await conn.fetchval(
                "INSERT INTO roster_bucket (user_id, name) VALUES ($1,$2) RETURNING id",
                user_id, (name or "").strip()[:120])
        return {"id": bid, "name": (name or "").strip()[:120], "count": 0}

    async def delete_bucket(self, user_id: str, bucket_id: int) -> bool:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            res = await conn.execute(
                "DELETE FROM roster_bucket WHERE id=$1 AND user_id=$2", int(bucket_id), user_id)
        return res.endswith(" 1")

    async def _owns_bucket(self, conn, user_id: str, bucket_id: int) -> bool:
        return bool(await conn.fetchval(
            "SELECT 1 FROM roster_bucket WHERE id=$1 AND user_id=$2", int(bucket_id), user_id))

    async def list_items(self, user_id: str, bucket_id: int) -> list[dict] | None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            if not await self._owns_bucket(conn, user_id, bucket_id):
                return None
            rows = await conn.fetch(
                "SELECT id, kind, ref_id, label, payload, created_at FROM roster_bucket_item "
                "WHERE bucket_id=$1 ORDER BY created_at DESC", int(bucket_id))
        return [{"id": r["id"], "kind": r["kind"], "ref_id": r["ref_id"], "label": r["label"],
                 "payload": json.loads(r["payload"]) if r["payload"] else None,
                 "at": r["created_at"].isoformat()} for r in rows]

    async def add_item(self, user_id: str, bucket_id: int, *, kind: str, ref_id: str,
                       label: str = "", payload: dict | None = None) -> bool:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            if not await self._owns_bucket(conn, user_id, bucket_id):
                return False
            await conn.execute(
                """INSERT INTO roster_bucket_item (bucket_id, kind, ref_id, label, payload)
                   VALUES ($1,$2,$3,$4,$5)
                   ON CONFLICT (bucket_id, kind, ref_id)
                   DO UPDATE SET label=EXCLUDED.label, payload=EXCLUDED.payload""",
                int(bucket_id), kind, ref_id[:200], (label or "")[:300],
                json.dumps(payload) if payload else None)
        return True

    async def delete_item(self, user_id: str, bucket_id: int, item_id: int) -> bool:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            res = await conn.execute(
                """DELETE FROM roster_bucket_item i USING roster_bucket b
                   WHERE i.id=$1 AND i.bucket_id=b.id AND b.id=$2 AND b.user_id=$3""",
                int(item_id), int(bucket_id), user_id)
        return res.endswith(" 1")

    # ---- candidate profile for smooth apply (superset of ATS fields) ----
    async def get_profile(self, user_id: str) -> dict:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                "SELECT profile, resume_name, resume_type, resume_at, updated_at "
                "FROM roster_candidate_profile WHERE user_id=$1", user_id)
        if not row:
            return {"profile": {}, "resume": None}
        return {"profile": json.loads(row["profile"]) if row["profile"] else {},
                "resume": ({"name": row["resume_name"], "type": row["resume_type"],
                            "at": row["resume_at"].isoformat() if row["resume_at"] else None}
                           if row["resume_name"] else None),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}

    async def set_profile(self, user_id: str, profile: dict) -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_candidate_profile (user_id, profile, updated_at)
                   VALUES ($1, $2::jsonb, now())
                   ON CONFLICT (user_id) DO UPDATE SET profile=EXCLUDED.profile, updated_at=now()""",
                user_id, json.dumps(profile or {}))

    async def set_resume(self, user_id: str, *, name: str, ctype: str, data: bytes) -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_candidate_profile (user_id, resume_name, resume_type, resume_bytes, resume_at)
                   VALUES ($1,$2,$3,$4, now())
                   ON CONFLICT (user_id) DO UPDATE SET resume_name=EXCLUDED.resume_name,
                     resume_type=EXCLUDED.resume_type, resume_bytes=EXCLUDED.resume_bytes,
                     resume_at=now(), updated_at=now()""",
                user_id, name[:200], ctype[:120], data)

    async def get_resume(self, user_id: str) -> "tuple[str, str, bytes] | None":
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                "SELECT resume_name, resume_type, resume_bytes FROM roster_candidate_profile WHERE user_id=$1",
                user_id)
        if not row or not row["resume_bytes"]:
            return None
        return (row["resume_name"] or "resume", row["resume_type"] or "application/octet-stream",
                bytes(row["resume_bytes"]))

    # ---- résumé→profile parse status (the docling+LLM work runs in a SEPARATE process) ----
    # ---- LinkedIn connections (the user's own export; intro path) ----
    async def replace_connections(self, user_id: str, rows: list[dict]) -> int:
        """Replace the user's connections with a fresh export (idempotent re-upload). Returns rows kept."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM roster_connection WHERE user_id = $1", user_id)
                n = 0
                for r in rows[:20000]:
                    co = (r.get("company") or "").strip()
                    if not (r.get("first_name") or r.get("last_name")):
                        continue
                    await conn.execute(
                        """INSERT INTO roster_connection (user_id, first_name, last_name, url, company, company_norm, position, connected_on)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                        user_id, (r.get("first_name") or "")[:80], (r.get("last_name") or "")[:80], (r.get("url") or "")[:300],
                        co[:160], _norm_company(co), (r.get("position") or "")[:160], (r.get("connected_on") or "")[:40])
                    n += 1
        return n

    async def connections_summary(self, user_id: str) -> dict:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT count(*) n, count(DISTINCT company_norm) cos, max(uploaded_at) at FROM roster_connection WHERE user_id = $1", user_id)
        return {"count": int(r["n"] or 0), "companies": int(r["cos"] or 0), "uploaded_at": (str(r["at"]) if r["at"] else None)}

    async def connections_at(self, user_id: str, company: str, *, limit: int = 25) -> list[dict]:
        """The user's connections whose CURRENT company (as exported) is this company — exact normalized
        match first, then a contains-match for suffix variants ('Stripe' vs 'Stripe, Inc.')."""
        await self._ensure()
        n = _norm_company(company)
        if not n:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT first_name, last_name, url, company, position, connected_on,
                          (company_norm = $2) AS exact
                   FROM roster_connection WHERE user_id = $1
                     AND (company_norm = $2 OR (length($2) >= 4 AND company_norm LIKE $2 || '%'))
                   ORDER BY exact DESC, connected_on DESC LIMIT $3""", user_id, n, int(limit))
        return [{"name": (r["first_name"] + " " + r["last_name"]).strip(), "url": r["url"], "company": r["company"],
                 "position": r["position"], "connected_on": r["connected_on"], "exact": bool(r["exact"])} for r in rows]

    async def delete_connections(self, user_id: str) -> None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM roster_connection WHERE user_id = $1", user_id)

    # ---- applications (controlled auto-apply) ----
    async def queue_application(self, user_id: str, *, job_ref: str, company: str, title: str, url: str, ats: str) -> dict:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            r = await conn.fetchrow(
                """INSERT INTO roster_application (user_id, job_ref, company, title, url, ats)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id, status, created_at""",
                user_id, job_ref[:300], company[:160], title[:200], url[:1000], ats[:40])
        return {"id": int(r["id"]), "status": r["status"], "created_at": str(r["created_at"])}

    async def update_application(self, user_id: str, app_id: int, **fields) -> bool:
        """Set any of: status, reason, filled, open_questions, answers, drafts, screenshot (bytes), submitted_at."""
        await self._ensure()
        allowed = {"status", "reason", "filled", "open_questions", "answers", "drafts", "screenshot", "submitted_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            vals.append(json.dumps(v) if k in ("filled", "open_questions", "answers", "drafts") else v)
            sets.append(f"{k} = ${len(vals) + 2}" + ("::jsonb" if k in ("filled", "open_questions", "answers", "drafts") else ""))
        if not sets:
            return False
        async with (await self._get_pool()).acquire() as conn:
            res = await conn.execute(f"UPDATE roster_application SET {', '.join(sets)}, updated_at = now() WHERE user_id = $1 AND id = $2",
                                     user_id, int(app_id), *vals)
        return res.endswith("1")

    async def list_applications(self, user_id: str, *, limit: int = 200) -> list[dict]:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, job_ref, company, title, url, ats, status, reason, submitted_at, created_at, updated_at,
                          jsonb_array_length(open_questions) AS n_open, (screenshot IS NOT NULL) AS has_shot
                   FROM roster_application WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2""", user_id, int(limit))
        return [{**dict(r), "submitted_at": (str(r["submitted_at"]) if r["submitted_at"] else None),
                 "created_at": str(r["created_at"]), "updated_at": str(r["updated_at"])} for r in rows]

    async def get_application(self, user_id: str, app_id: int) -> dict | None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM roster_application WHERE user_id = $1 AND id = $2", user_id, int(app_id))
        if not r:
            return None
        d = dict(r)
        for k in ("filled", "open_questions", "answers", "drafts"):
            d[k] = json.loads(d[k]) if isinstance(d[k], str) else (d[k] or ([] if k in ("filled", "open_questions") else {}))
        shot = d.pop("screenshot", None)
        d["screenshot_b64"] = base64.b64encode(shot).decode() if shot else ""
        for k in ("submitted_at", "created_at", "updated_at"):
            d[k] = str(d[k]) if d.get(k) else None
        return d

    async def delete_application(self, user_id: str, app_id: int) -> bool:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            res = await conn.execute("DELETE FROM roster_application WHERE user_id = $1 AND id = $2", user_id, int(app_id))
        return res.endswith("1")

    async def set_parse_pending(self, user_id: str) -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                "UPDATE roster_candidate_profile SET parse_status='pending', parsed_profile=NULL, "
                "parsed_at=NULL WHERE user_id=$1", user_id)

    async def save_parsed(self, user_id: str, profile: dict, status: str = "done") -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                "UPDATE roster_candidate_profile SET parse_status=$2, parsed_profile=$3::jsonb, "
                "parsed_at=now() WHERE user_id=$1", user_id, status, json.dumps(profile or {}))

    async def get_parse(self, user_id: str) -> dict:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parse_status, parsed_profile, parsed_at FROM roster_candidate_profile WHERE user_id=$1",
                user_id)
        if not row:
            return {"status": None, "profile": {}}
        return {"status": row["parse_status"],
                "profile": json.loads(row["parsed_profile"]) if row["parsed_profile"] else {},
                "at": row["parsed_at"].isoformat() if row["parsed_at"] else None}

    # ---- outreach log (Roster never sends; this records what the user drafted/opened for follow-up) ----
    async def add_outreach(self, user_id: str, *, person_ref: str, person_name: str,
                           channel: str, message: str) -> None:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            await conn.execute(
                "INSERT INTO roster_outreach (user_id, person_ref, person_name, channel, message) "
                "VALUES ($1,$2,$3,$4,$5)",
                user_id, person_ref[:200], (person_name or "")[:300], channel[:32], (message or "")[:4000])

    async def list_outreach(self, user_id: str, *, limit: int = 200) -> list[dict]:
        await self._ensure()
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, person_ref, person_name, channel, message, created_at FROM roster_outreach "
                "WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2", user_id, int(limit))
        return [{"id": r["id"], "person_ref": r["person_ref"], "person_name": r["person_name"],
                 "channel": r["channel"], "message": r["message"],
                 "at": r["created_at"].isoformat()} for r in rows]
