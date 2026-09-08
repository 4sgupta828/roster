"""Search the voices corpus and shape it as MOMENTS. Pure functions plus one SQL builder.

A moment is what the reader wants back: who wrote or said it, about what, when, and a link that opens
at the exact place. Two kinds reach the surface and they are NOT equal:

  essay    — the author's own words. Quotable, as OPINION: it evidences what they argue, never that
             they are right. The careers genre rewards confidence over correctness.
  chapter  — a publisher's or uploader's timestamped marker. A POINTER: where to listen or watch,
             never what was said. Quoting one would manufacture a statement nobody made.

The distinction is carried in the data (`source_kind`), not in a prompt, and `is_quotable` is the
single place that decides whether a moment may ever appear inside quotation marks.
"""
from __future__ import annotations

import re

from roster_vertical.voices_doc import deep_link

# The mode's sources. Roster's older `podcast` connector is NOT here: it holds publisher transcripts
# of ENGINEERING shows, which is practitioner talk but not about hiring or job search. It stays in
# the corpus for research answers; it is simply not this mode's material. Nor is `expert_feed`, whose
# allowlist is deep-tech researchers.
VOICE_SOURCE_KEYS = ("practitioner_essay", "show_notes", "youtube_chapters")

# Words that carry no signal in a question about work. The query is OR-ed, so left in, "how" and
# "what" would match half the corpus and drown the terms that matter.
_STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "at", "by", "from",
         "how", "what", "why", "when", "who", "do", "does", "did", "is", "are", "was", "were", "be",
         "i", "we", "you", "they", "it", "that", "this", "about", "after", "before", "my", "our",
         "should", "can", "get", "make", "best", "good"}

# Words that are near-universal in a corpus that is ENTIRELY about hiring and job search. They carry
# no signal here and, OR-ed into a query, they drown the words that do: "how to prepare for a system
# design interview" must search for `system` and `design`, not for `interview`, which every third
# block contains. Dropped only when something more specific survives.
_GENERIC = {"job", "jobs", "role", "roles", "hire", "hiring", "hired", "candidate", "candidates",
            "interview", "interviews", "interviewing", "recruiter", "recruiters", "recruiting",
            "recruitment", "talent", "career", "careers", "company", "companies", "team", "teams",
            "people", "person", "work", "working", "employer", "employers", "position", "positions",
            "applicant", "applicants", "process"}


def terms(q: str) -> list[str]:
    """Query words, cleaned for `to_tsquery`. Punctuation out, stopwords out, duplicates out."""
    out: list[str] = []
    for raw in re.split(r"[^A-Za-z0-9']+", (q or "").lower()):
        w = raw.strip("'")
        if len(w) < 2 or w in _STOP or w in out:
            continue
        out.append(w)
    specific = [w for w in out if w not in _GENERIC]
    # …unless the whole question is made of them ("how do recruiters read a resume"), in which case
    # they are all we have and dropping them would leave nothing to search.
    return (specific or out)[:12]


def tsqueries(words: list[str]) -> list[tuple[str, str]]:
    """The query ladder, strictest first: [(label, tsquery)].

    Answering a six-word question by OR-ing all six words is not a search, it is a guess: the longest
    document containing the most common word wins. So ask for ALL the words, then for any TWO of the
    most distinctive, and only then for any one — and say which rung answered.
    """
    ws = [w for w in words if w]
    if not ws:
        return []
    out = [("all words", " & ".join(ws))]
    if len(ws) > 2:
        top = sorted(ws, key=len, reverse=True)[:5]        # longer words are the more distinctive
        pairs = [f"{a} & {b}" for i, a in enumerate(top) for b in top[i + 1:]]
        if pairs:
            out.append(("most words", " | ".join(pairs)))
    if len(ws) > 1:
        out.append(("any word", " | ".join(ws)))
    return out


