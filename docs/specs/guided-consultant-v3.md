# Guided v3 — a recruiting consultant, not a form

Status: BUILT and LIVE in prod behind `ROSTER_GUIDED_V3=1` (2026-09-06 evening; owner: "Do #1 now"), panel-reviewed (Codex; Gemini;
code-grounded audit — verdicts in §9). v2 (`/intake/step`, `apps/api/intake.py`) stays in the tree; the flag switches the Guided tab.
Delivery status at the end of §11.
Supersedes the intake mechanics of `docs/specs/guided-intake.md` §0–§8; keeps its hand-off, JD-centre, briefs, index-aware
compile, smart relaxing and merged-search sections (§12) unchanged — those are the SEARCH side and stay the one operation.

Owner verdict on v2 (2026-09-06, verbatim): "The guided mode sucks. It is asking seemingly some fixed questions where it
should adapt to every situation, and not feeling like talking to someone who is expert in candidate or job search. This
should understand context, ask clarifying questions that would make difference in outcome, build comprehensive JD if
needed, or draw intelligently from Candidate Resume profile if needed. It doesn't allow starting over. It should really
feel like a recruiting consultant."

## 0. Why v2 is a form (code-grounded audit, 2026-09-06)

- Every question is a frozen string per checklist item (`roster_vertical/intake.py:29-54`, `QUESTION_WORDS :69-81`); no
  model call authors a question in context — every model call is a reader (direction, completeness, turn) or a document
  writer (JD group/draft). The kernel header promises "a model decides HOW (the words)"; nothing does.
- Order is a fixed list (`facets/intake.py:91-117`): required items → required keys → optional keys by corpus spread; no
  ranking by what would change THIS search.
- Steering is absent: the direction is read once (`apps/api/intake.py:262-274`); the turn model's `direction` field is
  discarded (`:354-356`); no start-over / undo / revise; "skip" only as a chip; one artifact, one contract, one checklist.
- The résumé is read as a slot filler (`completeness_prompt`, ≤ 12 words per item, "never infer"); no career-arc
  reasoning; the apply profile (`GET /me/profile`) is never read.
- The JD draft is one shot from `{"your words": opening}`; it never sees the answers, the contract or the transcript.
- No server session: the whole state rides every request; "New search" wipes everything.

## 1. The principle (what stays, what changes)

Unchanged: the OUTPUT is an artifact (profile or JD) + the ONE contract; code owns gates and measurements; the model owns
meaning; nothing runs before the user ratifies. Changed: the fixed checklist is replaced by a **brief** the consultant
maintains, and every turn is a **consultant move** chosen for this situation, authored in context, gated by code.

A consultant turn (Codex): an OBSERVATION from evidence → a DECISION POINT → its CONSEQUENCE for the search → a low-effort
answer surface (2–3 concrete options + free text). "I read your résumé as senior ML infra, not general backend. The
biggest fork is staff-level platform roles vs applied-AI product teams — very different searches. Which should I
optimize for?" — never "Which field is your work in?".

## 2. The brief (replaces checklist + answers)

One structured understanding, both directions, every field = `{value, source, note}` with `source ∈ résumé | jd | profile |
stated | inferred | asked | skipped | unknown` and, for résumé/jd/profile sources, the evidence SPAN (the sentence it came
from). Inferred values are shown as inferred and can never become a hard `must` without confirmation (code rule).

