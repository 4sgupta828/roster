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
import re
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
_TOP_ITEMS = 24
_PER_KIND = 4


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
    # items: the top few of EVERY kind (papers, talks, posts, patents, repos, orgs) — a starred
    # repo must not crowd a talk or a post out of the Rail
    _KIND_ORDER = ("paper", "talk", "post", "patent", "repo", "org")
    ordered = []
    for k in _KIND_ORDER:
        ordered += sorted([a for a in rows if a["kind"] == k], key=_prominence)[:_PER_KIND]
    ordered += [a for a in sorted(rows, key=_prominence) if a["kind"] not in _KIND_ORDER][:2]
    items = [artifact_item(a) for a in ordered[:_TOP_ITEMS]]
    reported = {}
    for s in scans or []:
        reported[s["source"]] = int(s.get("n_total") or 0)
    return {"scanned": sorted({s["source"] for s in (scans or [])}), "counts": counts,
            "total": len(rows), "newest": newest or None, "items": items,
            "affiliations": aff_list, "reported": reported}


def artifact_item(a: dict) -> dict:
    """One artifact row → the card/panel item shape (title, link, date, venue, role, a stat)."""
    d = a.get("detail") or {}
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:  # noqa: BLE001
            d = {}
    return {"kind": a["kind"], "title": a.get("title") or "", "url": a.get("url") or "",
            "date": str(a.get("date") or "")[:10] or None, "venue": a.get("venue") or "",
            "role": a.get("role") or "",
            "stat": (f"{d['citations']} citations" if d.get("citations") else
                     f"{d['stars']}★" if d.get("stars") else ""),
            # name+hint matches (talks) are shown as 'verify', never as proven
            "verify": (a.get("link_method") == "name_hint")}


