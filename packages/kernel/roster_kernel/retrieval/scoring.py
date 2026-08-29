"""Backend-agnostic ranking helpers — real BM25 + structural signal.

BM25 (IDF + document-length normalization) is a genuine upgrade over a plain
term-frequency / ts_rank lexical leg (which has neither). The structural signal
demotes low-content blocks (page headers/footers, stubs) — the generic analogue
of a boilerplate classifier. Both are pure functions so the Postgres source can
reuse the same logic (or push BM25 down to ParadeDB) behind the same port.
"""
from __future__ import annotations

import math
import re
from collections import Counter

BM25_K1 = 1.2
BM25_B = 0.75

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def bm25_rank(query: str, docs: list[tuple[str, str]], *, k1: float = BM25_K1,
              b: float = BM25_B) -> list[str]:
    """Rank doc ids best-first by Okapi BM25.

    docs: list of (id, text). IDF is computed over the given set (the filtered
    candidate pool), so stats never leak across a tenant/facet boundary.
    """
    n = len(docs)
    if n == 0:
        return []
    toks = {i: tokens(t) for i, t in docs}
    df: Counter[str] = Counter()
    for tl in toks.values():
        df.update(set(tl))
    avgdl = sum(len(tl) for tl in toks.values()) / n or 1.0
    q_terms = set(tokens(query))

    scored: list[tuple[str, float]] = []
    for i, tl in toks.items():
        if not tl:
            continue
        tf = Counter(tl)
        s = 0.0
        for term in q_terms:
            nq = df.get(term, 0)
            f = tf.get(term, 0)
            if nq == 0 or f == 0:
                continue
            idf = math.log(1 + (n - nq + 0.5) / (nq + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(tl) / avgdl))
        if s > 0:
            scored.append((i, s))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [i for i, _ in scored]


def signal(text: str) -> float:
    """A block's content-signal in (0, 1]. Low = likely boilerplate/stub → demote.

    Structural only (Rule 18: no semantic judgment) — length + alphabetic ratio.
    """
    t = text.strip()
    if len(t) < 40:
        return 0.3                         # header/footer/stub
    tl = tokens(t)
    if not tl:
        return 0.3
    alpha = sum(1 for x in tl if any(c.isalpha() for c in x))
    if alpha / len(tl) < 0.4:
        return 0.5                         # mostly numbers/punctuation (e.g. a bare table row)
    return 1.0
