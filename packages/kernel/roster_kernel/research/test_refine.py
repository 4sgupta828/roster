"""refine_question: 3-4 distinct options for a broad Q, [] when ≤1 / already-precise / error, and
dedup + drop-the-original. No grounding involved — it only proposes questions the user picks."""
import asyncio

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.refine import Refinements, refine_question


class FakeLLM:
    def __init__(self, questions, raise_it=False):
        self._q = questions
        self._raise = raise_it

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if self._raise:
            raise RuntimeError("provider down")
        return LLMResult(parsed=Refinements(questions=self._q), model="fake")


def _run(questions, raise_it=False, question="tell me about diabetes drugs"):
    return asyncio.run(refine_question(
        llm=FakeLLM(questions, raise_it), refine_prompt="sys", question=question))


def test_distinct_options_returned_capped_at_4():
    out = _run(["A for children?", "B vs C in adults?", "Safety of D?", "Dose of E?", "Cost of F?"])
    assert out == ["A for children?", "B vs C in adults?", "Safety of D?", "Dose of E?"]


def test_single_option_is_not_a_choice():
    assert _run(["just one refinement"]) == []       # <2 distinct → treat as "already precise"


def test_empty_when_already_precise():
    assert _run([]) == []


def test_dedup_and_drops_the_original():
    out = _run(["Same Q?", "same q?", "tell me about diabetes drugs", "A distinct one?"],
               question="tell me about diabetes drugs")
    assert out == ["Same Q?", "A distinct one?"]      # case-dup removed; original removed


def test_error_fails_open_to_empty():
    assert _run(["A?", "B?"], raise_it=True) == []    # provider error → [] (answer the original)