async def all_person_artifacts(pool, entity_id: str, *, cap_per_kind: int = 200) -> dict:
    """EVERY linked artifact for a person (and their linked identities), grouped by kind and ordered
    by prominence — the 'show all' behind the panel's top-3 per kind. Read-only, no scan."""
    ids = [entity_id]
    try:
        from api.identity_links import links_for
        ids += [l["id"] for l in (await links_for(pool, [entity_id])).get(entity_id) or []]
    except Exception:  # noqa: BLE001
        pass
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT entity_id, kind, artifact_key, title, url, date, venue, role, detail, link_method
               FROM rs_person_artifact WHERE entity_id = ANY($1)""", ids)
    by_kind: dict[str, list] = defaultdict(list)
    for r in rows:
        a = dict(r)
        d = a.get("detail")
        if isinstance(d, str):
            try:
                a["detail"] = json.loads(d)
            except Exception:  # noqa: BLE001
                a["detail"] = {}
        it = artifact_item(a)
        if a["entity_id"] != entity_id:
            it["via"] = a["entity_id"]
        by_kind[a["kind"]].append((a, it))
    out = {}
    for k, pairs in by_kind.items():
        pairs.sort(key=lambda p: _prominence(p[0]))
        out[k] = [it for _, it in pairs[:cap_per_kind]]
    return {"entity_id": entity_id, "counts": {k: len(v) for k, v in by_kind.items()}, "items": out}


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
        fam = next((f for k, f in (("paper", "openalex"), ("repo", "github"), ("post", "site"),
                                   ("talk", "youtube")) if counts.get(k)), "github")
        per_key["public_artifacts"] = {"type": "artifact_backed", "family": fam}
        fams.add(fam)
        packet["families"] = sorted(fams)
        types = {d.get("type") for d in per_key.values() if d.get("type")}
        # rank first, then alphabetical so same-rung ties (structured + artifact_backed) are stable
        # across processes: 'artifact-backed' leads for a scholar whose papers are linked
        packet["types"] = sorted(types, key=lambda t: (-_STRENGTH_RANK.get(t, 0), t))
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
            """SELECT entity_id, kind, artifact_key, title, url, date, venue, role, detail, link_method
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
    from api.evidence import calibrate
    found: dict[str, dict] = {}
    links: dict[str, list] = {}
    get_pool = getattr(store, "_get_pool", None)
    if get_pool is not None:
        try:
            from api.identity_links import links_for
            pool = await get_pool()
            ids = [r.get("entity_id") for r in rows]
            links = await links_for(pool, ids)
            linked_ids = sorted({l["id"] for ls in links.values() for l in ls})
            found = await fetch_person_artifacts(pool, ids + linked_ids)
            # CROSS-SOURCE: a person's papers (openalex identity) and repos (github identity) show on
            # ONE card when the identities are linked (name + shared employer, unique-name guarded)
            for eid, ls in links.items():
                base = found.get(eid) or {"scanned": [], "counts": {}, "total": 0, "newest": None,
                                          "items": [], "affiliations": [], "reported": {}}
                merged = None
                for l in ls:
                    other = found.get(l["id"])
                    if not other:
                        continue
                    merged = merged or dict(base)
                    merged["scanned"] = sorted(set(merged.get("scanned") or []) | set(other.get("scanned") or []))
                    cnt = dict(merged.get("counts") or {})
                    for k, n in (other.get("counts") or {}).items():
                        cnt[k] = cnt.get(k, 0) + n
                    merged["counts"] = cnt
                    merged["total"] = int(merged.get("total") or 0) + int(other.get("total") or 0)
                    if (other.get("newest") or "") > (merged.get("newest") or ""):
                        merged["newest"] = other.get("newest")
                    merged["items"] = (merged.get("items") or []) + [{**it, "via": l["id"]} for it in (other.get("items") or [])]
                    merged["affiliations"] = (merged.get("affiliations") or []) + (other.get("affiliations") or [])
                if merged is None:
                    merged = dict(base)            # linked, but the other identity holds no artifacts yet
                merged["linked"] = [{"id": l["id"], "method": l["method"], "confidence": l["confidence"],
                                     "employer": (l.get("evidence") or {}).get("employer", ""),
                                     "scanned": bool((found.get(l["id"]) or {}).get("scanned"))} for l in ls]
                found[eid] = merged
        except Exception as e:  # noqa: BLE001 — artifacts are additive; the map must still render
            _log.warning("attach_artifacts failed: %s", e)
    for r in rows:
        s = found.get(r.get("entity_id") or "")
        if get_pool is not None:
            r["artifacts"] = s or {"scanned": [], "counts": {}, "total": 0, "newest": None,
                                   "items": [], "affiliations": [], "reported": {}}
        if isinstance(r.get("evidence"), dict):
            if get_pool is not None:
                apply_artifacts_to_packet(r["evidence"], s)
            calibrate(r["evidence"], r)          # self-stated calibration band + reasons (code-owned)


# --------------------------------------------------------------------------- #
# Fetchers (HTTP metadata only — no LLM) + ON-DEMAND scan for one person       #
# --------------------------------------------------------------------------- #
_OA_WORKS = "https://api.openalex.org/works"
_OA_SELECT = ("id,doi,title,display_name,publication_year,publication_date,type,cited_by_count,"
              "primary_location,authorships")
_GH = "https://api.github.com"


def fetch_openalex_works(author_id: str, *, retries: int = 3, timeout: int = 30) -> tuple[list[dict], int]:
    """Top-25 works by citations for one OpenAlex author id → (works, total works count)."""
    import time
    import urllib.error
    import urllib.parse
    import urllib.request
    params = {"filter": f"authorships.author.id:{author_id}", "per-page": 25,
              "sort": "cited_by_count:desc", "select": _OA_SELECT}
    import os
    if os.environ.get("ROSTER_OPENALEX_MAILTO"):
        params["mailto"] = os.environ["ROSTER_OPENALEX_MAILTO"]
    if os.environ.get("ROSTER_OPENALEX_KEY"):
        params["api_key"] = os.environ["ROSTER_OPENALEX_KEY"]
    url = _OA_WORKS + "?" + urllib.parse.urlencode(params)
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "roster-artifacts/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            return data.get("results") or [], int((data.get("meta") or {}).get("count") or 0)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return [], 0
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); continue
            raise
    return [], 0


