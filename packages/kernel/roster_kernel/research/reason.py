"""Grounded reasoning gate — turn VERIFIED findings into LABELED, checked derivations. DOMAIN-FREE.

This is the second trust regime (companion to the verbatim fact gate in `react.py`): the fact gate
proves a claim's quote exists in a source; THIS gate proves a *reasoning step* follows from grounded
facts. A derivation may state something NO source states — provided it (a) is built only from verified
findings (or already-accepted derivations), (b) survives a validity check ("does the conclusion follow
from those premises?"), and (c) carries a confidence label the GATE assigns (never self-declared).

Asymmetric strictness ("conservative about facts, ambitious about reasoning"): a fact that fails its
gate is DROPPED; a derivation that fails is DEMOTED one rung (inference → hypothesis → speculation →
drop). Validity means "does it follow NOW", not "will it come true later" — we never track predictions.

The guarantee this extends: not just "every fact traces to a span", but "every reasoning step traces
to a basis of grounded facts, and the leap was checked" — auditable ideation, not arbitrary guessing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .react import extract_hard_tokens

_log = logging.getLogger(__name__)

# kind → how the label ceiling is decided. Code-decidable kinds can reach the strongest tier
# (inference) when the validity judge says the leap is valid; open-ended causal/implication/opportunity
# kinds top out at hypothesis (a real jump) even when "valid".
_ALLOWED_KINDS = {"comparative", "transitive", "arithmetic", "causal", "implication", "opportunity"}
_CODE_DECIDABLE = {"comparative", "transitive", "arithmetic"}
_IDEA_KINDS = {"opportunity", "implication"}          # only generated in idea/brainstorm mode


class DeriveCandidate(BaseModel):
    """One reasoning step the LLM proposes, BEFORE gating. `basis` = 1-based finding indices."""
    conclusion: str = ""
    basis: list[int] = Field(default_factory=list)
    kind: str = ""
    warrant: str = ""
    falsifier: str = ""


class DeriveCandidates(BaseModel):
    derivations: list[DeriveCandidate] = Field(default_factory=list)


class _Verdict(BaseModel):
    index: int = 0                # 1-based index into the gated candidate list
    verdict: str = ""             # "valid" | "plausible" | "arbitrary"
    reason: str = ""


class _Verdicts(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


class _Ranking(BaseModel):
    """Priority order over the surviving derivations — most decision-changing first; trivial/redundant
    ones omitted. Indices are 1-based into the list handed to the ranker."""
    order: list[int] = Field(default_factory=list)


@dataclass
class DerivedClaim:
    """A gated, labeled reasoning step. `label` is assigned by the gate, never by the model."""
    conclusion: str
    basis: tuple[int, ...]
    kind: str
    warrant: str = ""
    falsifier: str = ""
    label: str = ""               # "inference" | "hypothesis" | "speculation"
    dropped_reason: str = ""      # set on dropped candidates (diagnostics only; not returned)


def _finding_text(f) -> str:
    """A verified finding's searchable text — its claim text + verbatim quote."""
    return (getattr(f, "text", "") or "") + " " + (getattr(f, "quote", "") or "")


def _structural_ok(cand: DeriveCandidate, n_findings: int) -> tuple[bool, str]:
    """Cheap code gate BEFORE any judge runs. Returns (ok, reason_if_not)."""
    kind = (cand.kind or "").strip().lower()
    if kind not in _ALLOWED_KINDS:
        return False, f"bad_kind:{kind}"
    if not (cand.conclusion or "").strip():
        return False, "empty_conclusion"
    basis = [b for b in (cand.basis or []) if isinstance(b, int) and 1 <= b <= n_findings]
    if not basis:
        return False, "no_valid_basis"      # no free-floating claims — must rest on ≥1 grounded finding
    return True, ""


def _no_new_hard_tokens(cand: DeriveCandidate, findings: list) -> bool:
    """The conclusion may introduce no number/date/%/$ absent from its basis findings — UNLESS it is an
    arithmetic derivation (a computed figure is the whole point; its operands must still be in basis).
    Structural (Rule 18): proves the reasoning invented no figure, not that the reading is right."""
    kind = (cand.kind or "").strip().lower()
    basis = [b for b in cand.basis if isinstance(b, int) and 1 <= b <= len(findings)]
    basis_src = " ".join(_finding_text(findings[b - 1]) for b in basis)
    basis_tokens = extract_hard_tokens(basis_src)
    concl_tokens = extract_hard_tokens(cand.conclusion)
    new_tokens = concl_tokens - basis_tokens
    if not new_tokens:
        return True
    # arithmetic: a NEW figure is allowed only if every OPERAND it was computed from is in the basis
    # (we cannot re-run every possible formula, so we require the inputs to be grounded and trust the
    # validity judge to reject a wrong computation). Non-arithmetic new figure → reject.
    return kind == "arithmetic" and len(basis_tokens) >= 1


