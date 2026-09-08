"""Summarise one essay or episode for the expanded card — written once, then stored.

Two paths, and the card always says which one it got.

**Model.** When a JSON-capable model is configured and has credit, it writes 3-5 points about what
the piece actually ARGUES. Lazy: nothing is summarised until a reader opens that card, so spend
follows attention rather than corpus size (a corpus of 1,500 blocks summarised eagerly would be a
bill for text nobody read). Cached by document, so a second reader pays nothing.

**Extractive.** When no model is available, the same panel is filled by picking the most informative
sentences out of the piece itself. It is a weaker summary and the card says so — much better than an
empty panel or a spinner that never resolves, and it keeps the feature honest about what produced
the words.

A chapter-pointer document needs no model at all: its "summary" is the publisher's own chapter list,
which is already a summary of the episode. Nothing there is a quotation, so nothing is invented.

REGISTER. This is advice, so a summary says what the author ARGUES, never what is true. The prompt
carries that, and `verify()` enforces the one failure that would matter most in this genre: a
summary asserting a figure the piece never states.
"""
from __future__ import annotations

import re

MAX_INPUT_CHARS = 12000        # one call, bounded: ~3k tokens in, a few hundred out
MAX_POINTS = 5

SYSTEM = (
    "You prepare one piece of first-person writing about HIRING or JOB SEARCH — by a recruiter, a "
    "hiring manager, an engineer, a career coach or a hiring-tools vendor — for a reader who is "
    "either looking for a job or trying to hire well. The paragraphs are given to you NUMBERED. "
    "Return STRICT JSON:\n"
    '{"heading": str, "points": [str], "quotes": [str], '
    '"sections": [{"title": str, "paras": [int]}], "dropped": [int]}\n'
    "RULES\n"
    "points — 3 to 6 takeaways. Each is ONE sentence under 25 words stating something the piece "
    "actually argues or reports. Prefer the specific: a number, a mechanism, a named mistake, a "
    "concrete recommendation. Drop anything you could have written without reading the piece. Never "
    "invent a figure or an outcome. Fewer points beats padding.\n"
    "quotes — 0 to 2 sentences copied VERBATIM from the paragraphs, the ones a reader would "
    "underline. Copy exactly, character for character, or omit them.\n"
    "sections — group the paragraphs worth reading into 2 to 6 sections IN ORDER, each with a short "
    "title (2 to 6 words) naming what that run of paragraphs is about. List each kept paragraph's "
    "number once, in order. You are ORGANISING, not rewriting: never renumber, merge or reorder.\n"
    "dropped — the numbers of paragraphs that are NOISE: subscribe and share prompts, navigation, "
    "lists of other posts, sponsor copy, sign-offs, comment invitations, repeated boilerplate. "
    "PRESERVE EVERYTHING SUBSTANTIVE. If a paragraph carries an argument, an example, a number, a "
    "story or an opinion, it is substance even when it is short or informal. When unsure, keep it.\n"
    "This is one practitioner's experience and OPINION, not a finding. Report what THEY argue or "
    "recommend; never restate it as established fact, and add no judgment of your own. A vendor "
    "writing about their own product is making a claim, not reporting a result."
)

_FIG = re.compile(r"\$?\d[\d,.]*\s*(?:%|percent|k|m|bn|b|x|million|billion)?", re.I)


def verify(points: list[str], text: str) -> list[str]:
    """Drop a point the piece does not support. A code-owned gate, not a request in the prompt.

    The failure this prevents is the one that matters in this genre: a summary that invents a figure
    — "candidates who do X get 3x more callbacks" is exactly the sentence a reader would act on.
    Every number a point asserts must appear in the source text, normalised for separators, or the
    point goes. A point with no figures passes — prose paraphrase is the model's job — but a
    fabricated "$40M" or "3x" cannot survive.
    """
    def figs(s: str) -> set[str]:
        out = set()
        for m in _FIG.finditer(s or ""):
            raw = m.group(0).strip().lower().replace(",", "").replace("$", "").rstrip(".")
            digits = re.sub(r"[^0-9.]", "", raw)
            if digits and any(c.isdigit() for c in digits):
                out.add(digits.rstrip("."))
        return out

    src = figs(text)
    kept = []
    for p in points:
        if figs(p) - src:            # a number the source never states
            continue
        kept.append(p.strip()[:220])
    return kept


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")
_STOP = {"the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "at", "by",
         "from", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
         "those", "as", "we", "i", "you", "they", "he", "she", "our", "their", "my", "your", "have",
         "has", "had", "do", "does", "did", "not", "so", "if", "then", "than", "there", "what",
         "which", "who", "when", "where", "how", "will", "would", "can", "could", "should", "about"}


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENT.split(" ".join((text or "").split())):
        s = s.strip()
        if 40 <= len(s) <= 320:
            out.append(s)
    return out


