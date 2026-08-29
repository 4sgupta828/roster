"""Typed grounded-claim EXTRACTOR CORE (vertical-owned, source-agnostic, GROUNDED).

Turns ONE block that is already known to be about a KNOWN subject entity into a list
of VERIFIED typed claims constrained to a set of ACTIVE predicates. This is Task 2a of
the claim-graph build — the extractor CORE only: NO DB, NO corpus access, NO network of
its own, NO entity-id resolution (`object_entity_name` is emitted as a raw name; resolving
it to an `entity_id` is Task 2b).

Division of labor (Rule 18 — the LLM owns meaning, code owns the gates):
  * The LLM owns the SEMANTIC call: which predicate a span asserts, and what the object is.
    It MUST select a predicate from the provided `predicates` list or abstain — it can never
    coin a new predicate. There is deliberately NO regex/keyword predicate classifier and NO
    keyword relevance pre-filter here; that would be a heuristic for meaning.
  * CODE owns the three GATES, each fail-CLOSED (a claim that fails any gate is DROPPED, never
    laundered through):
      1. predicate-in-active-set  — the emitted predicate must be an active predicate name, and
         the object_kind is taken from the registry (never trusted from the model).
      2. span-verify              — normalize_quote(quote) must be a substring of
         normalize_quote(block_text), using the SAME normalization as the claim-graph store's
         span gate, so a quote that passes here also passes the kernel span gate later.
      3. entail                   — the quote must SUPPORT "<subject> <predicate>: <object>",
         judged by the REUSED batched `entail_claims`. A False/None verdict drops the claim
         (an unjudged claim is dropped, never kept).

Grounding is the moat: a claim with no verbatim supporting quote is DROPPED, never emitted.
Fully fail-safe → [] (no client, malformed model output, or an errored gate never raises).

The prompt is rendered from the passed `predicates` list (their names / object_kind /
description) — NOT hardcoded — so widening to slice-2/3 predicates needs no code change.
"""
from __future__ import annotations

# Reuse the claims-first plumbing: the client resolver, the JSON call helper, the extract
# model default, and the batched entailment judge. `entail_claims` is imported as a module
# attribute so tests can monkeypatch `claim_extract.entail_claims` deterministically/offline.
from roster_kernel.research.claims_first import (
    EXTRACT_MODEL,
    _client,
    _json,
    entail_claims,
)

# The span gate MUST use the byte-identical normalization the store's span gate uses so a
# quote that passes here passes there too. Both trace to the kernel's canonical `normalize`
# (the store's `normalize_quote` mirrors it) — import from the KERNEL, not the app, so the
# vertical does not depend on app-level code.
from roster_kernel.research.provenance import normalize as normalize_quote


def _active(predicates: list[dict]) -> dict[str, dict]:
    """Registry of the ACTIVE predicates, keyed by name. A predicate with no explicit
    `status` is treated as active (callers pass an already-active list)."""
    out: dict[str, dict] = {}
    for p in predicates or []:
        name = (p or {}).get("name")
        if name and p.get("status", "active") == "active":
            out[name] = p
    return out


def _extract_system(active: dict[str, dict], subject_name: str) -> str:
    """Render the extraction system prompt from the ACTIVE predicate registry (no predicate
    name is hardcoded — the vocabulary is data)."""
    lines = []
    for name, p in active.items():
        kind = p.get("object_kind", "value")
        obj_hint = ("another company/entity NAME" if kind == "entity"
                    else "a value NAME/PHRASE as stated")
        lines.append(f'  - "{name}" (object = {obj_hint}): {p.get("description", "").strip()}')
    catalog = "\n".join(lines)
    return (
        "You extract GROUNDED, TYPED CLAIMS about ONE known subject from ONE block of text.\n"
        f"The SUBJECT of every claim is exactly: {subject_name}\n\n"
        "You may ONLY use these predicates (choose the one that fits; you may NOT invent a new "
        "predicate):\n" + catalog + "\n\n"
        "For each fact the block states ABOUT THE SUBJECT that fits one of these predicates, emit "
        "one claim. Output STRICT JSON:\n"
        '{"claims":[{"predicate":"<one of the predicate names above>",'
        '"object":"<the object: an entity name for entity predicates, else the value>",'
        '"quote":"<a VERBATIM contiguous substring of the block that states this>"}]}\n'
        "RULES:\n"
        "- 'predicate' MUST be one of the names listed above, copied exactly. Never invent one.\n"
        "- 'quote' MUST be copied EXACTLY, character-for-character, as a contiguous substring of "
        "the block. Never paraphrase, summarize, translate, or reformat.\n"
        "- The quote must actually SUPPORT the claim about the subject — not merely mention the "
        "words. If the block states nothing that fits an allowed predicate, return "
        '{"claims":[]}. Do NOT invent facts.'
    )


