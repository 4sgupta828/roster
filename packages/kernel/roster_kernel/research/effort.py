"""Research effort multiplier — domain-free structural scaling.

A single float `effort` (1.0 = baseline) scales how HARD the research loop works on a
hard-to-answer question: more ReAct turns, more retrieved results considered, a wider
planner context window, deeper per-atom evidence, more citations, a bigger candidate
pool, and a proportionally larger LLM-call budget.

It scales STRUCTURAL SEARCH ONLY. It never touches the provenance span gate, the
entailment gate, or the compose grounding — "more effort buys more searching, not looser
standards." At `effort <= 1.0` every value is returned byte-identically (a true no-op), so
the feature is a guaranteed no-op when its flag is off.

Every knob is independently clamped to a HARD ceiling so a high multiplier can never run
away with credits (the standing cost constraint). Caps are deliberately conservative; raise
them only after observing `stopped_reason=budget` in production.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Allowed request range (UI exposes 1.0..2.5 in 0.25 steps; clamp defensively here too).
EFFORT_MIN, EFFORT_MAX = 1.0, 2.5

# base → hard cap for each structural knob. Bases match run_react's defaults; caps chosen
# conservatively for the credit budget (k and max_steps kept modest — their volume drives the
# claims-first extractor + fallback grounder call counts, which BudgetState now charges).
_ITERS_CAP = 16            # max_steps ceiling (base 8)
_K_CAP = 20               # retrieval top-k ceiling (base 10)
_PLANNER_WINDOW_CAP = 120  # planner atom window ceiling (base 60)
_ATOM_CAP_CEIL = 6000      # per-atom char window ceiling (base is caller-supplied: 1600, or 6000 under evidence-select)
_COMPOSE_CAP_CEIL = 60     # compose claim cap ceiling (base 30)
_EXTRACT_COLLECT_CEIL = 160  # candidate pool ceiling (base 80)
# LLM-call budget ceiling — the hard cost ceiling. Raised 60 → 120 in lockstep with the default
# base's 40 → 80 (Evidence Contract stage 2 re-plan, see budget.py DEFAULT_MAX_LLM_CALLS): the
# budget now meters calls that previously ran UNcounted (claims-first extract/entail batches, the
# binding judge, fallback grounder, frame repair — worst case ≈29 at default knobs), so the 2×
# raise preserves the same TRUE spend envelope as before — it does not authorize new spend.
_MAX_CALLS_CEIL = 120      # (base 80)


def clamp_effort(effort: float | None) -> float:
    """Coerce any input to a valid multiplier in [EFFORT_MIN, EFFORT_MAX]; 1.0 on garbage."""
    try:
        e = float(effort) if effort is not None else 1.0
    except (TypeError, ValueError):
        e = 1.0
    if math.isnan(e) or math.isinf(e):
        e = 1.0
    return min(max(e, EFFORT_MIN), EFFORT_MAX)


def _scale(base: int, effort: float, cap: int) -> int:
    """Half-up integer scaling, clamped to `cap`. `floor(x+0.5)` (not Python round(), whose
    banker's rounding would make some 0.25-step values non-monotonic)."""
    return min(cap, int(math.floor(base * effort + 0.5)))


@dataclass(frozen=True)
class ScaledEffort:
    effort: float
    max_steps: int
    k: int
    planner_atom_window: int
    atom_cap: int
    compose_claim_cap: int
    extract_collect: int
    max_calls: int


def scale_research_effort(
    effort: float | None,
    *,
    base_max_steps: int = 8,
    base_k: int = 10,
    base_planner_atom_window: int = 60,
    base_atom_cap: int = 1600,
    base_compose_claim_cap: int = 30,
    base_extract_collect: int = 80,
    base_max_calls: int = 80,      # 40 → 80: stage-2 charging re-plan (see _MAX_CALLS_CEIL note)
) -> ScaledEffort:
    """Return the scaled structural knobs for `effort`. At effort<=1.0 returns the bases
    EXACTLY (byte-identical no-op); above 1.0 scales each knob half-up and clamps to its cap.

    Bases are parameters (not hardcoded) so the caller can pass its real configured values
    — e.g. `base_atom_cap` is 1600 normally but 6000 under the evidence-select flag — and 1.0
    still round-trips to that exact value."""
    e = clamp_effort(effort)
    if e <= 1.0:
        return ScaledEffort(
            effort=1.0, max_steps=base_max_steps, k=base_k,
            planner_atom_window=base_planner_atom_window, atom_cap=base_atom_cap,
            compose_claim_cap=base_compose_claim_cap, extract_collect=base_extract_collect,
            max_calls=base_max_calls)
    return ScaledEffort(
        effort=e,
        max_steps=_scale(base_max_steps, e, _ITERS_CAP),
        k=_scale(base_k, e, _K_CAP),
        planner_atom_window=_scale(base_planner_atom_window, e, _PLANNER_WINDOW_CAP),
        atom_cap=_scale(base_atom_cap, e, _ATOM_CAP_CEIL),
        compose_claim_cap=_scale(base_compose_claim_cap, e, _COMPOSE_CAP_CEIL),
        extract_collect=_scale(base_extract_collect, e, _EXTRACT_COLLECT_CEIL),
        max_calls=_scale(base_max_calls, e, _MAX_CALLS_CEIL),
    )
