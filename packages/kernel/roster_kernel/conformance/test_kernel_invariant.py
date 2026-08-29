"""The kernel domain-noun invariant, as a test (mirrors tools/check_kernel_invariant.sh).

Runs in CI so the guardrail is enforced both as a shell gate and a pytest.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Semantic domain nouns that must never appear in the kernel package.
_DOMAIN_NOUNS = re.compile(
    r"docket|utilit(y|ies)|puco|\bohio\b|\brate case\b|case_number"
    r"|native_case_number|doc_family|\bstate_code\b|\bjurisdiction\b|\bfiling\b"
    r"|rate_base|\broe\b|commission_id|dis_client",
    re.IGNORECASE,
)

_KERNEL_ROOT = Path(__file__).resolve().parents[1]  # roster_kernel/
_SELF = Path(__file__).resolve()  # this checker necessarily contains the pattern


def test_kernel_names_no_domain_noun() -> None:
    leaks: list[str] = []
    for path in _KERNEL_ROOT.rglob("*.py"):
        if path.resolve() == _SELF:
            continue
        # The invariant governs SHIPPED kernel runtime code. Test fixtures legitimately use
        # domain words (a person's name, an example document) to exercise the generic mechanics.
        if path.name.startswith("test_"):
            continue
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            if "kernel-invariant: allow" in line:
                continue
            if _DOMAIN_NOUNS.search(line):
                leaks.append(f"{path.relative_to(_KERNEL_ROOT)}:{i}: {line.strip()}")
    assert not leaks, (
        "Domain nouns leaked into the kernel — move them to a vertical package:\n"
        + "\n".join(leaks)
    )