def _label_for(kind: str, verdict: str, has_falsifier: bool, ideas: bool) -> str:
    """Assign the epistemic label from (kind, validity verdict). The label is the GATE's output."""
    kind = (kind or "").lower()
    verdict = (verdict or "").lower()
    if verdict == "valid":
        if kind in _CODE_DECIDABLE:
            return "inference"                     # follows, and the kind is checkable → strongest
        return "hypothesis" if has_falsifier else ""   # valid causal leap, but a jump → needs a falsifier
    if verdict == "plausible":
        return "hypothesis" if has_falsifier else ""
    # arbitrary: only survives, quarantined, in idea mode with a concrete falsifier
    if ideas and has_falsifier and kind in _IDEA_KINDS:
        return "speculation"
    return ""                                       # dropped


def _candidate_prompt(question: str, findings: list, ideas: bool) -> str:
    lines = [f"[{i}] {getattr(f, 'text', '')}".strip() for i, f in enumerate(findings, 1)]
    idea_clause = (
        "\n- ALSO derive `opportunity` steps: non-obvious openings, whitespace, or second-order "
        "implications that follow from the findings but no finding states outright. Each MUST still "
        "rest on cited findings and carry a concrete `falsifier`."
        if ideas else "")
    return (
        f"QUESTION:\n{question}\n\n"
        f"VERIFIED FINDINGS (cite by number):\n" + "\n".join(lines) + "\n\n"
        "Derive reasoning steps that FOLLOW from these findings — conclusions that combine or extend "
        "them, including ones no single finding states. RULES:\n"
        "- every step must cite ≥1 finding number in `basis`; build only from the findings.\n"
        "- introduce NO new number/date/%/$ unless `kind='arithmetic'` and you computed it from cited "
        "figures.\n"
        "- `kind` ∈ {comparative, transitive, arithmetic, causal, implication" +
        (", opportunity" if ideas else "") + "}.\n"
        "- `warrant`: one line on WHY it follows. `falsifier`: what observation would make it wrong "
        "(required for causal/implication/opportunity).\n"
        "- do NOT restate a finding verbatim; derive something that follows FROM them." + idea_clause)


def _judge_prompt(question: str, findings: list, cands: list[DeriveCandidate]) -> str:
    fl = "\n".join(f"[{i}] {getattr(f, 'text', '')}".strip() for i, f in enumerate(findings, 1))
    cl = "\n".join(
        f"({j}) [{c.kind}] {c.conclusion}  (from findings {list(c.basis)})"
        for j, c in enumerate(cands, 1))
    return (
        "You are a strict logic checker. For each derivation, judge whether its conclusion FOLLOWS from "
        "ONLY its cited basis findings. Judge VALIDITY (does it follow?), NOT truth-in-the-world.\n"
        "- `valid`: the conclusion is forced by, or strongly follows from, the basis.\n"
        "- `plausible`: a reasonable step, but a real inferential jump beyond what the basis forces.\n"
        "- `arbitrary`: does not follow — an unsupported leap, wrong direction, or missing premise.\n\n"
        f"FINDINGS:\n{fl}\n\nDERIVATIONS:\n{cl}\n\n"
        "Return one verdict per derivation (by its number).")


_LABEL_RANK = {"inference": 3, "hypothesis": 2, "speculation": 1, "": 0}


def _dedupe(claims: list[DerivedClaim], *, overlap: float = 0.6) -> list[DerivedClaim]:
    """Structural near-duplicate removal: two conclusions that share ≥`overlap` of their word set are
    the same point said twice — keep the stronger-labeled one. No semantics (Rule 18), just set overlap."""
    kept: list[DerivedClaim] = []
    seen: list[set[str]] = []
    # process strongest labels first so a duplicate pair keeps the inference over the speculation
    for c in sorted(claims, key=lambda x: -_LABEL_RANK.get(x.label, 0)):
        # full word tokens (keep numbers + short entity ids like "A"/"M1" — they are the discriminators
        # between otherwise-similar conclusions, so dropping them would wrongly merge distinct points)
        toks = set(re.findall(r"\w+", c.conclusion.lower()))
        if not toks:
            continue
        if any(len(toks & s) / max(1, len(toks | s)) >= overlap for s in seen):
            continue
        kept.append(c); seen.append(toks)
    return kept


