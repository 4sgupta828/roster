"""Single-posting readers for the public ATS APIs — Greenhouse, Lever, Ashby — so a job link that is
JavaScript-rendered (or a company careers page that embeds the board, `?gh_jid=…`) still yields the
full description. Pure URL parsing + JSON reading; the caller supplies the HTTP getter (SSRF-guarded).
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlparse

_STRIP_HOST_WORDS = ("careers", "career", "jobs", "job", "www", "boards", "apply", "work", "join", "talent", "hiring")


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def greenhouse_token_candidates(url: str) -> list[str]:
    """Board tokens to try for an EMBEDDED Greenhouse posting: the careers host minus generic words
    (pinterestcareers.com → pinterest; careers.stripe.com → stripe), plus a path org if present."""
    p = urlparse(url)
    host = (p.netloc or "").lower().split(":")[0]
    labels = [l for l in host.split(".") if l and l not in ("com", "io", "co", "org", "net", "ai", "www")]
    out: list[str] = []
    for lab in labels:
        base = lab
        for w in _STRIP_HOST_WORDS:
            base = base.replace(w, "")
        base = re.sub(r"[^a-z0-9]", "", base)
        if len(base) >= 3 and base not in out:
            out.append(base)
        if lab not in out and len(lab) >= 3:
            out.append(lab)
    seg = [x for x in (p.path or "").split("/") if x]
    if seg and seg[0] not in ("jobs", "job", "careers", "openings") and re.match(r"^[a-z0-9-]{3,}$", seg[0]):
        out.insert(0, seg[0])
    return out[:5]


def ats_posting_text(url: str, get) -> str:
    """Full posting text via the ATS public API, or '' when the link is not an ATS posting we know."""
    p = urlparse(url)
    host = (p.netloc or "").lower()
    seg = [x for x in (p.path or "").split("/") if x]
    q = parse_qs(p.query or "")
    # --- Greenhouse: boards.greenhouse.io/<token>/jobs/<id> | job-boards.greenhouse.io/... | ?gh_jid=<id>
    gh_id = (q.get("gh_jid") or [""])[0]
    token = ""
    if host.endswith("greenhouse.io") and len(seg) >= 3 and seg[1] == "jobs":
        token, gh_id = seg[0], seg[2]
    if gh_id and gh_id.isdigit():
        for tok in ([token] if token else greenhouse_token_candidates(url)):
            body = get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs/{gh_id}")
            if not body:
                continue
            try:
                d = json.loads(body)
            except Exception:   # noqa: BLE001
                continue
            if not isinstance(d, dict) or not d.get("content"):
                continue
            loc = ((d.get("location") or {}).get("name") or "")
            head = " · ".join(x for x in [d.get("title") or "", loc] if x)
            return (head + ". " + _strip_html(d.get("content") or "")).strip()
    # --- Lever: jobs.lever.co/<co>/<uuid>
    if host.endswith("lever.co") and len(seg) >= 2:
        body = get(f"https://api.lever.co/v0/postings/{seg[0]}/{seg[1]}")
        if body:
            try:
                d = json.loads(body)
            except Exception:   # noqa: BLE001
                d = None
            if isinstance(d, dict) and (d.get("descriptionPlain") or d.get("description")):
                cats = d.get("categories") or {}
                head = " · ".join(x for x in [d.get("text") or "", cats.get("location") or "", cats.get("commitment") or "",
                                              (d.get("salaryRange") or {}).get("min") and f"salary {d['salaryRange'].get('min')}–{d['salaryRange'].get('max')} {d['salaryRange'].get('currency','')}" or ""] if x)
                lists = " ".join((l.get("text") or "") + ": " + _strip_html(l.get("content") or "") for l in (d.get("lists") or []))
                return (head + ". " + (d.get("descriptionPlain") or _strip_html(d.get("description") or "")) + " " + lists
                        + " " + (d.get("additionalPlain") or "")).strip()
    # --- Ashby: jobs.ashbyhq.com/<co>/<uuid>
    if host.endswith("ashbyhq.com") and len(seg) >= 2:
        body = get(f"https://api.ashbyhq.com/posting-api/job-board/{seg[0]}?includeCompensation=true")
        if body:
            try:
                d = json.loads(body)
            except Exception:   # noqa: BLE001
                d = None
            for j in ((d or {}).get("jobs") or []) if isinstance(d, dict) else []:
                if str(j.get("id")) == seg[1] or (j.get("jobUrl") or "").rstrip("/").endswith(seg[1]):
                    comp = ((j.get("compensation") or {}).get("compensationTierSummary") or "")
                    head = " · ".join(x for x in [j.get("title") or "", j.get("location") or "", j.get("employmentType") or "",
                                                  (j.get("isRemote") and "Remote") or "", comp and f"compensation {comp}" or ""] if x)
                    return (head + ". " + (j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml") or ""))).strip()
    return ""
