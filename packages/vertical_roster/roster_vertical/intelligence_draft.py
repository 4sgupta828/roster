"""INTELLIGENCE_DRAFT_PROMPT — the tech vertical's adversarial-draft directive (ROSTER_INTELLIGENCE_CORE).

Steers the kernel's `draft_intelligence` call. Domain wording lives HERE; the kernel stays domain-free
(kernel litmus). The model states a short analytical FRAME and drafts 2-3 genuinely COMPETING
hypotheses in a FLAT LINE PROTOCOL — each with a FOR-search (confirming) and an AGAINST-search
(disconfirming) query plus a falsifier. Retrieval (T2) tests each hypothesis for AND against; the
span-gate owns the facts.

The pipe LINE PROTOCOL (parsed by code, not a nested schema — the reliability lesson) is REQUIRED:
    Hn | <hypothesis as a claim> | <for-query> | <against-query> | <falsifier>
"""
from __future__ import annotations

INTELLIGENCE_DRAFT_PROMPT = """You are framing an inquiry BEFORE any retrieval. You do NOT answer the \
question — you set up how the evidence will be TESTED.

Produce two things:

(a) `frame`: 1-2 sentences naming the analytical FRAME — the key components, actors, mechanisms, or \
dimensions the question actually turns on. This is the world-model, not an answer. No facts, no verdict.

(b) `hypotheses_text`: 2-3 genuinely COMPETING hypotheses — real, mutually-exclusive candidate answers \
to the question, NOT restatements of it and NOT variations of one view. Output ONE hypothesis PER LINE, \
in EXACTLY this pipe format (five fields separated by ` | `):

    Hn | <the hypothesis stated as a claim> | <a search query to find evidence FOR it> | <a search query to find evidence AGAINST / disconfirming it> | <the observation that would prove Hn wrong>

Rules:
- The hypotheses must be genuinely COMPETING answers — if H2 is just H1 reworded, you have failed. \
Prefer the real tension in the question (e.g. "X is driven by A" vs "X is driven by B" vs "X is not \
happening at all").
- The AGAINST query must ACTIVELY SEEK DISCONFIRMING evidence for THAT hypothesis — the counter-case, \
the skeptics, the failure reports, the contradicting data — not a neutral restatement of the FOR query.
- The falsifier is the CONCRETE observation that, if found, would make you abandon that hypothesis.
- Keep each field SHORT (a phrase, not a paragraph). Name specific entities/mechanisms in the queries.
- Use a literal `|` between fields and start each line with the label H1, H2, H3. Emit nothing but the \
hypothesis lines in `hypotheses_text` — no prose, no numbering beyond the Hn label."""
