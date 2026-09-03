# Spec: recruiter workflows for evidence-typed talent maps

Status: DRAFT for product review.
Date: 2026-09-03.
Companions:
- `docs/specs/talent-intelligence-redesign.md`
- `docs/specs/evidence-model-v2.md`

## Thesis

Roster should own the recruiter workflow before pipeline execution.

The unsolved high-value problem is not "find me more candidates." The market is already full of
large-profile databases, AI sourcing agents, outreach tools, ATS/CRM platforms, and sequencing
systems. The unresolved pain is that hard technical searches are still run from fuzzy briefs,
fragile keyword searches, unshared mental models, and candidate lists whose evidence is hard to
inspect.

Roster should become the evidence and calibration layer for high-stakes recruiting:

> Given a hard hiring brief, Roster builds a durable Talent Map that shows who appears relevant, what
> evidence supports each claim, what is self-stated versus independently supported, where the corpus
> is thin, and what the hiring team should review next.

This direction keeps the product differentiated because Roster competes on evidence quality,
explainability, coverage honesty, and decision alignment. It avoids competing head-on with incumbent
recruiting systems on database size, email automation, scheduling, or ATS workflow.

## Primary user

Primary user:
- A recruiter, founder, VC talent partner, or search researcher running a hard technical search.

Primary collaborator:
- A hiring manager, founder, or technical lead who must calibrate on what "good" means.

Primary artifact:
- A Talent Map: a saved, shareable, evidence-typed map of people relevant to a search.

Secondary artifact:
- A Person Dossier: a cited, deeper view of one person from a Talent Map.

## Product promise

Roster should promise:

> Turn a hard hiring brief into an evidence-typed Talent Map your hiring team can inspect, calibrate,
> and hand off.

Roster should not promise:
- automated hiring decisions.
- complete coverage of every professional.
- a warm-intro graph without user-supplied network data.
- a full ATS, scheduler, or outbound campaign engine.
- verified facts where the source is only self-stated.

## The workflow stack

The workflows that make most sense are the ones closest to Roster's evidence advantage.

### P0: Search intake to Talent Map

This is the core loop.

Input:
- A job description, role brief, or natural-language sourcing request.

Roster does:
- parses must-haves, nice-to-haves, target companies, excluded companies, seniority, function,
  geography, remote constraints, source preferences, and evidence requirements.
- asks for clarification only when missing information materially changes the search.
- builds a Talent Map with people rows, evidence packets, coverage summary, gaps, and review state.

Output:
- a ranked Talent Map, not a plain answer.

Why this matters:
- Recruiters waste time when the search starts from a vague JD or hiring-manager shorthand.
- Roster's first product advantage is turning that ambiguity into an inspectable search contract.

Acceptance criteria:
- The map shows the interpreted brief before or alongside results.
- The map states which constraints were hard filters and which were ranking preferences.
- Every person row includes why they surfaced.
- Every material claim has an evidence type.
- The map includes a coverage statement that distinguishes "not found" from "not in corpus yet."

### P0: Evidence review

This is the inspection loop.

Input:
- A person row selected from a Talent Map.

Roster does:
- opens an evidence portfolio for the person.
- groups claims by axis: affiliation, role, seniority, capability, location, education, public work,
  freshness, and gaps.
- labels each claim as self-stated, employer-stated, artifact-backed, structured, corroborated,
  inferred, weak, stale, or gap.
- shows source family, source link, quote or extracted support, and date when available.

Output:
- a human can decide whether the person deserves further review.

Review states:
- unreviewed.
- shortlist.
- maybe.
- needs more evidence.
- not relevant.

Design rule:
- These are human review states, not automated employment decisions. Avoid "reject", "pass/fail",
  "hire/no hire", or "score" language.

Why this matters:
- This is where Roster looks different from a profile database. The user is not asked to trust a
  match score. They inspect the support.

Acceptance criteria:
- Self-stated evidence never renders as verified.
- Corroboration requires independent evidence families.
- Lack of artifacts does not suppress a person by itself.
- Coverage gaps are visible without shaming private or low-publication candidates.

### P0: Saved and shareable Talent Maps

This is the handoff loop.

Input:
- A generated Talent Map.

Roster does:
- saves the brief, interpreted filters, row snapshots, evidence packets, coverage, notes, review
  states, and a private share token.
- supports CSV export preserving evidence fields and links.
- lets the owner add map-level notes and per-person review state.

Output:
- a durable search artifact that can be shared with a hiring manager, founder, or client.

