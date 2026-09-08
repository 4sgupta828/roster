"""The VOICES SOURCE REGISTRY — every source, with the measurement that admitted it.

WHY A REGISTRY AND NOT A LIST IN EACH CONNECTOR. This genre punishes taste. Discovering shows by
topic through Apple's keyless search returned 98 feeds for ten recruiting/careers terms, and the
measurement said: 11 of them write chapter lists, 81 are title-only, and the single highest chapter
yield in the whole set belonged to "The Business Savvy Therapist". Yield says whether a source is
INDEXABLE. It says nothing about whether it is ABOUT anything we care about. So each source carries
both numbers and the date they were taken, and `check()` re-asserts the bars — a source that drifts
below them is a failing test, not a silent quality leak.

WHAT THE NUMBERS MEAN (measured 2026-09-08 over each source's most recent items):
  `yield_pct`     — essays: share of items with a full body (>2,500 chars). Chapters: share of
                    episodes/videos carrying a timestamped list. A teaser feed or a chapterless show
                    yields nothing, which is the failure mode we want.
  `on_topic_pct`  — share of items whose text mentions this domain at all (hiring, interviewing,
                    résumés, sourcing, compensation, careers). A blunt instrument, deliberately.

AND WHY A HUMAN STILL READS THE TITLES. `talenthero.io` scored 90% full text and 100% on-topic and
publishes celebrity astrology ("Lindsay Clancy: Audit Of The Tragedy Through BaZi Lens") — the word
"talent" is doing all the work. The bars are necessary and not sufficient; REJECTED records why a
source that passes the numbers was still kept out, so the next person does not re-add it.
"""
from __future__ import annotations

from dataclasses import dataclass

# Admission bars. A podcast or channel is admitted ONLY on chapters: without a chapter list there is
# no lesson-level unit, and a title-only index is the trap this design exists to avoid.
BARS = {"essay": 50, "podcast": 30, "youtube": 30}
ON_TOPIC_BAR = 50

JOB_SEEKER, HIRING_TEAM, BOTH = "job_seeker", "hiring_team", "both"


@dataclass(frozen=True)
class VoiceSource:
    label: str
    kind: str                 # essay | podcast | youtube
    url: str                  # the feed (a YouTube channel's feed is built from its id)
    audience: str             # who the material is FOR — the one filter the surface offers
    author: str = ""          # the named writer, or "" for an institutional publisher
    role: str = ""            # what the author IS: recruiter, engineer, coach, manager, vendor
    yield_pct: int = 0        # measured
    on_topic_pct: int = 0     # measured
    measured: str = "2026-09-08"
    channel_id: str = ""      # youtube only

    @property
    def feed(self) -> str:
        return (f"https://www.youtube.com/feeds/videos.xml?channel_id={self.channel_id}"
                if self.kind == "youtube" else self.url)


# ── ESSAYS — full text, a named or institutional author, quotable AS OPINION ──────────────────────
# The vendors (Karat, Metaview, BrightHire, HackerRank) earn their place on the owner's own topics:
# fake candidates and AI-assisted cheating are 7-9% of the general genre and concentrate here.
ESSAYS: tuple[VoiceSource, ...] = (
    VoiceSource("Recruiting Brainfood", "essay", "https://recruitingbrainfood.substack.com/feed",
                HIRING_TEAM, "Hung Lee", "recruiter", 100, 100),
    VoiceSource("Karat", "essay", "https://karat.com/feed/", HIRING_TEAM, "", "vendor", 100, 100),
    VoiceSource("Metaview", "essay", "https://www.metaview.ai/resources/rss/", HIRING_TEAM, "", "vendor", 100, 100),
    VoiceSource("BrightHire", "essay", "https://brighthire.com/feed/", HIRING_TEAM, "", "vendor", 100, 100),
    VoiceSource("HackerRank", "essay", "https://www.hackerrank.com/blog/feed/", HIRING_TEAM, "", "vendor", 90, 100),
    VoiceSource("Workable", "essay", "https://resources.workable.com/feed", HIRING_TEAM, "", "vendor", 92, 100),
    VoiceSource("Ask a Manager", "essay", "https://www.askamanager.org/feed", BOTH, "Alison Green", "coach", 64, 72),
    VoiceSource("The Pragmatic Engineer", "essay", "https://newsletter.pragmaticengineer.com/feed",
                BOTH, "Gergely Orosz", "engineer", 65, 60),
    VoiceSource("Lenny's Newsletter", "essay", "https://www.lennysnewsletter.com/feed",
                BOTH, "Lenny Rachitsky", "operator", 75, 60),
    VoiceSource("Irrational Exuberance", "essay", "https://lethain.com/feeds/",
                HIRING_TEAM, "Will Larson", "manager", 90, 90),
)

