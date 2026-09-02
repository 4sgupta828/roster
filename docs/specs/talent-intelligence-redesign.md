# Spec: Roster talent intelligence redesign

Status: DRAFT for review.
Date: 2026-09-02.
Scope: product positioning, information architecture, interaction model, and UI design direction for the recruiting/talent-intelligence wedge.

## Goal

Reposition the Roster web experience from a broad people/jobs/search tool into an evidence-typed talent intelligence workbench for high-stakes technical hiring.

The product should make its differentiation obvious within one session:

- Roster finds people with public evidence of relevant work, roles, and affiliations.
- Roster explains why each person surfaced.
- Roster shows the source evidence behind every material claim.
- Roster distinguishes self-stated, employer-stated, artifact-backed, structured, and corroborated evidence.
- Roster is honest about corpus coverage and missing evidence.

This is not a generic recruiting platform, ATS, applicant tracker, or consumer job board. The first commercial artifact is a Talent Map: a curated, evidence-cited map of people relevant to a hard hiring search, with each claim labeled by evidence type. Connection paths are a later expansion surface, not a v1 dependency.

## Strategic Positioning

### Target buyer

Primary:

- Seed to Series C founders hiring senior technical talent.
- VC talent partners supporting portfolio hiring.
- boutique technical recruiters and exec search researchers.

Secondary:

- internal recruiting research teams at technical companies.
- company-mapping and market-intelligence teams.

Non-goals for the first design pass:

- consumer job search as the default product.
- ATS replacement.
- automated candidate scoring or rejection.
- broad HR workflow management.

### Product promise

Current broad promise:

> Search people and jobs.

Target promise:

> Build evidence-typed talent maps for hard technical searches.

Supporting copy:

> Roster maps who appears relevant, what public evidence supports that, and what remains unverified.

## Competitive Differentiation

The recruiting market is crowded around large databases, AI sourcing, outreach automation, and ATS/CRM workflow. Roster should avoid competing on database size or campaign automation in v1.

Roster should compete on:

- public evidence of relevance, not keyword presence.
- cited professional evidence, not opaque profile enrichment.
- typed evidence that separates self-stated claims from employer-stated, artifact-backed, structured, and corroborated claims.
- source quality and freshness, not only match percentages.
- coverage honesty, not implied completeness.

The interface must therefore foreground evidence type, source strength, freshness, and coverage status. If the user mostly sees a chat box and generic profile cards, the product looks like a weaker incumbent.

## Design Principles

1. Evidence before persuasion.
   Every material UI element should answer: why is this person here, and what proves it?

2. Evidence is typed, not flattened.
   A self-stated LinkedIn headline, a company team page, a GitHub contribution, and a registry record are all useful, but they must render as different kinds of support.

3. Analyst workbench, not social network.
   The experience should feel like a serious sourcing brief, with dense comparison, source marks, and inspection tools.

4. Maps over feeds.
   The default artifact is a ranked, filterable Talent Map, not an infinite list of candidates.

5. Confidence is visible.
   Grounded, inferred, stale, weak, and missing evidence must be visibly different states.

6. No publication bias.
   Roster must not imply that excellent candidates need public artifacts. Role, affiliation, employer, and self-stated profile evidence remain valid when clearly labeled.

7. Human decision support only.
   Avoid automated hiring-decision language. Use evidence, reasons, and review state instead of opaque candidate scores.

8. Mobile remains supported.
   Every user-facing surface must work at <=400px. Dense tables need stacked cards or horizontal scrolling with clear preserved labels.

## Evidence Taxonomy

Roster should treat self-stated professional information as useful evidence, not as noise. The product's job is to label evidence accurately, not to pretend only public artifacts count.

### Evidence types

- Self-stated: LinkedIn page, personal site, resume, GitHub bio, X bio, or other profile controlled by the person.
- Employer-stated: company team page, job announcement, press release, speaker page, portfolio page, or other organization-authored source.
- Artifact-backed: GitHub repository, commit, technical post, paper, patent, talk, dataset, package, or other public work product tied to the person.
- Structured: constrained public systems such as GitHub organizations, OpenAlex, ORCID, EDGAR, Companies House, Wikidata, or other registry-like sources.
- Corroborated: two or more independent evidence families support the same material claim.
- Inferred: Roster normalizes meaning from grounded text, such as mapping "ML platform lead" to a canonical function.
- Weak: the source mentions the person or adjacent topic but does not strongly support the exact claim.
- Gap: the useful field is missing or not yet in the corpus.
- Stale: the source is old or insufficient for current-role/current-affiliation claims.

