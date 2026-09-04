"""ASHBY — the hosted apply page fetches its form definition from Ashby's GraphQL:
POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting   (the same query the page sends;
vendored in ashby_query.graphql) → jobPosting.applicationForm.sections[].fieldEntries[]
  {isRequired, field:{path, title, type, selectableValues[{label,value}], isNullable, humanReadablePath}}
Field `path` is the live input's name (_systemfield_name, _systemfield_email, <uuid>…). Ashby renders
Boolean questions as Yes / No buttons and ValueSelect as custom dropdowns; the selector carries the path
and the extension's Ashby driver knows the widgets."""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request

from . import question

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) roster-apply/1.0", "content-type": "application/json",
       "apollographql-client-name": "frontend_non_user", "apollographql-client-version": "0.1.0"}
_QUERY = open(os.path.join(os.path.dirname(__file__), "ashby_query.graphql")).read()
_KIND = {"String": "text", "LongText": "textarea", "Email": "email", "Phone": "tel", "File": "file", "Boolean": "boolean",
         "ValueSelect": "select", "MultiValueSelect": "multiselect", "Date": "date", "Number": "text", "Location": "text",
         "SocialLink": "url", "Url": "url"}


def parse_url(url: str) -> tuple[str, str]:
    m = re.search(r"ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def _post(org: str, job_id: str):
    body = json.dumps({"operationName": "ApiJobPosting", "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job_id}, "query": _QUERY}).encode()
    req = urllib.request.Request("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", data=body, headers=_UA, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def definition_to_form(jp: dict, *, org: str, job_id: str) -> dict:
    qs = []
    af = jp.get("applicationForm") or {}
    for sec in af.get("sections") or []:
        group = str(sec.get("title") or "")
        for fe in sec.get("fieldEntries") or []:
            f = fe.get("field") or {}
            path = str(f.get("path") or f.get("id") or "")
            if not path:
                continue
            kind = _KIND.get(str(f.get("type") or ""), "text")
            opts = [str(o.get("label") or o.get("value") or "") for o in (f.get("selectableValues") or [])]
            if kind == "boolean" and not opts:
                opts = ["Yes", "No"]
            qs.append(question(id=path, label=f.get("title") or path, kind=kind, options=opts, required=bool(fe.get("isRequired")),
                               group=group, selector=f"[name=\"{path}\"]"))
    return {"ats": "ashby", "board": org, "job_id": job_id, "title": jp.get("title") or "", "company": org,
            "form_url": f"https://jobs.ashbyhq.com/{org}/{job_id}/application", "questions": qs}


async def fetch_form(url: str) -> dict | None:
    org, job_id = parse_url(url)
    if not job_id:
        return None
    try:
        d = await asyncio.to_thread(_post, org, job_id)
    except Exception:  # noqa: BLE001
        return None
    jp = ((d or {}).get("data") or {}).get("jobPosting") or {}
    if not jp.get("applicationForm"):
        return None
    return definition_to_form(jp, org=org, job_id=job_id)