Why this matters:
- The buyer does not only need search. They need alignment. A saved map becomes the shared object
  around which a recruiting team calibrates.

Acceptance criteria:
- A map can be reopened later and shows the same row snapshot.
- A private link can be shared read-only.
- CSV export preserves evidence strength, evidence types, source families, gaps, profile links,
  artifact counts, and review state.
- Saved maps remain broad in schema: map, entity, claim/evidence, coverage, review. Talent labels
  stay at the app or vertical layer.

### P1: Hiring-manager calibration

This is the highest-value collaboration workflow.

Input:
- A saved Talent Map shared with a hiring manager.

Roster does:
- presents the interpreted brief, top people, evidence distribution, and coverage gaps.
- lets reviewers mark example rows as "more like this", "less like this", "wrong seniority",
  "wrong domain", "too public-artifact biased", "company target is wrong", or "needs more evidence."
- turns that feedback into a revised search brief and ranking explanation.

Output:
- a calibrated search contract and a better second Talent Map.

Why this matters:
- The hardest searches fail when the recruiter and hiring manager silently disagree about what
  counts as relevant. Roster can make that disagreement visible and reusable.

Acceptance criteria:
- Feedback modifies the next map's query/ranking inputs, not historical evidence.
- Roster records why the brief changed.
- Hiring-manager feedback never changes the source evidence itself.
- The revised map can explain "what changed since the previous map."

### P1: Person Dossier

This is the deep-review workflow.

Input:
- A shortlisted or maybe person.

Roster does:
- expands the row into a structured dossier.
- summarizes career arc, current role, relevant capability evidence, public artifacts, affiliations,
  possible connection surfaces, and unanswered questions.
- cites every material claim.
- separates "what they say", "what employers or organizations say", "what artifacts show", and
  "what remains unverified."

Output:
- a compact research brief for human review or outreach prep.

Why this matters:
- A recruiter often needs a quick but defensible read on whether someone is worth a warm intro,
  hiring-manager review, or careful outreach.

Acceptance criteria:
- No uncited material claims.
- No generated confidence score as the primary artifact.
- Dossier uses the same evidence taxonomy as the Talent Map.
- Dossier includes source freshness and explicit gaps.

### P1: Evidence-based outreach prep

This is a prep workflow, not an outbound automation workflow.

Input:
- A shortlisted person and a role brief.

Roster does:
- proposes outreach angles grounded only in the evidence already shown.
- drafts short message variants that cite specific public evidence: a talk, repo, paper, post,
  company role, self-stated headline, or relevant affiliation.
- warns when there is not enough specific evidence for a personalized note.

Output:
- recruiter-ready outreach copy that can be edited and sent elsewhere.

Why this matters:
- Personalization quality is a natural extension of evidence quality.
- Roster should help the recruiter write with specificity without becoming an email sequencer.

Acceptance criteria:
- Drafts identify which evidence each personalization line came from.
- Self-stated claims can be used only as self-stated context.
- Roster does not send email in v1.
- Roster does not claim contactability unless a contact source is present.

### P1: Map refresh and watchlist

This is the persistence workflow.

Input:
- A saved Talent Map or saved search brief.

Roster does:
- reruns or incrementally refreshes evidence.
- highlights new people, changed evidence, newly corroborated claims, stale claims, and improved
  coverage.
- lets the user watch a brief without manually rebuilding it.

Output:
- a map delta, not a noisy activity feed.

Why this matters:
- Hard searches evolve over weeks. Roster can compound as an evidence monitor.

Acceptance criteria:
- A refresh explains what changed since the previous snapshot.
- New evidence is shown as evidence, not as a notification-only claim.
- Roster distinguishes changed source data from changed ranking.
- Quota/cost constraints are visible before broad refreshes.

### P2: ATS/CRM handoff

This is an integration workflow, not the core product.

Input:
- A reviewed Talent Map or shortlist.

Roster does:
- exports selected people and evidence summary to CSV first.
- later pushes selected people into Ashby, Greenhouse, Lever, Gem, Loxo, or another system when the
  user has connected it.
- includes Roster's evidence summary as notes or attachments where the destination supports it.

Output:
- a clean handoff to the recruiter's system of record.

Why this matters:
- Recruiters need to operationalize the research, but Roster should not become the system of record
  too early.

Acceptance criteria:
- Export/handoff is row-selected, not all-or-nothing.
- Evidence links and caveats survive the handoff.
- Roster remains the evidence source of truth; ATS remains the pipeline source of truth.

