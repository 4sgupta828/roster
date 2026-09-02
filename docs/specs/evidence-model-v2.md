# Evidence model v2: broader footprints, calibrated self-statement, evidence as a portfolio

Status: DRAFT for review. Date: 2026-09-02.
Companion to `talent-intelligence-redesign.md` (Phases 0–3 implemented). This note answers three
questions raised after the first Evidence Rail shipped:

1. We are far behind on gathering public evidence about an individual — talks, videos, podcasts,
   and other professional footprints are absent.
2. "Self-stated" and "employer-stated" are too coarse; a self-stated claim should be CALIBRATED by
   its plausibility (industry, company, past work), and employer-stated is one data point, not a
   tier.
3. Evidence should be presented more generally than a single headline rung per person.

## 1. Where the evidence actually is (and what we hold today)

What the index holds per person today is almost entirely ONE family: a GitHub bio (self-stated)
or one registry row (OpenAlex / YC / NPI / SEC / TheOrg). 182 of 200 rows in a typical infra
search are self-stated-only. That is the honest state, and it is thin.

Public professional footprints we do NOT gather yet, ranked by evidentiary value per unit of ingest
effort:

| Footprint | What it proves | Source / how to get it | Evidence type | Notes |
|---|---|---|---|---|
| Conference talks & recorded presentations | capability + affiliation at a DATE | YouTube Data API v3 (search + video metadata, captions where public); conference sites (Strange Loop, QCon, KubeCon/CNCF, PyData, NeurIPS/ICML/ICLR talk pages); Sessionize speaker pages | artifact-backed, employer/event-stated (speaker bio) | Highest-value gap. Titles + abstracts + event + date are text we can span-verify; the VIDEO is a link, not evidence we quote. Captions are downloadable for many talks → quotable. |
| Podcasts | narrative career evidence, current focus | Podcast RSS feeds + episode show-notes (PodcastIndex API, Apple Podcasts search); transcripts where published | artifact-backed (guest), self-stated (what they say about themselves) | Show notes name the guest + affiliation at a date. |
| Publications & preprints | capability, collaborators | OpenAlex (have), Semantic Scholar, arXiv, DBLP, Google Scholar (no API — skip) | artifact-backed + structured | Already partly ingested; not yet linked per PERSON as artifacts in the Rail. |
| Patents | capability, employer at filing date | USPTO PatentsView (have connector), Google Patents | structured + artifact-backed | Inventor + assignee = employer-at-date. Strong for "worked at X in year Y". |
| Open-source contributions | capability, collaboration | GitHub (repos, commits, orgs, PR activity — GraphQL), npm/PyPI/crates maintainers | artifact-backed | Today we only keep the bio. Repo ownership vs contribution is the distinction the spec demands. |
| Engineering blogs & personal sites | capability, self-narrative | company engineering blogs (have `eng_blog` connector), personal site RSS, Medium/Substack | self-stated (personal), employer-stated (company blog byline = employment evidence) | A company-blog BYLINE is employer-stated employment evidence at a date. |
| Standards & community | expertise | IETF datatracker, W3C, PEP authors, RFC authors, CNCF TOC, Apache committers | structured + artifact-backed | Small populations, very high signal. |
| Press & interviews | affiliation at a date, seniority | GDELT (have), press releases (funding announcements list execs), Crunchbase-style directories | independent reporting | Lower per-claim strength, good corroboration. |
| Registries beyond ours | affiliation | Companies House officers (have), SEC insiders (have), state business filings, nonprofit boards (IRS 990) | structured | Board seats, officer roles. |
| Event attendance / program committees | community standing | conference PC pages, workshop organizers | employer/event-stated | Cheap, dated. |

Rule of thumb: prioritize footprints that are (a) DATED, (b) name an AFFILIATION, and (c) contain
TEXT we can quote. Talks, patents, company-blog bylines, and podcast show notes score on all three.

## 2. Ingest plan (prod-direct, connector pattern already exists)

Each footprint becomes a connector producing `Document → Block` rows with `source_kind` facets
(`talk`, `podcast`, `patent`, `repo`, `blog_post`, `press`) and, critically, a per-person LINK step
that attaches the artifact to the `rs_entity` person: `rs_claim(subject=person, predicate=
spoke_at|authored|invented|contributed_to|interviewed_on, object=artifact)` with the evidence span.
That claim row is what the Evidence Rail lists under "Public artifacts", each with a date.

Identity linking is the hard part (a talk by "Tom Brown" is not automatically github:tombrown):
- link only on STRONG keys first — a GitHub handle or personal-site URL that appears in the
  speaker bio / show notes / paper author page; an ORCID; an email domain + name match against a
  company we already hold for the person;
