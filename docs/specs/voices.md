# Voices — practitioner content about job search and hiring, as a mode

**Status:** Phase 1 BUILT and LIVE in production behind `ROSTER_VOICES`. Measured and panel-reviewed
before any code was written.
**Owner ask (2026-09-08):** "Can we add a voices mode just like Eigen's where the content is relevant for
Talent and Job search? Centered around how to search for jobs, figure out talent, deal with fake resumes,
AI based cheating in interviews, doing interviews right, how to prepare for interviews in specific domains
and all. Pull for Youtube or Podcast places and any relevant places."

The sibling product (eigen) shipped this for founder/VC content. **The mechanism ports; the source
landscape does not.** Everything below was measured before a line was written, because the eigen design
turns on facts about a genre and this is a different genre.

---

## 1. What the measurements say (all free, run 2026-09-08, before any code)

### 1.1 Podcasts: the genre is poor

98 distinct feeds discovered through Apple's keyless podcast search over ten topic terms (recruiting,
talent acquisition, job interview, job search, hiring, interview preparation, career advice, technical
interview, sourcing candidates, resume), each measured over its latest 25 episodes:

| | |
|---|---|
| feeds with a timestamped chapter list on ≥30 % of episodes | **11 of 98 (11 %)** |
| feeds with substantial show notes but no chapters | 6 |
| **title-only — nothing to index** | **81 of 98 (83 %)** |

For comparison, eigen's founder/VC allowlist runs 20–100 %. The best on-topic shows here
(chapters % / full-notes %): Recruiting Conversations 96/100, Recruiting is No Joke 96/100, Talent
Acquisition Leaders 96/68, Beyond the Résumé 84/68, No B.S. Job Search Advice Radio 80/12, Recruiting
Better 48/52; plus The Modern People Leader 100 and The Pragmatic Engineer Podcast 100/64.

**Topic search cannot pick the allowlist.** It returned "A Hot Dog Is a Sandwich", "Bloody Brilliant
Beers" and "The Pet Business Hiring Podcast" — one of them at 100 % chapter yield. Yield measures
whether a show is *indexable*, never whether it is *relevant*.

### 1.2 YouTube: a leg eigen does not have, and it works

Captions remain closed (the timedtext endpoint is token-gated — eigen measured 0 bytes on every format,
and nothing has changed). But the video DESCRIPTION in a channel's RSS often carries the chapter list,
and `&t=<seconds>` deep-links to the moment. Chapter yield over each channel's latest 15 videos:

| ≥30 % (usable) | 0 % (metadata only) |
|---|---|
| @engineeringwithutsav 73, @ThinkSoftware 67, @interviewingio 67, @KenJee_ds 60, @codingwithjohn 53, @abovethenoise 47, @ALifeEngineered 40, @techwithtim 33, @keeponcoding 33 | @tryexponent, @NeetCode, @ByteByteGo, @CareerVidz, @biginterview, @interviewkickstart, @andylacivita, @AmazonJobs, @linkedin |

**Constraint:** a channel feed returns only the **latest 15 videos**. There is no back catalogue without
the YouTube Data API (key + quota). We accumulate over time instead.

### 1.3 Essays: the strongest leg, as in eigen

Full-text yield (% of items over 2 500 chars / median chars):

Recruiting Brainfood 100/11.0k · Karat 100/11.0k · Metaview 100/14.0k · BrightHire 100/16.2k ·
Dev Interrupted 100/6.8k · Charity Majors 95/10.3k · Irrational Exuberance 90/6.2k · HackerRank 90/4.5k ·
Workable 90/6.3k · Lenny's 75/3.9k · Ask a Manager 70/4.8k · The Pragmatic Engineer 65/12.8k ·
Rands in Repose 65/4.7k.

No feed advertised at all (would need scraping — not in v1): interviewing.io, Greenhouse, Ashby, Gem,
SeekOut, ERE, SourceCon.

### 1.4 Do these sources cover what the owner actually asked for?

