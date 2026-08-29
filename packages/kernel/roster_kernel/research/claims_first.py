"""Claims-first extraction — mine ALL gathered evidence into grounded claims, cost-optimized.

The ReAct model emits only a few claims in one terse pass, so most retrieved evidence is never used
(2 grounded from 18 atoms). This runs a dedicated extraction over ALL atoms, then two gates:
  1. VERBATIM span gate (unchanged, caller-owned) — the quote must exist in its atom.
  2. ENTAILMENT gate (here) — the quote must SUPPORT the claim, not merely contain the words
     (Rule 6: the span gate is a fabrication guard, NOT a relevance check).

COST OPTIMIZED (user priority): extraction and entailment both run on a CHEAP model (gpt-4o-mini by
default), BATCHED — all atoms in 1–2 extraction calls (lenses passed as a CHECKLIST in one prompt, not
fanned out per lens), all claims in one entailment call. This moves the heavy per-atom work OFF the
expensive Anthropic loop model. Fully fail-safe: no key / API error / nothing parseable → [].

`lenses` are DOMAIN vocabulary supplied by the vertical (kept out of this generic kernel module).
"""
from __future__ import annotations

import os

from roster_kernel.research.atoms import IDENTITY_INSTRUCTION

# Extraction is bulk + span-gate-protected → cheap gpt-4o-mini. The ENTAILMENT judge is the
# correctness safety gate (Rule 6: catches a real quote stapled to an unsupported claim), so it runs
# on the STRONGER gpt-4o — it's one batched call, so the cost bump is small. Both env-overridable.
EXTRACT_MODEL = os.environ.get("ROSTER_EXTRACT_MODEL", "gpt-4o-mini")
ENTAIL_MODEL = os.environ.get("ROSTER_ENTAIL_MODEL", "gpt-4o")
_ATOMS_PER_CALL = 10          # batch size for extraction (recall vs cost)
_ENTAIL_CHUNK = 12            # batch size for the entailment judge (factra: avoid one oversized call)
_ATOM_CAP = 1600              # DEFAULT chars/atom shown to the extractor. Full-text europepmc blocks
# are one paragraph with NO max size (Results paragraphs run 1.5-4k chars), so at 1600 the extractor
# never sees the sentence holding the effect size / CI — it silently can't become a claim. The caller
# raises this via `atom_cap` under the evidence-select flag; span+entail gates stay unchanged, so a
# bigger window only lets MORE real evidence be found — never weakens provenance.


def _extract_system(lenses: list[str], evidence_identity: bool = False) -> str:
    lens_line = ("Cover EVERY relevant aspect — " + " · ".join(lenses) + " — a separate claim each. "
                 if lenses else "")
    return (
        "You extract GROUNDED CLAIMS from retrieved evidence. You are given a QUESTION and numbered "
        "evidence atoms, each prefixed with its id like [a5]. Output STRICT JSON: "
        '{"claims":[{"text":"<one specific factual sentence>","atom_id":"a5","quote":"<verbatim span>"}]}.\n'
        "RULES:\n"
        "- Extract EVERY specific fact the atoms support — be COMPREHENSIVE. " + lens_line +
        "One claim = one specific factual sentence supported by the SINGLE atom you cite.\n"
        "- 'quote' MUST be an EXACT SUBSTRING of that atom's text: copy the characters byte-for-byte, "
        "INCLUDING every comma, period, capital, number, and unit. Never paraphrase, summarize, "
        "translate, reformat, trim trailing words, or add/remove/'fix' punctuation. If unsure where a "
        "span ends, copy a LONGER exact span. A quote that is not an exact substring is DISCARDED.\n"
        "- Cite ONLY atom ids present in the list; never invent one. Prefer specific facts (drugs, "
        "doses, outcomes, populations) over vague synthesis. Return [] only if NOTHING is relevant."
        # Evidence-identity flag (stage 1): the caller prefixed each atom with its ⟨title — source⟩
        # tag; the extractor must attribute claims to that identity. OFF → byte-identical system.
        + ("\n- " + IDENTITY_INSTRUCTION if evidence_identity else "")
    )


