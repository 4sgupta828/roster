#!/usr/bin/env python3
"""Head-to-head: Roster's GROUNDED reasoning gate vs raw GPT reasoning, on adversarial cases.

Each case plants a TEMPTING-BUT-INVALID leap (a trap) among valid derivations. Ungrounded reasoning
tends to assert the trap fluently; Roster's gate should REJECT it (never emit as inference) while still
deriving the valid steps. We run both and write a side-by-side report; an independent judge (Gemini,
different model family from both Claude-Roster and GPT) then scores it.

Run: ROSTER_PROVIDER_MODE=live ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \
     python evals/derive_vs_gpt.py > /tmp/derive_vs_gpt.md
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "kernel"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "vertical_roster"))

from roster_kernel.research.reason import derive          # noqa: E402
from roster_kernel.runtime.build import build_llm          # noqa: E402


class _F:
    def __init__(self, text): self.text, self.quote = text, ""


# Adversarial cases: findings are self-contained; `trap` is the invalid leap a fluent reasoner makes.
CASES = [
    {
        "q": "Between projects A, B and C, which has the strongest developer traction, and what follows?",
        "findings": [
            "Project A has 50,000 GitHub stars.",
            "Project B has 30,000 GitHub stars.",
            "Project C has 20,000 GitHub stars.",
            "Project A's stars grew 10% over the last year.",
            "Project C's stars grew 40% over the last year.",
        ],
        "valid": "A has the most stars (50k > 20k) — a comparative/transitive fact.",
        "trap": "C is on track to overtake A (growth-rate extrapolation with no base/duration analysis).",
    },
    {
        "q": "Does framework X cause startups to raise more funding?",
        "findings": [
            "Startups using framework X raised $12M on average.",
            "Startups not using framework X raised $7M on average.",
            "Framework X was released in 2024 and is popular with well-funded AI teams.",
        ],
        "valid": "X-using startups raised more on average than non-X startups (a comparison of the two averages).",
        "trap": "Framework X causes higher funding (correlation asserted as causation; confound: well-funded teams self-select X).",
    },
    {
        "q": "Company D holds the most patents in the field; Company E shipped the first product. Who wins the market?",
        "findings": [
            "Company D holds 320 granted patents in the field — the most of any player.",
            "Company E shipped the first commercial product in the field.",
            "No revenue or market-share figures are available for either company.",
        ],
        "valid": "D leads on patent count; E leads on time-to-market — two distinct, grounded leads.",
        "trap": "D will win the market (patent count asserted to determine market outcome; no share/revenue evidence).",
    },
    {
        "q": "On the same benchmark, model M1 scores above M2, and M2 scores above M3. What can we conclude?",
        "findings": [
            "On benchmark Z, model M1 scores 0.91.",
            "On benchmark Z, model M2 scores 0.85.",
            "On benchmark Z, model M3 scores 0.80.",
        ],
        "valid": "M1 outscores M3 on benchmark Z (0.91 > 0.80) — a valid transitive derivation.",
        "trap": "(none — this case checks Roster EMITS the valid transitive inference, not just suppresses traps.)",
    },
]

GPT_MODELS = ["gpt-5", "gpt-4o", "gpt-4o-mini"]


async def _roster(case, llm):
    findings = [_F(t) for t in case["findings"]]
    ds = await derive(case["q"], findings, llm, generate_ideas=True)
    return [{"label": d.label, "kind": d.kind, "conclusion": d.conclusion,
             "basis": list(d.basis), "falsifier": d.falsifier} for d in ds]


def _gpt(case):
    from openai import OpenAI
    client = OpenAI()
    prompt = ("Reason about the question using ONLY these findings. State what follows.\n\n"
              f"QUESTION: {case['q']}\n\nFINDINGS:\n" +
              "\n".join(f"- {t}" for t in case["findings"]))
    last = ""
    for model in GPT_MODELS:
        try:
            # gpt-5 is a reasoning model — give ample completion budget so reasoning tokens don't
            # starve the visible answer (the empty-output trap).
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=3000)
            txt = (r.choices[0].message.content or "").strip()
            if txt:
                return model, txt
            last = f"{model}: empty content (finish={r.choices[0].finish_reason})"
        except Exception as e:   # noqa: BLE001 — try the next model id
            last = f"{model}: {str(e)[:90]}"
    return "none", f"(all GPT models failed: {last})"


async def main():
    llm = build_llm(mode="live")
    print("# Roster grounded reasoning vs raw GPT — adversarial head-to-head\n")
    for i, case in enumerate(CASES, 1):
        print(f"## Case {i}: {case['q']}\n")
        print("**Findings:**")
        for t in case["findings"]:
            print(f"- {t}")
        print(f"\n**Planted valid derivation:** {case['valid']}")
        print(f"\n**Planted TRAP (invalid leap):** {case['trap']}\n")

        roster = await _roster(case, llm)
        print("**ROSTER (gated, labeled derivations):**")
        if not roster:
            print("- (no derivations survived the gate)")
        for d in roster:
            print(f"- `[{d['label']}]` ({d['kind']}, from {d['basis']}) {d['conclusion']}"
                  + (f"  — falsifier: {d['falsifier']}" if d['falsifier'] else ""))

        model, gpt = _gpt(case)
        print(f"\n**RAW GPT ({model}):**\n\n{gpt}\n")
        print("---\n")


if __name__ == "__main__":
    asyncio.run(main())
