#!/usr/bin/env python3
"""AST import-graph guardrail (plan W5, second half).

The kernel must not import any vertical package. Domain-noun grep catches
vocabulary leaks; this catches *dependency* leaks the grep can't see (a kernel
module importing roster_vertical_* would couple the console to a cartridge).

Exit 0 if clean, 1 on any violation. Run from repo root.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

KERNEL_ROOT = Path("packages/kernel/roster_kernel")
FORBIDDEN_PREFIXES = ("roster_vertical",)


def _module_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.level == 0:
                names.append(n.module)
    return names


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else KERNEL_ROOT
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        # Governs shipped kernel runtime code; tests may reference vertical fixtures.
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as e:  # pragma: no cover
            violations.append(f"{path}: syntax error {e}")
            continue
        for mod in _module_names(tree):
            if any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                violations.append(f"{path}: imports vertical module '{mod}'")

    if violations:
        print("✗ KERNEL IMPORT VIOLATION — kernel must not depend on a vertical:")
        print("\n".join("  " + v for v in violations))
        return 1
    print(f"✓ kernel imports clean — no vertical dependencies in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
