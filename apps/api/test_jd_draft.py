"""JD draft from the centre of peer postings (spec §4): peers deduped by company; the centre = groups recurring in
≥ 30 % of peers; no uncited line survives; a sparse pool drafts from the manager's words only; market signal
comes from the peers' facets. Fake model; no DB."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import Contract

from api.jd_draft import build_jd_draft, centre_of, dedupe_peers, redraft


def _run(c):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(c)


ROWS = [{"id": i, "company": f"co{i % 12}", "title": "Backend Engineer", "url": f"https://x/{i}", "facets": {"comp": ["200k_300k"] if i % 2 else [], "work_mode": ["remote"] if i % 3 else ["hybrid"]},
         "display": {"comp": "$200,000 – $280,000"} if i % 2 else {}} for i in range(40)]


def _summaries(peers):
    out = {}
    for i, p in enumerate(peers):
        out[str(p["id"])] = {"key_requirements": ["5+ years backend engineering", "Go or Python", "distributed systems"] + (["Kafka"] if i < 2 else []),
                             "nice_to_have": ["PCI exposure"] if i < 4 else [], "responsibilities": ["own the ledger service"]}
    return out


class FakeLLM:
    def __init__(self): self.calls = []
    def __call__(self, system, user):
        self.calls.append(system[:30])
        if "group job-posting requirement lines" in system:
            return {"groups": [{"label": "5+ years backend engineering", "kind": "must", "peers": list(range(10))},
                               {"label": "Go or Python", "kind": "must", "peers": list(range(10))},
                               {"label": "distributed systems experience", "kind": "must", "peers": list(range(9))},
                               {"label": "Kafka / event streaming", "kind": "must", "peers": [0, 1]},
                               {"label": "PCI exposure", "kind": "nice", "peers": [0, 1, 2, 3]},
                               {"label": "own the ledger service", "kind": "responsibility", "peers": list(range(10))},
                               {"label": "invented thing", "kind": "must", "peers": []}]}
        if "You write a job description" in system:
            return {"title": "Senior Backend Engineer, Payments", "summary": "Own ledger and money movement.",
                    "responsibilities": [{"text": "Own the ledger service end to end", "source": "g5"}],
                    "must_have": [{"text": "Built payments or ledgering systems in production", "source": "you"},
                                  {"text": "5+ years backend engineering", "source": "g0"},
                                  {"text": "Go or Python", "source": "g1"},
                                  {"text": "Knows Rust deeply", "source": "g99"},          # uncited → dropped
                                  {"text": "Has a PhD", "source": ""}],                   # uncited → dropped
                    "nice_to_have": [{"text": "PCI exposure", "source": "g4"}]}
        raise AssertionError(system[:40])


def _compile(kind, text, *, limit=60, scope=None):
    return Contract(kind=kind, text=text, must={"field": ["software"]}, limit=limit)


def test_peers_dedupe_by_company_and_the_centre_is_share_based():
    peers = dedupe_peers(ROWS, 10)
    assert len(peers) == 10 and len({p["company"] for p in peers}) == 10
    centre, optional = centre_of([{"label": "a", "kind": "must", "peers": [0, 1, 2]}, {"label": "b", "kind": "must", "peers": [0, 1]}, {"label": "", "peers": [0]}], 10, 0.3)
    assert [g["label"] for g in centre] == ["a"] and [g["label"] for g in optional] == ["b"]
    assert centre[0]["share"] == 0.3 and centre[0]["id"] == "g0"


def test_draft_cites_every_line_and_drops_uncited_ones():
    llm = FakeLLM()
    async def evaluate_fn(c): return {"rows": ROWS}
    async def summaries_fn(peers): return _summaries(peers)
    d = _run(build_jd_draft(role_text="senior backend engineer payments", context={"must_skills": "built payments or ledgering systems"},
                            evaluate_fn=evaluate_fn, summaries_fn=summaries_fn, compile_fn=_compile, llm_json=llm))
    assert len(d["peers"]) == 10 and not d["sparse"]
    assert [g["label"] for g in d["centre"]][:3] == ["5+ years backend engineering", "Go or Python", "own the ledger service"] or len(d["centre"]) == 5
    assert all(g["share"] >= 0.3 for g in d["centre"]) and any(g["label"].startswith("Kafka") for g in d["optional"])
    musts = [x["text"] for x in d["must_have"]]
    assert "Knows Rust deeply" not in musts and "Has a PhD" not in musts                     # no uncited line
    assert d["must_have"][0]["support"] == {"you": True}                                     # the manager's must-have first
    assert d["must_have"][1]["support"]["count"] == 10 and d["must_have"][1]["support"]["share"] == 1.0
    assert d["market"]["stated_pay"] == 5 and d["market"]["comp"] == {"200k_300k": 5} and d["market"]["work_mode"]["remote"] >= 6
    assert "Market signal: 5 of 10 peer postings state pay" in d["text"] and "- Go or Python" in d["text"]
    assert d["sources"]["posting_ids"] == [p["id"] for p in d["peers"]]
    assert sum(c.startswith("You group") for c in llm.calls) == 1 and sum(c.startswith("You write") for c in llm.calls) == 1


def test_sparse_pool_drafts_from_the_managers_words_only():
    class OnlyDraft(FakeLLM):
        def __call__(self, system, user):
            if "group" in system and "requirement lines" in system:
                raise AssertionError("no grouping on a sparse pool")
            return {"title": "Payments Engineer", "summary": "s", "responsibilities": [], "must_have": [{"text": "ledgering", "source": "you"}, {"text": "x", "source": "g0"}], "nice_to_have": []}
    async def evaluate_fn(c): return {"rows": ROWS[:3]}
    async def summaries_fn(peers): return _summaries(peers)
    d = _run(build_jd_draft(role_text="payments engineer", context={}, evaluate_fn=evaluate_fn, summaries_fn=summaries_fn, compile_fn=_compile, llm_json=OnlyDraft()))
    assert d["sparse"] and d["centre"] == [] and [x["text"] for x in d["must_have"]] == ["ledgering"]   # g0 does not exist → dropped


def test_redraft_applies_a_change_in_words_and_keeps_the_citation_rule():
    llm = FakeLLM()
    async def evaluate_fn(c): return {"rows": ROWS}
    async def summaries_fn(peers): return _summaries(peers)
    d = _run(build_jd_draft(role_text="senior backend engineer payments", context={}, evaluate_fn=evaluate_fn, summaries_fn=summaries_fn, compile_fn=_compile, llm_json=llm))
    class Changer(FakeLLM):
        def __call__(self, system, user):
            assert "CHANGE REQUEST" in user and "add Kafka" in user
            return {"title": d["title"], "summary": d["summary"], "responsibilities": [], "must_have": [{"text": "Kafka", "source": "you"}, {"text": "Go or Python", "source": "g1"}], "nice_to_have": []}
    d2 = _run(redraft(d, "add Kafka as a must, drop the nice-to-haves", Changer()))
    assert [x["text"] for x in d2["must_have"]] == ["Kafka", "Go or Python"] and d2["nice_to_have"] == []
    assert d2["sources"]["changes"] == ["add Kafka as a must, drop the nice-to-haves"] and "- Kafka" in d2["text"]


def test_peers_are_the_similarity_neighbourhood_only_the_field_filters():
    seen = {}
    async def evaluate_fn(c): seen.update(c); return {"rows": ROWS}
    async def summaries_fn(peers): return _summaries(peers)
    def strict(kind, text, *, limit=60, scope=None):
        return Contract(kind=kind, text=text, must={"field": ["software"], "metro": ["san_francisco"], "skill": ["kafka", "ledger"]}, limit=limit)
    _run(build_jd_draft(role_text="payments engineer", context={}, evaluate_fn=evaluate_fn, summaries_fn=summaries_fn, compile_fn=strict, llm_json=FakeLLM()))
    assert seen["must"] == {"field": ["software"]} and sorted(seen["prefer"]["skill"]) == ["kafka", "ledger"] and seen["prefer"]["metro"] == ["san_francisco"]


def test_peers_below_the_relevance_floor_do_not_form_a_centre():
    """'Keep going' as a role text once drafted an ICU-nurse JD from ten irrelevant 'peers'."""
    low = [dict(r, match_pct=8) for r in ROWS]
    async def evaluate_fn(c): return {"rows": low}
    async def summaries_fn(peers): return _summaries(peers)
    class OnlyDraft(FakeLLM):
        def __call__(self, system, user):
            if "group" in system and "requirement lines" in system:
                raise AssertionError("no grouping on irrelevant peers")
            assert "MANAGER'S ROLE" in user and "hire a CTO" in user
            return {"title": "CTO", "summary": "s", "responsibilities": [], "must_have": [{"text": "led a 10–15 person team", "source": "you"}], "nice_to_have": []}
    d = _run(build_jd_draft(role_text="hire a CTO for my startup to lead a 10-15 person team", context={}, evaluate_fn=evaluate_fn, summaries_fn=summaries_fn, compile_fn=_compile, llm_json=OnlyDraft()))
    assert d["sparse"] and d["peers"] == [] and d["title"] == "CTO" and d["must_have"][0]["support"] == {"you": True}
