# 04 · The Medical vertical

← [03-answering](03-answering-questions.md) · [Back to README](README.md) · Next: [05-design-philosophy](05-design-philosophy.md)

The kernel is a domain-blind engine. This doc shows how the **medical vertical**
(`packages/vertical_medical/noesis_vertical_medical/`) teaches it one domain —
biomedical research — purely by supplying objects that plug into the contract
sockets from [01-architecture](01-architecture.md). Every piece of medical
knowledge lives here; none of it leaks into the kernel (`__init__.py:1-7`).

The whole vertical is one function: `build_manifest()` (`manifest.py:31-67`)
returns a `VerticalManifest` bundling connectors, prompts, gating, UI, and eval
gold. The app discovers it by entry-point name (`medical`) at startup.

---

## 1. The persona — who the agent *is*

The system prompt is the vertical's single biggest lever on behavior. The medical
persona (`persona.py:4-23`, returned by `system_prompt()`) frames the agent as
"a careful biomedical research agent working over clinical trial registrations,
drug labels, adverse-event reports, and the primary literature." Its load-bearing
rules:

- **Grounding is mandatory:** "Ground every claim in retrieved evidence; cite an
  atom and a VERBATIM quote. Never state an efficacy, dosing, or safety figure
  that is not in a cited quote." (`persona.py:6-8`)
- **Respect the evidence hierarchy:** "systematic reviews and guidelines outrank
  individual trials; a completed phase 3 trial with results outranks an
  early-phase or unfinished one." (`persona.py:9-11`)
- **Intent vs. finding:** "Distinguish what a trial was DESIGNED to test from what
  it FOUND. Registrations describe intent; results describe outcomes."
  (`persona.py:12-13`)
- **Advice questions:** for "what's safe to take" / "best treatment" it must still
  *report grounded facts* but must **not** rank a single "best/safest" option or
  give individualized recommendations — "This is research support, not medical
  advice." It only answers with no claims when *nothing* retrieved is relevant
  (`persona.py:16-22`).

This is where medicine's safety posture enters: the model is told to inform, not
prescribe, and to defer to the evidence pyramid.

---

## 2. Answer format — how the answer is *shaped*

Two directives, both threaded into the compose step (opaque strings; the kernel
never parses them):

- **`MEDICAL_ANSWER_FORMAT`** (`answer_format.py:14-57`) — markdown for a clinical
  audience. Sections appear *only when supported* by verified findings (never
  fabricate to fill a heading, `answer_format.py:8-10`): **Bottom line →
  Efficacy → Safety & adverse effects → Population → Evidence quality → Not
  addressed.** Every factual sentence carries an inline `[n]`. "Not addressed" is
  framed as an evidence *gap*, not a clinical inference. It also defines the
  optional highlight markers `[[F]]…[[/F]]` (fact), `[[R]]` (reasoning), `[[K]]`
  (key context) the UI can render.
- **`MEDICAL_CLINICAL_SYNTHESIS_FORMAT`** (`answer_format.py:69-123`) — an A/B
  variant behind `ROSTER_CLINICAL_SYNTHESIS` (default off). It uses the **same
  section set on purpose** and only *sharpens* in-section discipline: treat
  ClinicalTrials.gov entries as protocol-level design intent (not efficacy) unless
  posted results exist, preserve specific figures, don't translate surrogate
  outcomes into clinical outcomes, avoid vague words like "promising."

> **Surprise worth understanding:** the "enhanced" format deliberately adds *no
> new headings*. The comment (`answer_format.py:60-68`) records that a fixed
> template with new sections was rejected by the review panel because empty
> mandatory headings *pressure the model to fabricate* to fill them — which would
> attack the provenance gate from the prompt side. Structure that demands content
> the evidence doesn't have is an anti-pattern here.

Both are gated behind `ROSTER_STRUCTURED_ANSWERS`; when off, the kernel's plain
flat-prose compose runs, byte-identical to pre-flag (`app.py:207-216`).

---

## 3. Source-URL linking — making citations clickable

A verified claim carries a `document_id` like `"clinicaltrials:NCT00841061"`.
[`links.source_url`](../packages/vertical_medical/noesis_vertical_medical/links.py)
(`links.py:22-47`) turns that into a canonical page the user can open:

| source_key | URL built |
|------------|-----------|
| `clinicaltrials` | `https://clinicaltrials.gov/study/{NCT}` (`links.py:34-35`) |
| `openfda` / `dailymed` | `https://dailymed.nlm.nih.gov/.../drugInfo.cfm?setid={setid}` — **both** map to DailyMed (`links.py:36-37`) |
| `europepmc` | `https://europepmc.org/article/{db}/{ext}` (`links.py:38-42`) |
| `cdc` | `https://data.cdc.gov/d/{native}` (`links.py:43-44`) |
| `faers` | **`None`** — no clean per-report page (`links.py:45-46`) |
| a raw `http(s)://…` doc id (web findings) | the URL *is* the link (`links.py:28-29`) |

It also appends a `#:~:text=` **text-fragment** anchor built from the first ~120
chars of the cited quote (`links.py:14-19`), so a supporting browser opens the
page *scrolled to the exact citation*. This is the payoff of storing document ids
as `"{source}:{native_id}"` back in ingestion — the vertical parses them back
apart to reconstruct the real source URL.

---

## 4. Gating — coverage and scope

[`MedicalGatingPolicy`](../packages/vertical_medical/noesis_vertical_medical/gating.py)
implements the `GatingPolicy` protocol (`gating.py:12-34`):

- `gate_applies` — true if the question looks like an NCT id or the plan binds a
  medical dimension (`condition`/`intervention`/`drug`/`trial`/`phase`,
  `gating.py:13-16`).
- `claim_in_scope` — simply `bool(cited_hits)` (`gating.py:18-19`).
- `coverage_gap` — **currently a stub that always returns `None`**
  (`gating.py:21-34`). The docstring explains why (Rule 18): real condition-scope
  detection should come from an LLM-extracted, ontology-validated plan, not
  free-text scanning, and that's deferred. So in practice today the *real* gap
  signal is the compose `directly_addresses` honesty judgment from
  [03-answering §8](03-answering-questions.md), not this method.

Adjacent domain vocabulary:
- `scope.py` sets `SCOPE_DIMENSION = "condition"` (`scope.py:10`) — the disease is
  the primary narrowing dimension.
- `entities.py` declares `ENTITY_TYPES = ("trial","intervention","condition",
  "drug","guideline")` and the *structural* `NCT_RE` regex (`entities.py:6`,
  `:17`) — a computable format check, not a semantic heuristic.
- [`authority.py`](../packages/vertical_medical/noesis_vertical_medical/authority.py)
  encodes the **evidence pyramid** as ranks (`authority.py:11-22`): case_report(1)
  < case_series < cross_sectional < cohort < rct(5) < systematic_review /
  guideline(6). `is_controlling` = systematic review or guideline
  (`authority.py:35-36`). This backs the persona's "respect the hierarchy" rule
  with actual comparable ranks.

---

## 5. Vision — reading a clinical image (safely)

[`MEDICAL_VISION_PROMPT`](../packages/vertical_medical/noesis_vertical_medical/vision.py)
(`vision.py:9-30`) instructs the vision pre-step to describe a clinical image
(dermatology photo, wound, X-ray/CT) in order to *frame an evidence search* —
modality, morphology, distribution, density. Its hard rule: **"DESCRIBE ONLY. Do
NOT state or imply a diagnosis, disease name, differential, severity grade, or
treatment"** (`vision.py:25-26`), plus don't infer patient identity or read
identifying text.

The kernel *also* stapes on its own non-removable guardrail (`_GUARD`,
`kernel research/vision.py:27-34`) repeating "no diagnosis, no treatment" — a
kernel-level safety floor the vertical can't turn off. And recall from
[03-answering §2](03-answering-questions.md): the visual observation is context
only, kept strictly out of compose, so it can never surface as a grounded claim.
For medicine this is doubly important — an image reading is a *search hint*, never
a diagnosis.

---

## 6. Gap and suggest prompts — self-healing and discovery

- **`MEDICAL_GAP_PROMPT`** (`gaps.py:9-49`) is "the ONLY place that knows what each
  medical connector fetches and what 'high-quality evidence' means in medicine."
  It casts the LLM as a research librarian proposing ingest jobs `{query, limit}`
  per connector (clinicaltrials for trials, europepmc for findings, openfda/
  dailymed for labels, faers for a *specific drug's* safety signals, cdc for
  epidemiology, `gaps.py:20-33`) plus `recommendations` for gold sources no
  connector can fetch (Cochrane, NICE/NCCN/USPSTF, NEJM/JAMA/Lancet,
  `gaps.py:43-46`). This feeds the gap-fill queue from
  [02-ingestion §8](02-ingestion.md).
- **`MEDICAL_SUGGEST_PROMPT`** (`suggest.py:4-24`) proposes 3–4 self-contained
  follow-up questions across four angles — deeper understanding, adjacent
  discovery, safety & tradeoffs, toward action — each concrete, answerable from
  the corpus, and *not* individual medical advice.