def _gh_json(path: str, *, token: str, params: dict | None = None, timeout: int = 15):
    import urllib.parse
    import urllib.request
    url = _GH + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "roster-artifacts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), dict(r.headers)


def fetch_github_person(login: str, *, token: str) -> tuple[list[dict], list[dict], int, int]:
    """(kept repo artifacts, org artifacts, public repo count, rate-limit remaining) — 2 core calls,
    single attempt (the on-demand path must stay fast; the batch script adds retry/reserve logic)."""
    repos, _ = _gh_json(f"/users/{login}/repos", token=token,
                        params={"per_page": 100, "type": "owner", "sort": "pushed"})
    orgs, hdr = _gh_json(f"/users/{login}/orgs", token=token, params={"per_page": 50})
    kept = select_repos(repos if isinstance(repos, list) else [], login)
    org_rows = [a for a in (github_org_artifact(o) for o in (orgs if isinstance(orgs, list) else [])) if a]
    remaining = int(hdr.get("X-RateLimit-Remaining", "9999") or 9999)
    return kept, org_rows, (len(repos) if isinstance(repos, list) else 0), remaining


def _as_date(s):
    from datetime import date
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


async def write_person_artifacts(conn, eid: str, source: str, arts: list[dict], n_total: int,
                                 status: str = "done") -> None:
    """Upsert one person's artifact rows + their scan marker (one transaction)."""
    async with conn.transaction():
        for a in arts:
            await conn.execute(
                """INSERT INTO rs_person_artifact (entity_id, kind, artifact_key, title, url, date, venue,
                                                   role, detail, link_method, confidence, source_family, fetched_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12, now())
                   ON CONFLICT (entity_id, kind, artifact_key) DO UPDATE SET
                     title=EXCLUDED.title, url=EXCLUDED.url, date=EXCLUDED.date, venue=EXCLUDED.venue,
                     role=EXCLUDED.role, detail=EXCLUDED.detail, fetched_at=now()""",
                eid, a["kind"], a["artifact_key"], a["title"], a["url"], _as_date(a.get("date")), a["venue"],
                a["role"], json.dumps(a["detail"]), a["link_method"], float(a["confidence"]), a["source_family"])
        await conn.execute(
            """INSERT INTO rs_artifact_scan (entity_id, source, status, n_found, n_total, scanned_at)
               VALUES ($1,$2,$3,$4,$5, now())
               ON CONFLICT (entity_id, source) DO UPDATE SET status=EXCLUDED.status, n_found=EXCLUDED.n_found,
                 n_total=EXCLUDED.n_total, scanned_at=now()""", eid, source, status, len(arts), int(n_total))


async def scan_linked_identities(pool, entity_id: str, *, timeout: float = 12.0) -> int:
    """On demand: scan the identities LINKED to this person (their papers/repos show on this card)."""
    try:
        from api.identity_links import links_for
        ls = (await links_for(pool, [entity_id])).get(entity_id) or []
        n = 0
        for l in ls[:3]:
            if await scan_person_now(pool, l["id"], timeout=timeout):
                n += 1
        return n
    except Exception as e:  # noqa: BLE001
        _log.info("scan_linked_identities(%s) skipped: %s", entity_id, e)
        return 0


async def scan_person_now(pool, entity_id: str, *, timeout: float = 12.0) -> bool:
    """ON-DEMAND artifact scan for ONE person (a person lookup): fetch + write if this person's source
    has not been scanned yet. Bounded by `timeout`; never raises. Returns True when a scan ran."""
    import asyncio
    import os
    src = (entity_id or "").split(":", 1)[0]
    if src not in ("openalex", "github"):
        return False
    key = entity_id.split(":", 1)[1]
    try:
        await ensure_schema(pool)
        async with pool.acquire() as conn:
            done = await conn.fetchval(
                "SELECT 1 FROM rs_artifact_scan WHERE entity_id = $1 AND source = $2", entity_id, src)
        if done:
            return False
        if src == "openalex":
            works, total = await asyncio.wait_for(
                asyncio.to_thread(fetch_openalex_works, key, retries=1, timeout=10), timeout)
            arts = [a for a in (openalex_work_artifact(w, key) for w in works) if a]
        else:
            token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
            if not token:
                return False
            repos, orgs, total, _rem = await asyncio.wait_for(
                asyncio.to_thread(fetch_github_person, key, token=token), timeout)
            arts = repos + orgs
        async with pool.acquire() as conn:
            await write_person_artifacts(conn, entity_id, src, arts, total)
        return True
    except Exception as e:  # noqa: BLE001 — on-demand enrichment is best-effort
        _log.info("scan_person_now(%s) skipped: %s", entity_id, e)
        return False


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


