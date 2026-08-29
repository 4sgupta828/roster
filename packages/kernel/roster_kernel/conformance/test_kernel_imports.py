"""Kernel must not import any vertical package (mirrors tools/check_kernel_imports.py)."""
from __future__ import annotations

import ast
from pathlib import Path

_KERNEL_ROOT = Path(__file__).resolve().parents[1]  # roster_kernel/
_FORBIDDEN = ("roster_vertical",)


def test_kernel_does_not_import_a_vertical() -> None:
    violations: list[str] = []
    for path in _KERNEL_ROOT.rglob("*.py"):
        # The invariant governs SHIPPED kernel runtime code: it must never import a vertical.
        # Kernel tests may reference a vertical's fixtures to exercise the kernel↔vertical contract.
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for n in ast.walk(tree):
            mods: list[str] = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods = [n.module]
            for mod in mods:
                if any(mod == p or mod.startswith(p + ".") for p in _FORBIDDEN):
                    violations.append(f"{path.relative_to(_KERNEL_ROOT)}: imports {mod}")
    assert not violations, "kernel imports a vertical:\n" + "\n".join(violations)
