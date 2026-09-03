"""CROSS-SOURCE IDENTITY LINKS — one person across GitHub, OpenAlex, TheOrg, YC, SEC identities.

Roster mints one entity per source (github:login, openalex:A…, theorg:…). A link joins two of them
so a person's papers AND repos show on one card. Links are written ONLY by code, on evidence, and
never by name alone (the design invariant: same-named people stay separate):

  name+employer  — the full name matches AND both identities carry the same normalized employer /
                   affiliation, AND the name is UNIQUE on both sides (≤ `max_dupes` same-named
                   identities per source) so a common name at a big employer is never merged.

Each link records its method, confidence and the evidence (the shared employer), and can be
revoked. Read side: `links_for` returns the linked ids per entity so artifacts merge across them,
labeled 'via linked identity' in the Rail. App-level module; the kernel stays domain-free.
"""
from __future__ import annotations

import json
import logging

_log = logging.getLogger("roster.identity")

_DDL = """
CREATE TABLE IF NOT EXISTS rs_person_link (
    a_id        text NOT NULL,
    b_id        text NOT NULL,
    method      text NOT NULL,                  -- name+employer | orcid | website | manual
    confidence  real NOT NULL DEFAULT 0.7,
    evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status      text NOT NULL DEFAULT 'active', -- active | revoked
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (a_id, b_id)
);
CREATE INDEX IF NOT EXISTS ix_rs_person_link_b ON rs_person_link (b_id);
"""

# employers too generic to identify a person (a shared 'freelance' proves nothing)
_GENERIC_EMPLOYERS = {"freelance", "freelancer", "self", "self_employed", "independent", "student",
                      "none", "n_a", "na", "home", "personal", "private", "unknown", "various",
                      "open_source", "opensource", "consultant", "consulting", "remote"}

_PAIR_SQL = """
WITH names AS (
    SELECT lower(name) AS nm, split_part(entity_id, ':', 1) AS src, entity_id, name
    FROM rs_entity WHERE kind = 'person' AND status = 'active' AND name ~ '\\s'
),
dupes AS (SELECT n.nm, n.src, count(*) AS n,
                 count(DISTINCT fc.facet_value_norm) AS n_employers
          FROM names n
          LEFT JOIN roster_entity_facet fc ON fc.entity_id = n.entity_id AND fc.facet_key = 'company'
          GROUP BY n.nm, n.src)
SELECT g.entity_id AS a_id, o.entity_id AS b_id, g.name AS name, fg.facet_value_norm AS employer,
       da.n AS n_a, db.n AS n_b, da.n_employers AS emp_a, db.n_employers AS emp_b
FROM names g
JOIN names o ON o.nm = g.nm AND o.src = ANY($2)
     AND ((o.src <> g.src) OR (o.src = g.src AND o.entity_id > g.entity_id))
JOIN roster_entity_facet fg ON fg.entity_id = g.entity_id AND fg.facet_key IN ('company','worked_at')
JOIN roster_entity_facet fo ON fo.entity_id = o.entity_id AND fo.facet_key IN ('company','worked_at')
     AND fo.facet_value_norm = fg.facet_value_norm
JOIN dupes da ON da.nm = g.nm AND da.src = g.src
JOIN dupes db ON db.nm = o.nm AND db.src = o.src
LEFT JOIN rs_person_link l ON (l.a_id = g.entity_id AND l.b_id = o.entity_id)
WHERE g.src = $1 AND length(fg.facet_value_norm) >= 4 AND l.a_id IS NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""


def acceptable(row: dict, *, max_dupes: int = 3) -> bool:
    """CODE-OWNED acceptance: a unique-enough name on both sides and a non-generic shared employer.
    'Unique enough' = at most `max_dupes` same-named identities per source — OR, for a SAME-SOURCE
    split, every namesake in that source carries the ONE same employer (eleven 'Simon Kornblith'
    OpenAlex ids, all Anthropic, are one person; a common name spans many employers and stays apart)."""
    emp = (row.get("employer") or "").strip().lower()
    if not emp or emp in _GENERIC_EMPLOYERS or len(emp) < 4:
        return False
    same_source = (row.get("a_id") or "").split(":", 1)[0] == (row.get("b_id") or "").split(":", 1)[0]
    def _unique(n, n_emp):
        n = int(n or 99)
        return n <= max_dupes or (same_source and int(n_emp or 99) == 1)
    if not (_unique(row.get("n_a"), row.get("emp_a")) and _unique(row.get("n_b"), row.get("emp_b"))):
        return False
    return len((row.get("name") or "").split()) >= 2


async def ensure_schema(conn) -> None:
    await conn.execute(_DDL)


async def find_and_write_links(conn, *, src: str = "github", others: tuple[str, ...] = ("openalex", "theorg", "yc", "sec"),
                               max_dupes: int = 3, dry: bool = False) -> dict:
    """Link `src` identities to same-person identities in `others` by name + shared employer.
    `others` may include `src` itself: SAME-SOURCE SPLITS (OpenAlex mints several author ids for one
    person — three 'Jack Lindsey' ids all affiliated with Anthropic) link the same way, method
    'name+employer:split', so one card shows instead of three."""
    await ensure_schema(conn)
    rows = [dict(r) for r in await conn.fetch(_PAIR_SQL, src, list(others))]
    kept = [r for r in rows if acceptable(r, max_dupes=max_dupes)]
    if not dry:
        for r in kept:
            same = r["a_id"].split(":", 1)[0] == r["b_id"].split(":", 1)[0]
            await conn.execute(
                """INSERT INTO rs_person_link (a_id, b_id, method, confidence, evidence)
                   VALUES ($1, $2, $4, 0.7, $3::jsonb)
                   ON CONFLICT (a_id, b_id) DO NOTHING""",
                r["a_id"], r["b_id"], json.dumps({"name": r["name"], "employer": r["employer"],
                                                  "same_name_count": [r["n_a"], r["n_b"]]}),
                "name+employer:split" if same else "name+employer")
    return {"candidates": len(rows), "linked": len(kept), "rejected": len(rows) - len(kept),
            "sample": [(r["a_id"], r["b_id"], r["employer"]) for r in kept[:5]]}


async def links_for(pool, entity_ids: list[str]) -> dict[str, list[dict]]:
    """{entity_id: [{id, method, confidence, evidence}]} for active links touching each id."""
    ids = [e for e in (entity_ids or []) if e]
    if not ids:
        return {}
    try:
        async with pool.acquire() as conn:
            await ensure_schema(conn)
            rows = await conn.fetch(
                """SELECT a_id, b_id, method, confidence, evidence FROM rs_person_link
                   WHERE status = 'active' AND (a_id = ANY($1) OR b_id = ANY($1))""", ids)
    except Exception as e:  # noqa: BLE001
        _log.info("links_for failed: %s", e)
        return {}
    out: dict[str, list[dict]] = {}
    want = set(ids)
    for r in rows:
        ev = r["evidence"]
        ev = json.loads(ev) if isinstance(ev, str) else (ev or {})
        for me, other in ((r["a_id"], r["b_id"]), (r["b_id"], r["a_id"])):
            if me in want:
                out.setdefault(me, []).append({"id": other, "method": r["method"],
                                               "confidence": float(r["confidence"]), "evidence": ev})
    return out