# --------------------------------------------------------------------------- #
# SELF-PUBLISHED WRITING (declared site / Medium / Substack / dev.to feeds)     #
# --------------------------------------------------------------------------- #
# The person DECLARED these links on their own profile — a strong identity key — and each feed
# entry is a dated, linkable piece of their own writing (self-authored: an artifact, not a claim).
_FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml", "/rss", "/blog/feed", "/blog/rss.xml")


def feed_candidates(url: str) -> list[str]:
    """Where a declared link's feed probably is: known platforms first, then autodiscovery paths."""
    import re
    from urllib.parse import urlparse
    u = (url or "").strip()
    if not u:
        return []
    if not u.startswith("http"):
        u = "https://" + u
    p = urlparse(u)
    host, path = p.netloc.lower(), p.path.rstrip("/")
    if "medium.com" in host:
        m = re.search(r"/@([^/]+)", path)
        if m:
            return [f"https://medium.com/feed/@{m.group(1)}"]
        if host != "medium.com" and host.endswith(".medium.com"):
            return [f"https://{host}/feed"]
        return [f"https://medium.com/feed{path}"] if path else []
    if host.endswith("substack.com"):
        return [f"https://{host}/feed"]
    if host in ("dev.to", "www.dev.to"):
        m = re.search(r"^/([^/]+)", path)
        return [f"https://dev.to/feed/{m.group(1)}"] if m else []
    if "youtube.com" in host or "twitter.com" in host or "x.com" == host or "linkedin.com" in host \
            or "github.com" in host or "instagram.com" in host or "facebook.com" in host:
        return []
    base = f"{p.scheme or 'https'}://{host}"
    return [base + path + fp for fp in _FEED_PATHS[:4]] + [base + fp for fp in _FEED_PATHS]


def parse_feed(xml_text: str, *, limit: int = 15) -> list[dict]:
    """RSS 2.0 / Atom → [{title, url, date}] (stdlib only). Bad XML → []."""
    import re
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    try:
        root = ET.fromstring(xml_text.strip()[:2_000_000])
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    def _date(s: str) -> str | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return parsedate_to_datetime(s).date().isoformat()
        except Exception:  # noqa: BLE001
            m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
            return m.group(1) if m else None
    for it in root.iter("item"):                                   # RSS
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "date": _date(it.findtext("pubDate") or "")})
    if not out:
        for e in root.findall("a:entry", ns):                     # Atom
            title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
            link = ""
            for l in e.findall("a:link", ns):
                if l.get("rel", "alternate") == "alternate" and (l.get("href") or "").startswith("http"):
                    link = l.get("href"); break
            d = e.findtext("a:published", default="", namespaces=ns) or e.findtext("a:updated", default="", namespaces=ns)
            if title and link:
                out.append({"title": title, "url": link, "date": _date(d)})
    return out[:limit]