### Evidence rules

- Self-stated evidence can justify inclusion in a Talent Map when relevant, but it must render as self-stated.
- Self-stated evidence cannot silently become "verified" or "corroborated."
- Employer-stated evidence is stronger than self-stated for role and affiliation claims, but still remains stated evidence unless independently corroborated.
- Artifact-backed evidence is strongest for capability claims, but lack of artifacts must not suppress otherwise relevant private-company talent.
- Structured evidence is strong for the fields the source actually constrains, not for broader capability claims.
- Corroboration upgrades confidence only when the sources are independent enough to avoid echoing the same biography.

## Information Architecture

Replace the current top-level emphasis on People / Jobs / Q&A with buyer tasks.

### Primary navigation

- Talent Map
- Company Map
- Person Dossier
- Evidence Types
- Saved
- Coverage

### Secondary or gated surfaces

- Job Search
- Apply Assistant
- Connection Path
- Expert Panel
- Pulse
- Admin Settings
- Corpus Search

Jobs, Apply Assistant, and Connection Path can remain available, but they should not compete with Talent Map for first-screen attention during the GTM wedge.

## First-Screen Experience

The first viewport should communicate the product through a live-feeling artifact, not a marketing hero.

### Desktop layout

```
+--------------------------------------------------------------------------------+
| ROSTER                      Talent Map | Company Map | Evidence | Saved          |
+--------------------------------------------------------------------------------+
| Search brief                                                                    |
| [ Senior infra engineers with public evidence of vector DB or distributed ML ]  |
| [Sources: GitHub] [Papers] [Company pages] [Talks] [Filings]  [Build map]       |
+------------------------------+-------------------------------------------------+
| Coverage                     | Talent Map                                      |
| 184 people searched          | Person        Evidence type  Strength   Review  |
| 42 strong matches            | A. Nguyen     artifact      strong     Open    |
| 6 sources active             | M. Patel      self+paper    mixed      Open    |
| 3 known gaps                 | J. Lee        employer page stated    Open    |
+------------------------------+-------------------------------------------------+
| Selected person: why surfaced, citations, source quotes, evidence ladder        |
+--------------------------------------------------------------------------------+
```

### Mobile layout

```
+------------------------------------+
| ROSTER              Talent Map     |
+------------------------------------+
| [Search brief input              ] |
| [Build map]                         |
| Coverage: 184 searched, 42 strong  |
+------------------------------------+
| A. Nguyen                           |
| Why: built vector indexing infra    |
| Evidence: GitHub, talk              |
| Strength: artifact-backed           |
| [Inspect] [Save]                    |
+------------------------------------+
| M. Patel ...                        |
+------------------------------------+
```

## Core Workflow

### 1. Define search

The search form should collect a role/search brief in natural language and expose structured controls only when they improve precision.

Inputs:

- search brief.
- target role family.
- seniority.
- location or geography.
- company include/exclude.
- source types.
- evidence requirements.
- optional connection target for later graph-enhanced searches, such as "connected to our team" or "connected to ex-Stripe".

Primary action:

- Build Talent Map.

Secondary actions:

- Preview coverage.
- Save brief.
- Import job description.

### 2. Generate candidate universe

Roster returns a map, not a plain answer.

Required response structure:

- coverage summary.
- ranked groups, not only a flat list.
- candidate rows.
- source and evidence distribution.
- known gaps.
- suggested next query refinements.

Candidate groups:

- strongest artifact-backed evidence.
- employer-stated or structured role evidence.
- self-stated but relevant.
- likely relevant, weaker evidence.
- requires more evidence.

### 3. Review evidence

Clicking a person opens a dossier pane.

Dossier sections:

- Why surfaced.
- Evidence ladder.
- Career and affiliation evidence.
- Public artifacts, when available.
- Contact/profile links.
- Gaps and ambiguity.
- Notes and review state.

Every cited claim should show:

- source type.
- source title.
- quote or extracted span.
- date or freshness.
- evidence tier.
- evidence type.
- link to source.

### 4. Inspect relationship evidence later

