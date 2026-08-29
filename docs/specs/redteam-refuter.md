# Red-team refuter + cross-family judge activation (intelligence-core hardening)

## Why
Intelligence-core's adversarial retrieval uses the DRAFTING model's own self-authored `against_query` —
so it grades its own homework (soft/blind disconfirmation). And the cross-family GROUNDING GATE is inert
in prod (no `derive_judge_llm`). Both need a DIFFERENT-family model. Build the OpenAI adapter once →
activates the grounding gate AND enables a genuine red-team refuter (a separate, uncorrelated mind
authoring the disconfirming searches). Plus: flag a hypothesis UNDER-TESTED if its against-search finds
nothing (disconfirmation attempted ≠ found).

## T-A — OpenAI LLMClient adapter + wire as the cross-family judge
- NEW `packages/kernel/roster_kernel/providers/openai_client.py`: `class OpenAILLMClient` implementing the
  `LLMClient` protocol (`packages/kernel/roster_kernel/providers/llm.py`): `async def complete(*, system,
  messages, response_format: type[BaseModel], max_tokens=2048, temperature=None) -> LLMResult`. Use
  `AsyncOpenAI` (mirror `openai_llm.py::OpenAIJsonLLM` construction + lazy import + OPENAI_API_KEY). Get
  structured output by passing OpenAI's structured-output — prefer `response_format` json_schema built
  from `response_format.model_json_schema()` (OpenAI structured outputs), or a forced tool-call, whichever
  is reliable; parse the returned JSON into `response_format.model_validate(...)`; return
  `LLMResult(parsed=..., output_tokens=usage.completion_tokens, model=...)`. Model from an env
  (`ROSTER_JUDGE_MODEL`, default a current OpenAI model, e.g. "gpt-4o" — match what openai_llm uses/expects).
  Fail cleanly (raise on API/parse error — the callers already fail-safe on judge errors).
- `apps/api/app.py::build_default_service`: construct `OpenAILLMClient()` when `OPENAI_API_KEY` is set and
  pass it as `derive_judge_llm=` into `ResearchService(...)`. If no key → `derive_judge_llm=None` (today's
  behavior; the grounding gate + refuter fail-closed). This ACTIVATES the cross-family grounding gate and
  makes derive's validity judge cross-family. Gate the wiring behind a flag `ROSTER_CROSS_FAMILY_JUDGE`
  (default OFF) so it's opt-in / rollback-able (Rule 20) — OFF → derive_judge_llm stays None, byte-identical.
- Tests: adapter builds an OpenAI structured call from a pydantic response_format and parses the result
  (mock the AsyncOpenAI client — do NOT hit the network); flag ON with a key → service.derive_judge_llm is
  an OpenAILLMClient; OFF or no key → None.

## T-B — red-team refuter (uncorrelated disconfirming queries) + under-tested flag
- NEW `packages/kernel/roster_kernel/research/refuter.py`: `async def refute_hypothesis(claim, judge_llm, *,
  budget, n=2) -> list[str]` — a DIFFERENT-family call whose ONLY job is to find evidence that REFUTES the
  claim: "You are a skeptic. Give up to N search queries that would surface the STRONGEST evidence AGAINST
  this claim (disconfirming data, counterexamples, failures, contrary findings)." Closed pydantic shape
  `{queries: list[str]}`. Fail-CLOSED: `judge_llm is None` / error / empty → return `[]` (caller falls back
  to the hypothesis's self-authored `against_query`). Charge budget.
- `react.py` adversarial retrieval (the T2 for/against block, `hypotheses is not None`): for the AGAINST
  leg, when a cross-family `judge_llm` (the new `derive_judge_llm`) is available, call `refute_hypothesis`
  to get the disconfirming queries and run THOSE (up to n=2) instead of the self-authored `against_query`;
  if the refuter returns [] (no judge / error) → fall back to `hyp.against_query` (today's behavior).
  So: a genuine red-team authors disconfirmation when a cross-family model exists; else self-authored.
- UNDER-TESTED flag: track per hypothesis whether the against-search returned ANY hits. If a hypothesis's
  disconfirming search found nothing, mark it under-tested — surface in the compose addendum / cruxes as
  "H_n: not yet disconfirmed by evidence (treat with caution)", and record diag. A hypothesis with no
  disconfirming evidence found is UNDER-TESTED, not confirmed.
- Gate everything on `hypotheses is not None` (intelligence mode) + a cross-family judge for the refuter;
  fail-safe to self-authored against_query. OFF/non-intelligence byte-identical.
- Tests: refuter fail-closed (None/error → []); the against-leg uses refuter queries when a judge exists,
  self-authored when not; an empty against-result marks the hypothesis under-tested; OFF byte-identical.

## Constraints
Rule 18 (the refuter/judge own the semantic call; code owns mechanics + fail-safe). Rule 20 (flags default
OFF, byte-identical OFF). Fail-CLOSED (no cross-family model → today's self-authored behavior, never worse).
Span-gate + grounding untouched. Credit discipline (refuter = +1 call per hypothesis only when intelligence
mode + a judge exists; bounded by the hypothesis cap of 3).
