"""PUBLIC ARTIFACTS per person — evidence-model-v2 step 1 (docs/specs/evidence-model-v2.md §5.1).

A person's public FOOTPRINT — papers, repositories, org memberships (later: patents, talks, podcasts,
posts) — linked to the indexed person by a STRONG IDENTITY KEY only:
  - openalex:A…  → works filtered by that author id (the registry's own key, no name matching);
  - github:login → the login's own public repositories and public org memberships.
Name-only matches are never written here (the design invariant: never merge same-named people).

Each artifact is DATED and LINKED, which is what the facet read-model lacks: it gives the Evidence
Rail a freshness axis, an "artifact-backed" capability rung, and a footprint-count strip that makes
per-person coverage gaps visible. Everything in this module is code-derived from source metadata —
no model text. App-level module (recruiting vocabulary allowed); the kernel stays domain-free.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

_log = logging.getLogger("roster.artifacts")

_DDL = """
CREATE TABLE IF NOT EXISTS rs_person_artifact (
    entity_id     text NOT NULL,
    kind          text NOT NULL,             -- paper | repo | org | patent | post | talk | podcast | press
    artifact_key  text NOT NULL,             -- stable per source: openalex work id / repo full_name / org login
    title         text NOT NULL DEFAULT '',
    url           text NOT NULL DEFAULT '',
    date          date,                      -- publication / last-push date (the freshness axis)
    venue         text NOT NULL DEFAULT '',  -- journal or conference | repo language | ''
    role          text NOT NULL DEFAULT '',  -- author | first_author | owner | member | inventor | speaker
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- citations/stars/affiliations — source metadata only
    link_method   text NOT NULL DEFAULT '',  -- author_id | login  (strong keys only; never name_candidate here)
    confidence    real NOT NULL DEFAULT 1,
    source_family text NOT NULL DEFAULT '',
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, kind, artifact_key)
);
CREATE INDEX IF NOT EXISTS ix_rs_person_artifact_ent ON rs_person_artifact (entity_id);
CREATE TABLE IF NOT EXISTS rs_artifact_scan (
    entity_id  text NOT NULL,
    source     text NOT NULL,                -- openalex | github
    status     text NOT NULL DEFAULT 'done', -- done | error
    n_found    int NOT NULL DEFAULT 0,
    n_total    int NOT NULL DEFAULT 0,       -- source-reported total (works count / public repos)
    scanned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, source)
);
"""

KINDS = ("paper", "repo", "org", "patent", "post", "talk", "podcast", "press")
KIND_LABELS = {"paper": "papers", "repo": "repos", "org": "orgs", "patent": "patents", "post": "posts",
               "talk": "talks", "podcast": "podcasts", "press": "press"}
# the kinds that evidence CAPABILITY (org membership evidences affiliation, not capability)
_CAPABILITY_KINDS = {"paper", "repo", "patent", "talk", "post"}
_TOP_ITEMS = 8


# --------------------------------------------------------------------------- #
# Parsers: source metadata → artifact row (pure; unit-tested)                  #
# --------------------------------------------------------------------------- #
def _tail(s: str) -> str:
    return (s or "").rstrip("/").rsplit("/", 1)[-1]


def openalex_work_artifact(work: dict, author_id: str) -> dict | None:
    """One OpenAlex work → a 'paper' artifact for the author whose OpenAlex id is `author_id`.
    Role is first_author when the author holds the first authorship; the author's institutions on
    THIS work (a dated affiliation) go into detail.affiliations."""
    wid = _tail(work.get("id") or "")
    title = (work.get("display_name") or work.get("title") or "").strip()
    if not wid or not title:
        return None
    role, affs = "author", []
    for i, a in enumerate(work.get("authorships") or []):
        if _tail((a.get("author") or {}).get("id") or "") == author_id:
            if i == 0 or (a.get("author_position") or "") == "first":
                role = "first_author"
            affs = [(inst.get("display_name") or "").strip() for inst in (a.get("institutions") or [])]
            break
    loc = work.get("primary_location") or {}
    venue = ((loc.get("source") or {}).get("display_name") or "").strip()
    doi = (work.get("doi") or "").strip()
    url = doi if doi.startswith("http") else (loc.get("landing_page_url") or work.get("id") or "")
    year = work.get("publication_year")
    d = (work.get("publication_date") or "")[:10] or (f"{int(year)}-01-01" if year else None)
    return {"kind": "paper", "artifact_key": wid, "title": title[:300], "url": url[:500], "date": d,
            "venue": venue[:200], "role": role,
            "detail": {"citations": int(work.get("cited_by_count") or 0), "year": year,
                       "affiliations": [x for x in affs if x][:4], "type": work.get("type") or "",
                       "n_authors": len(work.get("authorships") or [])},
            "link_method": "author_id", "confidence": 1.0, "source_family": "openalex"}


def github_repo_artifact(repo: dict, login: str) -> dict | None:
    """One GitHub repo → a 'repo' artifact. Forks are NOT the person's work and are skipped (the
    spec's ownership-vs-contribution distinction); a private repo never appears."""
    fn = (repo.get("full_name") or "").strip()
    if not fn or repo.get("private") or repo.get("fork"):
        return None
    owner = ((repo.get("owner") or {}).get("login") or fn.split("/")[0]).lower()
    d = ((repo.get("pushed_at") or repo.get("updated_at") or "")[:10]) or None
    return {"kind": "repo", "artifact_key": fn, "title": (repo.get("name") or fn)[:300],
            "url": (repo.get("html_url") or f"https://github.com/{fn}")[:500], "date": d,
            "venue": (repo.get("language") or "")[:60],
            "role": "owner" if owner == (login or "").lower() else "contributor",
            "detail": {"stars": int(repo.get("stargazers_count") or 0),
                       "forks": int(repo.get("forks_count") or 0),
                       "description": (repo.get("description") or "")[:240],
                       "created": (repo.get("created_at") or "")[:10],
                       "topics": list(repo.get("topics") or [])[:8]},
            "link_method": "login", "confidence": 1.0, "source_family": "github"}