Connection Path is valuable, but it is not required for the first wedge. The first version should only show relationship claims when Roster has explicit base edges from public evidence. It should never imply a personal warm-intro graph unless the user has supplied seed nodes or integrations.

Base edges to collect before making paths prominent:

- person works at company.
- person founded company.
- person advises or sits on the board of company.
- person authored paper, post, patent, or talk.
- person contributed to repo or organization.
- person spoke at event.
- company funded by investor or fund.

Derived relationship examples:

- cofounder path from two people tied to the same company.
- coauthor path from two people tied to the same work.
- possible colleague path from overlapping employer evidence.
- collaborator path from shared repo or organization evidence.

Relationship states:

- No path data yet.
- Public path found.
- Possible shared affiliation.
- Warm path from your network, only after the user supplies network seeds.

Path design should be compact and inspectable when present. Avoid decorative graph clouds as the default view. Use graph visualization only when it clarifies clusters.

### 5. Shortlist and export

Talent Maps should produce durable artifacts.

Required actions:

- save candidate.
- assign review state.
- add note.
- export CSV.
- export brief.
- share private link.

Later integrations:

- Ashby.
- Greenhouse.
- Lever.
- Gem.
- Loxo.
- Clay.

## Candidate Card Redesign

Current candidate cards are useful but should become evidence packets.

Required fields:

- Name.
- Current role and company.
- Location.
- Why surfaced.
- Evidence chips.
- strongest quote or proof snippet.
- Evidence type and strength.
- Optional public relationship summary when available.
- Freshness.
- Review actions.

Example:

```
+-------------------------------------------------------------------+
| Anika Nguyen                                      [Save] [Inspect] |
| Staff infra engineer, ExampleDB       Bay Area    last seen 2026   |
| Why surfaced: built vector indexing and distributed query systems  |
| Evidence: GitHub repo | conference talk | self-stated profile      |
| Proof: "..."                                                    [1] |
| Support: artifact-backed + self-stated                             |
| State: unreviewed                                                 |
+-------------------------------------------------------------------+
```

States:

- Self-stated: the person represents the claim on a profile, personal site, resume, or bio.
- Employer-stated: a company, event, or third-party page states the role or affiliation.
- Artifact-backed: a repo, paper, patent, talk, post, or similar work product supports the capability.
- Structured: a constrained public system, such as GitHub, OpenAlex, ORCID, EDGAR, or Companies House, supports the claim.
- Corroborated: two or more independent evidence families support the same material claim.
- Inferred: model-normalized meaning from grounded text.
- Weak: source exists but does not strongly support the claim.
- Gap: useful field is missing.
- Stale: source is old or no longer enough for current-role claims.

Do not use candidate "fit score" as the primary signal. If ranking is shown, label it as map rank or evidence match, and explain it with reasons.

## Talent Map Table

The map table is the main paid surface.

Default columns:

- Person.
- Current company.
- Role.
- Evidence-backed capability.
- Proof sources.
- Evidence type.
- Evidence strength.
- Freshness.
- Review state.

Optional columns:

- prior companies.
- education.
- GitHub.
- publications.
- talks.
- patents.
- likely contact route.
- public relationship path.
- notes.

Filters:

- source type.
- evidence strength.
- evidence type.
- connection distance.
- current company.
- prior company.
- location.
- seniority.
- function.
- freshness.
- reviewed/saved state.

The table should support dense scanning on desktop and switch to stacked evidence cards on mobile.

## Coverage Panel

Coverage is a first-class product surface, not diagnostics.

Every Talent Map should show:

- population searched.
- sources included.
- source recency.
- number of candidates by evidence type.
- number of candidates with strong or corroborated evidence.
- number with weak or missing evidence.
- active filters.
- known corpus gaps.
- a plain-language caveat: "Not found means not in the current corpus, not nonexistent."

Coverage should appear before and after map generation:

- before: "what Roster can search for this brief."
- after: "what evidence supports this map."

## Visual Direction

Subject: evidence-typed technical talent research for founders, VC talent partners, and search researchers.

Single job of the UI: turn a hard hiring question into an inspectable, shareable Talent Map.

### Token system

Colors:

- Canvas: `#F6F7F4`, a cool off-white that feels like a working document.
- Ink: `#17201B`, near-black with a green cast for readability.
- Panel: `#FFFFFF`, for cards and active panes.
- Evidence green: `#08744E`, verified facts and strong evidence.
- Gap amber: `#B86A22`, missing, stale, or weak support.
- Graph blue: `#315C96`, connection paths and graph actions.
- Source gray: `#6A716C`, secondary metadata and inactive source chips.

