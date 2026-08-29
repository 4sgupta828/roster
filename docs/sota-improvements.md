# Roster — Path to a SOTA VC-Diligence Platform: Detailed Improvement Plan

*Living document. Sections 1–6 are grounded in issues surfaced + measured during the 2026-08-27/28
work. Section 7 (Empirical Eval) is filled by the 50-question diligence stress-test + judge panel.*

---

## 0. What "SOTA" means here (the bar)

Roster's moat is **not** beating ChatGPT on freeform answers — it's **grounded, current, STRUCTURED
intelligence a VC can act on**: every claim quote-verified to a source, proven-vs-unproven cleanly
separated, coverage honestly accounted, and the answer *shaped* to the decision. A SOTA diligence
platform must, on a cold question, produce an answer a partner would trust in a memo. The gaps below are
ranked by how much they move that bar.

---

## 1. Retrieval Latency — **P0, the single biggest UX gap**

**Problem.** A simple factual question takes **~190s**; a heavy enumerative one **~260s+**. The UI hides
this behind streaming, but ~3–4 minutes to an answer is disqualifying for interactive diligence.

**Evidence.** Measured live this session: `"What did ElevenLabs raise?"` → 189s; the enumerative "tools"
question → 256s. Reverting the fan-out widening only moved a simple Q from 200s→189s — so **latency is
baseline, not fan-out.**

**Root-cause hypotheses (need profiling to confirm):**
- Per-web-result full-text fetch + embedding rerank + per-passage claim-extraction over 8–12 web legs is
  the dominant cost (each web page → chunks → embed → extract).
- The reasoned-engine scaffold + the new best-of-3 contract vote add LLM round-trips.
- Sequential stages that could overlap (retrieve → extract → compose).

**Fix (in priority order):**
1. **Profile first** — add per-stage timing to `diagnostics` (retrieve/rerank/extract/compose/derive) and
   read a real breakdown on 5 representative questions. Do NOT optimize blind.
2. Parallelize claim-extraction across web passages (likely already concurrent — verify), cap the number
   of passages extracted per answer, and short-circuit when enough grounded claims exist.
3. Cache web fetches + embeddings by URL (a hot URL re-fetched across questions is waste).
4. Consider a cheaper/faster extractor model for the first pass, escalating only on thin evidence.

**Target:** simple Q < 40s, heavy enumerative < 120s.

---

## 2. Answer Shape Reliability — **P0, mostly fixed, needs hardening + a gold gate**

**Problem.** Enumerative ("build me a table of all X") questions were muting to a thin thesis. Root cause
was **not** compose — it was the contract **classifier** conflating "wants a table" with "entities named,"
plus run-to-run **non-determinism** on mixed questions.

**Done this session:** voice ⟂ shape compose (contract renders the answer); a shape classifier that emits
enumerative even when items are discovered from evidence; a best-of-3 self-consistency vote.

**Evidence.** Muted Q2 (982 chars) → 8,190-char 10-row grounded table. Mixed "tools" question: single-call
was 3/6 enumerative → **vote made it 6/6**; decision questions stay 6/6 exploratory (no over-tabulation).

**Remaining fixes:**
- **Held-out A/B gold (Rule 16) — the real gate.** Author ~40 cases (enumerative + decision + mixed +
  survey) scored by the `lookup_scoring` cell-grid; gate any future compose change on it. *Currently the
  tech vertical has ZERO enumerative gold — the biggest eval hole.*
- **Re-derive mode from bound claims** for genuinely-ambiguous questions (the vote reduces but can't
  eliminate variance) — the panel's flagged weak link.
- **Delete the now-inert `answer_mode_routing` / `enumerative_compose_addendum` path** (superseded).

---

## 3. Enumeration Coverage & the "sample = universe" trap — **P0 for whitespace/diligence**

**Problem.** "List ALL X" / "map the whitespace" questions are only as complete as **retrieval**. If the
answer's rows come from the top-k retrieved passages, it silently presents a *sample* as the *universe* —
fatal for whitespace discovery and competitive-landscape diligence.

**Evidence.** The tools table showed 8 industries with two rows ("real estate", "sales") honestly marked
empty — good honesty, but the row-set was evidence-bounded, not population-bounded. The graph holds only
**89 companies across 10 clusters** — far short of a real market map.

