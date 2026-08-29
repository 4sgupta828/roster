"""Effort-scaling helper: no-op at 1.0, capped + monotonic above it, defensive clamps."""
from roster_kernel.research.effort import clamp_effort, scale_research_effort

STOPS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]

# The default bases used by run_react / ResearchService (kept in sync with the docstring table).
# base_max_calls 40 → 80: stage-2 BudgetState-honesty re-plan (claims-first/binding/fallback/
# frame-repair calls are now charged; see budget.py DEFAULT_MAX_LLM_CALLS).
BASES = dict(base_max_steps=8, base_k=10, base_planner_atom_window=60,
             base_atom_cap=1600, base_compose_claim_cap=30, base_extract_collect=80, base_max_calls=80)


def test_effort_1_0_is_byte_identical_noop():
    sc = scale_research_effort(1.0, **BASES)
    assert sc.effort == 1.0
    assert (sc.max_steps, sc.k, sc.planner_atom_window, sc.atom_cap,
            sc.compose_claim_cap, sc.extract_collect, sc.max_calls) == (8, 10, 60, 1600, 30, 80, 80)


def test_none_and_garbage_are_noop():
    for bad in (None, "nope", float("nan"), float("inf"), 0.0, -5.0, 0.5):
        sc = scale_research_effort(bad, **BASES)
        assert sc.effort == 1.0
        assert (sc.max_steps, sc.k, sc.atom_cap, sc.max_calls) == (8, 10, 1600, 80), bad


def test_effort_2_5_hits_the_capped_table():
    sc = scale_research_effort(2.5, **BASES)
    assert sc.effort == 2.5
    assert sc.max_steps == 16          # 8*2.5=20 → cap 16
    assert sc.k == 20                  # 10*2.5=25 → cap 20
    assert sc.planner_atom_window == 120  # 60*2.5=150 → cap 120
    assert sc.atom_cap == 4000         # 1600*2.5=4000 (< 6000 ceil)
    assert sc.compose_claim_cap == 60  # 30*2.5=75 → cap 60
    assert sc.extract_collect == 160   # 80*2.5=200 → cap 160
    assert sc.max_calls == 120         # 80*2.5=200 → cap 120 (hard cost ceiling, 2× the old 60)


def test_half_up_rounding_at_1_25():
    sc = scale_research_effort(1.25, **BASES)
    assert sc.max_steps == 10          # floor(8*1.25+0.5)=floor(10.5)=10
    assert sc.k == 13                  # floor(10*1.25+0.5)=floor(13.0)=13
    assert sc.planner_atom_window == 75  # floor(60*1.25+0.5)=floor(75.5)=75
    assert sc.max_calls == 100         # floor(80*1.25+0.5)=100


def test_every_knob_is_monotonic_non_decreasing():
    seqs = {k: [] for k in ("max_steps", "k", "planner_atom_window", "atom_cap",
                            "compose_claim_cap", "extract_collect", "max_calls")}
    for e in STOPS:
        sc = scale_research_effort(e, **BASES)
        for k in seqs:
            seqs[k].append(getattr(sc, k))
    for k, vals in seqs.items():
        assert all(b >= a for a, b in zip(vals, vals[1:])), (k, vals)


def test_clamps_above_2_5_to_2_5():
    assert clamp_effort(3.0) == 2.5
    assert clamp_effort(99) == 2.5
    assert scale_research_effort(10.0, **BASES).max_steps == 16   # treated as 2.5


def test_evidence_select_atom_cap_base_6000_round_trips():
    bases = {**BASES, "base_atom_cap": 6000}
    assert scale_research_effort(1.0, **bases).atom_cap == 6000     # no-op preserves the wider base
    assert scale_research_effort(2.5, **bases).atom_cap == 6000     # 6000*2.5 → clamped to 6000 ceil
