"""Lenient JSON parsing for LLM output.

Models routinely emit backslashes that are VALID markdown/LaTeX/paths but INVALID JSON escapes inside
a string value — `\\_`, `\\-`, `\\$` (markdown), `\\frac` / `\\times` (LaTeX), `C:\\Users` (paths). Python's
strict `json.loads` raises `JSONDecodeError: Invalid \\escape`, which — when uncaught — surfaced to users
as a hard "provider error" and failed the whole research run on a stochastic flake.

`loads()` tries strict first (the happy path is untouched), then REPAIRS only the invalid escapes — a
backslash not followed by one of `" \\ / b f n r t u` is treated as a literal backslash (`\\` doubled) —
and retries. Valid escapes and `\\uXXXX` are preserved; a genuine `\\\\` pair is consumed intact by the
left-to-right walk, so legitimate escaped backslashes are never corrupted. Structural (Rule 18): a purely
computable repair of malformed provider output, not a semantic decision.
"""
from __future__ import annotations

import json

_VALID_ESCAPE = set('"\\/bfnrtu')


def repair_escapes(s: str) -> str:
    """Double every backslash that does NOT begin a valid JSON escape, walking left-to-right so real
    `\\\\` pairs and `\\uXXXX` are preserved."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in _VALID_ESCAPE:
                out.append(c)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")          # invalid (or trailing) escape → literal backslash
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads(s: str | None):
    """`json.loads` tolerant of the invalid backslash escapes LLMs emit. Strict first; on an escape
    error, repair and retry (a non-escape syntax error still raises, so real malformations aren't hidden)."""
    try:
        return json.loads(s or "{}")
    except json.JSONDecodeError as e:
        if "escape" not in str(e).lower():
            raise                       # not an escape problem — a genuine malformation, surface it
        return json.loads(repair_escapes(s or "{}"))