def github_org_artifact(org: dict) -> dict | None:
    """A PUBLIC org membership → an 'org' artifact (affiliation evidence: the person chose to show
    membership in this organization on GitHub). Undated — GitHub does not expose join dates."""
    login = (org.get("login") or "").strip()
    if not login:
        return None
    return {"kind": "org", "artifact_key": login.lower(), "title": login[:300],
            "url": f"https://github.com/{login}", "date": None, "venue": "", "role": "member",
            "detail": {"description": (org.get("description") or "")[:240]},
            "link_method": "login", "confidence": 1.0, "source_family": "github"}


def select_repos(repos: list[dict], login: str, *, keep: int = 15) -> list[dict]:
    """The repos worth keeping per person: own non-fork repos, most-starred first, then most recently
    pushed; capped so a prolific account does not swamp the row."""
    rows = [a for a in (github_repo_artifact(r, login) for r in repos or []) if a]
    # most-starred first; among equals the most recently pushed first
    rows.sort(key=lambda a: (-a["detail"]["stars"], -int((a["date"] or "0000-00-00").replace("-", ""))))
    return rows[:keep]


# --------------------------------------------------------------------------- #
# Summary: artifact rows → the per-person payload the cards/Rail/CSV render    #
# --------------------------------------------------------------------------- #
def _prominence(a: dict) -> tuple:
    d = a.get("detail") or {}
    return (-(int(d.get("citations") or 0) + int(d.get("stars") or 0)), str(a.get("date") or ""))


