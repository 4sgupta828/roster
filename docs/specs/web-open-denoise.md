# ROSTER_WEB_OPEN_DENOISE — open Exa to the full web + multi-gate denoising funnel

## Contract (Rule 1)

**Behavior:** When `ROSTER_WEB_OPEN_DENOISE` is ON, the aux web (Exa) leg searches the FULL
open web (whitelist dropped as a hard filter) for **every** web-eligible question, and its hits
pass through a denoising funnel before entering the atom pool: cheap structural pre-filter →
embedding cosine relevance floor → authority BOOST (whitelisted venues ranked up, not others
filtered out) → **LLM semantic screen** (relevance + subject-binding + reputation-as-meaning) →
the unchanged span-gate. On a screen error / exhausted budget (can't-judge), the leg **fails safe
to the authoritative subset** (hits carrying a venue authority facet) — i.e. today's behavior —
never to an empty leg. OFF → byte-identical (whitelisted Exa + DDG exactly as today).

**Why:** boxing Exa to a 38-domain whitelist throws away its neural-search value and blinds it to
any subject whose primary sources aren't whitelisted (VC firms' own sites, a startup's own site).
The whitelist was a blunt stand-in for denoising; the correct design opens the web and denoises
downstream, ranking authority up instead of filtering everything else out. Panel-designed (Codex +
Gemini + code-grounded subagent); generalizes the prod-proven `ROSTER_WEB_ENTITY_OPEN` screen from
single-entity to all questions.

**Invariants that MUST hold:**
- Provenance span-gate unchanged; every emitted sentence still needs a verbatim quote.
- Rule 18: structural signals (length, dedup, cosine, facet-presence) are CHEAP PRE-FILTERS only;
  the semantic keep/drop (relevance/quality/binding) is LLM-owned and FAILS SAFE (never a heuristic
  quality guess). The authoritative-subset fallback is structural (facet presence), not a quality guess.
- Rule 20: `ROSTER_WEB_OPEN_DENOISE` default OFF; OFF path byte-identical (leg set, `_rerank`, and
  request payloads all unchanged when off). Keep both code paths.
- Credit discipline: bounded to ~1 batched screen LLM call per web step (no per-result fan-out),
  capped per run; no extra Exa calls (the single aux leg is opened, not duplicated).
- Corpus-first: the curated corpus stays the primary leg; web augments.

**Success criterion (dark-launch, shadow-diff):** with the flag ON, the "top VCs in tech" landscape
question surfaces multiple firms' OWN sites (sequoiacap.com/foundersfund.com/…) and the Blazel
question keeps citing blazel.com — while the historical junk (icreat.ai/swfte.com class) does NOT
reach the compose cap. OFF path byte-identical (asserted by test). The diagnostics
`{raw, structural_kept, embedding_kept, llm_kept}` per step let us confirm breadth-up-without-junk
before flipping ON in prod.

## Design (panel-converged)

Reuse the entity-open machinery. The single aux web leg is OPENED (not duplicated); ALL its open-web
hits are screened; authority becomes a ranking boost. The whitelist stays as `WEB_DOMAIN_FACETS`
authority stamps (keyed on URL host, independent of `includeDomains`) — demoted from hard filter to
boost, which is nearly free.

| Gate | Kind | Role | Seam |
|---|---|---|---|
| 0. open the aux web leg (`web_open=True`, drop `includeDomains`) | plumbing | full-web reach | react.py base_req + exa_web (open_web already drops includeDomains) |
| 1. structural (dedup/near-dup already exist; add length floor + per-page cap already exists) | computable | thin the herd | web.py `_dedup`/chunking |
| 2. cosine relevance floor (reuse sims already computed) | computable | cut off-topic (NOT junk) | web.py `_rerank` |
| 3. authority boost via `WEB_DOMAIN_FACETS` | computable | authoritative pages lead into the atom pool | web.py `_rerank` |
| 4. **LLM screen (relevance + subject-binding + reputation-as-meaning)**, fail-safe → authoritative subset | **LLM-owned** | **decisive denoiser** | react.py + web_quality.py |
| 5. span-gate + claim congruence | unchanged | provenance | react.py (untouched) |

## Tasks (execute in order; review between)

### T1 — `web_quality.py`: distinguish can't-judge from judged-all-drop (fail-safe contract)

`packages/kernel/roster_kernel/research/web_quality.py`:
- Change `screen_open_web_hits` to return **`None` when it could not judge** (no hits is still `[]`;
  but `llm is None` / blank prompt / budget exhausted / ANY exception → `None`), and a `list` (possibly
  empty) when it DID judge. This lets the caller apply the authoritative-subset fallback on can't-judge
  while respecting a legitimate all-drop. Keep the existing behavior otherwise (one batched call,
  budget.charge, index filtering). Update the docstring.
- Update the EXISTING caller in `react.py` (the `web:entity_open` branch) to the new contract: on
  `None`, apply the authoritative-subset fallback (see T3's helper); on a list, use it as today.
- Update `test_web_quality.py`: can't-judge cases now assert `None`; judged-all-drop asserts `[]`;
  happy path asserts the kept list. Keep every existing fail-safe assertion, adjusted to the new sentinel.

### T2 — `web.py`: flag-gated structural + cosine floor + authority boost (OFF byte-identical)

`packages/kernel/roster_kernel/contract/dto.py`: add `web_denoise: bool = False` to `RetrievalRequest`
(next to `web_open`), default False → OFF byte-identical.

`packages/kernel/roster_kernel/retrieval/web.py`:
- In `search`/`_rerank`, GATE all new logic behind `getattr(req, "web_denoise", False)`. When OFF,
  `_rerank` and the candidate assembly must be **byte-identical** to today.
- Gate 2 (cosine floor): the sims are already computed in `_rerank`. When `web_denoise`, drop chunks
  whose `sim < _DENOISE_SIM_FLOOR` (a lenient module constant, e.g. 0.18) BUT always keep ≥1 chunk so
  a leg never empties from the floor alone.
- Gate 3 (authority boost): when `web_denoise`, add a large rank bonus to chunks whose host has a venue
  authority facet (reuse `self._authority(url) > 0`, which checks `WEB_DOMAIN_FACETS` pub_type) so
  authoritative pages sort to the top of the 3×k→k cut and survive into the atom pool. Strip the bonus
  before returning; don't corrupt `score` semantics for downstream (mirror how `_rerank` returns today).
- Gate 1 (structural length floor): when `web_denoise`, drop chunks below a small char floor
  (`_DENOISE_MIN_CHARS`, e.g. 80) as boilerplate — but never drop provider highlights.
- Tests: `test_web.py` (or a new `test_web_denoise.py`) — OFF path identical ordering to today; ON path
  drops a sub-floor/off-topic chunk and bubbles an authoritative-domain chunk above a higher-cosine
  unknown-domain chunk. Use fake hits with controlled facets/text.

### T3 — `react.py`: open the leg + screen ALL open-web hits + authoritative-subset fail-safe

`packages/kernel/roster_kernel/research/react.py`:
- Add `run_react` param `web_open_denoise: bool = False` (near `entity_open_web`).
- When `web_open_denoise`: set the aux web leg's request to open + denoise — build `base_req` (or a web-
  specific copy) with `web_open=True, web_denoise=True`. (Corpus ignores both, as today.) Do NOT fire the
  separate `web:entity_open` additive leg when `web_open_denoise` is on (the base leg is already open —
  gate `_entity_open` with `and not web_open_denoise` to avoid a redundant 2nd Exa call).
- Add a small domain-free helper `_authoritative_subset(hits)` = `[h for h in hits if (h.facets or {}).get("source_kind")]`
  (a venue authority stamp ⇒ whitelisted/known source). This is the fail-safe fallback (structural, Rule-18-safe).
- In the hits-merge loop: for the `web` leg (when `web_open_denoise`) AND the `web:entity_open` leg,
  run the screen; apply the new contract: `screened = await screen_open_web_hits(r, question=question,
  llm=llm, prompt=web_quality_prompt, budget=budget)`; `r = _authoritative_subset(r) if screened is None
  else screened`. Emit `{"type":"retrieving","source":<leg>":kept","hits":len(r)}` and record diag
  `{raw, kept}` (extend to `{raw, structural_kept, embedding_kept, llm_kept}` if cheap; raw+kept is the floor).
- OFF path: `web_open_denoise=False` → base leg stays whitelisted, no `web` screening, leg set + payloads
  identical to today (entity-open path unchanged).

### T4 — wiring: service + app flag

`packages/kernel/roster_kernel/runtime/research.py`: add `ResearchService.web_open_denoise: bool = False`;
pass `web_open_denoise=self.web_open_denoise` into `run_react` (next to `entity_open_web`).
`apps/api/app.py`: add `open_web_denoise_enabled()` (reads `ROSTER_WEB_OPEN_DENOISE`); pass
`web_open_denoise=open_web_denoise_enabled()` in `build_default_service` (next to `entity_open_web=`).
Tests mirror `test_entity_open_web.py`: flag ON→service True; unset→False.

### T5 — verification

- All new + existing kernel research/contract + api flag tests green; OFF byte-identical (leg-set +
  `_rerank` identity tests). Confirm the 2 known pre-existing failures are still the only failures.
- Dark-launch (flag OFF in prod initially): after deploy, run the VC + Blazel questions with the flag
  toggled via a request path or a temporary ON to capture the `{raw,kept}` diag; confirm breadth-up
  (firm own-sites surface) and no junk in the compose cap. Only flip the prod env ON after shadow-diff.
  Report exact input/observed (Rule 3).

## Non-goals / deferred
- Per-mode screen prompts (specific_entity/landscape/general) — Codex suggested; defer unless the single
  general screen underperforms in shadow-diff.
- Raising `max_results` — defer; the funnel quality matters before quantity.
