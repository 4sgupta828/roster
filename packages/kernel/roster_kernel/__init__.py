"""roster_kernel — the domain-agnostic evidence/research platform.

No domain-specific vocabulary appears in this package. All domain nouns,
schemas, and policy live in a vertical package (e.g. roster_vertical)
and reach the kernel only through the VerticalManifest contract. Enforced by
tools/check_kernel_invariant.sh (and conformance/test_kernel_invariant.py) in CI.
"""
