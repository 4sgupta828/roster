"""Guided v3 — the consultant service over fakes (no DB, no model): documents read once into the brief with sources, the
planner's move parsed and gated, forks probed against the index, answers land as `asked`, readiness → the ONE contract
with the hardness rule, drafting, restart, the call budget."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import InMemoryFacetStore
from roster_vertical.facet_schema import FACET_SCHEMA

from api.consultant import IntakeConsultant

PEOPLE = [{"id": f"p{i}", "kind": "person", "sim": 0.7 - i * 0.01, "facets": {"field": ["data_ml"], "function": ["engineering"], "level": ["leadership" if i % 3 else "staff_plus"],
                                                                         "metro": ["bay_area"], "country": ["us"], "work_type": ["executive" if i % 3 else "ic"]}} for i in range(30)]
JOBS = [{"id": f"j{i}", "kind": "job", "sim": 0.7 - i * 0.01, "facets": {"field": ["data_ml"], "function": ["engineering"], "level": ["leadership" if i % 2 else "staff_plus"],
                                                                        "metro": ["bay_area"], "country": ["us"], "work_mode": ["remote" if i % 2 else "hybrid"], "comp": ["200k_300k"] if i % 2 else []}} for i in range(30)]


def _run(c):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(c)


class Planner:
    """Scripted planner: `moves` are consumed in order for consultant calls; the reader prompt gets `read`."""
    def __init__(self, moves, read=None):
        self.moves = list(moves); self.read = read or {"fields": {}}; self.prompts = []
    def __call__(self, system, user):
        if "You read" in system:
            self.prompts.append(("read", user)); return self.read
        if "meeting someone new" in system:
            self.prompts.append(("direction", user))
            low = user.lower()
            if "hiring" in low or "hire" in low: return {"move": "infer", "brief_delta": {"direction": {"value": "candidate", "source": "stated"}}}
            if "looking" in low or "next role" in low or "start over" in low or "more" in low or "anything" in low: return {"move": "infer", "brief_delta": {"direction": {"value": "job", "source": "stated"}}}
            return {"move": "fork", "say": "Could be either.", "question": {"field": "direction", "text": "Looking, or hiring?", "options": [{"label": "Looking for a role", "value": "job"}, {"label": "Hiring", "value": "candidate"}]}}
        self.prompts.append(("plan", user))
        return self.moves.pop(0) if self.moves else {"move": "ready", "say": "Searching with what I have."}


def _svc(planner, *, profile=None, stored=None, draft=None, slice_sizes=None):
    store = InMemoryFacetStore(JOBS + PEOPLE, FACET_SCHEMA)
    async def counts_fn(kind, must): return await store.counts(kind, must, FACET_SCHEMA)
    async def slice_fn(kind, must):
        if slice_sizes is not None:
            return slice_sizes(kind, must)
        return await store.count_rows(kind, must) if hasattr(store, "count_rows") else 10
    async def profile_fn(user): return profile
    async def stored_fn(user): return stored
    async def briefs_fn(user): return []
    async def draft_fn(role_text, ctx):
        draft["calls"].append((role_text, ctx)); return {"title": "Head of ML Infrastructure", "text": "Head of ML Infrastructure\n\nWhat you will own\n- the platform", "peers": [1, 2, 3]}
    async def ia(c, *, kind, user_keys, place_or_mode): return c, [{"rule": "readings", "readings": []}]
    return IntakeConsultant(schema=FACET_SCHEMA, llm_json=planner, counts_fn=counts_fn, slice_fn=slice_fn, profile_fn=profile_fn, stored_fn=stored_fn,
                            briefs_fn=briefs_fn, draft_fn=(draft_fn if draft is not None else None), index_aware_fn=ia)


RESUME = "Sam Doe. Head of ML Infrastructure at Fintech Co (2018–now), leading 12 engineers; before that Staff Engineer at Google. Kubernetes, Ray, feature stores. San Francisco." * 2
READ = {"fields": {"role_family": {"value": "head of ml infrastructure", "span": "Head of ML Infrastructure at Fintech Co"},
                   "level": {"value": "leadership", "span": "Head of ML Infrastructure"}, "field": {"value": "data_ml", "span": "ML Infrastructure"},
                   "metro": {"value": "bay_area", "span": "San Francisco"}, "career_arc": {"value": "six years leading ML infra, from staff engineer at Google to head of a 12-person team", "span": ""}}}


def test_a_seeker_with_a_resume_gets_a_posture_fork_first_and_the_answer_becomes_the_contract():
    fork = {"move": "fork", "say": "You've led ML infra for six years, so the real fork is a step up or a lateral move. That changes the level I search at.",
            "brief_delta": {"direction": {"value": "job", "source": "stated", "span": "my next role"}},
            "question": {"field": "posture", "text": "Step up, lateral, or a switch?", "why": "it sets the level centre",
                         "options": [{"label": "Step up", "value": "step_up", "effect": {"center": {"key": "level", "value": "leadership"}}},
                                     {"label": "Lateral", "value": "lateral", "effect": {"center": {"key": "level", "value": "staff_plus"}}}]}}
    ready = {"move": "ready", "say": "I'll search leadership ML infra roles in the Bay Area; assuming you're open to remote."}
    pl = Planner([fork, ready], read=READ)
    s = _svc(pl, profile={"_resume_text": RESUME}, stored={"profile": {"city": "San Francisco", "work_authorization": "US citizen"}})
    out = _run(s.step(message="I'm looking for my next role", user={"id": "u1"}))
    # documents were read once, into the brief with sources
    assert out["state"]["docs_read"] and out["state"]["artifact"]["source"] == "on_file"
    b = {x["key"]: x for x in out["brief"]}
    assert b["role_family"]["source"] == "document" and b["level"]["value"] == "leadership" and b["career_arc"]["source"] == "inferred" and b["authorization"]["source"] == "stored"
    # the planner saw the arc, the leverage and the market; and asked the posture fork with two surviving options
    plan = [u for k, u in pl.prompts if k == "plan"][0]
    assert "career_arc" in plan and "LEVERAGE" in plan and "REQUIRED BEFORE SEARCH" in plan
    assert out["stage"] == "question" and out["question"]["field"] == "posture" and [o["label"] for o in out["question"]["options"]] == ["Step up", "Lateral"]
    # tap "Step up" → asked; the planner says ready → the contract: document fields filter, the tap's centre applies, user_keys carry the tap
    out2 = _run(s.step(answer={"name": "posture", "value": "step_up"}, state=out["state"], user={"id": "u1"}))
    assert out2["stage"] == "ready"
    c = out2["ready"]["contract"]
    assert c["center"] == {"key": "level", "value": "leadership", "span": 1} and c["must"]["field"] == ["data_ml"] and c["must"]["metro"] == ["bay_area"]
    assert "head of ml infrastructure" in " ".join(c["prefer"].get("role_family") or []) and c["kind"] == "person" or c["kind"] == "job"
    assert {x["key"]: x["source"] for x in out2["ready"]["brief"]}["posture"] == "asked"
    assert "next role" in c["text"] and "Fintech Co" not in c["text"]                                   # intent words, never the résumé body


def test_a_question_on_a_known_field_is_dropped_and_the_fork_gate_drops_dead_options():
    asks_known = {"move": "fork", "say": "Which domain?", "brief_delta": {"direction": {"value": "job", "source": "stated"}},
                  "question": {"field": "field", "text": "Which field is your work in?", "options": [{"label": "Software", "effect": {"must": {"field": ["software"]}}}, {"label": "Data", "effect": {"must": {"field": ["data_ml"]}}}]}}
    dead = {"move": "fork", "say": "Remote or the Bay Area?", "question": {"field": "work_mode", "text": "Remote or the Bay Area?",
            "options": [{"label": "Remote", "effect": {"must": {"work_mode": ["remote"]}}}, {"label": "Onsite Bay Area", "effect": {"must": {"metro": ["bay_area"]}}}]}}
    pl = Planner([asks_known, dead, {"move": "ready", "say": "ok"}], read=READ)
    s = _svc(pl, profile={"_resume_text": RESUME}, slice_sizes=lambda kind, must: 0 if "work_mode" in must else 25)
    out = _run(s.step(message="I'm looking for my next role", user={"id": "u1"}))
    assert out["question"] is None and any("known field 'field'" in n for n in out["notes"])          # never asks what the résumé states
    out2 = _run(s.step(message="anything else?", state=out["state"], user={"id": "u1"}))
    # the Remote option has an empty pool; one survivor is no fork — but work_mode is still open, so the question stays, in words
    assert out2["question"] and out2["question"]["options"] == [] and any("kept as free text" in n for n in out2["notes"])


def test_a_hiring_manager_without_a_jd_is_asked_the_mission_first_then_the_draft_builds_from_the_brief():
    mission = {"move": "fork", "say": "Before a title, what will this person own?", "brief_delta": {"direction": {"value": "candidate", "source": "stated", "span": "I'm hiring"}},
               "question": {"field": "mission", "text": "What will they own, and what fails if you don't hire them?", "options": []}}
    record = {"move": "confirm", "say": "So: own the ML platform end to end, reporting to you. I'll read that as a leadership hire.",
              "brief_delta": {"mission": {"value": "own the ML platform end to end", "source": "stated", "span": "own our ML platform end to end"},
                              "level": {"value": "leadership", "source": "inferred"}, "role_family": {"value": "head of ml platform", "source": "inferred"}, "field": {"value": "data_ml", "source": "inferred"}},
              "question": {"field": "level", "text": "Leadership hire — right?", "options": [{"label": "Yes", "effect": {"center": {"key": "level", "value": "leadership"}}}, {"label": "No, staff IC", "effect": {"center": {"key": "level", "value": "staff_plus"}}}]}}
    draft = {"move": "draft", "say": "Let me write the JD from that and the market."}
    calls = {"calls": []}
    pl = Planner([mission, record, draft])
    s = _svc(pl, draft=calls)
    out = _run(s.step(message="I'm hiring", user={"id": "u2"}))
    assert out["stage"] == "question" and out["question"]["field"] == "mission" and out["question"]["options"] == [] and out["artifact"]["status"] == "missing"
    out2 = _run(s.step(message="They'd own our ML platform end to end, reporting to me. If we don't hire, the platform stalls.", state=out["state"], user={"id": "u2"}))
    b = {x["key"]: x for x in out2["brief"]}
    assert b["mission"]["source"] == "stated" and b["level"]["source"] == "inferred" and out2["question"]["move"] == "confirm"
    out3 = _run(s.step(answer={"name": "level", "value": "Yes"}, state=out2["state"], user={"id": "u2"}))
    assert out3["stage"] == "draft" and out3["jd"]["title"] == "Head of ML Infrastructure" and out3["artifact"]["source"] == "drafted"
    role_text, ctx = calls["calls"][0]
    assert ctx["What this person owns"].startswith("own the ML platform") and "own the ML platform" in role_text


def test_start_over_and_the_call_budget():
    pl = Planner([{"move": "restart", "say": "Starting over — what are we working on?"}], read=READ)
    s = _svc(pl, profile={"_resume_text": RESUME})
    st = {"v": 3, "brief": {"direction": {"value": "job", "source": "stated"}, "field": {"value": "software", "source": "stated"}}, "transcript": [{"role": "user", "text": "x"}], "pending": None,
          "artifact": {"kind": "resume", "status": "present"}, "artifact_text": "abc", "overlay": {"must": {"field": ["software"]}}, "forks": 2, "calls": 3, "docs_read": True, "record_id": "r1", "opening": "x"}
    out = _run(s.step(message="start over", state=st, user={"id": "u1"}))
    assert out["stage"] == "restarted" and out["state"]["brief"] == {} and out["state"]["overlay"] == {} and out["state"]["calls"] == 0 and out["brief"] == []
    from roster_vertical.consultant import MAX_PLANNER_CALLS
    spent = dict(st); spent["calls"] = MAX_PLANNER_CALLS
    pl2 = Planner([{"move": "fork", "say": "one more?", "question": {"field": "comp", "text": "Comp?", "options": [{"label": "a"}, {"label": "b"}]}}])
    out2 = _run(_svc(pl2).step(message="more", state=spent, user={"id": "u1"}))
    assert out2["stage"] == "ready" and pl2.moves                                                     # no planner call was made; the budget forced the hand-off
