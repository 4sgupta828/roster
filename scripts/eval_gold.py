#!/usr/bin/env python
"""GOLDEN EVAL runner — evals/gold_people_jobs.json against the live API. Every check is code; the
report is a pass/fail table plus the failing evidence. Exit 1 when any case fails (a baseline you
can diff). Spend: ~one LLM compile per people/jobs case — run it on purpose, not in CI loops.

    python scripts/eval_gold.py                      # prod
    python scripts/eval_gold.py --only co_stripe_backend --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

BASE = os.environ.get("ROSTER_EVAL_BASE", "https://roster-api-production-3405.up.railway.app")
ROUTE = {"people": "/research", "jobs": "/jobs", "match-people": "/match-people"}
BOT_RX = re.compile(r"(^|[^a-z])(chatgpt|codex|copilot|dependabot|bot)([^a-z]|$)", re.I)


def call(base: str, surface: str, body: dict) -> dict:
    b = dict(body)
    b.setdefault("tenant_id", "demo")
    if surface == "people":
        b["surface"] = "people"
    req = urllib.request.Request(base + ROUTE[surface], data=json.dumps(b).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read())


def company_of(row: dict) -> str:
    return next((str(a.get("display") or "").lower() for a in row.get("attributes") or [] if a.get("key") == "company"), "")


def row_text(row: dict) -> str:
    return " ".join([row.get("blurb") or "", json.dumps(row.get("attributes") or []), row.get("name") or ""]).lower()


def run_checks(case: dict, d: dict) -> list[str]:
    fails: list[str] = []
    checks = case["checks"]
    rows = d.get("people_rows") or []
    jobs = d.get("jobs") or []
    lk = d.get("person_lookup") or {}
    for k, v in checks.items():
        if k == "min_rows" and len(rows) < v:
            fails.append(f"min_rows {len(rows)} < {v}")
        elif k == "min_jobs" and len(jobs) < v:
            fails.append(f"min_jobs {len(jobs)} < {v}")
        elif k == "jobs_count" and len(jobs) != v:
            fails.append(f"jobs_count {len(jobs)} != {v}")
        elif k == "company_share":
            co, share = v
            n = sum(1 for r in rows if co in company_of(r).replace(" ", ""))
            if not rows or n / len(rows) < share:
                fails.append(f"company_share {co}: {n}/{len(rows)} < {share}")
        elif k in ("company_present", "company_present2"):
            co, mn = v
            n = sum(1 for r in rows if co in company_of(r).replace(" ", ""))
            if n < mn:
                fails.append(f"company_present {co}: {n} < {mn}")
        elif k == "mentions_share":
            rx, share = v
            n = sum(1 for r in rows if re.search(rx, row_text(r)))
            if not rows or n / len(rows) < share:
                fails.append(f"mentions_share /{rx}/: {n}/{len(rows)} < {share}")
        elif k == "role_share":
            terms, share = v
            n = sum(1 for r in rows if any(t in row_text(r) for t in terms))
            if not rows or n / len(rows) < share:
                fails.append(f"role_share {terms}: {n}/{len(rows)} < {share}")
        elif k == "no_duplicate_names":
            names = [str(r.get("name") or "").lower() for r in rows]
            dup = {n for n in names if names.count(n) > 1}
            if dup:
                fails.append(f"duplicate names: {sorted(dup)[:5]}")
        elif k == "no_bot_names":
            bots = [r.get("name") for r in rows if BOT_RX.search(str(r.get("name") or ""))]
            if bots:
                fails.append(f"bot-like names: {bots[:5]}")
        elif k == "every_card_linkedin_way_in":
            miss = [r.get("name") for r in rows if not any((l.get("kind") or "") in ("linkedin", "linkedin_search") for l in r.get("links") or [])]
            if miss:
                fails.append(f"no LinkedIn way-in: {miss[:5]}")
        elif k == "person_resolution":
            if lk.get("resolution") not in v:
                fails.append(f"resolution {lk.get('resolution')!r} not in {v}")
        elif k == "row_company":
            if not rows or v not in company_of(rows[0]).replace(" ", ""):
                fails.append(f"row_company {company_of(rows[0]) if rows else None!r} != {v}")
        elif k == "no_unrelated_namesakes_when_none":
            real = [r for r in rows if not str(r.get("entity_id") or "").startswith("search:")]
            if lk.get("resolution") == "none" and real:
                fails.append(f"'none' but {len(real)} namesake rows listed")
        elif k == "no_text":
            blob = json.dumps(rows)
            if v in blob:
                fails.append(f"text {v!r} present")
        elif k == "all_have_artifact":
            bad = [r.get("name") for r in rows if not int((((r.get("artifacts") or {}).get("counts") or {}).get(v)) or 0)]
            if bad:
                fails.append(f"rows without {v}: {bad[:5]}")
        elif k == "jobs_company_share":
            co, share = v
            n = sum(1 for j in jobs if co in str(j.get("company") or "").lower().replace(" ", ""))
            if not jobs or n / len(jobs) < share:
                fails.append(f"jobs_company_share {co}: {n}/{len(jobs)} < {share}")
        elif k == "jobs_all_remote":
            bad = [j.get("title") for j in jobs if "remote" not in (str(j.get("location") or "") + " " + str(j.get("title") or "")).lower()]
            if bad:
                fails.append(f"non-remote jobs: {bad[:4]}")
        elif k == "geo_scope_label":
            lab = str((d.get("geo_scope") or {}).get("label") or "")
            if v.lower() not in lab.lower():
                fails.append(f"geo_scope label {lab!r} lacks {v!r}")
        elif k == "jobs_no_out_of_scope":
            gs = d.get("geo_scope") or {}
            if gs and gs.get("counts", {}).get("in", 0) == 0:
                fails.append("no in-scope jobs at all")
        elif k == "linkedin_profile_name":
            nm = str((d.get("linkedin_profile") or {}).get("name") or "")
            if v.lower() not in nm.lower():
                fails.append(f"linkedin_profile {nm!r} != {v!r}")
        elif k == "note_contains":
            if v.lower() not in str(d.get("note") or "").lower():
                fails.append(f"note lacks {v!r}: {str(d.get('note') or '')[:120]!r}")
        elif k == "redirect_to_qa":
            if bool(d.get("redirect_to_qa")) != bool(v):
                fails.append(f"redirect_to_qa {d.get('redirect_to_qa')!r}")
        elif k == "no_company":
            bad = [r.get("name") for r in rows if v in company_of(r).replace(" ", "")]
            if bad:
                fails.append(f"rows at excluded company {v}: {bad[:4]}")
    return fails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--only", default="")
    ap.add_argument("--file", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals", "gold_people_jobs.json"))
    args = ap.parse_args()
    cases = json.load(open(args.file))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    results = []
    for c in cases:
        t0 = time.time()
        try:
            d = call(args.base, c["surface"], c["body"])
            fails = run_checks(c, d)
        except Exception as e:   # noqa: BLE001
            fails = [f"error: {str(e)[:160]}"]
        dt = time.time() - t0
        results.append((c["id"], fails, dt))
        print(f"{'PASS' if not fails else 'FAIL'}  {c['id']:32s} {dt:5.1f}s  {'; '.join(fails)[:200]}", flush=True)
    n_pass = sum(1 for _, f, _ in results if not f)
    print(f"\n{n_pass}/{len(results)} passed")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