def summarize_artifacts(rows: list[dict], scans: list[dict] | None = None) -> dict:
    """{scanned, counts, total, newest, items, affiliations, reported} — code-counted. `scanned`
    lists the sources that were actually queried for this person, so the UI can tell 'no artifacts
    found' from 'not yet scanned' (a coverage gap is never presented as an absence)."""
    rows = list(rows or [])
    counts: dict[str, int] = {}
    newest = ""
    for a in rows:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
        d = str(a.get("date") or "")[:10]
        if d > newest:
            newest = d
    affs: dict[str, dict] = {}
    for a in rows:
        if a["kind"] != "paper":
            continue
        yr = (a.get("detail") or {}).get("year")
        for name in (a.get("detail") or {}).get("affiliations") or []:
            rec = affs.setdefault(name, {"name": name, "n": 0, "years": []})
            rec["n"] += 1
            if yr and yr not in rec["years"]:
                rec["years"].append(int(yr))
    aff_list = sorted(affs.values(), key=lambda r: (-r["n"], r["name"]))[:6]
    for r in aff_list:
        r["years"] = sorted(r["years"])
    ordered = sorted(rows, key=_prominence)
    items = []
    for a in ordered[:_TOP_ITEMS]:
        d = a.get("detail") or {}
        items.append({"kind": a["kind"], "title": a.get("title") or "", "url": a.get("url") or "",
                      "date": str(a.get("date") or "")[:10] or None, "venue": a.get("venue") or "",
                      "role": a.get("role") or "",
                      "stat": (f"{d['citations']} citations" if d.get("citations") else
                               f"{d['stars']}★" if d.get("stars") else "")})
    reported = {}
    for s in scans or []:
        reported[s["source"]] = int(s.get("n_total") or 0)
    return {"scanned": sorted({s["source"] for s in (scans or [])}), "counts": counts,
            "total": len(rows), "newest": newest or None, "items": items,
            "affiliations": aff_list, "reported": reported}


def apply_artifacts_to_packet(packet: dict, summary: dict | None) -> dict:
    """Fold a person's artifact summary into their evidence packet (mutates + returns it):
    - capability artifacts add the 'artifact_backed' rung (the headline becomes 'mixed' when a
      self-stated profile coexists with artifact-backed work — honest, and the strip explains it);
    - newest artifact date becomes the freshness axis;
    - a person SCANNED with nothing found gains the 'public_artifacts' gap; unscanned is unknown."""
    if not isinstance(packet, dict):
        return packet
    from api.evidence import _STRENGTH_RANK  # ladder ranks live with the typing rules
    s = summary or {}
    counts = s.get("counts") or {}
    cap_n = sum(n for k, n in counts.items() if k in _CAPABILITY_KINDS)
    per_key = packet.setdefault("per_key", {})
    fams = set(packet.get("families") or [])
    if cap_n:
        fam = "openalex" if counts.get("paper") else "github"
        per_key["public_artifacts"] = {"type": "artifact_backed", "family": fam}
        fams.add(fam)
        packet["families"] = sorted(fams)
        types = {d.get("type") for d in per_key.values() if d.get("type")}
        packet["types"] = sorted(types, key=lambda t: -_STRENGTH_RANK.get(t, 0))
        packet["strength"] = ("mixed" if len({_STRENGTH_RANK.get(t, 0) for t in types}) > 1
                              else packet["types"][0])
    packet["freshness"] = {"newest_artifact": s.get("newest"), "profile_text": "undated"}
    gaps = list(packet.get("gaps") or [])
    if s.get("scanned") and not s.get("total") and "public_artifacts" not in gaps:
        gaps.append("public_artifacts")
    packet["gaps"] = gaps
    return packet


def footprint_coverage(rows: list[dict]) -> dict:
    """Map-level footprint statement, counted by code from the rows' artifact summaries."""
    total = len(rows or [])
    with_art = scanned = 0
    kind_tot: dict[str, int] = {}
    newest = ""
    for r in rows or []:
        s = r.get("artifacts") or {}
        if s.get("scanned"):
            scanned += 1
        if s.get("total"):
            with_art += 1
            for k, n in (s.get("counts") or {}).items():
                kind_tot[k] = kind_tot.get(k, 0) + n
            if (s.get("newest") or "") > newest:
                newest = s.get("newest") or ""
    return {"people": total, "with_artifacts": with_art, "scanned": scanned,
            "unscanned": max(0, total - scanned), "kinds": kind_tot, "newest": newest or None}