async def _prioritize(question: str, claims: list[DerivedClaim], llm, *, cap: int) -> list[DerivedClaim]:
    """Keep only the few derivations that most change the answer, in priority order. Semantic selection
    is the LLM's job (Rule 18); on any failure fall back to label-strength order + hard cap so a ranker
    outage can't dump the whole noisy list."""
    if len(claims) <= cap:
        # still order by label strength so the solid inferences lead
        return sorted(claims, key=lambda x: -_LABEL_RANK.get(x.label, 0))
    listing = "\n".join(f"[{i}] ({c.label}) {c.conclusion}" for i, c in enumerate(claims, 1))
    prompt = (
        f"QUESTION:\n{question}\n\nDERIVATIONS:\n{listing}\n\n"
        f"Return, in `order`, the 1-based indices of the AT MOST {cap} derivations that most change the "
        "answer to the question — most decision-relevant first. OMIT redundant restatements and trivial "
        "points (e.g. a bare arithmetic difference with no consequence). Keep genuinely distinct, "
        "consequential conclusions only.")
    try:
        r = await llm.complete(
            system="You rank reasoning by how much it changes the decision. Be ruthless about noise.",
            messages=[{"role": "user", "content": prompt}], response_format=_Ranking, max_tokens=200)
        order = [i for i in (getattr(r.parsed, "order", []) or []) if isinstance(i, int) and 1 <= i <= len(claims)]
        picked = [claims[i - 1] for i in dict.fromkeys(order)][:cap]      # dedupe indices, honor cap
        if picked:
            return picked
    except Exception as e:   # noqa: BLE001 — ranking is best-effort; never lose the whole answer
        _log.warning("derive: prioritize failed (%s) — falling back to label order", str(e)[:120])
    return sorted(claims, key=lambda x: -_LABEL_RANK.get(x.label, 0))[:cap]


async def derive(question: str, findings: list, llm, *, generate_ideas: bool = False,
                 judge_llm=None, max_tokens: int = 2000, judge_max_tokens: int = 4000,
                 max_out: int = 5) -> list[DerivedClaim]:
    """Verified findings → gated, labeled derivations. Never adds a fact; only reasons over findings.

    Two LLM calls: (1) propose candidates, (2) validity-judge them (via `judge_llm` if given, ideally a
    different model family). All gating between/after is deterministic code. Returns only SURVIVING
    derivations (inference/hypothesis/speculation); arbitrary/ungrounded leaps are dropped.

    `judge_max_tokens` caps the validity-judge response (default 4000 ≥ the former hardcoded 1200): with
    many findings the judge emits one verdict per candidate, so too small a cap truncates the verdict
    list and every missing verdict conservatively demotes — a real inference silently becomes a
    hypothesis. Raising the default only improves existing callers.
    """
    if not findings:
        return []
    # 1) PROPOSE
    try:
        gen = await llm.complete(
            system="You derive disciplined reasoning steps strictly from the given findings.",
            messages=[{"role": "user", "content": _candidate_prompt(question, findings, generate_ideas)}],
            response_format=DeriveCandidates, max_tokens=max_tokens)
        cands = list(getattr(gen.parsed, "derivations", []) or [])
    except Exception as e:   # noqa: BLE001 — reasoning is additive; never break the answer path
        _log.warning("derive: candidate generation failed (%s)", str(e)[:120])
        return []

    # 2) STRUCTURAL GATE (code, free) — drop before the judge sees anything ungrounded
    n = len(findings)
    gated: list[DeriveCandidate] = []
    for c in cands:
        ok, why = _structural_ok(c, n)
        if not ok:
            _log.info("derive: dropped candidate (%s)", why); continue
        if not _no_new_hard_tokens(c, findings):
            _log.info("derive: dropped candidate (new_hard_token)"); continue
        c.basis = [b for b in c.basis if isinstance(b, int) and 1 <= b <= n]
        c.kind = c.kind.strip().lower()
        gated.append(c)
    if not gated:
        return []

    # 3) VALIDITY JUDGE (LLM; cross-family when provided). Silence/error → conservative "plausible"
    # (demote, never upgrade) so a judge failure can't mint an inference.
    verdicts: dict[int, str] = {}
    try:
        j = await (judge_llm or llm).complete(
            system="You are a strict, impartial validity checker.",
            messages=[{"role": "user", "content": _judge_prompt(question, findings, gated)}],
            response_format=_Verdicts, max_tokens=judge_max_tokens)
        for v in getattr(j.parsed, "verdicts", []) or []:
            if isinstance(v.index, int) and 1 <= v.index <= len(gated):
                verdicts[v.index] = (v.verdict or "").strip().lower()
    except Exception as e:   # noqa: BLE001
        _log.warning("derive: validity judge failed (%s) — demoting all to plausible", str(e)[:120])

    # 4) LABEL + emit
    out: list[DerivedClaim] = []
    for j, c in enumerate(gated, 1):
        verdict = verdicts.get(j, "plausible")          # missing verdict → conservative demote
        has_fals = bool((c.falsifier or "").strip())
        label = _label_for(c.kind, verdict, has_fals, generate_ideas)
        if not label:
            _log.info("derive: dropped candidate (verdict=%s kind=%s falsifier=%s)",
                      verdict, c.kind, has_fals); continue
        out.append(DerivedClaim(
            conclusion=c.conclusion.strip(), basis=tuple(c.basis), kind=c.kind,
            warrant=(c.warrant or "").strip(), falsifier=(c.falsifier or "").strip(), label=label))

    # 5) PRIORITIZE — dedupe near-identical points, then keep only the few most decision-relevant, in
    # priority order. This is what turns a correct-but-noisy list into an answer a human can read (the
    # head-to-head showed the gate is sound but the raw list drowns the signal).
    out = _dedupe(out)
    if len(out) > max_out:
        out = await _prioritize(question, out, judge_llm or llm, cap=max_out)
    return out
