"""Turn a podcast episode's SHOW NOTES into a timestamped CHAPTER-POINTER document.

WHY THIS EXISTS. Talent and careers shows do not publish transcripts, and mostly publish nothing at
all: measured 2026-09-08 over 98 shows found by topic search, 83% are TITLE-ONLY and 11% write a
chapter list. YouTube's caption endpoint returns zero bytes without a signed token, so video is closed
too. What the good publishers DO write is a timestamped chapter list — Recruiting Conversations 96% of
episodes, Talent Acquisition Leaders 96%, Beyond the Résumé 84%, and on YouTube, Engineering with
Utsav 73%, interviewing.io 67%. A chapter is a real lesson-level unit ("01:59 What shaped Victor's
approach to solving TA problems at scale") and it deep-links to the exact moment.

WHAT A CHAPTER IS NOT. A chapter title is written by the publisher's producer or the video's uploader.
It is NOT speech, so it is NOT testimony and may never be quoted as if the speaker said it. `source_kind="chapter_pointer"`
marks it as a POINTER: indexed so a question can find the moment, barred from the evidence path so no
claim can rest on it. The honest register is "listen from 13:00", never "the founder said".

ATTRIBUTION. A guest is read only from the TITLE, never the body copy — a name in the notes is a
mention, and treating a mention as the guest is how a moment lands on the wrong person.
"""
from __future__ import annotations

import html
import re

from .feed_dates import iso_date

# "13:00 Title", "(00:02:16) Title", "[1:02:33] - Title" — an offset then the chapter's words.
# The offset must OPEN its line (optionally in brackets). Requiring that kills the prose match:
# "At 08:30 we wake up and start coding" is a sentence, not a chapter, and a looser pattern turned it
# into one.
_CHAPTER = re.compile(
    r"^[\s\u2022*\-–—]*[(\[]?(\d{1,2}:\d{2}(?::\d{2})?)[)\]]?\s*[-–—:|]*\s*([^\n<|]{6,110})",
    re.M)

# Role and company words that are never part of a person's name. They appear next to the guest in
# titles like "with Netic Founder Melisa Tokmak", where a naive capture returns "Netic Founder Melisa".
_ROLE_WORDS = {"founder", "founders", "cofounder", "co-founder", "co-founders", "cofounders",
               "ceo", "cto", "coo", "cfo", "cpo", "cmo", "chairman", "president", "partner",
               "gp", "vp", "head", "lead", "director", "chief", "general", "managing", "operating",
               "capital", "ventures", "partners", "fund", "labs", "inc", "llc", "co", "the"}
_TITLE_VERBS = {"how", "why", "what", "when", "building", "chasing", "making", "rethinking", "leaving",
                "inside", "breaking", "redefining", "introducing", "scaling", "learning", "growing"}

_NAME = r"[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’.\-]+){1,3}"
# Ordered by how strongly each shape identifies the GUEST, most explicit first.
_GUEST_HINTS = (
    re.compile(r"\bwith\s+(" + _NAME + r")"),                    # "… with Ryan Petersen, CEO @ Flexport"
    re.compile(r"\bft\.?\s+(" + _NAME + r")"),
    re.compile(r"\bfeaturing\s+(" + _NAME + r")"),
    re.compile(r"\|\s*(" + _NAME + r")\s*(?:[,(]|$)"),           # "… | Michael Tannenbaum, CEO of Figure"
    re.compile(r"^(" + _NAME + r")\s+[-–—]\s+"),                 # "Sam Altman - How to Make an Abundant Future"
    re.compile(r"^(" + _NAME + r")\s+on\s+\S"),                  # "Ben Thompson on Big Tech, China …"
)

MIN_CHAPTERS = 3          # fewer than this is a stray timestamp, not a chapter list
MAX_CHAPTERS = 60


def strip_html(raw: str) -> str:
    txt = html.unescape(html.unescape(raw or ""))
    txt = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", txt).strip()