Both are opt-in: gap behind `ROSTER_GAP_HEALING`, suggest behind
`ROSTER_CONVERSATION` (`app.py:218-219`).

---

## 7. Extraction lenses and trusted web domains

- **Extraction lenses** (`manifest.py:58-65`) are six plain strings — the aspects
  the claims-first extractor should cover per atom: interventions, outcomes/effect
  sizes, comparisons, population/eligibility, safety, mechanism/design. They're
  passed as a single checklist in one extraction call (there's no lens-processing
  code in the vertical — the kernel consumes them, `claims_first.py:34-48`). This
  is how a medical bulk-extraction knows to look for effect sizes and
  contraindications specifically.
- **`TRUSTED_WEB_DOMAINS`** (`web_domains.py:11-31`) whitelists ~60 authoritative
  domains — PubMed/PMC, ClinicalTrials.gov, FDA/CDC/NIH/WHO, NEJM/JAMA/Lancet/BMJ,
  Cochrane/NICE/NCCN, specialty societies, Mayo/Cleveland Clinic. When set, web
  search (the Exa client) is *restricted* to these, so corpus is augmented only
  with high-quality sources, never the open web (`manifest.py:57`,
  `build.py:46-60`).

---

## 8. The UI contract — declared, not coded

[`MedicalUI`](../packages/vertical_medical/noesis_vertical_medical/ui.py)
(`ui.py:16-88`) implements the `UIContract` so the generic app shell renders a
medical UI with zero app edits. It declares:

- `console()` — heading "What does the evidence show?" + 16 real example prompts
  (lecanemab, tirzepatide, CAR-T myeloma, SGLT2 inhibitors, …, `ui.py:26-52`).
- `navigation()` — "Trials" and "Research" (`ui.py:54-58`).
- `search_facets()` — condition, phase, status, intervention controls
  (`ui.py:60-66`).
- `entity_views()` — the trial list/detail schema (columns NCT/Condition/Phase/
  Status, `ui.py:68-81`).
- `citation_renderers()` — `{"block_span":"trial-quote","url":"web-link"}`
  (`ui.py:83-84`).

All of this is *data*, echoed to the frontend via `/config` (`app.py:344-371`).
The app never contains the word "trial" — it just renders whatever the active
vertical declares.

---

## 9. Why medical evidence needs *this much* grounding

Everything above is stricter than a general chatbot would be, on purpose. In a
medical context, a fabricated dose, an invented efficacy number, or a
hallucinated contraindication isn't an embarrassing error — it's a safety hazard.
The vertical's design answers that risk at every layer:

1. **The persona forbids ungrounded figures** and forbids ranking a "best" option
   or giving individualized advice (`persona.py:6-8`, `16-22`).
2. **The provenance gate makes ungrounded figures impossible** to emit —
   [03-answering §4](03-answering-questions.md).
3. **The evidence hierarchy** (`authority.py`) means a case report can't
   masquerade as guideline-strength evidence.
4. **The answer format separates trial *intent* from trial *findings*** and marks
   unanswered parts as gaps, not inferences (`answer_format.py`).
5. **Vision describes, never diagnoses** — with a kernel guardrail the vertical
   can't disable (`vision.py:25-26`, kernel `vision.py:27-34`).
6. **Web augmentation is whitelisted** to authoritative sources
   (`web_domains.py`).
7. **The honesty signal** makes a tangential answer confess it's tangential
   (`react.py:561-564`).

The through-line: the system is built so that the *worst* it can do is say "I
don't have direct evidence for that" — never confidently invent a clinical fact.

---

## Surprises worth flagging

- **`coverage_gap` is a no-op stub** (`gating.py:21-34`) — real gaps come from the
  compose honesty signal today.
- **RxNorm exists but is dormant** — a drug-name→RxCUI cross-linking utility
  (`rxnorm.py`) that is *not* wired into ingest facets (`coverage.py:17`).
- **openFDA and DailyMed cite the same DailyMed page** (`links.py:36-37`); FAERS
  findings get no link at all (`links.py:45-46`).
- **The offline fixture corpus is just 2 trials** (`source.py:25-45`,
  `fixtures.py`) — enough for the held-out eval (`eval_gold.py`: one factual case,
  one "should refuse" case), not for real answers. Real deployments run the
  Postgres corpus fed by the connectors.

Next: [the design philosophy behind all of this →](05-design-philosophy.md)