def _claim_text(subject_name: str, predicate: str, obj: str) -> str:
    """Readable claim rendering fed to the entailment judge."""
    return f"{subject_name} — {predicate}: {obj}"


async def extract_typed_claims(
    *,
    block_text: str,
    subject_name: str,
    predicates: list[dict],
    client=None,
    model: str | None = None,
) -> list[dict]:
    """From ONE block about `subject_name`, extract typed claims constrained to the ACTIVE
    `predicates`. Returns a list of dicts:
        {predicate, object_kind, object_value, object_entity_name, quote}
    Every returned claim is already:
        (1) predicate ∈ the active set, object_kind taken from the registry;
        (2) span-verified: normalize_quote(quote) is a substring of normalize_quote(block_text);
        (3) entailed: the quote SUPPORTS "<subject> — <predicate>: <object>" (via entail_claims).
    Fail-safe → [] (no client, malformed output, or any gate failure drops the claim — never
    laundered). Grounding is the moat: a claim with no verbatim supporting quote is DROPPED.

    Back-compat wrapper over `extract_typed_claims_counted` — the return contract is
    UNCHANGED (list only). A caller that needs the entail-call count for cost accounting
    uses `extract_typed_claims_counted` instead."""
    out, _n_entail = await extract_typed_claims_counted(
        block_text=block_text, subject_name=subject_name,
        predicates=predicates, client=client, model=model)
    return out


async def extract_typed_claims_counted(
    *,
    block_text: str,
    subject_name: str,
    predicates: list[dict],
    client=None,
    model: str | None = None,
) -> tuple[list[dict], int]:
    """Same as `extract_typed_claims` but ALSO returns the number of entailment-judge
    calls made: `(claims, n_entail_calls)`. This extractor batches ALL of a block's span-
    verified survivors into a SINGLE `entail_claims` call, so `n_entail_calls` is 0 (no
    survivor reached the judge) or 1 (the batched judge call was made, even if it then
    raised or dropped every claim). Cost accounting (the extraction job's ledger) needs
    this because the entail call is a real LLM spend the outer extract-call count misses."""
    block_text = block_text or ""
    subject_name = (subject_name or "").strip()
    active = _active(predicates)
    if not block_text.strip() or not subject_name or not active:
        return [], 0

    cl = _client(client)
    if cl is None:                       # no injected client and no key → fail-safe
        return [], 0

    mdl = model or EXTRACT_MODEL
    system = _extract_system(active, subject_name)
    user = (
        f"SUBJECT: {subject_name}\n\nBLOCK:\n{block_text.strip()}\n\nReturn ONLY the JSON."
    )
    try:
        raw = await _json(cl, mdl, system, user)
    except Exception:                    # noqa: BLE001 — malformed / API error → fail-safe
        return [], 0
    items = (raw.get("claims") if isinstance(raw, dict) else None) or []

    # --- Gates 1 & 2 (structural + span), code-owned, fail-closed --------------------------- #
    norm_block = normalize_quote(block_text)
    survivors: list[dict] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        predicate = (c.get("predicate") or "").strip()
        obj = (c.get("object") or "").strip()
        quote = (c.get("quote") or "").strip()
        if predicate not in active:      # gate 1: predicate must be an ACTIVE predicate name
            continue
        if not obj or not quote:         # empty object / quote → nothing groundable
            continue
        # object_kind is taken from the REGISTRY, never trusted from the model. 'either' → value.
        declared = active[predicate].get("object_kind", "value")
        object_kind = "entity" if declared == "entity" else "value"
        if normalize_quote(quote) not in norm_block:   # gate 2: verbatim span-verify
            continue
        survivors.append({
            "predicate": predicate,
            "object_kind": object_kind,
            "object": obj,
            "quote": quote,
        })

    if not survivors:
        return [], 0

    # --- Gate 3 (entail), REUSED batched judge, fail-closed (False/None → drop) -------------- #
    to_judge = [
        {"text": _claim_text(subject_name, s["predicate"], s["object"]), "quote": s["quote"]}
        for s in survivors
    ]
    n_entail = 1                         # the batched judge call is made (spent) now
    try:
        verdicts = await entail_claims(claims=to_judge, client=cl)
    except Exception:                    # noqa: BLE001 — judge error → drop all (never launder)
        return [], n_entail

    out: list[dict] = []
    for s, v in zip(survivors, verdicts):
        if v is not True:                # fail-closed: only an affirmative True survives
            continue
        obj = s["object"]
        is_entity = s["object_kind"] == "entity"
        out.append({
            "predicate": s["predicate"],
            "object_kind": s["object_kind"],
            "object_value": "" if is_entity else obj,
            "object_entity_name": obj if is_entity else "",
            "quote": s["quote"],
        })
    return out, n_entail