- name-only matches are stored as `candidate_links` with a confidence and are NOT shown as the
  person's evidence until a second key agrees (the same discipline as corroboration);
- never merge two people who share a name (the design invariant).

Cost: HTTP + embeddings only for most (YouTube API quota is the binding limit: 10k units/day ≈
100 searches or 10k video-metadata reads — run it as a nightly worker leg with per-person quotas,
targeting people ALREADY in maps/shortlists first, then the rest of the index).

## 3. Calibrating self-stated claims (plausibility, not truth)

A self-stated claim ("Staff Engineer at Stripe, payments infra") is neither verified nor worthless.
Treat it as a CLAIM WITH A PRIOR, and compute a code-owned plausibility band from independent,
grounded signals:

- **Consistency with the person's other evidence**: does the stated company appear in any artifact
  (a company-blog byline, a talk speaker bio, a patent assignee, a repo org membership)? Each
  independent family that agrees raises the band; this is the same corroboration machinery, applied
  as a graded score instead of a binary.
- **Employer plausibility**: is the stated company a real, indexed employer? Does its size (from
  registries / index headcount) make the stated title plausible (a "VP of Engineering" at a 4-person
  startup is plausible; five "Chief Architects" claiming the same 30-person company is not)?
- **Domain consistency**: the person's artifacts (repos, papers, talk topics) cluster in the stated
  domain? "Payments infra" + Kafka/Go repos is consistent; "ML researcher" with no artifacts and a
  crypto-only footprint is not.
- **Temporal consistency**: the stated tenure fits dated artifacts (a 2021 talk bio at company A and
  a 2024 patent assigned to company B support "moved A→B", contradict "at A since 2015").
- **Profile hygiene signals** (weak, never decisive): account age, activity, a linked personal site
  that links back.

Output: `self_stated_calibration ∈ {consistent, uncorroborated, contradicted}` with the grounded
reasons listed — shown in the Rail as "Self-stated · consistent with 2 independent artifacts" or
"Self-stated · no independent evidence yet" or "Self-stated · CONTRADICTED by …". Never a numeric
"truth score"; the reader sees the reasons.

Employer-stated becomes ONE input to the same calibration rather than a rung: an org-chart page and
a self-statement that agree = corroborated; an org-chart page alone = employer-stated (dated),
exactly as today.

## 4. Presenting evidence as a portfolio, not a rung

Replace the single headline pill with an EVIDENCE PORTFOLIO per person:

```
Evidence portfolio — Ada Nguyen
  Affiliation   Stripe (2022–)     self-stated · consistent: company-blog byline 2023, talk bio 2024
  Capability    distributed infra  artifact-backed: 3 repos (owner), 1 talk (QCon 2024), 1 patent
  Seniority     Staff              self-stated · uncorroborated
  Location      Bay Area           structured (talk venue) + self-stated
  Footprint     talks 2 · repos 7 · papers 0 · patents 1 · podcasts 1 · press 0
  Freshness     newest artifact 2024-11 · profile text undated
  Gaps          no independent evidence of seniority; no publications
```

Design rules:
- one line per CLAIM AXIS (affiliation, capability, seniority, location, education), each with its
  own type + calibration, not one label for the whole person;
- a FOOTPRINT COUNT strip (talks/repos/papers/patents/podcasts/press) — the most honest "how much
  public evidence exists" signal, and it directly exposes coverage gaps per person;
- freshness per axis from artifact dates (the missing ingredient today);
- the map-level coverage panel gains a "footprint coverage" histogram (people with ≥1 dated
  artifact, ≥1 talk, ≥1 corroborated axis).

Ranking implication: "top talent" queries should rank by EVIDENCE DEPTH within relevance bands
(corroborated affiliation + dated artifacts first), never by self-stated seniority words — which
also fixes the "top X compiles into a seniority AND-gate" problem at the root.

## 5. Sequencing (each step ships value alone)

1. Per-person artifact links from sources we ALREADY hold (OpenAlex works, PatentsView inventors,
   eng-blog bylines, GitHub repos/orgs via the existing token) → "Public artifacts" section +
   footprint counts in the Rail. Zero new APIs.
2. Talks: YouTube Data API + 6 conference sites; podcasts via PodcastIndex/RSS. Nightly worker leg,
   quota-aware, shortlist-first.
3. Calibration bands from (1)+(2), shown as reasons. Corroboration becomes graded.
4. Portfolio presentation + evidence-depth ranking; coverage histogram.
5. Identity linking hardening (candidate_links, second-key rule) as artifact volume grows.

Kernel stays domain-free: connectors + link steps live in the vertical; claim/evidence/coverage
contracts are already generic.