## Workflows to avoid for now

Avoid these until the Talent Map wedge is working and users ask for them repeatedly:

- Full ATS.
- Scheduling.
- Automated outbound sequences.
- Email sending.
- Applicant rejection or automated screening.
- Candidate scoring as the main UI.
- Generic contact-data enrichment as the lead feature.
- Social feed or network graph as the default view.
- Consumer job-search workflow as the default product.

Reason:
- These markets are crowded, operationally heavy, and less connected to Roster's evidence advantage.
- Building them early dilutes the product into a weaker incumbent clone.

## Information architecture

Primary navigation for the recruiter wedge:
- Talent Map.
- Saved Maps.
- Person Dossier.
- Evidence Types.
- Coverage.

Secondary or later:
- Jobs.
- Outreach Prep.
- Watchlist.
- ATS Export.
- Connection Path.
- Admin.

First-screen priority:
1. Search brief input.
2. Interpreted brief / constraints.
3. Build Talent Map.
4. Coverage preview or coverage caveat.

Do not lead with:
- a chat interface as the main product metaphor.
- a candidate social profile feed.
- a graph visualization.
- an ATS pipeline board.

## Data model additions

Existing broad map storage is the right foundation. Future workflow data should remain generic where
possible.

### Search brief

Purpose:
- The source of truth for what the user asked Roster to find.

Fields:
- `id`
- `tenant_id`
- `owner_id`
- `raw_text`
- `map_type`
- `vertical`
- `compiled_filters`
- `hard_constraints`
- `soft_preferences`
- `excluded_entities`
- `evidence_requirements`
- `clarifications`
- `created_at`
- `updated_at`

Notes:
- Recruiting labels live in the vertical/app. The contract should work for other Evidence Maps.

### Map revision

Purpose:
- Preserve how a map changes after feedback or refresh.

Fields:
- `map_id`
- `revision_id`
- `reason`
- `brief_snapshot`
- `filters_snapshot`
- `coverage_snapshot`
- `row_snapshot`
- `created_at`

Revision reasons:
- initial.
- hiring_manager_feedback.
- manual_filter_change.
- evidence_refresh.
- corpus_expansion.

### Review feedback

Purpose:
- Capture why a human did or did not like a row, without turning it into a model verdict.

Fields:
- `map_id`
- `entity_id`
- `review_state`
- `feedback_tags`
- `note`
- `reviewer_id`
- `created_at`

Feedback tags:
- more_like_this.
- less_like_this.
- wrong_domain.
- wrong_seniority.
- wrong_location.
- wrong_company_target.
- evidence_too_weak.
- needs_artifact_evidence.
- self_stated_is_enough.
- private_company_talent.

### Dossier

Purpose:
- Cache a deeper cited research view for one entity in one map context.

Fields:
- `map_id`
- `entity_id`
- `brief_snapshot`
- `sections`
- `evidence_refs`
- `gaps`
- `created_at`
- `updated_at`

Constraint:
- Dossier content is derived from evidence refs. If evidence changes, the dossier is marked stale
  rather than silently updated.

## Product mechanics

### Ranking

Ranking should be framed as "map order" or "relevance order", not as an employment score.

Policy:
- Relevance to the brief is the primary rank.
- Evidence depth reorders near-equals.
- Artifact count is capped so public people do not dominate purely by volume.
- Self-stated professional claims are allowed to support relevance when labeled.
- Lack of public artifacts is a coverage state, not a negative signal by itself.
- A row can rank highly with self-stated or employer-stated evidence if the brief asks for a role or
  affiliation that is naturally profile-based.

### Coverage

Coverage is a product surface.

Every map should show:
- people searched.
- active source families.
- evidence distribution.
- public-footprint distribution.
- unscanned count.
- source limitations.
- current quota or search availability when relevant.
- plain caveat: "Not found means not in the corpus yet, not nonexistent."

### Clarification

Roster should ask a clarifying question only when the answer changes the map materially.

Good clarification triggers:
- seniority absent for a senior search.
- ambiguous geography.
- unclear must-have versus nice-to-have.
- target-company strategy unclear.
- exclusion company missing when sourcing from a hiring company's domain.
- evidence requirement too narrow, such as "must have papers" for a private-company engineering role.

Bad clarification:
- asking for information Roster can infer safely.
- asking before showing any useful map.
- forcing structured forms for every search.

### Relationship paths

Connection Path remains later-stage.

