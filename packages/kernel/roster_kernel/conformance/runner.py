"""VerticalConformance — the suite every vertical must pass (CI gate).

A registry of named checks run against a VerticalManifest. Checks are added as
each phase lands its contract; a check declares the minimum phase at which it
becomes REQUIRED, so early (partial) manifests aren't failed for slots that
don't exist yet.

`run_conformance(manifest, phase=...)` returns a report; `report.ok` is True iff
every check required at/below `phase` passed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from roster_kernel.contract.manifest import VerticalManifest
from roster_kernel.contract.protocols import (
    CitationVerifier,
    Connector,
    GatingPolicy,
    Persona,
    RetrievalSource,
    UIContract,
)

# phase ordering for "required at/below"
_PHASES = ["P0", "P1", "P2", "P3", "P4", "P5"]


def _phase_leq(a: str, b: str) -> bool:
    return _PHASES.index(a) <= _PHASES.index(b)


@dataclass
class CheckResult:
    name: str
    required_phase: str
    passed: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class ConformanceReport:
    results: list[CheckResult]

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results if not r.skipped)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = "skip" if r.skipped else ("pass" if r.passed else "FAIL")
            lines.append(f"[{mark}] {r.required_phase} {r.name}{' — ' + r.detail if r.detail else ''}")
        lines.append(f"=> {'OK' if self.ok else 'FAILED'}")
        return "\n".join(lines)


# (name, required_phase, fn: manifest -> (passed, detail))
_CHECKS: list[tuple[str, str, Callable[[VerticalManifest], tuple[bool, str]]]] = []


def check(name: str, required_phase: str):
    def deco(fn: Callable[[VerticalManifest], tuple[bool, str]]):
        _CHECKS.append((name, required_phase, fn))
        return fn
    return deco


def run_conformance(manifest: VerticalManifest, *, phase: str = "P0") -> ConformanceReport:
    results: list[CheckResult] = []
    for name, req_phase, fn in _CHECKS:
        required_now = _phase_leq(req_phase, phase)
        if not required_now:
            results.append(CheckResult(name, req_phase, passed=True, skipped=True,
                                       detail="not required until " + req_phase))
            continue
        passed, detail = fn(manifest)
        results.append(CheckResult(name, req_phase, passed=passed, detail=detail))
    return ConformanceReport(results)


# ---- checks (grow per phase) --------------------------------------------

@check("manifest-has-name", "P0")
def _has_name(m: VerticalManifest) -> tuple[bool, str]:
    return (bool(m.name and m.name.strip()), m.name)


@check("entity-taxonomy-declared", "P1")
def _entities(m: VerticalManifest) -> tuple[bool, str]:
    return (len(m.entity_types) > 0, f"{len(m.entity_types)} entity type(s)")


@check("connectors-conform", "P1")
def _connectors(m: VerticalManifest) -> tuple[bool, str]:
    if not m.connectors:
        return (False, "no connectors declared")
    bad = [k for k, c in m.connectors.items() if not isinstance(c, Connector)]
    return (not bad, "all conform" if not bad else f"non-conforming: {bad}")


@check("scope-dimensions-declared", "P2")
def _scope(m: VerticalManifest) -> tuple[bool, str]:
    return (len(m.scope_dimensions) > 0, f"{len(m.scope_dimensions)} dim(s)")


@check("gating-policy-conforms", "P2")
def _gating(m: VerticalManifest) -> tuple[bool, str]:
    ok = m.gating_policy is not None and isinstance(m.gating_policy, GatingPolicy)
    return (ok, "conforms" if ok else "missing/mis-shaped")


@check("retrieval-source-conforms", "P2")
def _retrieval(m: VerticalManifest) -> tuple[bool, str]:
    if not m.retrieval_sources:
        return (False, "no retrieval source declared")
    bad = [k for k, s in m.retrieval_sources.items() if not isinstance(s, RetrievalSource)]
    return (not bad, "all conform" if not bad else f"non-conforming: {bad}")


@check("persona-and-authority-declared", "P3")
def _persona(m: VerticalManifest) -> tuple[bool, str]:
    ok = isinstance(m.persona, Persona) and m.authority_policy is not None
    return (ok, "conforms" if ok else "persona/authority missing or mis-shaped")


@check("citation-verifier-conforms", "P3")
def _citation(m: VerticalManifest) -> tuple[bool, str]:
    # Optional: the kernel verifies block_span itself. A vertical declares a
    # verifier only for NEW locator kinds; if present it must conform.
    if m.citation_verifier is None:
        return (True, "none (kernel handles block_span)")
    ok = isinstance(m.citation_verifier, CitationVerifier)
    return (ok, "conforms" if ok else "mis-shaped")


@check("eval-gold-present", "P3")
def _eval_gold(m: VerticalManifest) -> tuple[bool, str]:
    return (len(m.eval_gold) > 0, f"{len(m.eval_gold)} gold set(s)")


@check("ui-contract-conforms", "P4")
def _ui(m: VerticalManifest) -> tuple[bool, str]:
    ok = m.ui is not None and isinstance(m.ui, UIContract)
    return (ok, "conforms" if ok else "missing/mis-shaped")
