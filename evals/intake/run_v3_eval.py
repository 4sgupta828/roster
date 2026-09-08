"""GUIDED v3 EVAL — deterministic checks over scripted personas against prod (docs/specs/guided-consultant-v3.md §10).
Per persona: the turns run; each turn's move / question / brief are recorded; checks:
  first_question_field       — the first question's brief field (mission for hiring; posture / level for a seeker with a résumé)
  never_asks_known           — no question on a field already known (the kernel gate notes such drops; a question that reached the
                               user on a known field is the failure)
  reaches_ready_within       — ready within N user turns
  contract_must_keys_include / contract_center / contract_kind
  max_calls, max_secs_per_turn
Spend ≈ $0.01 per persona. Throwaways only for seeded résumés (@roster.test) — sweep afterwards.
  python evals/intake/run_v3_eval.py [--only a,b]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_intake_eval import post  # noqa: E402

PERSONAS = [
    {"id": "v3_seeker_no_resume", "steps": [{"message": "I'm looking for my next role"},
                                            {"message": "Senior backend engineer at Acme for 5 years, Go and Kubernetes, built our billing platform. In New York."},
                                            {"auto": 3}],
     "expect": {"direction": "job", "reaches_ready_within": 5, "contract_kind": "job", "contract_must_keys_include": ["metro"], "brief_known_include": ["role_family", "metro"], "max_calls": 8}},
    {"id": "v3_seeker_resume_on_file", "seed_resume": "Sam Doe\nSan Francisco, CA\nHead of ML Infrastructure at Fintech Co (2018–now): lead a 12-person team building training and serving platforms (Kubernetes, Ray, feature stores).\nStaff Engineer, Google (2012–2018): distributed storage.\nB.S. Computer Science.",
     "steps": [{"message": "I'm looking for my next role"}, {"auto": 3}],
     "expect": {"direction": "job", "first_question_field_in": ["posture", "level", "work_type", "title_flexibility", "metro", "work_mode"], "never_asks_known": True, "reaches_ready_within": 4,
                "brief_known_include": ["role_family", "level"], "contract_kind": "job", "max_calls": 8}},
    {"id": "v3_hiring_no_jd", "steps": [{"message": "I'm hiring"},
                                        {"message": "A backend engineer for our payments team. They'd own the ledger and money movement end to end; if we don't hire, we can't launch in Q1."},
                                        {"message": "Must have built a ledger or payments system in production; Go or Python. Senior, not staff. Remote US. $180-230k."},
                                        {"auto": 3}],
     "expect": {"direction": "candidate", "first_question_field_in": ["mission"], "reaches_ready_within": 6, "contract_kind": "person", "contract_center_value": "senior",
                "brief_known_include": ["mission", "role_family", "level"], "contract_must_keys_exclude": ["metro"], "max_calls": 8}},
    {"id": "v3_ambiguous_titles", "steps": [{"message": "Founder CTO or VP / Director Engineering ML Infra, Distributed Systems"}, {"answer": {"name": "direction", "value": "job"}}, {"auto": 3}],
     "expect": {"first_question_field_in": ["direction"], "direction": "job", "reaches_ready_within": 5, "contract_kind": "job"}},
    {"id": "v3_owner_jobs_for_profile", "steps": [{"message": "jobs for mid level software engineer skilled in building large scale systems using java ecosystems and has experience in k8 and aws tools."},
                                                  {"skip_if_question": True}, {"auto": 3}],
     "expect": {"direction_or_switchable": "job", "reaches_ready_within": 5, "never_repeats_question": True, "contract_kind_if_job": "job"}},
    {"id": "v3_start_over", "steps": [{"message": "I'm hiring"}, {"message": "start over — actually I'm looking for a role myself"}],
     "expect": {"stage_last_in": ["restarted", "question", "statement"], "direction_last_in": ["job", None]}},
]


def run(base: str, pc: dict) -> dict:
    token = ""; email = ""
    if pc.get("seed_resume"):
        email = f"intake-eval-{int(time.time()*1000)}@roster.test"
        reg = post(base, "/auth/register", {"email": email, "name": "V3 Eval", "password": "Eval-pass-1234"}); token = reg.get("token") or ""
        post(base, "/me/briefs", {"kind": "resume", "title": "seed", "text": pc["seed_resume"], "make_active": True}, token)
    state = None; turns = []; user_turns = 0; t0 = time.time(); secs = []
    def step(**kw):
        nonlocal state
        t = time.time(); d = post(base, "/intake/v3/step", {**kw, "state": state, "tenant_id": "demo", "country": "us"}, token); secs.append(round(time.time() - t, 1))
        state = d.get("state") or state; turns.append(d); return d
    d = None
    for st in pc["steps"]:
        if d and d.get("stage") == "ready":
            break
        if "message" in st:
            user_turns += 1; d = step(message=st["message"])
        elif "answer" in st:
            user_turns += 1; d = step(answer=st["answer"])
        elif "skip_if_question" in st:
            q = (d or {}).get("question") or {}
            if q:
                user_turns += 1; d = step(answer={"name": q.get("field") or "", "value": "__decline__"})
        elif "auto" in st:
            for _ in range(int(st["auto"])):
                if d and d.get("stage") == "ready":
                    break
                q = (d or {}).get("question") or {}
                user_turns += 1
                if q.get("options"):
                    d = step(answer={"name": q.get("field") or "", "value": q["options"][0]["value"]})
                elif q:
                    d = step(message="Open on that — you decide. Remote or the city is fine.")
                else:
                    d = step(search_now=True)
    e = pc.get("expect") or {}; F = []
    qs = [t["question"] for t in turns if t.get("question")]
    first_q = qs[0]["field"] if qs else None
    if "first_question_field_in" in e and first_q not in e["first_question_field_in"]:
        F.append(f"first question on {first_q!r}, expected one of {e['first_question_field_in']}")
    if e.get("never_asks_known"):
        known_before = set()
        for t in turns:
            q = t.get("question")
            if q and q.get("field") in known_before:
                F.append(f"asked the known field {q['field']!r}")
            known_before |= {b["key"] for b in (t.get("brief") or []) if b.get("source") in ("document", "stored", "stated", "asked")}
    last = turns[-1] if turns else {}
    if "direction_or_switchable" in e:
        if last.get("direction") != e["direction_or_switchable"] and last.get("direction_source") != "inferred":
            F.append(f"direction {last.get('direction')!r} locked in (source {last.get('direction_source')!r}) — expected {e['direction_or_switchable']!r} or an assumption the user can switch")
        if last.get("direction") == e["direction_or_switchable"] and "contract_kind_if_job" in e and last.get("ready") and ((last.get("ready") or {}).get("contract") or {}).get("kind") != e["contract_kind_if_job"]:
            F.append("contract kind mismatch")
    if e.get("never_repeats_question"):
        texts = [q.get("text") for q in qs]
        if len(texts) != len(set(texts)):
            F.append("the same question was asked twice")
    if "direction" in e and last.get("direction") != e["direction"]:
        F.append(f"direction {last.get('direction')!r} != {e['direction']!r}")
    if "direction_last_in" in e and last.get("direction") not in e["direction_last_in"]:
        F.append(f"direction {last.get('direction')!r} not in {e['direction_last_in']}")
    if "stage_last_in" in e and last.get("stage") not in e["stage_last_in"]:
        F.append(f"last stage {last.get('stage')!r} not in {e['stage_last_in']}")
    if "reaches_ready_within" in e:
        if last.get("stage") != "ready":
            F.append(f"not ready after {user_turns} user turns (stage {last.get('stage')!r})")
        elif user_turns > e["reaches_ready_within"]:
            F.append(f"ready only after {user_turns} user turns (> {e['reaches_ready_within']})")
    c = ((last.get("ready") or {}).get("contract") or {})
    if "contract_kind" in e and last.get("ready") and c.get("kind") != e["contract_kind"]:
        F.append(f"contract kind {c.get('kind')!r} != {e['contract_kind']!r}")
    for k in e.get("contract_must_keys_include", []):
        if last.get("ready") and k not in (c.get("must") or {}):
            F.append(f"must lacks {k!r}: {c.get('must')}")
    for k in e.get("contract_must_keys_exclude", []):
        if last.get("ready") and k in (c.get("must") or {}):
            F.append(f"must should not carry {k!r}: {c.get('must')}")
    if "contract_center_value" in e and last.get("ready") and (c.get("center") or {}).get("value") != e["contract_center_value"]:
        F.append(f"center {c.get('center')} != {e['contract_center_value']}")
    known = {b["key"] for b in (last.get("brief") or []) if b.get("source") in ("document", "stored", "stated", "asked")}
    for k in e.get("brief_known_include", []):
        if k not in known:
            F.append(f"brief does not know {k!r} (known: {sorted(known)})")
    calls = int((state or {}).get("calls") or 0)
    if "max_calls" in e and calls > e["max_calls"]:
        F.append(f"{calls} planner calls > {e['max_calls']}")
    if token:
        try:
            for b in (post(base, "/me/briefs", {}, token) if False else []):
                pass
        except Exception:   # noqa: BLE001
            pass
    return {"id": pc["id"], "ok": not F, "failures": F, "user_turns": user_turns, "calls": calls, "secs": secs, "total_secs": round(time.time() - t0, 1),
            "questions": [(q.get("move"), q.get("field"), q.get("text"), [o["label"] for o in q.get("options") or []]) for q in qs],
            "says": [t.get("say") for t in turns], "notes": [t.get("notes") for t in turns], "brief": last.get("brief"), "contract": c, "throwaway": email}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("ROSTER_EVAL_BASE", "https://roster-api-production-3405.up.railway.app"))
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    results = []
    for pc in PERSONAS:
        if only and pc["id"] not in only:
            continue
        try:
            r = run(args.base, pc)
        except Exception as e:   # noqa: BLE001
            r = {"id": pc["id"], "ok": False, "failures": [f"crashed: {str(e)[:120]}"], "questions": [], "secs": [], "calls": 0, "user_turns": 0, "total_secs": 0}
        results.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['id']:28s} turns={r['user_turns']} calls={r['calls']} secs={r['secs']}" + (("  " + " | ".join(r["failures"])) if r["failures"] else ""), flush=True)
        for q in r["questions"]:
            print(f"      Q[{q[0]}/{q[1]}] {str(q[2])[:110]} {q[3]}")
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    out = os.path.join(HERE, "runs", f"v3-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump({"base": args.base, "results": results}, open(out, "w"), indent=1)
    print(f"\n{sum(1 for r in results if r['ok'])}/{len(results)} passed → {out}")


if __name__ == "__main__":
    main()