Shared: direction · role / title family · domain (field) · specialties · level (current; target posture: step-up / lateral
/ switch) · place / work mode / timezone · comp (floor vs target; flexibility) · company context (stage, type, size) ·
employment type · timing · deal-breakers (job-relevant only) · evidence wanted.
Hiring adds: mission (what the person OWNS; what fails if unhired) · success in 6 months · hiring reason (backfill / new
team / urgent gap / exploratory) · reporting line & team · must-have vs trainable · calibration examples ("people like X
at Y") · seniority trade-off (strong senior vs weaker staff) · disqualifiers.
Seeker adds: search posture (active / passive / exploratory) · title flexibility · stage appetite · domains to avoid ·
commute / timezone · risk tolerance · authorization · confidentiality · résumé positioning (the story to lead with).

The brief is VISIBLE ("what I understand", labels `from résumé` · `you said` · `inferred` · `needs confirmation`) and
EDITABLE (tap a field to change it; that is a `stated` source). Numeric confidence is internal only (Codex: noise in UI).

## 3. The consultant move (one planner call per turn, structured)

The planner (vertical prompt; the persona = senior recruiting consultant) returns ONE move:

    infer      — set brief fields from evidence, with spans (no question)
    confirm    — "I'm reading X as Y — right?" (one tap)
    fork       — the highest-impact open decision, 2–3 options, each with its declared contract effect
    challenge  — vague / contradictory input ("remote AND Seattle-only?")
    trade_off  — "strong senior vs weaker staff at this comp?"
    draft      — build / revise the artifact (JD interview step or résumé positioning)
    split      — two roles → two child briefs / contracts (never one contaminated contract)
    ready      — summarize assumptions + trade-offs, propose the search
    restart    — start over (new session; old one recoverable) / direction change

Schema: `{move, say (≤ 2 sentences, says WHY), brief_delta, question?: {text, why_it_matters, options: [{label,
effect: contract_delta}], multi, free_text}, contract_delta?, ready?}`. Strictly validated; unknown fields rejected;
schema versioned; code truncates to ONE question and ≤ 2 sentences whatever comes back (Gemini: the model WILL return
four); a failed parse → a safe "tell me more" turn, never a crash. SINGLE PASS: there is no mid-turn tool loop — code
computes the LEVERAGE before the call and injects it: the pool size for the current contract, the keys whose values split
that pool (with the counts), the market centre for the role (peer postings' comp bands, work modes, level mix), and the
brief with sources. The planner's forks must pick from measured leverage; an option that would yield zero results is not
offered (code strips it). The only two-call turn is `draft` (the JD interview step needs the peer centre built first).
HYPOTHESIS-LED: the planner proposes a default with the market number and asks for variance, and consolidates intent
("hands-on IC or management track?" settles level posture, title family and work type together).

## 4. Code-owned gates (what keeps it from being "a checklist in a prompt")

0. **Taxonomy**: every brief / contract delta is normalized against the index vocabulary (schema `validate_value`); what
   the index does not know is dropped with a note, never stored as a must (the model WILL invent skills).
1. **Never ask a known thing**: a question on a field whose source ≠ unknown/inferred is dropped unless the move is
   `confirm`. Regression test: "known fact re-asked" = fail.
2. **Consequence-declared, measured**: every `fork` option carries a contract effect; code probes the options in parallel
   (slice sizes / top-rows preview, tens of ms each — the existing `slice_size` + evaluate) and asks only when the options
   differ in pool, top results, hardness (must vs prefer) or centre. A fork whose branches converge is answered by
   inference and stated, not asked. (The old 30/70 corpus-spread gate becomes one signal, not the rule.)
3. **Budget**: one fork per turn; ≤ 4 forks per intake before `ready` is offered; "search now" any time shows the
   assumptions it is making.
4. **Readiness**: direction · role family · level posture · place / mode · hard deal-breakers known or explicitly skipped.
5. **Legality**: protected attributes / proxies never enter the contract (compiler rule; the planner may explain a drop).
6. **Hardness**: inferred → prefer only; must requires a `stated` / `asked` source.
7. **Cost / latency**: ONE planner call per turn (two only for `draft`), leverage probes concurrent and cached (counts
   cache, peer clusters, profile reads); ≤ 8 calls per intake, ≤ $0.04; p50 turn ≤ 4 s.

## 5. Read everything first

Before the first question: résumé on file (text + parse), apply profile (`/me/profile` — never read by v2), saved briefs,
the user's recent searches. The opening turn states what was understood (with sources) and the ONE thing that matters
most next. A seeker with a rich résumé should reach `ready` in ≤ 2 turns; a hiring manager with a full JD in ≤ 2.

## 6. The JD interview (no JD on file)

Order of impact (Codex): mission — "what will this person own, and what fails if you don't hire them?" → what they must
have DONE before → what they can learn on the job → the level you are actually calibrated for → hard constraints (place /
mode, comp, authorization, start) → team / stage / reporting. Never first: title, years, skill lists. Peer postings
(the existing centre builder) supply the market centre for each section; the JD is presented whole (sections; source-
backed — "from you" / "from N peer postings" per section, not per line) for confirmation, then saved to the account.

## 7. The seeker flow (résumé on file)

The planner reads the arc (tenure, trajectory, domain, seniority signals) and asks the consultant's first question: step
up, lateral in a better environment, or a switch — then deal-breakers, comp floor/target, timing, authorization. Never:
"what field are you in", current salary, protected-class or proxy questions, "what are your strengths". The résumé
positioning ("lead with the platform story, not the founder year") feeds the improved résumé offer.

## 8. State and steering

The turn path is STATELESS (the client carries the brief + the compressed transcript; the raw résumé / JD is summarized
into the brief on the first turn, so the 3-page document does not ride every request). The server keeps a WRITE-BEHIND
record per intake (`roster_intake_record`, additive: id, user, brief snapshots, contract(s), artifact versions, hand-off)
for History, "start over" recovery and resume-by-id; a turn never depends on it. "Start over" = the client drops its
state and a new record begins (the old one stays in History); direction change = same record when the artifact is
reusable, else a branch; "skip X" = an explicit `skipped` source (never re-asked); two roles = an array of briefs on the
client (a record each), never one contaminated contract. Typed steering ("start over", "actually I'm hiring", "skip
location", "make it two roles") is understood by the planner (`restart` / `split` moves); a visible **Start over**
control and **Edit** on the brief exist too.

## 9. Panel verdicts (2026-09-06)

Codex — BUILD WITH CHANGES: the direction is right (sessions, editable brief, steering, résumé-aware), but the v3 brief as
first drafted "still trusts the model to act like a consultant"; consultant behaviour must be MECHANICS: evidence spans on
brief fields, consequence-declared questions probed before asking, a code-owned eligibility layer, explicit trade-offs,
separate contracts per role, required assumptions shown before search; cut numeric confidence in the UI and line-level
JD citations (section-level source backing instead); ask mission first for hiring, posture first for seekers. ALL ADOPTED
(§2–§8). Gemini — see below. Code audit — the v2 facts in §0.

Gemini — BUILD WITH CHANGES: the dynamic brief is right but the first draft "will fail on latency and UX": (1) kill the
mid-turn tool loop — code computes the pool and the highest-leverage splits BEFORE the planner call and injects them
(single pass); (2) hypothesis-led turns — propose a default with market numbers and ask for variance ("Staff ML in SF
clusters $200–250k base — does that match, or higher?"), consolidate intent ("hands-on IC or management track?" infers
the rest); (3) kill confidence scores; (4) code truncates to ONE question and ≤ 2 sentences whatever the model returns;
(5) normalize every contract delta against the taxonomy (drop what the index does not know); (6) never ask a question one
of whose answers yields zero results; (7) never grade gpt-4o-mini with gpt-4o-mini — judge on another family or with
deterministic checks; (8) keep the turn path stateless (client carries brief + compressed transcript). ADOPTED (1)–(7).

Where the two disagree — sessions — the synthesis: the TURN PATH stays stateless (the client carries the brief and the
compressed transcript, as v2 does; no TTL / stale-session bugs), and the server keeps a WRITE-BEHIND record per intake
(id, brief snapshots, contract, hand-off) for History, "start over" recovery and resume-by-id. Persistence is a record,
never a dependency of a turn. Two roles = an array of briefs on the client, one record each.

## 10. Evals (honest with one model family)

- Rubric (blind judge + hand review), per transcript: inferred the obvious from artifacts · asked only consequence-declared
  questions · explained why · first question hit the highest-impact uncertainty · challenged vague / contradictory input ·
  preserved steering (restart / skip / direction change) · handled "search now" with stated assumptions · final hand-off
  summarized assumptions and trade-offs · no illegal or proxy criteria · never re-asked a known fact.
- Deterministic checks: contract-diff vs the direct search's compile; "known fact re-asked" = fail; the 19 outcome
  scenarios kept (contract correctness, hand-off rows); adversarial personas (misleading résumé / JD; two roles; a
  direction flip mid-way).
- Frozen golden transcripts with human labels; the judge is a DIFFERENT model family from the planner (the research LLM,
  not the mini JSON model) and blind to v2 / v3; hand review of judge disagreements. Deterministic items first: hypothesis
  rate (a default proposed?), redundancy (asked what the context states?), concision (≤ 2 sentences), one question per
  turn, pool reduction per turn.
- Budgets asserted per intake (calls, $, p50 turn seconds).

## 11. Delivery

1. Spec + panel (this) → owner go.
2. Kernel: brief model + sources, move schema + validation, eligibility gates (never-ask-known, consequence probe, budget,
   hardness, readiness) — pure, opaque keys, TDD.
3. Vertical: consultant persona + move prompt, brief fields per direction, JD-interview order, seeker-arc prompt, legality
   list. App: `IntakeConsultant` service (replaces `IntakeService`), the leverage pre-computation (pool, splits, market
   centre), write-behind records (additive DDL), the planner call, hand-off unchanged.
4. Web: brief panel (labels, edit), Start over, session history, move-shaped turns; mobile pass.
5. Evals: rubric judge + goldens + budgets; the 19 scenarios re-pointed. Flag `ROSTER_GUIDED_V3`; v2 stays until the
   rubric beats it on the goldens.

### 11.1 Status (2026-09-06, end of day)

LIVE: kernel `facets/brief.py` (Brief + sources + spans; `parse_move`; `gate_fork`; `readiness` — accepts an inferred value as an
ASSUMPTION, never as a filter; `leverage`; `contract_from_brief` with the hardness rule), vertical `consultant.py` (fields, mapping,
REQUIRED per direction — posture for seekers, mission for hiring; `DOCUMENT_FIELDS` per document kind; the consultant, direction,
reader and JD-assemble prompts), app `consultant.py` (`IntakeConsultant`: single-pass turn; documents read ONCE with the EVIDENCE
GATE — a document field lives only if its span is in the text, verbatim or ≥ 70 % of its words, and only if that kind of document
can state it; leverage + market + metro tokens computed before the planner call; required-first re-plan, bounded; forks probed
against the index, dead options dropped, a fork whose options die stays as free text; remote never a metro; `search_now`
deterministic; restart ignored on turn one; write-behind `roster_intake_record`), `POST /intake/v3/step`, `/config.triage_v3`, web
(say + question + why + options + Skip; the brief panel with sources, tap to change; Start over; ready-card assumptions; JD
accept / save). Verified at 390 px. Eval `evals/intake/run_v3_eval.py` (5 personas, deterministic checks): 5 / 5 on prod, 2–8 s per
turn, ≤ 6 planner calls per intake.

Lessons from the live planner (gpt-4o-mini, DeepSeek out): it hallucinated 26 "document" facts from a five-line résumé (→ the evidence
gate); it wrote brief-field names as contract keys (→ the effect-key list in the prompt + the kernel drop); it read "I'm hiring" as a
restart (→ restart only after turn one); it asked a city for a remote role (→ remote settles the metro); it marked the user's own
words as inferred (→ the paraphrase rule + readiness accepting assumptions); it invented `san_francisco` (→ metro tokens from the
index, enforced in code).

NOT DONE: the rubric judge on another model family (§10) and frozen golden transcripts; the History list of records in the UI;
`split` (two roles) end to end; the JD interview's `jd_assemble_prompt` (the draft still uses the v1.1 centre builder with the brief
as context); resume by record id. The question WORDING still reads form-like at times ("What is your desired posture…") — the
observation → decision → consequence shape is in the prompt but not yet enforced by an eval; that is the rubric's job.

### 11.2 Where the reasoning was lacking — and the rebuild (2026-09-06, late)

Owner: "We should not patch. Guided intake is the most important part of the product. Find out where we are lacking intelligent
reasoning to discover user intent and map it to the right search with the right parameters, seeking clarification only where
needed" / "use the best model for the planner".

Diagnosis (from the live transcripts): the planner never SAW the search it would run, so it reasoned about fields — a checklist
wearing a persona (posture asked twice, then company type, comp, work mode as "confirms"); REQUIRED fields recreated the form
(posture forced even on a fully specified ask); confirms were free; the extractor-class model (deepseek-chat / gpt-4o-mini) could
not weigh impact. The decisive experiment: the same SEARCH-FIRST scaffold on the owner's own phrasing — every model but the mini
one asked exactly one high-impact question (place or mode) and none re-asked posture. The scaffold was the missing intelligence;
the model is the second lever.

Rebuilt turn: code builds the search from the brief, runs a PREVIEW (pool + top results; rows only, ≤ 12 s) and measures the
leverage BEFORE the one strategic call; the planner (gpt-5.4 via `ROSTER_PLANNER_MODEL`, `ROSTER_PLANNER_EFFORT`=low — medium
was 30–50 s a turn) reasons in order: understanding (sources) → the search it would run → a verdict on the preview → gaps ranked
by how much an answer would CHANGE the results → ready or ONE question. REQUIRED = the side (+ the mission for a JD from nothing);
one budget of 3 questions; a field never asked twice; a question is always about a field; the side alone is not an ask (the open
question opens); a stated side is switched only by a tap or a restart (shown as an assumption with a switch when read between the
lines). Result: the owner's phrasing ("jobs for mid level software engineer … java … k8 and aws") → ready in ONE turn; personas
5 / 6 → 6 / 6 after the empty-brief rule. Latency is the open problem: 6–17 s a turn with spikes to 60 s (the reasoning model
plus a cold preview); the extractor stays on the cheap model.