def fetch_site_posts(link: str, *, timeout: int = 12) -> tuple[list[dict], str]:
    """Discover + read the feed behind a declared link → (posts, feed_url). HTTP only; first feed
    that parses wins; a page with a <link rel=alternate> feed hint is followed once."""
    import re
    import urllib.request
    hdr = {"User-Agent": "roster-artifacts/1.0 (+public feed reader)"}
    tried = []
    def _get(u):
        req = urllib.request.Request(u, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "ignore"), r.headers.get("Content-Type", "")
    for u in feed_candidates(link):
        if len(tried) >= 5:
            break
        tried.append(u)
        try:
            body, ctype = _get(u)
        except Exception:  # noqa: BLE001
            continue
        posts = parse_feed(body)
        if posts:
            return posts, u
        if "html" in ctype.lower() or body.lstrip().startswith("<!"):
            m = re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"', body, re.I) \
                or re.search(r'<link[^>]+href="([^"]+)"[^>]+type="application/(?:rss|atom)\+xml"', body, re.I)
            if m:
                fu = m.group(1)
                if fu.startswith("/"):
                    from urllib.parse import urlparse
                    p = urlparse(u); fu = f"{p.scheme}://{p.netloc}{fu}"
                try:
                    fb, _ = _get(fu)
                    posts = parse_feed(fb)
                    if posts:
                        return posts, fu
                except Exception:  # noqa: BLE001
                    pass
    return [], ""


_PLACEHOLDER_HOSTS = {"example.com", "example.org", "example.net", "localhost", "127.0.0.1", "yoursite.com",
                      "your-domain.com", "yourdomain.com", "mysite.com", "domain.com"}
_PLACEHOLDER_TITLE = re.compile(
    r"^(a post with\b|blog post number \d+|a distill-style|welcome( to jekyll)?!?$|hello,? world!?$|"
    r"(my )?first post!?$|(test|sample|example|demo) post|lorem ipsum|about( me)?$|home$|contact$|"
    r"projects?$|blog$|resume$|cv$|untitled)", re.I)
_PLACEHOLDER_PATH = re.compile(
    r"(/blog/post-\d+/?$|/projects?/project-\d+/?$|/post-\d+/?$|/(hello|first|sample|example)-post/?$|"
    r"/blog/(post|sample|example)\d*/?$)", re.I)


def _site_of(host: str) -> str:
    """Registrable-domain approximation: the last two labels (blog.foo.com → foo.com)."""
    h = (host or "").lower().removeprefix("www.")
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def placeholder_post(post: dict, feed_url: str) -> str:
    """Why a feed item is TEMPLATE FILLER rather than the person's writing ('' = genuine). A site
    built from a starter theme (astro-nano, al-folio, Jekyll…) ships with sample posts whose links
    point at the theme's demo host or at placeholder paths; a personal site's feed must link to the
    person's OWN site (Medium feeds may land on publication domains, so those are exempt)."""
    from urllib.parse import urlparse
    url, title = (post.get("url") or "").strip(), (post.get("title") or "").strip()
    pu, fu = urlparse(url), urlparse(feed_url or "")
    host = pu.netloc.lower().removeprefix("www.")
    if not host or host in _PLACEHOLDER_HOSTS or re.search(r"(^|[.-])demo[.-]|template|starter|\.example$", host):
        return "demo host"
    if _PLACEHOLDER_TITLE.match(title) or _PLACEHOLDER_PATH.search(pu.path or ""):
        return "template sample"
    fhost = fu.netloc.lower().removeprefix("www.")
    if fhost and "medium.com" not in fhost and _site_of(fhost) != _site_of(host):
        return "off-site link"          # the feed lives on their site; the item points elsewhere
    return ""


def site_post_artifact(post: dict, feed_url: str) -> dict | None:
    from urllib.parse import urlparse
    title, url = (post.get("title") or "").strip(), (post.get("url") or "").strip()
    if not title or not url.startswith("http"):
        return None
    if placeholder_post(post, feed_url):
        return None
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return {"kind": "post", "artifact_key": url[:300], "title": title[:300], "url": url[:500],
            "date": post.get("date"), "venue": host[:120], "role": "author",
            "detail": {"feed": feed_url[:300]}, "link_method": "declared_site", "confidence": 0.95,
            "source_family": "site"}


# --------------------------------------------------------------------------- #
# TALKS & PRESENTATIONS (YouTube / Vimeo via search snippets — name + hint gated)#
# --------------------------------------------------------------------------- #
def talk_artifacts_from_results(row: dict, results: list[dict]) -> list[dict]:
    """Search results → 'talk' artifacts ONLY when the full name appears in the title or snippet
    AND a grounded hint (employer / past employer / role) appears too — the same gate as LinkedIn.
    Stored with link_method 'name_hint' and lower confidence: shown as 'verify', never as proven."""
    import re
    from api.linkedin_resolve import hint_hits, hints_from_row
    name = " ".join((row.get("name") or "").split())
    if len(name.split()) < 2:
        return []
    nm = re.compile(r"\b" + r"\s+".join(re.escape(t) for t in name.split()) + r"\b", re.I)
    hints = {k: v for k, v in hints_from_row(row).items() if k in ("company", "worked_at", "role")}
    out = []
    for r in results or []:
        url = (r.get("url") or "").strip()
        if not re.search(r"(youtube\.com/watch|youtu\.be/|vimeo\.com/\d+)", url):
            continue
        text = (r.get("title") or "") + " " + (r.get("snippet") or "")
        if not nm.search(text):
            continue
        hits = hint_hits(hints, text)
        if not hits:
            continue
        title = re.sub(r"\s*-\s*YouTube\s*$", "", (r.get("title") or "").strip())
        out.append({"kind": "talk", "artifact_key": url[:300], "title": title[:300], "url": url[:500],
                    "date": None, "venue": "YouTube" if "youtu" in url else "Vimeo", "role": "speaker",
                    "detail": {"hits": hits, "snippet": (r.get("snippet") or "")[:240]},
                    "link_method": "name_hint", "confidence": 0.6,
                    "source_family": "youtube" if "youtu" in url else "vimeo"})
    return out[:8]


async def fetch_talks(row: dict, *, search=None) -> list[dict]:
    """One search per person: '"<name>" <company> (site:youtube.com OR site:vimeo.com)'."""
    from api.linkedin_resolve import hints_from_row, search_snippets
    search = search or search_snippets
    name = " ".join((row.get("name") or "").split())
    if len(name.split()) < 2:
        return []
    co = (hints_from_row(row).get("company") or [""])[0]
    q = f'"{name}" {co} (site:youtube.com OR site:vimeo.com) talk OR presentation OR conference'.replace("  ", " ")
    res = await search(q)
    return talk_artifacts_from_results(row, res)


def declared_links(facet_rows: list[dict]) -> list[str]:
    """The person's own declared writing links (site / medium / substack / dev.to) from facets."""
    out = []
    for f in facet_rows or []:
        k = f.get("facet_key") or ""
        if k in ("link_website", "link_medium", "link_substack", "link_devto"):
            v = (f.get("display_value") or f.get("value_norm") or "").strip()
            if v and v not in out:
                out.append(v)
    return out[:3]


async def scan_person_extras(pool, entity_id: str, facet_rows: list[dict], row: dict | None = None, *,
                             talks: bool = True, search=None, timeout: float = 20.0) -> dict:
    """ON-DEMAND extra layers for one person: posts from declared feeds ('site' source) and gated
    talks ('talks' source). Each source is scanned once (rs_artifact_scan). Returns counts."""
    import asyncio
    out = {"posts": 0, "talks": 0}
    try:
        await ensure_schema(pool)
        async with pool.acquire() as conn:
            done = {r["source"] for r in await conn.fetch(
                "SELECT source FROM rs_artifact_scan WHERE entity_id = $1", entity_id)}
        if "site" not in done:
            arts = []
            links = declared_links(facet_rows)
            for link in links:
                try:
                    posts, feed = await asyncio.wait_for(asyncio.to_thread(fetch_site_posts, link), timeout)
                except Exception:  # noqa: BLE001
                    posts, feed = [], ""
                arts += [a for a in (site_post_artifact(p, feed) for p in posts) if a]
            async with pool.acquire() as conn:
                await write_person_artifacts(conn, entity_id, "site", arts, len(arts),
                                             status="done" if links else "skipped")
            out["posts"] = len(arts)
        if talks and row is not None and "talks" not in done:
            try:
                arts = await asyncio.wait_for(fetch_talks(row, search=search), timeout)
            except Exception:  # noqa: BLE001
                arts = []
            async with pool.acquire() as conn:
                await write_person_artifacts(conn, entity_id, "talks", arts, len(arts))
            out["talks"] = len(arts)
    except Exception as e:  # noqa: BLE001
        _log.info("scan_person_extras(%s) skipped: %s", entity_id, e)
    return out
