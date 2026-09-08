"""LEVER — the hosted apply page is server-rendered and stable: https://jobs.lever.co/{site}/{id}/apply
Standard inputs by name (name, email, phone, location, org, urls[LinkedIn|GitHub|Portfolio|Twitter|Other],
resume, comments); custom questions as <li class="application-question custom-question"> blocks with a
label div and an input/select/textarea named cards[<uuid>][field<n>]; EEO / consent blocks likewise.
One parser, no DOM guessing at fill time — the extension addresses fields by name."""
from __future__ import annotations

import asyncio
import html as _html
import re
import urllib.request

from . import question

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) roster-apply/1.0"}
_STD = [("name", "Full name", "text"), ("email", "Email", "email"), ("phone", "Phone", "tel"), ("location", "Current location", "text"),
        ("org", "Current company", "text"), ("urls[LinkedIn]", "LinkedIn URL", "url"), ("urls[GitHub]", "GitHub URL", "url"),
        ("urls[Portfolio]", "Portfolio URL", "url"), ("urls[Twitter]", "Twitter URL", "url"), ("urls[Other]", "Other website", "url"),
        ("resume", "Resume/CV", "file"), ("comments", "Additional information", "textarea")]


def parse_url(url: str) -> tuple[str, str]:
    m = re.search(r"lever\.co/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def html_to_form(page: str, *, site: str, job_id: str) -> dict:
    qs = []
    names = set(re.findall(r'<(?:input|select|textarea)[^>]*\sname="([^"]+)"', page))
    for name, label, kind in _STD:
        if name in names:
            req = bool(re.search(rf'name="{re.escape(name)}"[^>]*\srequired', page)) or name in ("name", "email", "resume")
            qs.append(question(id=name, label=label, kind=kind, required=req, selector=f"[name=\"{name}\"]"))
    for blk in re.findall(r'<li class="application-question[^"]*">(.*?)</li>', page, re.S):
        lab_m = re.search(r'<div class="application-label[^"]*">(.*?)</div>', blk, re.S)
        label = _text(lab_m.group(1)) if lab_m else ""
        required = "✱" in label or "required" in blk
        label = label.replace("✱", "").strip()
        for tag, attrs in re.findall(r"<(input|select|textarea)([^>]*)>", blk):
            name = (re.search(r'name="([^"]+)"', attrs) or [None, ""])[1]
            if not name or not name.startswith("cards["):
                continue
            typ = (re.search(r'type="([^"]+)"', attrs) or [None, "text"])[1]
            if tag == "select":
                opts = [_text(o) for o in re.findall(r"<option[^>]*>(.*?)</option>", blk, re.S)]
                opts = [o for o in opts if o and not o.lower().startswith("select")]
                kind = "select"
            elif tag == "textarea":
                opts, kind = [], "textarea"
            elif typ in ("radio", "checkbox"):
                opts = [_text(v) for v in re.findall(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', blk)]
                kind = typ
            else:
                opts, kind = [], "text"
            if any(q["id"] == name for q in qs):
                continue
            qs.append(question(id=name, label=label or name, kind=kind, options=opts, required=required, selector=f"[name=\"{name}\"]"))
    title = _text((re.search(r"<h2[^>]*>(.*?)</h2>", page, re.S) or [None, ""])[1])
    return {"ats": "lever", "board": site, "job_id": job_id, "title": title, "company": site,
            "form_url": f"https://jobs.lever.co/{site}/{job_id}/apply", "questions": qs}


async def fetch_form(url: str) -> dict | None:
    site, job_id = parse_url(url)
    if not job_id:
        return None
    try:
        page = await asyncio.to_thread(_get, f"https://jobs.lever.co/{site}/{job_id}/apply")
    except Exception:  # noqa: BLE001
        return None
    if 'name="email"' not in page:
        return None
    return html_to_form(page, site=site, job_id=job_id)