# --------------------------------------------------------------------------- #
# Read path: attach summaries to built people rows (fail-safe, one query)      #
# --------------------------------------------------------------------------- #
_ensured: set[int] = set()


async def ensure_schema(pool) -> None:
    if id(pool) in _ensured:
        return
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
    _ensured.add(id(pool))


async def fetch_person_artifacts(pool, entity_ids: list[str]) -> dict[str, dict]:
    ids = [e for e in (entity_ids or []) if e]
    if not ids:
        return {}
    await ensure_schema(pool)
    async with pool.acquire() as conn:
        arts = await conn.fetch(
            """SELECT entity_id, kind, artifact_key, title, url, date, venue, role, detail
               FROM rs_person_artifact WHERE entity_id = ANY($1)""", ids)
        scans = await conn.fetch(
            "SELECT entity_id, source, status, n_found, n_total FROM rs_artifact_scan WHERE entity_id = ANY($1)", ids)
    by_ent: dict[str, list] = defaultdict(list)
    for a in arts:
        d = dict(a)
        d["detail"] = json.loads(d["detail"]) if isinstance(d["detail"], str) else (d["detail"] or {})
        by_ent[d["entity_id"]].append(d)
    scan_by: dict[str, list] = defaultdict(list)
    for s in scans:
        scan_by[s["entity_id"]].append(dict(s))
    out = {}
    for eid in ids:
        if eid in by_ent or eid in scan_by:
            out[eid] = summarize_artifacts(by_ent.get(eid, []), scan_by.get(eid, []))
    return out


async def attach_artifacts(store, rows: list[dict]) -> None:
    """Attach `artifacts` (summary) to each built people row and fold it into the row's evidence
    packet. In place; never raises — a row without artifacts simply has none (unscanned)."""
    if not rows:
        return
    get_pool = getattr(store, "_get_pool", None)
    if get_pool is None:
        return
    try:
        pool = await get_pool()
        found = await fetch_person_artifacts(pool, [r.get("entity_id") for r in rows])
    except Exception as e:  # noqa: BLE001 — artifacts are additive; the map must still render
        _log.warning("attach_artifacts failed: %s", e)
        return
    for r in rows:
        s = found.get(r.get("entity_id") or "")
        r["artifacts"] = s or {"scanned": [], "counts": {}, "total": 0, "newest": None,
                               "items": [], "affiliations": [], "reported": {}}
        if isinstance(r.get("evidence"), dict):
            apply_artifacts_to_packet(r["evidence"], s)


def artifact_lines(summary: dict | None, *, limit: int = 8) -> list[str]:
    """Plain-text lines for a citable index document (the person dossier route)."""
    s = summary or {}
    if not s.get("total"):
        return []
    parts = [f"{n} {KIND_LABELS.get(k, k)}" for k, n in sorted((s.get("counts") or {}).items())]
    lines = [f"- public artifacts (linked by identity key): {', '.join(parts)}"
             + (f"; newest {s['newest']}" if s.get("newest") else "")]
    for it in (s.get("items") or [])[:limit]:
        meta = ", ".join(x for x in [it.get("venue") or "", (it.get("date") or "")[:4], it.get("stat") or ""] if x)
        lines.append(f"  - {it.get('kind')}: {it.get('title')}" + (f" ({meta})" if meta else "")
                     + (f" {it.get('url')}" if it.get("url") else ""))
    for a in (s.get("affiliations") or [])[:4]:
        yrs = a.get("years") or []
        span = f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else (str(yrs[0]) if yrs else "undated")
        lines.append(f"  - affiliation on published work: {a['name']} ({span}, {a['n']} works)")
    return lines