# "[00:13:00] What a screen should catch — https://show.fm/ep?t=780"
_CHAPTER_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.+?)(?:\s+—\s+(https?://\S+))?$")

AUDIENCES = ("job_seeker", "hiring_team")


def is_pointer(kind: str) -> bool:
    """Podcast and video moments are chapter pointers: navigation, never speech."""
    return kind in ("podcast", "video")


def is_quotable(source_kind: str) -> bool:
    """False for a chapter pointer. A producer's chapter title is not speech, so quoting it would
    manufacture a statement nobody made. Everything else may be quoted with attribution."""
    return (source_kind or "").lower() != "chapter_pointer"


def kind_of(source_kind: str, source_key: str) -> str:
    """A podcast and a video are not the same object to a reader: one is listened to on the move, the
    other watched. They share the chapter MECHANICS and differ in how you consume them, so the card
    kind separates them even though the tier and the gates are identical."""
    if source_key == "youtube_chapters":
        return "video"
    if source_key == "show_notes" or (source_kind or "").lower() == "chapter_pointer":
        return "podcast"
    return "essay"


def seconds_of(stamp: str) -> int:
    try:
        h, m, s = (int(p) for p in stamp.split(":"))
    except ValueError:
        return 0
    return h * 3600 + m * 60 + s


def register_of(kind: str, role: str = "") -> str:
    """The line the card MUST print. This is the whole discipline of the mode in one string.

    A pointer is never presented as something anyone said. An essay is never presented as a finding:
    it is what one practitioner argues, and naming what they ARE (a recruiter, a coach, an engineer)
    is most of what a reader needs to weigh it.
    """
    if is_pointer(kind):
        return ("Chapter marker written by the publisher — "
                + ("watch from this point" if kind == "video" else "listen from this point"))
    who = {"recruiter": "a recruiter", "coach": "a career coach", "engineer": "an engineer",
           "manager": "an engineering manager", "operator": "an operator",
           "vendor": "a hiring-tools vendor"}.get(role, "a practitioner")
    return f"Advice from {who} — what they argue, not a verified outcome"


def moment(row: dict) -> dict:
    """One `rs_block` row → a card. Never invents a link, a speaker or a timestamp."""
    facets = row.get("facets") or {}
    text = (row.get("text") or "").strip()
    source_kind = str(facets.get("source_kind") or "")
    kind = kind_of(source_kind, str(row.get("source_key") or ""))

    t_start, url, body = 0, str(facets.get("episode_url") or facets.get("url") or ""), text
    if is_pointer(kind):
        m = _CHAPTER_LINE.match(text)
        if m:
            t_start = seconds_of(m.group(1))
            # the stored line is "[hh:mm:ss] Title — <url>"; when the episode had no link the
            # separator is still there, and "What a screen should catch —" is not a title anyone wrote
            body = m.group(2).strip().rstrip(" -–—")
            # the line's own link when it has one; otherwise point the episode's address at this
            # moment, so a show that ships only an audio enclosure still opens at the right second
            url = m.group(3) or deep_link(url, t_start)
    else:
        # the passage that matched, when the query produced one; else the block's opening
        body = (row.get("snippet") or body or "").strip() or body
    role = str(facets.get("voice_role") or "")
    vid, audio = str(facets.get("video_id") or ""), str(facets.get("audio_url") or "")
    if is_pointer(kind) and vid:
        media = {"kind": "youtube", "id": vid, "t": t_start}
    elif is_pointer(kind) and audio:
        media = {"kind": "audio", "url": audio, "t": t_start}
    else:
        media = {}
    return {
        "id": f"{row.get('document_id')}::{row.get('block_id')}",
        "kind": kind,
        "quotable": is_quotable(source_kind),
        "text": body,
        "title": str(row.get("document_title") or ""),
        "show": str(facets.get("publication") or ""),
        "speaker": str(facets.get("guest") or facets.get("author") or facets.get("writer") or ""),
        "role": role,
        "audience": str(facets.get("audience") or ""),
        # The exact date if we have one, else the feed's own string. NEVER the bare year: a card
        # rendering "2026" as a date shows the wrong day to a reader west of UTC.
        "published": str(facets.get("published_at") or facets.get("published") or ""),
        "year": str(facets.get("year") or ""),
        "url": url,
        "t_start": t_start,
        "image": str(facets.get("image") or ""),
        "views": int(facets["views"]) if str(facets.get("views") or "").isdigit() else 0,
        "media": media,
        "register": register_of(kind, role),
    }


def build_query(*, q: str, kinds: tuple[str, ...] = (), audience: str = "", speaker: str = "",
                limit: int = 30, table: str = "rs_block", per_document: bool = False,
                since: str = "", order: str = "relevance", tsquery: str = "") -> tuple[str, list]:
    """Keyword search over the voices corpus. Returns (sql, params) — no I/O, so it is testable.

    An empty `q` is a BROWSE, not a failed search. A browse orders by the piece's own PUBLICATION
    date (`published_at`, ISO text, so it sorts as text), never by when we happened to ingest a row.
    """
    where = [
        "source_key = ANY($1)",
        # Newsletters repeat a sidebar of post titles in every item's body; without this one block
        # floods a search five times over.
        "NOT (facets ? 'boilerplate')",
        # A document carries a header — a byline and a "URL: …" line — and the splitter indexes those
        # as ordinary paragraphs. A pointer's real content always opens with its timestamp, so
        # anything else in those sources is furniture.
        "text NOT LIKE 'URL: %'",
        "(source_key NOT IN ('show_notes','youtube_chapters') OR text LIKE '[%')",
        "text NOT LIKE '%(expert analysis / opinion%'",
        "text NOT LIKE '%Chapter pointers written by the publisher%'",
    ]
    params: list = [list(VOICE_SOURCE_KEYS)]
    n = 1

    if kinds:
        by_kind = {"podcast": ["show_notes"], "video": ["youtube_chapters"],
                   "chapter": ["show_notes", "youtube_chapters"],
                   "essay": ["practitioner_essay"]}
        keys: list[str] = []
        for k in kinds:
            keys += by_kind.get(k, [])
        # An unrecognised kind must narrow to nothing, never widen back to everything: silently
        # returning the whole corpus for a typo'd filter is a lie about what the filter did.
        params[0] = keys
    if audience in AUDIENCES:
        # "both" material answers either reader, so it is never filtered out — only the OTHER
        # audience's material is. A source with no audience stamped is treated as "both".
        n += 1
        where.append(f"(NOT (facets ? 'audience') OR facets->>'audience' IN (${n}, 'both'))")
        params.append(audience)
    if since:
        n += 1
        where.append(f"(facets->>'published_at') >= ${n}")
        params.append(since)
    if order == "watched":
        n += 1
        where.append(f"(facets->>'views') ~ ${n}")
        params.append(r"^\d+$")
    if speaker:
        n += 1
        where.append(f"(facets->>'guest' ILIKE ${n} OR facets->>'author' ILIKE ${n} "
                     f"OR facets->>'publication' ILIKE ${n})")
        params.append(speaker)

    q_terms = terms(q)
    q_expr = ""
    if q_terms:
        q_expr = tsquery or " | ".join(q_terms)
        # OR, not AND. `plainto_tsquery` requires EVERY word, so a question phrased as a sentence
        # finds nothing. OR-ing and letting ts_rank order the result is what the kernel's own
        # retrieval does, and it is the difference between a mode that answers and one that shrugs.
        n += 1
        params.append(q_expr)
        where.append(f"tsv @@ to_tsquery('english', ${n})")
        # Normalisation 1 divides the rank by 1 + log(length). WITHOUT IT a 4,000-character essay
        # mentioning a word twice outranks a chapter titled exactly that word, and the moments — the
        # thing this mode exists to surface — never appear at all. This is the concrete answer to the
        # panel's objection that keyword search over short chapter titles returns dead ends.
        rank = (f"ts_rank(tsv, to_tsquery('english', ${n}), 1) * "
                f"CASE WHEN (facets ? 'url' OR facets ? 'episode_url') THEN 1.0 ELSE 0.7 END")
        order_sql = f"{rank} DESC, facets->>'published_at' DESC NULLS LAST"
        outer_order = "score DESC, facets->>'published_at' DESC NULLS LAST"
        # An essay block runs to thousands of characters, so its head is rarely the part that
        # answered. ts_headline returns the passage that actually matched.
        snippet = (f"ts_headline('english', text, to_tsquery('english', ${n}), "
                   f"'MaxWords=48, MinWords=20, ShortWord=3, MaxFragments=1, StartSel=«, StopSel=»')")
    else:
        rank = "0.0"
        order_sql = ("(facets->>'views')::bigint DESC, facets->>'published_at' DESC NULLS LAST"
                     if order == "watched"
                     else "facets->>'published_at' DESC NULLS LAST, created_at DESC NULLS LAST")
        snippet = "left(text, 320)"
        outer_order = order_sql

    n += 1
    params.append(int(max(1, min(limit, 100))))
    cols = (f"document_id, block_id, text, document_title, source_key, facets, created_at, "
            f"{rank} AS score, {snippet} AS snippet")
    if per_document:
        # One row per PIECE. Without this a result fills with the first eight chapters of one episode
        # ("00:00 Intro", "02:00 Early days") and every other piece is pushed out.
        sql = (f"SELECT * FROM (SELECT DISTINCT ON (document_id) {cols} "
               f"FROM {table} WHERE {' AND '.join(where)} ORDER BY document_id, {order_sql}) s "
               f"ORDER BY {outer_order} LIMIT ${n}")
    else:
        sql = f"SELECT {cols} FROM {table} WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ${n}"
    return sql, params


def dedupe(moments: list[dict], *, per_document: int = 2, per_source: int = 3,
           limit: int = 30) -> list[dict]:
    """Keep a result set readable: no repeated text, and no one voice taking it over.

    Ranking alone does not do this. One essay split into blocks can hold the top five slots with
    near-identical passages, and the most prolific publisher can hold the rest — either way it reads
    as though the corpus knows one thing. Two passes over the same ranked list, so the order is still
    the ranking's; only the crowding is removed.
    """
    seen_text: set[str] = set()
    per_doc: dict[str, int] = {}
    per_pub: dict[str, int] = {}
    out: list[dict] = []
    spill: list[dict] = []
    for m in moments:
        key = " ".join((m.get("text") or "").lower().split())[:160]
        doc = str(m.get("id", "")).split("::", 1)[0]
        pub = (m.get("show") or m.get("speaker") or "").lower()
        if not key or key in seen_text or per_doc.get(doc, 0) >= per_document:
            continue
        seen_text.add(key)
        per_doc[doc] = per_doc.get(doc, 0) + 1
        if pub and per_pub.get(pub, 0) >= per_source:
            spill.append(m)          # held back, not discarded: it still fills a thin result set
            continue
        per_pub[pub] = per_pub.get(pub, 0) + 1
        out.append(m)
        if len(out) >= limit:
            return out
    return (out + spill)[:limit]