_ENTAIL_SYSTEM = (
    "You are a strict entailment judge. For each item you are given a CLAIM and the QUOTE it cites. "
    "Decide whether the quote DIRECTLY SUPPORTS the specific claim (not merely on the same topic). "
    'Output STRICT JSON: {"verdicts":[{"i":<index>,"ok":true|false}]}. Be conservative: if the quote '
    "does not state what the claim asserts, ok=false."
)

# Evidence Contract stage 2 (claim-congruence flag): the entailment judge extended into a BINDING
# judge. Deliberately domain-free ("document", "subject", "kind of finding" — kernel litmus): the
# vertical's kind labels arrive as DATA on each item's SOURCE line, never as prompt vocabulary.
_BIND_SYSTEM = (
    "You are a strict evidence-binding judge. For each item you are given a CLAIM, the SOURCE "
    "document the claim cites (its title, and when known the kind of finding that document "
    "provides), and the QUOTE cited from it. Return THREE verdicts per item:\n"
    '- "entailed": the quote DIRECTLY SUPPORTS the specific claim (not merely the same topic). Be '
    "conservative: if the quote does not state what the claim asserts, entailed=false.\n"
    '- "on_subject": the claim attributes its content to the subject the SOURCE document is '
    "actually about (judge from the document title and the quote). A claim that presents the "
    "document's content as being about a DIFFERENT subject, or generalizes it to a broader class, "
    "is off-subject. An item with no SOURCE line → on_subject=true.\n"
    '- "kind_ok": the KIND of assertion the claim makes matches the kind of evidence the quote and '
    "source provide — a claim asserting one type of finding backed by evidence of a different type "
    "of finding is a mismatch. Unknown or compatible kind → kind_ok=true.\n"
    'Output STRICT JSON: {"verdicts":[{"i":<index>,"entailed":true|false,"on_subject":true|false,'
    '"kind_ok":true|false}]}. Mark on_subject/kind_ok false ONLY when the mismatch is clear.'
)


async def _json(client, model, system, user):
    return await client.complete_json(model=model, system=system, user=user)


def _client(explicit=None):
    if explicit is not None:
        return explicit
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    from roster_kernel.providers.openai_llm import OpenAIJsonLLM
    return OpenAIJsonLLM()


async def extract_claims(
    *, question: str, atoms: list[tuple[str, str]], lenses: list[str] | None = None,
    client=None, model: str | None = None, atom_cap: int | None = None,
    evidence_identity: bool = False,
) -> list[dict]:
    """Batched, comprehensive extraction over `atoms` (list of (atom_id, text)). Returns candidate
    claim dicts {text, atom_id, quote}. Caller still span-verifies AND entails each. Fail-safe → [].
    `atom_cap` overrides the per-atom char window shown to the extractor (default `_ATOM_CAP`).
    `evidence_identity` (stage-1 flag) adds the attribute-to-the-source's-subject instruction — the
    caller renders each atom's ⟨title — source⟩ tag into the text itself. OFF → byte-identical."""
    import asyncio
    cap = atom_cap if atom_cap else _ATOM_CAP
    eligible = [(aid, (t or "").strip()[:cap]) for aid, t in atoms if (t or "").strip()]
    if not eligible:
        return []
    cl = _client(client)
    if cl is None:
        return []
    mdl = model or EXTRACT_MODEL
    sys = _extract_system(list(lenses or []), evidence_identity=evidence_identity)
    batches = [eligible[i:i + _ATOMS_PER_CALL] for i in range(0, len(eligible), _ATOMS_PER_CALL)]

    async def _one(batch):
        block = "\n".join(f"[{aid}] {txt}" for aid, txt in batch)
        user = f"QUESTION:\n{question}\n\nEVIDENCE ATOMS:\n{block}\n\nReturn ONLY the JSON."
        try:
            raw = await _json(cl, mdl, sys, user)
        except Exception:   # noqa: BLE001
            return []
        valid = {aid for aid, _ in batch}
        out = []
        for c in (raw.get("claims") if isinstance(raw, dict) else None) or []:
            if isinstance(c, dict) and c.get("atom_id") in valid and (c.get("quote") or "").strip():
                out.append({"text": (c.get("text") or "").strip(),
                            "atom_id": c["atom_id"], "quote": c["quote"].strip()})
        return out

    results = await asyncio.gather(*(_one(b) for b in batches), return_exceptions=True)
    claims: list[dict] = []
    for r in results:
        if not isinstance(r, Exception):
            claims += r
    return claims