def extractive_points(text: str, k: int = MAX_POINTS) -> list[str]:
    """The most informative sentences, in the order the piece makes them.

    Scored by the frequency of the content words they carry, normalised by length so a long
    sentence does not win merely for being long, with a small bonus for a sentence containing a
    figure — in this genre the sentence with the number is usually the sentence with the point.
    """
    sents = sentences(text)
    if not sents:
        return []
    freq: dict[str, int] = {}
    for s in sents:
        for w in re.findall(r"[a-z][a-z'-]+", s.lower()):
            if w not in _STOP and len(w) > 2:
                freq[w] = freq.get(w, 0) + 1
    scored = []
    for i, s in enumerate(sents):
        words = [w for w in re.findall(r"[a-z][a-z'-]+", s.lower()) if w not in _STOP and len(w) > 2]
        if not words:
            continue
        score = sum(freq.get(w, 0) for w in words) / (len(words) ** 0.6)
        if re.search(r"\d", s):
            score *= 1.15
        scored.append((score, i, s))
    scored.sort(reverse=True)
    picked = sorted(scored[:k], key=lambda t: t[1])
    return [s for _score, _i, s in picked]


def chapter_points(text: str, k: int = MAX_POINTS) -> list[str]:
    """For an episode, the publisher's own chapter titles — longest first, since a one-word chapter
    ("Intro") says nothing. Returned as the list they are, never as speech."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("[")]
    titles = []
    for ln in lines:
        m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s*(.+?)(?:\s+—\s+https?://\S+)?$", ln)
        if m:
            t = m.group(1).strip().rstrip(" -–—")
            if len(t) >= 8:
                titles.append(t)
    return sorted(titles, key=len, reverse=True)[:k]


def user_payload(title: str, author: str, paras: list[str]) -> str:
    """Numbered paragraphs — the numbers are how the model points at text without retyping it."""
    who = f"By {author}. " if author else ""
    lines, used = [], 0
    for i, p in enumerate(paras):
        if used + len(p) > MAX_INPUT_CHARS:
            break
        used += len(p)
        lines.append(f"[{i}] {p}")
    return f"{who}Title: {title}\n\n" + "\n\n".join(lines)


NOISE = re.compile(
    r"^(subscribe|share this|share on|follow me|read more|related posts?|previously|sponsored|"
    r"advertisement|thanks for reading|if you liked|sign up|join \d|comments?|leave a comment|"
    r"tags?:|categor|posted (in|on)|filed under|photo by|image credit)", re.I)


def paragraphs(text: str, *, max_paras: int = 120) -> list[str]:
    """The piece as paragraphs. Blocks arrive newline-joined, so this is the unit a reader reads."""
    out = []
    for raw in re.split(r"\n{1,}", text or ""):
        p = " ".join(raw.split())
        if p:
            out.append(p)
    return out[:max_paras]


def drop_noise(paras: list[str]) -> list[str]:
    """The fallback judgement when no model is available: obvious chrome out, everything else kept.

    Deliberately timid. Losing a paragraph of someone's argument is far worse than showing one line
    of 'Subscribe', so this only removes what announces itself as furniture.
    """
    kept = []
    for p in paras:
        if len(p) < 25 and not re.search(r"[.!?]", p):
            continue                        # a bare label or a stray nav word
        if NOISE.match(p) and len(p) < 200:
            continue
        kept.append(p)
    return kept


def assemble(sections: list, paras: list[str]) -> list[dict]:
    """Build the readable body from the ORIGINAL paragraphs by index.

    The model chooses the grouping and the titles; it never supplies the prose. That is the whole
    point: a section cannot contain a sentence the author did not write, because the text is copied
    out of the source by number.
    """
    out, used = [], set()
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        body = []
        for i in sec.get("paras") or []:
            try:
                idx = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(paras) and idx not in used:
                used.add(idx)
                body.append(paras[idx])
        if body:
            out.append({"title": str(sec.get("title") or "").strip()[:80], "paragraphs": body})
    return out


def _fallback(text: str, note: str) -> dict:
    paras = drop_noise(paragraphs(text))
    return {"heading": "Key passages", "points": extractive_points(text), "quotes": [],
            "sections": ([{"title": "", "paragraphs": paras}] if paras else []),
            "note": note, "basis": "extractive"}


async def summarize(*, title: str, author: str, text: str, is_chapter: bool, llm_json=None) -> dict:
    """Return {heading, points, quotes, sections, note, basis}.

    Never raises: a summary is a convenience, and a card that fails to summarise must still show the
    piece. `sections` always holds the author's own paragraphs — organised, sometimes trimmed of
    chrome, never rewritten.
    """
    if is_chapter:
        return {"heading": "What the episode covers", "points": chapter_points(text), "quotes": [],
                "sections": [], "note": "The publisher's own chapter list. Nothing here is a quotation.",
                "basis": "chapters"}

    paras = paragraphs(text)
    if llm_json is not None:
        try:
            out = await llm_json(SYSTEM, user_payload(title, author, paras))
            raw = [str(p).strip() for p in (out.get("points") or []) if str(p).strip()][:MAX_POINTS]
            pts = verify(raw, text)          # a figure the piece never states is not a summary
            quotes = [q for q in (str(x).strip() for x in (out.get("quotes") or []))
                      if q and " ".join(q.split()) in " ".join(text.split())][:2]
            sections = assemble(out.get("sections"), paras)
            if not sections:                  # the model organised nothing: show the piece anyway
                sections = [{"title": "", "paragraphs": drop_noise(paras)}]
            if pts:
                kept = sum(len(sec["paragraphs"]) for sec in sections)
                trimmed = max(0, len(paras) - kept)
                note = ("Read and organised from the full piece"
                        + (f", by {author}" if author else "")
                        + (f" — {trimmed} paragraph{'s' if trimmed != 1 else ''} of navigation and "
                           "subscribe copy left out." if trimmed else "."))
                return {"heading": str(out.get("heading") or "What it says")[:80], "points": pts,
                        "quotes": quotes, "sections": sections, "note": note, "basis": "model"}
        except Exception as e:      # noqa: BLE001 — an outage degrades the panel, never breaks it
            return _fallback(text, f"No model available ({type(e).__name__}), so these are the "
                                   "piece's own sentences and its own paragraphs.")
    return _fallback(text, "Selected from the piece's own sentences — no model was available to "
                           "summarise it.")


# ── cache ────────────────────────────────────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS vo_summary (
    document_id text PRIMARY KEY,
    heading     text NOT NULL DEFAULT '',
    points      jsonb NOT NULL DEFAULT '[]'::jsonb,
    note        text NOT NULL DEFAULT '',
    basis       text NOT NULL DEFAULT '',
    made_at     timestamptz NOT NULL DEFAULT now());
ALTER TABLE vo_summary ADD COLUMN IF NOT EXISTS quotes jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE vo_summary ADD COLUMN IF NOT EXISTS sections jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE vo_summary ADD COLUMN IF NOT EXISTS v int NOT NULL DEFAULT 1;
"""

