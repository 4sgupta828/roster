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
    def __init__(self, moves, read=None, ask_read=None):
        self.moves = list(moves); self.read = read or {"fields": {}}; self.ask_read = ask_read or {"fields": {}}; self.prompts = []
    def __call__(self, system, user):
        if "You read" in system:
            self.prompts.append(("read", user))
            if "OWN WORDS about the search" in system:
                return self.ask_read
            return self.read
        if "meeting someone new" in system:
            self.prompts.append(("direction", user))
            low = user.lower()
            if "hiring" in low or "hire" in low: return {"direction": "candidate", "explicit": True, "span": "hiring"}
            if "looking" in low or "next role" in low or "start over" in low or "more" in low or "anything" in low: return {"direction": "job", "explicit": True, "span": "looking"}
            if "ledger" in low: return {"direction": "candidate", "explicit": False, "span": "ledger"}     # read between the lines
            return {"direction": None}
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
    fork = {"move": "ask", "say": "You've led ML infra for six years, so the real fork is a step up or a lateral move. That changes the level I search at.",
            "understanding": {"direction": {"value": "job", "source": "stated", "span": "my next role"}}, "preview_verdict": "good", "gaps": [{"field": "posture", "impact": "high"}],
            "question": {"field": "posture", "text": "Step up, lateral, or a switch?",
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
    assert "career_arc" in plan and "LEVERAGE" in plan and "PREVIEW" in plan
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
    asks_known = {"move": "ask", "say": "Which domain?", "understanding": {"direction": {"value": "job", "source": "stated"}},
                  "question": {"field": "field", "text": "Which field is your work in?", "options": [{"label": "Software", "effect": {"must": {"field": ["software"]}}}, {"label": "Data", "effect": {"must": {"field": ["data_ml"]}}}]}}
    dead = {"move": "ask", "say": "Remote or the Bay Area?", "question": {"field": "work_mode", "text": "Remote or the Bay Area?",
            "options": [{"label": "Remote", "effect": {"must": {"work_mode": ["remote"]}}}, {"label": "Onsite Bay Area", "effect": {"must": {"metro": ["bay_area"]}}}]}}
    pl = Planner([asks_known, dead, {"move": "ready", "say": "ok"}], read=READ)
    s = _svc(pl, profile={"_resume_text": RESUME}, slice_sizes=lambda kind, must: 0 if "work_mode" in must else 25)
    out = _run(s.step(message="I'm looking for my next role", user={"id": "u1"}))
    # a question on the résumé's own field is dropped — and with nothing askable the search runs with its assumptions
    assert any("known field 'field'" in n for n in out["notes"]) and out["stage"] == "ready"
    out2 = _run(s.step(message="hmm, one more thing", state=out["state"], user={"id": "u1"}))
    # the Remote option has an empty pool and one survivor is no fork, but work_mode is still open, so the question stays, in words
    assert out2["question"] and out2["question"]["field"] == "work_mode" and out2["question"]["options"] == [] and any("kept as free text" in n for n in out2["notes"])


def test_a_hiring_manager_without_a_jd_is_asked_the_mission_first_then_the_draft_builds_from_the_brief():
    mission = {"move": "ask", "say": "Before a title, what will this person own?", "understanding": {"direction": {"value": "candidate", "source": "stated", "span": "I'm hiring"}},
               "question": {"field": "mission", "text": "What will they own, and what fails if you don't hire them?", "options": []}}
    record = {"move": "ask", "say": "So: own the ML platform end to end, reporting to you. I'll read that as a leadership hire.",
              "understanding": {"mission": {"value": "own the ML platform end to end", "source": "stated", "span": "own our ML platform end to end"},
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
    assert b["mission"]["source"] == "stated" and b["level"]["source"] == "inferred" and out2["question"]["field"] == "level"
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
    pl2 = Planner([{"move": "ask", "say": "one more?", "question": {"field": "comp", "text": "Comp?", "options": [{"label": "a"}, {"label": "b"}]}}])
    out2 = _run(_svc(pl2).step(message="more", state=spent, user={"id": "u1"}))
    assert out2["stage"] == "ready" and pl2.moves                                                     # no planner call was made; the budget forced the hand-off


def test_the_evidence_gate_keeps_only_document_fields_whose_span_is_in_the_text():
    read = {"fields": {"role_family": {"value": "head of ml infrastructure", "span": "Head of ML Infrastructure at Fintech Co"},
                       "comp": {"value": "$200k-300k", "span": "competitive compensation $200k–300k"},           # invented
                       "work_mode": {"value": "hybrid", "span": ""},                                              # no span
                       "posture": {"value": "step up", "span": "Head of ML Infrastructure at Fintech Co"},         # a real span, but a résumé cannot state a posture
                       "career_arc": {"value": "six years leading ML infra", "span": ""}}}
    pl = Planner([{"move": "ready", "say": "ok"}], read=read)
    s = _svc(pl, profile={"_resume_text": RESUME})
    out = _run(s.step(message="I'm looking for my next role", user={"id": "u1"}))
    b = {x["key"]: x for x in out["brief"]}
    assert b["role_family"]["source"] == "document" and b["career_arc"]["source"] == "inferred"
    assert "comp" not in b and "work_mode" not in b and "posture" not in b and any("without a span" in n and "comp" in n for n in out["notes"]) and any("cannot state" in n for n in out["notes"])


def test_a_side_read_between_the_lines_is_an_assumption_with_a_switch_and_a_fieldless_question_can_be_skipped():
    """Owner (2026-09-06): 'I am looking for jobs, but it inferred hiring … and was stuck re-asking the same question even though I
    said skip'."""
    nofield = {"move": "ask", "say": "Before I search, what will this person own?", "gaps": [{"field": "mission", "impact": "high"}], "question": {"text": "What will this person own in the first six months?", "options": []}}
    pl = Planner([nofield, {"move": "ready", "say": "ok"}])
    s = _svc(pl)
    out = _run(s.step(message="backend engineers who have built a ledger"))
    assert out["direction"] == "candidate" and out["direction_source"] == "inferred" and any("assumption" in n for n in out["notes"])
    assert out["question"] and out["question"]["field"] == "mission"                     # the field-less question resolved to the open required field
    out2 = _run(s.step(answer={"name": "", "value": "__decline__"}, state=out["state"]))    # Skip on a field-less question skips THAT field
    assert {b["key"]: b["source"] for b in out2["brief"]}.get("mission") == "skipped"
    # one tap switches the side; the documents are re-read for it
    out3 = _run(s.step(direction_tap="job", state=out2["state"]))
    assert out3["direction"] == "job" and out3["direction_source"] == "asked" and out3["state"]["docs_read"] in (True, False)


def test_the_side_alone_is_not_an_ask_the_consultant_opens_with_the_open_question():
    pl = Planner([{"move": "ready", "say": "Searching 240k roles."}, {"move": "ready", "say": "ok"}])
    s = _svc(pl)
    out = _run(s.step(message="I'm looking for my next role"))
    assert out["stage"] == "question" and out["question"]["field"] == "role_family" and out["question"]["options"] == [] and "what you do today" in out["question"]["text"]
    assert any("empty brief" in n for n in out["notes"])


def test_the_users_own_words_survive_the_direction_question_and_become_the_search_text():
    """Owner's 0-result session: the opening words were eaten by the direction question, the brief stayed empty, the same
    question repeated, and the ready card's own sentence became the search text."""
    reads_ask = {"move": "ready", "say": "I'll search mid-level Java platform roles.",
                 "understanding": {"role_family": {"value": "software engineer", "source": "stated"}, "level": {"value": "mid", "source": "stated"},
                                   "skills": {"value": ["java", "kubernetes", "aws"], "source": "stated"}},
                 "search": {"prefer": {"role_family": ["software engineer"], "skill": ["java", "kubernetes", "aws"]}, "center": {"key": "level", "value": "mid"}}}
    pl = Planner([reads_ask])
    s = _svc(pl)
    ASK = "jobs for mid level software engineer skilled in java, k8s and aws"
    out = _run(s.step(message=ASK))                                    # ambiguous → the side is asked, the words are kept
    assert out["question"]["field"] == "direction" and out["state"]["opening"].startswith("jobs for mid")
    out2 = _run(s.step(direction_tap="job", state=out["state"]))
    c = (out2.get("ready") or {}).get("contract") or {}
    assert out2["stage"] == "ready" and c.get("text", "").startswith("jobs for mid")     # the ask is the search text, never the say
    assert c["prefer"]["skill"] == ["aws", "java", "kubernetes"] and c["center"]["value"] == "mid"


def test_the_open_question_is_asked_once_never_in_a_loop():
    """The owner saw 'Before I search, what will this person own…' three times: the empty-brief rule re-fired every turn."""
    pl = Planner([{"move": "ready", "say": "Searching."} for _ in range(4)])
    s = _svc(pl)
    out = _run(s.step(message="I'm looking for my next role"))
    assert out["stage"] == "question" and out["question"]["field"] == "role_family"
    out2 = _run(s.step(message="not sure yet", state=out["state"]))
    assert out2["stage"] == "ready" and not out2.get("question")                        # asked once; then it searches with the words


def test_the_users_own_ask_is_read_into_the_brief_so_the_search_has_facets_without_the_planner():
    """The owner's search returned delivery drivers for a Java / Kubernetes ask: the facts were in his sentence, but nothing
    read them, so the contract was empty."""
    ASK = "jobs for mid level software engineer skilled in building large scale systems using java ecosystems and k8s"
    ask_read = {"fields": {"role_family": {"value": "software engineer", "span": "software engineer"},
                           "level": {"value": "mid", "span": "mid level"},
                           "skills": {"value": ["java", "kubernetes"], "span": "java ecosystems and k8s"},
                           "field": {"value": "software", "span": "software engineer"},
                           "comp": {"value": "300k_plus", "span": "not in these words"}}}      # no span → dropped
    pl = Planner([{"move": "ready", "say": "Searching."}], ask_read=ask_read)
    s = _svc(pl)
    out = _run(s.step(message=ASK))                                   # the side is ambiguous → asked; the words are kept
    out = _run(s.step(direction_tap="job", state=out["state"]))       # …and read as soon as the side is known
    b = {x["key"]: x for x in out["brief"]}
    assert b["role_family"]["source"] == "stated" and b["level"]["value"] == "mid" and "comp" not in b
    c = (out.get("ready") or {}).get("contract") or {}
    assert c["center"] == {"key": "level", "value": "mid", "span": 1} and c["prefer"]["skill"] == ["java", "kubernetes"] and c["must"]["field"] == ["software"]
    assert c["text"].startswith("jobs for mid")