Type:

- Display: Charter or Iowan for page-level titles and map names, used sparingly.
- Body: system sans for dense product UI.
- Data: SF Mono or equivalent for source labels, confidence, timestamps, and counts.

Layout:

- Three-pane desktop workbench: brief and coverage on the left, map in the center, evidence inspector on the right.
- Single-column mobile stack: brief, coverage summary, candidate cards, then inspector.
- Tables for comparison, cards for individual review, and optional path strips for relationships.

Signature element:

- The Evidence Rail: a persistent right-side inspector that follows the selected row and shows the exact evidence ladder, quotes, source types, freshness, and gaps. This is the visual distinction from generic recruiting search.

Self-critique:

- Avoid a broadsheet/newspaper aesthetic even though citations invite it; that would make Roster feel like a research publication rather than a recruiting workbench.
- Avoid a dark, neon command-center look; technical recruiting buyers need trust and legibility more than drama.
- Avoid large rounded social cards as the main surface; they make Roster look like another profile database.

## Copy And Naming

Use buyer-facing names:

- Talent Map, not People Search.
- Person Dossier, not Profile Card.
- Evidence ladder, not enrichment.
- Public relationship path, not social graph.
- Coverage, not diagnostics.
- Evidence strength, not confidence score when the claim is about a person.
- Review state, not hiring verdict.

Avoid:

- match score.
- hireability score.
- best candidates.
- automated ranking.
- reject.
- likely to accept.

Allowed:

- strongest evidence match.
- surfaced because.
- public evidence.
- self-stated.
- employer-stated.
- artifact-backed.
- corroborated.
- needs review.
- no evidence found.
- coverage gap.

## Compliance And Trust

The product must stay in a research-assistant posture.

Rules:

- Never imply automated hiring decisions.
- Do not recommend rejection.
- Do not rank protected-class or sensitive attributes.
- Do not infer sensitive traits.
- Keep every material recommendation inspectable through evidence.
- Make source coverage and uncertainty visible.
- Keep a human review action in the workflow before export or outreach.

If a later implementation adds outreach drafting, drafts must cite the public evidence used for personalization and avoid sensitive or speculative claims.

## Required Product Surfaces

### Talent Map home

Default landing view for the recruiting wedge.

Must include:

- search brief input.
- source and evidence controls.
- coverage preview.
- sample or live map area.
- saved maps entry point.

### Talent Map results

Main workbench for candidate discovery.

Must include:

- map table or grouped candidate list.
- coverage panel.
- evidence rail.
- filters.
- shortlist actions.
- export/share actions.

### Person Dossier

Inspector or full-page view for a selected person.

Must include:

- why surfaced.
- evidence ladder.
- evidence-backed capabilities.
- career and affiliation evidence.
- public artifacts when available.
- public relationship paths when available.
- public links.
- gaps.
- notes/review state.

### Connection Path

Later standalone and contextual relationship inspection. This surface is not required for the first Talent Map wedge.

Must include:

- from and to entities.
- path alternatives.
- evidence per hop.
- source links.
- confidence or support state per hop.
- clear empty states when the graph lacks base edges.

### Coverage

Trust and corpus transparency view.

Must include:

- source inventory.
- searchable dimensions.
- recency.
- known gaps.
- source tier distribution.

## Backend And Data Implications

This design can reuse current primitives but requires cleaner contracts.

Existing raw material:

- people enumeration by facets.
- `people_rows` result shape.
- reverse JD matching through `/match-people`.
- grounded answer/citation rendering.
- graph path endpoints behind `ROSTER_EDGE_MODEL`.
- saved searches and shortlists.
- admin coverage endpoints.

Likely API needs:

- `POST /talent-maps` to create a map from a search brief.
- `GET /talent-maps/{id}` to load a saved map.
- `GET /talent-maps/{id}/candidates`.
- `GET /person-dossiers/{entity_id}`.
- `GET /coverage/talent?brief=...`.
- `POST /talent-maps/{id}/exports`.

Later API needs:

- `GET /connection-paths?from=&to=`.

Likely data needs:

- map entity.
- candidate snapshot tied to a map.
- evidence packet per candidate reason.
- review state and notes.
- source coverage snapshot.
- optional relationship path snapshot.