454 items across 18 measured sources, counted by topic regex. **The headline asks are the rarest:**

| topic | share of items | topic | share of items |
|---|---|---|---|
| hiring process | 51 % | screening / assessment | 30 % |
| compensation | 41 % | sourcing | 25 % |
| job search | 34 % | interviewing well | 10 % |
| résumé | 31 % | **fake résumés / fake candidates** | **9 %** |
| | | **AI cheating in interviews** | **7 %** |
| | | **interview prep (domain-specific)** | **6 %** |

This is the finding that shapes the allowlist: the two topics the owner named most vividly are thin in
general career content and **concentrated in the assessment/interview-intelligence vendors** — Karat
(3 of 9 items on fake candidates, 3 on AI cheating), Metaview (4/15, 5/15), BrightHire (2/10, 4/10),
HackerRank (0/10, 3/10), Dev Interrupted (5/20, 5/20). A general "careers podcast" allowlist would have
missed the ask almost entirely.

---

## 2. Panel review (2026-09-08)

Two external reviewers (Codex `gpt-5.5`, Gemini 3 Pro) plus a code-grounded pass. Where they agreed, the
agreement is recorded as a decision; where they split, the reasoning for the call is recorded.

**Agreed, and adopted:**
1. **Essays first.** Both, independently, chose essays as the single leg to ship if only one could: full
   text, a named author, no diarization problem, and the only leg with enough text for keyword search.
2. **One surface with an audience control**, not two modes. The topics are two-sided (interviewing well,
   compensation, AI cheating, domain prep); splitting the mode hides the overlap and doubles navigation.
3. **A distinct authority register for advice.** This corpus is opinion. It may support "X argues Y",
   never "Y works". Codex: *"the single biggest risk is laundering career advice into product authority."*
4. **Curated allowlist with quantitative admission criteria**, recorded per source — not automated topic
   discovery, which the measurement already showed produces hot-dog podcasts.