def to_seconds(stamp: str) -> int:
    """'13:00' → 780, '01:02:33' → 3753. The unit the deep link needs."""
    parts = [int(p) for p in stamp.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def hhmmss(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def chapters(description: str) -> list[dict]:
    """Timestamped chapters, in order, deduplicated by offset. Empty when the notes have no list.

    A chapter must move FORWARD: publishers put stray times in prose ("we raised at 3:00 am"), so a
    non-increasing offset ends the list rather than corrupting it.
    """
    text = strip_html(description)
    out: list[dict] = []
    last = -1
    for stamp, raw_title in _CHAPTER.findall(text):
        try:
            secs = to_seconds(stamp)
        except (ValueError, IndexError):
            continue
        title = " ".join(raw_title.split()).strip(" -–—:•|")
        if secs <= last or secs > 6 * 3600:
            break        # offsets only move forward; a backward one means the list ended and prose began
        if len(title) < 6:
            continue
        # a chapter title is a phrase, not a paragraph of body copy
        if len(title) > 110 or title.count(".") > 2:
            continue
        out.append({"t_start": secs, "stamp": hhmmss(secs), "title": title})
        last = secs
        if len(out) >= MAX_CHAPTERS:
            break
    return out if len(out) >= MIN_CHAPTERS else []


def _clean_name(raw: str) -> str:
    """Trim a captured span down to the PERSON, or return "" if it is not one.

    Two failures came from real prod titles. "with Netic Founder Melisa Tokmak" captured the company
    and the role; the fix is to drop everything up to and including the last role word. "Chasing
    Trillion-Dollar Companies, Founder Ambition…" captured a sentence; the fix is to reject a span
    whose first word is a title verb."""
    toks = [t for t in " ".join((raw or "").split()).split() if t]
    while toks and toks[0].lower().strip(".,") in _ROLE_WORDS:
        toks = toks[1:]
    # a role word INSIDE the span means the real name follows it ("Netic Founder Melisa Tokmak")
    last_role = max((i for i, t in enumerate(toks) if t.lower().strip(".,") in _ROLE_WORDS), default=-1)
    if last_role >= 0:
        toks = toks[last_role + 1:]
    if not toks or toks[0].lower() in _TITLE_VERBS:
        return ""
    if any(t.lower().strip(".,") in _ROLE_WORDS for t in toks):
        return ""
    if not (2 <= len(toks) <= 3):
        return ""
    return " ".join(toks)


def guest_from_title(title: str) -> str:
    """The guest as the TITLE names them, or "". Body copy is never consulted — a name in the notes
    is a mention, and binding a mention to the episode is how a quote lands on the wrong person."""
    t = " ".join((title or "").split())
    for pat in _GUEST_HINTS:
        for m in pat.finditer(t):
            name = _clean_name(m.group(1))
            if name:
                return name
    return ""


def episode_id(rec: dict) -> str:
    return str(rec.get("guid") or rec.get("id") or rec.get("link") or rec.get("enclosure") or "").strip()


_MEDIA = (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".wav", ".m4v")


def episode_link(rec: dict) -> str:
    """The best address the publisher gives for this episode: its page, else its audio file.

    Half the shows in the allowlist publish no <link> element, so without the enclosure fallback
    their chapters have nowhere to send the listener — a marker saying "listen from 15:35" with no
    way to listen is worse than no card at all."""
    return str(rec.get("link") or rec.get("enclosure") or "").strip()


def deep_link(link: str, t_start: int) -> str:
    """The episode URL pointed at the moment. Sends the listener to the publisher, which is the whole
    point of a pointer: we hold the index, they hold the work.

    A page takes `?t=` (the convention every podcast host uses); a bare audio file takes the media
    fragment `#t=`, which the browser's own player honours. Using `?t=` on an audio URL would just
    add a query string the CDN ignores and drop the listener at zero."""
    if not link:
        return ""
    if t_start <= 0:
        return link
    path = link.split("?", 1)[0].split("#", 1)[0].lower()
    if path.endswith(_MEDIA):
        return f"{link}#t={int(t_start)}"
    sep = "&" if "?" in link else "?"
    return f"{link}{sep}t={int(t_start)}"


def _year(rec: dict) -> str:
    m = re.search(r"(19|20)\d{2}", str(rec.get("published") or ""))
    return m.group(0) if m else ""


def video_id(link: str) -> str:
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", link or "")
    return m.group(1) if m else ""


def facets(rec: dict) -> dict:
    link = episode_link(rec)
    audio = str(rec.get("enclosure") or "").strip()
    f = {
        "source_kind": "chapter_pointer",     # NOT evidence — a navigation aid (see module docstring)
        "entity_type": "episode",
        # A pointer card is mostly a picture and a play button, so the artwork and the playable
        # address belong in the facets the card renders from, not only in the document body.
        "image": str(rec.get("image") or "").strip(),
        "audio_url": audio if audio.lower().split("?")[0].endswith(_MEDIA) else "",
        "video_id": video_id(link),
        "publication": " ".join(str(rec.get("publication") or "").split()).strip(),
        "guest": guest_from_title(str(rec.get("title") or "")),
        # the source card, carried from the registry: who this material is FOR, and what the person
        # speaking IS. A recruiter's take on résumés and a coach's take on résumés are different
        # evidence, and a surface that cannot say which it is holding is guessing.
        "audience": str(rec.get("audience") or "").strip(),
        "voice_role": str(rec.get("voice_role") or "").strip(),
        "writer": str(rec.get("writer") or "").strip(),
        "voices": "1",
        "episode_url": link,
        "published": str(rec.get("published") or "").strip()[:64],
        # sortable date — "newest first" is meaningless without one, and ISO text sorts correctly
        "published_at": iso_date(rec.get("published")),
        # what the platform itself publishes about attention. Present for video, absent for podcasts,
        # and shown as the platform's own number rather than turned into a ranking of our own.
        "views": str(rec.get("views") or "").strip(),
        "year": _year(rec),
    }
    return {k: v for k, v in f.items() if v}


def title(rec: dict) -> str:
    return " ".join(str(rec.get("title") or "").split()).strip()


def to_markdown(rec: dict) -> str:
    """One paragraph per chapter, each opening with its offset, so the kernel's paragraph splitter
    yields ONE BLOCK PER CHAPTER and every block carries the offset its deep link needs."""
    chs = chapters(str(rec.get("summary") or rec.get("content") or ""))
    show = " ".join(str(rec.get("publication") or "").split()).strip()
    guest = guest_from_title(title(rec))
    link = episode_link(rec)
    published = str(rec.get("published") or "").strip()

    head = " — ".join(x for x in [show, title(rec)] if x)
    parts = [f"# {head}", ""]
    who = f"Guest: {guest}. " if guest else ""
    parts += [
        f"{who}{show or 'Podcast'}{', ' + published if published else ''}. "
        "Chapter pointers written by the publisher — where to listen, not what was said. "
        "Nothing here is a quotation.",
        "",
    ]
    if link:
        parts += [f"URL: {link}", ""]
    parts += ["## Chapters", ""]
    for ch in chs:
        parts += [f"[{ch['stamp']}] {ch['title']} — {deep_link(link, ch['t_start'])}", ""]
    return "\n".join(parts).strip() + "\n"