# ── PODCASTS — the chapter list is the unit; the audio is never fetched ───────────────────────────
PODCASTS: tuple[VoiceSource, ...] = (
    VoiceSource("Recruiting Conversations", "podcast",
                "https://rss.libsyn.com/shows/133172/destinations/815992.xml", HIRING_TEAM, "Richard Milligan", "recruiter", 96, 96),
    VoiceSource("Recruiting is No Joke", "podcast", "https://feeds.megaphone.fm/recruitingisnojoke/",
                HIRING_TEAM, "", "recruiter", 96, 100),
    VoiceSource("Talent Acquisition Leaders", "podcast", "https://feeds.cohostpodcasting.com/KmXnxmaI",
                HIRING_TEAM, "", "recruiter", 96, 100),
    VoiceSource("The Modern People Leader", "podcast", "https://anchor.fm/s/450a9174/podcast/rss",
                HIRING_TEAM, "", "manager", 100, 80),
    VoiceSource("Beyond the Resume", "podcast", "https://anchor.fm/s/10c04281c/podcast/rss",
                JOB_SEEKER, "", "coach", 84, 100),
    VoiceSource("No B.S. Job Search Advice Radio", "podcast", "https://anchor.fm/s/86c94c4/podcast/rss",
                JOB_SEEKER, "Jeff Altman", "coach", 80, 96),
    VoiceSource("Recruiting Better", "podcast", "https://feeds.captivate.fm/recruiting-better/",
                HIRING_TEAM, "Ben Browning", "recruiter", 48, 100),
)

# ── YOUTUBE — captions are closed; the DESCRIPTION's chapter list is public and deep-links ────────
YOUTUBE: tuple[VoiceSource, ...] = (
    VoiceSource("interviewing.io", "youtube", "", JOB_SEEKER, "", "vendor", 67, 100,
                channel_id="UCNc-Wa_ZNBAGzFkYbAHw9eg"),
    VoiceSource("Engineering with Utsav", "youtube", "", JOB_SEEKER, "Utsav Sheth", "engineer", 73, 60,
                channel_id="UC4HiUdMwzyZhBUoNKXenO-A"),
    VoiceSource("ThinkSoftware", "youtube", "", JOB_SEEKER, "", "engineer", 67, 100,
                channel_id="UCVa66dAkbs60_A0P52Yjj7Q"),
    VoiceSource("Ken Jee", "youtube", "", JOB_SEEKER, "Ken Jee", "engineer", 60, 80,
                channel_id="UCiT9RITQ9PW6BhXK0y2jaeg"),
    VoiceSource("A Life Engineered", "youtube", "", JOB_SEEKER, "Steve Huynh", "engineer", 40, 67,
                channel_id="UCEHFikgnRuLd1HYKTLrae9Q"),
)

SOURCES: tuple[VoiceSource, ...] = ESSAYS + PODCASTS + YOUTUBE

# Kept OUT on purpose, with the reason — so a later reader does not re-add them by looking at the
# numbers alone. (measured 2026-09-08; yield / on-topic)
REJECTED: tuple[tuple[str, str], ...] = (
    ("Talent Hero (talenthero.io)", "90% full text and 100% 'on-topic', and it publishes celebrity "
     "astrology — the word 'talent' matched every item. The numeric bars cannot catch this; a human read can."),
    ("Dev Interrupted", "100% full text but 45% on-topic — an engineering-leadership show, hiring only in passing"),
    ("Rands in Repose", "36% on-topic"),
    ("Charity Majors", "40% on-topic — observability, not hiring"),
    ("Boolean Strings", "on-topic 80% but only 20% full text: a link-blog, nothing to quote"),
    ("Evil HR Lady", "0% full text — teaser feed"),
    ("The Pragmatic Engineer Podcast", "100% chapters, 44% on-topic — the newsletter is in; the show is mostly not about hiring"),
    ("Hiring Matters / The Recruiting Brainfood Podcast", "on-topic but 10% and 0% chapters: nothing to point at"),
    ("@hello_interview, @SelfMadeMillennial, @CareerVidz, @andylacivita, @ProfessorHeatherAustin",
     "on-topic but 0-7% chapters — no timestamps means no moment to link to"),
    ("@codingwithjohn, @techwithtim, @keeponcoding, @abovethenoise, @TinaHuang1",
     "chaptered but 7-40% on-topic — language tutorials, not hiring or interviewing"),
    ("interviewing.io blog, Greenhouse, Ashby, Gem, SeekOut, ERE, SourceCon",
     "no feed advertised at all; we do not scrape a publisher that does not offer one"),
)


def by_kind(kind: str) -> tuple[VoiceSource, ...]:
    return tuple(s for s in SOURCES if s.kind == kind)


def by_feed(feed: str) -> VoiceSource | None:
    return next((s for s in SOURCES if s.feed == feed), None)


def check() -> list[str]:
    """Every registered source still meets the bar it was admitted on. Returns the violations."""
    bad = []
    for s in SOURCES:
        if s.kind not in BARS:
            bad.append(f"{s.label}: unknown kind {s.kind!r}")
        elif s.yield_pct < BARS[s.kind]:
            bad.append(f"{s.label}: {s.kind} yield {s.yield_pct}% < {BARS[s.kind]}%")
        if s.on_topic_pct < ON_TOPIC_BAR:
            bad.append(f"{s.label}: on-topic {s.on_topic_pct}% < {ON_TOPIC_BAR}%")
        if s.audience not in (JOB_SEEKER, HIRING_TEAM, BOTH):
            bad.append(f"{s.label}: unknown audience {s.audience!r}")
        if not s.feed.startswith("https://"):
            bad.append(f"{s.label}: no feed")
    return bad
