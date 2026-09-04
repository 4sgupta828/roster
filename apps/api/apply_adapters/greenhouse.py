"""GREENHOUSE — the public Job Board API returns the application form definition:
GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true
  questions[]            {label, required, fields[{name, type, values[{label, value}]}]}
  location_questions[]   same shape (city, country…)
  demographic_questions  {header, questions[{id, label, required, type, answer_options[{id, label}]}]}
Field NAMES are the live form's input ids/names (first_name, email, question_31687694003 …), so the
extension finds them without guessing. URL shapes: boards.greenhouse.io/{board}/jobs/{id},
job-boards.greenhouse.io/{board}/jobs/{id}, …/embed/job_app?for={board}&token={id}, and company career
pages with ?gh_jid={id} (the board token is read from the page's embed snippet)."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.request

from . import question

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) roster-apply/1.0"}
_KIND = {"input_text": "text", "textarea": "textarea", "input_file": "file", "multi_value_single_select": "select",
         "multi_value_multi_select": "multiselect", "input_hidden": "hidden"}


def parse_url(url: str) -> tuple[str, str]:
    """(board, job_id) from any Greenhouse posting URL shape; board may be '' for gh_jid pages."""
    u = url or ""
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)(?:/jobs/|&token=|\?token=)(\d+)", u)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"greenhouse\.io/embed/job_app\?(?:.*&)?for=([A-Za-z0-9_-]+).*?[&?]token=(\d+)", u)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"[?&]gh_jid=(\d+)", u)
    if m:
        return "", m.group(1)
    return "", ""


def _get(url: str, timeout: int = 20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def board_from_page(url: str) -> str:
    """A company career page embeds Greenhouse with `for=<board>` in its snippet / iframe."""
    try:
        html = _get(url).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"greenhouse\.io/embed/job_(?:board|app)[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)", html)
    if m:
        return m.group(1)
    m = re.search(r"boards\.greenhouse\.io/([A-Za-z0-9_-]+)", html)
    return m.group(1) if m else ""


def definition_to_form(d: dict, *, board: str, job_id: str) -> dict:
    qs = []
    seen = set()

    def add(q, group=""):
        for f in q.get("fields") or []:
            name = str(f.get("name") or "")
            kind = _KIND.get(str(f.get("type") or ""), "text")
            if kind == "hidden" or not name or name in seen:
                continue
            seen.add(name)
            opts = [str(v.get("label") or v.get("value") or "") for v in (f.get("values") or [])]
            qs.append(question(id=name, label=q.get("label") or name, kind=kind, options=opts, required=bool(q.get("required")),
                               group=group, selector=f"[name=\"{name}\"], #{re.sub(r'[^A-Za-z0-9_-]', '_', name)}"))
    for q in d.get("questions") or []:
        add(q)
    for q in d.get("location_questions") or []:
        add(q, group="location")
    dem = d.get("demographic_questions") or {}
    for q in (dem.get("questions") or []) if isinstance(dem, dict) else []:
        name = f"demographic_question_{q.get('id')}"
        opts = [str(o.get("label") or "") for o in (q.get("answer_options") or [])]
        kind = "multiselect" if str(q.get("type") or "") == "multi_value_multi_select" else "select"
        qs.append(question(id=name, label=q.get("label") or name, kind=kind, options=opts, required=bool(q.get("required")),
                           group="demographic", selector=f"[name=\"{name}\"]", policy="identity_sensitive"))
    return {"ats": "greenhouse", "board": board, "job_id": job_id, "title": d.get("title") or "", "company": d.get("company_name") or board,
            "form_url": f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={job_id}", "questions": qs}


async def fetch_form(url: str) -> dict | None:
    board, job_id = parse_url(url)
    if not job_id:
        return None
    if not board:
        board = await asyncio.to_thread(board_from_page, url)
        if not board:
            return None
    try:
        raw = await asyncio.to_thread(_get, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true")
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not (d.get("questions") or d.get("location_questions")):
        return None
    return definition_to_form(d, board=board, job_id=job_id)