# Bump when the SHAPE of a summary changes. A row written by an older shape is re-read rather than
# rendered half-empty — the alternative is a panel missing the part the reader came for.
VERSION = 2

# An essay does not change, so the summary of one does not either. The TTL exists only so that an
# EXTRACTIVE summary written during a model outage is re-attempted later, when credit exists — a
# model summary is kept far longer because re-buying it would be paying twice for the same words.
TTL_DAYS = {"model": 365, "chapters": 365, "extractive": 3}


async def cached(conn, document_id: str) -> dict | None:
    """The stored summary, or None when there is none or it has aged out of its basis's window."""
    import json
    await conn.execute(DDL)
    r = await conn.fetchrow(
        "SELECT heading, points, quotes, sections, note, basis, v, "
        "       EXTRACT(EPOCH FROM (now() - made_at)) / 86400.0 AS age "
        "FROM vo_summary WHERE document_id = $1", document_id)
    if not r or int(r["v"] or 1) < VERSION:
        return None
    if float(r["age"] or 0) > TTL_DAYS.get(r["basis"], 7):
        return None

    def js(v, default):
        return (json.loads(v) if isinstance(v, str) else v) or default
    return {"heading": r["heading"], "points": js(r["points"], []), "quotes": js(r["quotes"], []),
            "sections": js(r["sections"], []), "note": r["note"], "basis": r["basis"]}


async def store(conn, document_id: str, summary: dict) -> None:
    """Never cache an empty read: a summary with no points is a failure, not a result."""
    if not (summary.get("points") or []):
        return
    import json
    await conn.execute(DDL)
    await conn.execute(
        "INSERT INTO vo_summary (document_id, heading, points, quotes, sections, note, basis, v, made_at) "
        "VALUES ($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6,$7,$8, now()) ON CONFLICT (document_id) DO UPDATE "
        "SET heading=EXCLUDED.heading, points=EXCLUDED.points, quotes=EXCLUDED.quotes, "
        "    sections=EXCLUDED.sections, note=EXCLUDED.note, basis=EXCLUDED.basis, v=EXCLUDED.v, "
        "    made_at=now()",
        document_id, summary.get("heading", "")[:120], json.dumps(summary.get("points") or []),
        json.dumps(summary.get("quotes") or []), json.dumps(summary.get("sections") or []),
        summary.get("note", "")[:200], summary.get("basis", "")[:20], VERSION)