**SHIPPED 2026-08-28 — the retrieval-starvation half (the Claude Code bug):**
- Root cause found via user report: "table of the main AI coding assistants" DROPPED Claude Code
  (436 corpus blocks — MORE than Copilot's 81, which was kept). NOT coverage. For an enumerative ask
  with no user-named items, `build_legs` did AXIS-ONLY retrieval ("pricing"/"model" verbatim, cap 4) —
  no per-entity search — so row membership was an accident of axis-query ranking; github-heavy flagships
  got crowded out.
- Fix (commit ad8a5c4, flag `ROSTER_ENUM_ENTITY_PROBE`, unanimous 3-panel option X): the derivation
  proposes `probe_entities` (≤8 known named instances) which SEED targeted entity×axis retrieval legs
  but are NEVER interpolated as rows — rows stay evidence-discovered, so a bad parametric guess can't
  force a row. ≥1 targeted leg per candidate guaranteed; unioned across the 3 votes; 0 extra latency.

**Remaining:**
1. **Coverage accounting as a hard invariant** — DONE for enumerative answers (the coverage line +
   naming expected-but-missing items shipped 465858c; prod-verified naming JetBrains/Tabnine/Replit).
2. **Population source for enumeration** — for "all X in sector Y," enumerate rows from the **claim graph
   population** (a SELECT over the sector), not the retrieval sample. Wire the graph as the row source for
   sector-scoped enumeration. (probe-entities is the parametric complement; the graph is the grounded one.)
3. **Scale the graph** (see §5) so the population is real — and the authentic-source ingest campaign
   (Form D funding, USPTO patents, GDELT news, deeptech sectors, YC/Wikidata) to fill the thin axes the
   new /admin coverage diagnostic surfaces (funding 1.4K, patents 174, news 288).

---

## 4. Proven-vs-Unproven & Evidence Quality — **P1, core diligence discriminator**

**Problem.** Diligence lives or dies on separating *demonstrated* from *claimed*, *deployment* from *demo*,
*filing* from *blog*. The authority-tier machinery exists but it's unclear how sharply answers use it.

**SHIPPED 2026-08-28 (commit 465858c, prod-verified) — the compose voice/shape now enforce it:**
- **Evidence hierarchy in VOICE** — answers rank on a diligence ladder (filings/benchmarks/traction >
  press > WEAK encyclopedias/blogs/forums); a weak source may never carry a hard investment claim alone.
- **Vendor-claim vs independent-proof** is now a load-bearing VOICE clause — "the company claims" vs
  "demonstrated in <benchmark>". *Prod-verified:* the Cursor answer flagged "$200M ARR … a company-reported
  figure" and "independent benchmarks … absent here"; the tools table cited "median PR throughput gain
  7.76% … far from the promised 3x" against vendor claims.
- **Stance + key diligence question** close on strategic/default answers; **coverage accounting +
  diligence takeaway + drop-dead-structure** on enumerative answers (the sample≠universe fix, §3).
  *Prod-verified:* the tools answer named JetBrains AI / Tabnine / Replit AI as expected-but-missing and
  marked the list partial.

**Remaining:**
- Surface **evidence-tier per claim** in the UI (filing/benchmark/press/blog) so a VC can weight it
  (currently the weighting is silent inside the prose).
- Held-out gold gate that scores claimed-vs-proven + coverage-honesty explicitly (the §2/§7 eval hole).

---

## 5. Graph Coverage, Densification & Freshness — **P1, the structured-intelligence moat**

**Problem.** The claim graph is the differentiator, but it's small and sparse.

**Evidence.** 89 companies / 10 clusters. Investors were 106 mentions all at corr=1 (alias-fragmented);
the LLM alias-resolver merged 12 investor surface-forms but ran once. No reverse edges (consumed_by,
investor→portfolio). No autonomous accretion.

**Fix:**
1. **Scale population** to hundreds of companies per sector (the crawler / batched accretion) so
   enumeration/whitespace questions have a real universe.
2. **Run the alias-resolver on a schedule** (investor/tech densification) so hubs form.
3. **Reverse edges** (consumed_by, investor→portfolio, person→prior-companies) — the graph, not a list.
4. **Freshness / change-detection** — a diligence graph must show *what changed* (new rounds, new
   entrants); wire the Evidence-Pulse currency layer + change events.

---

## 6. Web Sourcing — **P1, largely addressed, one metric + provider gaps**

**Done:** Exa maximized (highlights, recency, category); Brave added as breadth; **Brave-discovers →
Exa-hydrates** turns Brave's 300-char snippets into 4,000-char groundable text (no duplication);
per-answer **search-source attribution** (exa/brave retrieved/cited/novel) in the response + UI.

**Remaining:**
- **True landing RATE** — display `cited-distinct-pages / retrieved-pages`, not `cited-claims /
  retrieved-pages` (current signal reads oddly, cited>retrieved).
- **Tavily** is disabled (out of credits) — restore for a 3rd real provider.
- **DDG dead** (datacenter-IP blocked) — Brave replaced it; consider a second contents-hydration source.
- **Ablation harness** — Exa-only vs composite over a question set to quantify each engine's *marginal*
  "push to the limits" value (the aggregate half of the attribution).

---

## 7. Empirical Eval — 50 VC-Diligence Questions

### 7.0 The eval hit the platform's own latency wall (a P0 finding in itself)

The first attempt (50 questions, 4-concurrent, blocking `/research`) **failed with HTTP 502 "upstream
error"** on every call. Diagnosis:
- The 502 is **Railway's edge timeout (~300s)**, not an app error — a single blocking call measured
  **300.17s**. Research now **exceeds the edge's 5-minute request limit**.
- Two causes: (a) the **Brave per-leg hydration** (now reverted behind `ROSTER_WEB_HYDRATE`) added an Exa
  `/contents` round-trip to every web leg, pushing ~200–260s research past 300s; (b) **4 concurrent heavy
  runs** compete server-side (research completes server-side even after the client disconnects), slowing
  each call further.

**Implications (raise §1 to the clear #1 priority):**
1. The **blocking `/research` endpoint is unusable for heavy questions** — it races the 300s edge limit.
   Only the **resumable streaming** endpoint (`/research/stream`) survives it. Any programmatic/eval/API
   consumer MUST use streaming, or cap question complexity.
2. **Per-answer latency is the top SOTA blocker** — it broke the eval and degrades every interactive use.
3. **Hydration must be redesigned** to hydrate ONCE post-retrieval (one `/contents` call), not per-leg.
4. **Concurrency safety**: heavy runs don't shed load; N concurrent users degrade each other. Needs a
   concurrency cap / queue + the latency fixes.

### 7.1 Quality scores (rerun pending — via streaming or a reduced sample)

*Method: novel VC questions across whitespace / proven-vs-unproven / diligence / team / health / market,
scored 1–5 on the rubric by an external judge panel (Codex + Gemini). Blocked by 7.0 on the blocking
endpoint; rerun via streaming or a small representative sample once latency is under the edge limit.*

- Per-dimension averages: _pending rerun_
- Failure-tag histogram: _pending rerun_
- Top-5 systemic gaps (evidence + fix): _pending rerun_

---

## 8. Observability & Debt — **P2**

- **Contract-in-response** — DONE (was never a response field; the muted-answer probes read `mode: None`
  as an artifact). Diagnostics promoted to a top-level UI disclosure.
- **Per-stage timing in diagnostics** — needed for §1 (latency profiling).
- **Repeatable answer-quality eval** — turn the §7 harness into a checked-in regression eval.
- **Pre-existing `test_react::test_zero_evidence_answer_converts_to_search` failure** — fails on clean
  HEAD; triage.
- **Held-out gold gates** for the diligence route (currently thin).

---

## Prioritized Roadmap (impact-ordered)

| # | Improvement | Priority | Why it moves the SOTA bar |
|---|-------------|----------|---------------------------|
| 1 | Latency profiling + optimization | **P0** | 3–4 min answers are disqualifying for interactive diligence |
| 2 | Enumerative gold eval + coverage accounting | **P0** | Whitespace/landscape answers must be complete + honest, not samples |
| 3 | Scale graph population + population-sourced enumeration | **P0** | The structured-intelligence moat; fixes sample≠universe |
| 4 | Proven-vs-unproven as first-class + evidence-tier UI | **P1** | The core diligence discriminator vs a generic chatbot |
| 5 | Graph densification (alias schedule, reverse edges, freshness) | **P1** | Turns a list into an actionable, current graph |
| 6 | Landing-rate metric + ablation + Tavily restore | **P1** | Completes the web-contribution measurement |
| 7 | Delete dead compose paths + observability timing | **P2** | Debt; enables §1 |