5. **Cut for v1:** company binding (eigen's guest→company card), the domain sub-taxonomy, any model spend,
   and any synthesized recommendation.

**Split — and the call:**
- *YouTube.* Codex: ship it as a **navigation layer** with a ≥30 % chapter bar. Gemini: **cut it** — the
  15-video window plus sparse chapter text makes `tsvector` return dead ends.
  **Call: ship it, with Gemini's objection answered concretely.** The owner asked for YouTube by name, and
  the density objection is real but already solved in eigen's production run: OR-ed query terms with
  stopwords dropped, and `ts_rank(..., 1)` length normalisation, without which a long essay mentioning a
  word twice outranks a chapter titled exactly that word. On top of that, a chapter block here carries
  **the channel, the video title and the chapter title** as its searchable text, not the chapter title
  alone — which is the density fix stated as data rather than as ranking.
- *Block-level topic tagging.* Gemini called the taxonomy over-built for a keyword-only v1: classifying a
  six-word chapter title into a rigid taxonomy by keyword "will yield terrible precision". Correct.
  **Call: topic and audience are properties of the SOURCE, measured and curated, not guesses about a
  block.** A source card declares what it covers; the query does the rest. Block-level topics wait for
  embeddings.

---

## 3. The design

### 3.1 Three content types, two of them quotable

| type | source | quotable? | register |
|---|---|---|---|
| `essay` | full text from the publisher's own feed | **yes**, as opinion | "X argues …", attributed to the named author |
| `chapter` (podcast) | a timestamped show-note marker | **no — pointer only** | where to listen; never what was said |
| `chapter` (YouTube) | a timestamped line in the video description | **no — pointer only** | where to watch; never what was said |

A chapter title is written by a producer or an uploader, not spoken by anyone. It is indexed for
retrieval and display and is structurally barred from the evidence path, exactly as in eigen.

### 3.2 Advice is a signal, not a fact — the register this feature lives by

Roster already holds that market sentiment can never be controlling. Practitioner advice is the same
class of thing and gets the same treatment, one tier lower than analysis:

- An essay passage supports **"this author argues X"** and nothing else. It can never support "X is
  effective", "X beats the ATS", or a recommendation in Roster's own voice.
- The surface shows **what practitioners say, including where they disagree** — never one synthesized
  answer. Two sources that contradict each other are the useful output, not a bug to resolve.
- The factual engine must not reach into this corpus **at all**. "What is Stripe's interview process" is
  not answerable from advice content; "how do people prepare for it" is. Enforced structurally, not by
  prompt: the manifest declares `non_evidence_facets={"source_kind": ("chapter_pointer",),
  "voices": ("1",)}` and the retrieval source merges those exclusions into EVERY request, so a caller
  cannot forget. Both facets are declared and either alone would do it — the chapter pointers because
  they are not speech, the whole corpus because it is advice.

### 3.3 The source registry replaces taste

Every source is admitted by a recorded **source card**: feed URL, kind, the date measured, items sampled,
chapter yield, full-text yield, the topics it demonstrably covers, and its audience. Admission bars:

| kind | bar |
|---|---|
| essay feed | stable feed, named or institutional author, full-text yield ≥ 50 % |
| YouTube channel | chapter yield ≥ 30 % over the latest 15, and on-topic |
| podcast | chapter yield ≥ 30 %, or full show notes ≥ 50 %, and on-topic |

A source that stops writing chapters simply yields nothing, which is the failure mode we want.

### 3.4 Surfaces

v1 is one **Voices** mode beside Jobs and Talent, with an audience control (job seekers / hiring teams /
both) defaulted from the tab the user came from. The contextual "how to prepare" strip on a job card is
Phase 2 — it is the right idea and it is not what makes v1 work.

---

## 4. Build order

**Phase 1 (this build) — zero model spend.** Blocks land in `rs_block` with no embedding; `tsv` is a
generated column, so rows are searchable the moment they land and embeddings backfill when they are worth
buying.
1. The source registry with the measured cards.
2. One chapter connector covering podcasts and YouTube (both are "a feed item whose description holds a
   timestamp list"), emitting one pointer block per chapter with its offset and deep link.
3. The practitioner-essay connector over the full-text allowlist.
4. `advice` and `pointer` tiers in the vertical's evidence policy, with the structural non-evidence bar.
5. `/voices/search` — keyword, audience- and kind-filtered, length-normalised, deduped per document.
6. The Voices mode in the web shell.

**Phase 2 — when credits are worth spending.** Embeddings for semantic ranking; block-level topic
extraction; the disagreement clustering; the job-card strip.

**Explicitly not built:** machine transcription (cost + ToS), YouTube caption scraping (closed),
scraping publishers who advertise no feed, and any title-only podcast index.

---

## 5. In production (2026-09-08)

Live at `ROSTER_VOICES=1`. The first ingest took **7.7 seconds and spent nothing** — no embeddings, no
model calls:

| | blocks |
|---|---|
| podcast chapter pointers (`show_notes`) | 819 |
| YouTube chapter pointers (`youtube_chapters`) | 480 |
| essay passages (`practitioner_essay`) | 216 |
| **total** | **1,515** |
| embeddings written | **0** |
| boilerplate blocks stamped (newsletter sidebars) | 23 |

**Verified on prod**, phone-width (390 px):

- "AI cheating in interviews" → 6 essay cards, all from Karat / BrightHire / Metaview — the vendors
  §1.4 predicted would carry this topic. Each is quoted and labelled *"Advice from a hiring-tools
  vendor — what they argue, not a verified outcome."*
- "how to prepare for a system design interview" → 25 cards, 8 of them video moments, each opening at
  its own second (e.g. `watch?v=rrWbgtG5Bes&t=836`). This is the case Gemini predicted would dead-end;
  the length normalisation is what makes it work.
- A chapter card is never rendered as a quotation, and says *"Chapter marker written by the publisher —
  watch from this point."*
- The audience filter re-runs the question and narrows 25 → 12.
- No horizontal overflow; tap targets 40 px; no console errors.

**Two bugs the mobile pass caught and fixed**: a fourth tab pushed the mode row past a 390 px viewport
(the row now scrolls itself), and the filter/link tap targets were 36 px.

### What is honestly weak

1. **Vendor marketing.** Karat, Metaview and BrightHire are the best sources on fake candidates and AI
   cheating AND they are selling something; some passages are product copy. The register names them as
   a vendor, which is the mitigation, not a cure.
2. **Keyword false positives.** "fake candidates" matched a video titled "Being Enthusiastic Is Not
   Being Fake". Semantic ranking (Phase 2) is the real fix.
3. **YouTube has no back catalogue** — 15 videos per channel per fetch, so the index grows forward.
4. **No topic facets yet**, by decision: a six-word chapter title cannot be classified by keyword
   without lying about the precision.

### Operations

`POST /admin/voices/jobs` with `X-Admin-Token`: `{"kind":"ingest"}` (re-fetch every feed; safe to
re-run, blocks key on a hash of their text) and `{"kind":"mark_boilerplate"}`. Both are free. Run
ingest on a cadence to keep the YouTube leg growing.

---

## 6. The surface (2026-09-08, later) — a reading feed, not a result list

The first cut rendered moments as chat turns. That was wrong for this material: a corpus of takes is
something you BROWSE, and a thread of answers is something you read once. Voices now owns a surface.

**What a card is.** Artwork · kind · show · date · ⏱ offset · view count, then the moment itself, then
who said it **and what they are** — a role badge (`recruiter`, `career coach`, `engineer`,
`eng manager`, `vendor`), because a vendor's take on their own product and a working recruiter's take
are not the same evidence and the reader must be able to weigh them. Then three actions and the
register line.

**Play where the reader is.** ▶ Play here embeds the video at its second (`youtube-nocookie`) or plays
the audio enclosure from the same offset; the link beside it still sends the traffic to the publisher.
This is the affordance a pointer corpus lives on — a marker saying "listen from 20:30" with no way to
listen is worse than no card.

**☰ What it says** reads the whole piece on demand: 3–6 points, up to 2 verbatim quotes, and the
author's own paragraphs grouped under model-written headings — the model ORGANISES, it never supplies
prose, so a section cannot contain a sentence the author did not write. `verify()` drops any point
asserting a figure the piece never states. Lazy and cached per document, so spend follows attention:
one call, ~2.6 s, ≈ $0.001, and free forever after. With no model available the same panel is filled
extractively from the piece's own sentences and the card says so.

**One-tap starts.** The empty state of a small curated corpus should be a destination, not a blank
box, and the shelves change with the audience — a job seeker sees *Fake résumé claims · Beating the
screen · System design prep · Behavioural interviews · Negotiating an offer · Referrals*; a hiring
team sees *Fake candidates · AI in the interview · Structured interviews · Take-home vs live ·
Sourcing · Debriefs*.

**ⓘ Sources** shows every source with the numbers that admitted it and the rejections with their
reasons. A reader weighing advice is entitled to know whose advice, and why it is here at all.

**★ Keep** stores a moment against the ACCOUNT with a snapshot of the card, so a kept item still
renders after the feed has rolled past it.

Also: browse windows (week / month / quarter / all time) and a "most watched" order that uses
YouTube's own number and never turns it into a ranking of ours.

### Fixed in this pass
- The surface started **927 px** below the fold behind the shell's nav and account banner; entering the
  mode now brings it to the top.
- "Open at 0:00" for a chapter that opens its piece — now "Watch ↗" / "Listen ↗".
- **HTML entities were reaching the corpus as text** ("the team&#x2019;s mind") — feeds ship
  entity-encoded prose and several double-encode it. Unescaped twice at the doc builder; 0 entity
  blocks in prod after a re-ingest.
- The summary's model call passed `timeout` positionally against a keyword-only parameter, so every
  summary silently fell back to extractive and cached that outage for three days. `refresh: true`
  now exists precisely so an outage's result cannot outlive it.