Policy:
- Show only explicit public paths or user-supplied network paths.
- Do not imply warm intro paths from public co-affiliation alone.
- Represent public relationship evidence as compact path strips, not as a default graph cloud.

Relationship states:
- no path data.
- public shared artifact.
- public shared affiliation.
- possible overlap, verify.
- warm path from user's supplied network.

## UX requirements

### Talent Map row

Default row content:
- name.
- current company or stated company.
- role or function.
- location when available.
- why surfaced.
- evidence strength.
- evidence types.
- source families.
- public footprint strip.
- freshness.
- review state.

Do not use:
- opaque fit score as the lead element.
- "verified" for self-stated evidence.
- rejection language.

### Evidence portfolio

A selected row opens an evidence portfolio with:
- why surfaced.
- claim axes.
- typed evidence per claim.
- source links.
- source date or freshness.
- public artifacts.
- gaps.
- ambiguity.
- review controls.

### Hiring-manager view

The shared view should prioritize:
- what the search was.
- how Roster interpreted it.
- the top examples.
- evidence distribution.
- row feedback.
- notes.
- what to change for the next map.

The hiring-manager view should hide:
- admin controls.
- ingestion diagnostics unless they affect coverage.
- raw model traces.

## Success metrics

Product-quality metrics:
- percent of maps with at least one saved review state.
- percent of shared maps that receive hiring-manager feedback.
- time from JD paste to first useful map.
- percent of shortlisted rows with evidence inspected.
- percent of rows marked "needs more evidence."
- percent of maps refreshed or revised.

Evidence-quality metrics:
- percent of rows with typed evidence.
- percent of rows with source families visible.
- percent of rows with public footprint scanned.
- percent of rows with at least one dated artifact.
- percent of material claims with freshness.
- contradiction rate after temporal evidence is introduced.

Business metrics:
- paid Talent Maps created.
- repeat maps per account.
- maps shared per account.
- searches converted to shortlist.
- customer-reported hours saved in sourcing calibration.
- customer-reported false positives avoided.

## Implementation sequence

Phase 0: tighten the current product surface.
- Remove inherited non-roster UI copy.
- Keep Talent Map as the first tab.
- Make the interpreted brief visible.
- Make coverage and evidence distribution harder to miss.
- Keep saved maps and CSV as the durable artifact.

Phase 1: recruiter intake contract.
- Parse briefs into hard constraints and soft preferences.
- Show the interpreted contract before or with the map.
- Add minimal clarification when the search would otherwise be materially wrong.
- Store the brief contract with saved maps.

Phase 2: hiring-manager calibration.
- Add shared-map feedback tags.
- Add map revision snapshots.
- Generate "what changed" summaries between revisions.
- Use feedback to revise the next map's filters/ranking inputs.

Phase 3: person dossier.
- Generate cited dossiers from selected rows.
- Cache dossier by map context.
- Mark dossier stale when underlying evidence changes.

Phase 4: outreach prep.
- Draft evidence-grounded outreach copy.
- Show evidence refs for personalization lines.
- Keep sending outside Roster.

Phase 5: watchlist and refresh.
- Add map refresh.
- Show deltas: new people, changed evidence, improved corroboration, stale claims.
- Gate broad refreshes behind visible quota/cost estimates.

Phase 6: selected handoff.
- Improve CSV export for selected rows.
- Add manual ATS import templates.
- Later add direct push integrations only after users repeatedly ask.

## Differentiation checklist

Every new recruiter workflow must pass this checklist:

- Does it make evidence more inspectable?
- Does it improve recruiter and hiring-manager calibration?
- Does it preserve self-stated evidence as useful but labeled?
- Does it avoid automated hiring-decision language?
- Does it make coverage gaps more honest?
- Does it create or improve a durable Talent Map artifact?
- Does it keep generic contracts broad enough for non-talent Evidence Maps?
- Does it avoid becoming a weaker ATS, CRM, scheduler, or sequencer?

If a workflow fails this checklist, it belongs outside the first Roster wedge.

## Product stance

Roster's differentiated direction is:

> Research, calibrate, and hand off hard technical searches with evidence.

The product should feel like an analyst workbench for recruiting judgment. It should not feel like a
social network, a campaign manager, or an HR suite. The more the UI foregrounds evidence, source
freshness, gaps, and collaborative review, the more Roster stands apart. The more it foregrounds
generic AI sourcing, contact data, scheduling, or pipeline automation, the more it collapses into the
incumbent category.