async def entail_claims(
    *, claims: list[dict], client=None, model: str | None = None,
    tags: list[str] | None = None,
    congruence: bool = False, kinds: list[str] | None = None,
) -> list:
    """Batched entailment: returns a parallel list of bool (does the quote SUPPORT the claim?).
    Tri-state fail-safe: an errored batch → False for its items (drop, never launder). No key → all
    True is NOT used; caller must treat no-client as 'entailment unavailable' (see run_react).
    `tags` (stage-1 evidence-identity flag) is a list parallel to `claims` of document-identity tags
    (⟨title — source⟩); a non-empty tag adds a SOURCE line to its item. None/off → byte-identical.

    `congruence` (Evidence Contract stage 2, claim-congruence flag) switches the judge into BINDING
    mode: each item carries CLAIM / SOURCE (identity tag + the claim's structural evidence kind,
    both caller-supplied DATA — `kinds` is parallel to `claims`) / QUOTE, and the return value is a
    parallel list of {"entailed","on_subject","kind_ok"} dicts. None per item = the judge did NOT
    rule on it (chunk error / missing verdict); all-None = no client available. The CALLER owns the
    fail-safe policy for None (annotate-vs-drop) — this module never guesses and never falls back
    to a keyword heuristic (Rule 18). congruence=False → byte-identical stage-1 behavior."""
    import asyncio
    if not claims:
        return []
    cl = _client(client)
    if cl is None:
        if congruence:
            return [None] * len(claims)  # judge unavailable — the caller annotates ("unjudged")
        return [False] * len(claims)     # fail closed: without a judge, don't ship extractor claims
    mdl = model or ENTAIL_MODEL
    verdicts: list = [None] * len(claims) if congruence else [False] * len(claims)
    chunks = [(i, claims[i:i + _ENTAIL_CHUNK]) for i in range(0, len(claims), _ENTAIL_CHUNK)]

    def _item(start: int, j: int, c: dict) -> str:
        tag = (tags[start + j] if tags and start + j < len(tags) else "") or ""
        if congruence:
            # SOURCE = identity tag + the structural evidence kind riding along as DATA (the
            # vertical's label verbatim — never kernel prompt vocabulary; kernel litmus).
            kind = (kinds[start + j] if kinds and start + j < len(kinds) else "") or ""
            src_txt = (tag + (f" [kind of finding: {kind}]" if kind else "")).strip()
            src = f"\nSOURCE: {src_txt}" if src_txt else ""
        else:
            src = f"\nSOURCE: {tag}" if tag else ""
        return f"ITEM {j}\nCLAIM: {c['text']}{src}\nQUOTE: {c['quote']}"

    async def _one(start, chunk):
        items = "\n\n".join(_item(start, j, c) for j, c in enumerate(chunk))
        user = items + "\n\nReturn ONLY the JSON verdicts."
        try:
            raw = await _json(cl, mdl, _BIND_SYSTEM if congruence else _ENTAIL_SYSTEM, user)
        except Exception:   # noqa: BLE001
            return       # chunk unruled: congruence → None (caller policy); else False (fail closed)
        for v in (raw.get("verdicts") if isinstance(raw, dict) else None) or []:
            if not isinstance(v, dict):
                continue
            j = v.get("i")
            if not (isinstance(j, int) and 0 <= j < len(chunk)):
                continue
            if congruence:
                # entailed: conservative (missing → False → drop, as today). on_subject/kind_ok:
                # conservative the OTHER way (missing → True) — drop/demote only on an affirmative
                # mismatch judgment, never on the judge's silence.
                verdicts[start + j] = {
                    "entailed": v.get("entailed") is True,
                    "on_subject": v.get("on_subject") is not False,
                    "kind_ok": v.get("kind_ok") is not False,
                }
            elif v.get("ok") is True:
                verdicts[start + j] = True

    await asyncio.gather(*(_one(s, c) for s, c in chunks), return_exceptions=True)
    return verdicts
