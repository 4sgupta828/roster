"""THE HIRING COMPANY'S OWN PAGE, derived from the posting URL (owner, 2026-09-07: "in the Jobs Map we should have a
link to the hiring company"). No new source and no guess: every posting already carries the applicant-tracking URL it
was ingested from, and each ATS puts the employer's own board at a known prefix of that URL. We link to THAT — the
employer's careers page — never to a search engine and never to a guessed domain.

    https://jobs.ashbyhq.com/vori/68decf…              → https://jobs.ashbyhq.com/vori
    https://jobs.lever.co/fullscript/9cd6…             → https://jobs.lever.co/fullscript
    https://boards.greenhouse.io/acme/jobs/123         → https://boards.greenhouse.io/acme
    https://app.careerpuck.com/job-board/lyft/job/87…  → https://app.careerpuck.com/job-board/lyft
    https://jobs.smartrecruiters.com/accenture/963…    → https://jobs.smartrecruiters.com/accenture
    https://7eleven.wd3.myworkdayjobs.com/7eleven/job/…→ https://7eleven.wd3.myworkdayjobs.com/7eleven
    https://apply.workable.com/j/6EDFC2AB01            → (nothing: the URL names no employer)

A host we do not know, or a URL whose employer segment is missing, yields None — the card then shows the company name
as plain text, exactly as today."""
from __future__ import annotations

from urllib.parse import urlsplit

# host suffix → how many path segments name the employer (and any fixed prefix segment that precedes it)
_BOARDS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("ashbyhq.com", (), 1),
    ("lever.co", (), 1),
    ("greenhouse.io", (), 1),
    ("careerpuck.com", ("job-board",), 1),
    ("smartrecruiters.com", (), 1),
    ("myworkdayjobs.com", (), 1),
    ("recruitee.com", (), 1),
    ("jobvite.com", (), 1),
    ("bamboohr.com", (), 1),
    ("teamtailor.com", (), 1),
    ("personio.de", (), 1),
    ("pinpointhq.com", (), 1),
    ("rippling.com", (), 1),
    ("eightfold.ai", (), 1),
    ("icims.com", (), 1),
)
_SKIP_SEGMENTS = {"job", "jobs", "job-board", "board", "boards", "opportunities", "postings", "careers", "en", "en-us", "us", "p", "j", "o", "c"}


def employer_page(url: str, source: str = "") -> str | None:
    """The employer's own job board for a posting URL, or None when the URL does not name one."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host, path = (parts.hostname or "").lower(), [p for p in (parts.path or "").split("/") if p]
    if not host or not path or parts.scheme not in ("http", "https"):
        return None
    for suffix, prefix, n in _BOARDS:
        if not (host == suffix or host.endswith("." + suffix)):
            continue
        segs = list(path)
        for want in prefix:                       # a fixed segment the board puts before the employer (careerpuck)
            if not segs or segs[0] != want:
                return None
            segs.pop(0)
        keep = segs[:n]
        if not keep or keep[0].lower() in _SKIP_SEGMENTS:
            return None
        base = f"{parts.scheme}://{parts.hostname}"
        return "/".join([base, *prefix, *keep])
    return None