Design invariant:

- The kernel stays domain-free. Recruiting vocabulary belongs in `packages/vertical_roster` and app-level product surfaces.

## Phased Redesign Plan

### Phase 1: Reframe the existing app

Goal: make the wedge visible without a backend rewrite.

Changes:

- Rename People default to Talent Map.
- Move Jobs and Apply Assistant out of primary mode prominence.
- Replace the generic placeholder with talent-map examples.
- Update candidate cards to show why surfaced, evidence chips, evidence type, evidence strength, and coverage state when available.
- Move coverage language into the result area.
- Replace broad footer copy with talent-intelligence copy.

Acceptance:

- A first-time user understands that Roster produces evidence-cited talent maps.
- Existing people search still works.
- Existing jobs/apply surfaces remain reachable.
- Mobile at <=400px remains usable.

### Phase 2: Add the Evidence Rail

Goal: make proof inspection the product signature.

Changes:

- Add a selected-candidate inspector.
- Render citations, source tiers, quotes, freshness, and gaps in one persistent rail.
- Render self-stated, employer-stated, artifact-backed, structured, corroborated, inferred, weak, stale, and gap states.
- Add a compact public relationship strip only when graph evidence exists.
- Keep citation hover behavior for prose answers.

Acceptance:

- Selecting any candidate with evidence shows why they surfaced and what supports the claim.
- Weak or missing evidence is visibly distinct.
- Self-stated claims are useful but never visually upgraded to corroborated claims.
- Mobile renders the rail as a bottom sheet or inline expandable panel.

### Phase 3: Talent Map artifacts

Goal: turn searches into durable deliverables.

Changes:

- Save map state.
- Add map-level notes and review states.
- Add CSV export.
- Add shareable private map view.
- Add PDF or brief export after CSV is stable.

Acceptance:

- A recruiter or founder can send a Talent Map to a stakeholder without explaining the product.
- Exports preserve source links and evidence strength.

### Phase 4: Relationship-path workflows

Goal: make public relationship paths and later warm paths a separate reason to buy after the evidence-first wedge works.

Changes:

- Add a standalone Connection Path mode.
- Add connection-distance filters to Talent Map.
- Add target network input during search setup.
- Add path alternatives and evidence per hop.
- Add seed-node ingestion for user/team/investor networks before claiming warm paths.

Acceptance:

- A user can ask how two people/companies connect and inspect every hop.
- A Talent Map can be filtered by reachable warm paths when seed network data exists.
- Empty and weak-path states are explicit and do not imply missing people are unconnected.

## Open Questions

1. Should the first commercial UI assume a recruiter/founder persona only, or preserve a job-seeker mode behind account settings?
2. Should a Talent Map be synchronous at first, or created as a background job with progress and notification?
3. Which export is most important for early pilots: CSV, share link, PDF brief, or ATS handoff?
4. What is the minimum evidence packet required for a candidate to appear in the strongest group?
5. Should self-stated evidence be enough for inclusion in the map, or only for lower-confidence groups?
6. Should map rank be deterministic from source/evidence rules, LLM-assisted with explanations, or both?

## Implementation Constraints

- Keep current user-facing web changes mobile-friendly.
- Avoid new LLM-spending workflows until free structural tests and UI mocks are exhausted.
- Avoid production ingest from local data; corpus expansion happens in prod-direct workflows.
- Keep kernel/vertical separation clean.
- Preserve existing Q&A, jobs, and apply workflows until the Talent Map path has replacement value.

## Success Metrics

Qualitative:

- Users describe Roster as an evidence-backed talent map, not as a chatbot.
- Users trust the surfaced candidates because they can inspect proof.
- Users discover candidates or relevant evidence they did not already have.

Product:

- search brief to useful map in one flow.
- selected candidate to evidence inspection in one click.
- map export/share completed without manual cleanup.
- mobile review flow works without horizontal breakage.

Commercial:

- five paid pilot Talent Maps within 60 days.
- at least two repeat map requests.
- customers report at least 20 percent of surfaced candidates were new or better-qualified than their existing sourcing.

## Not In This Spec

- Outreach automation.
- email sequencing.
- ATS replacement.
- applicant tracking.
- compensation benchmarking.
- consumer job-search redesign.
- broad visual rebrand beyond the product surfaces above.
