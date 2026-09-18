"""Phase-2 extractive grounding for drafted free-text answers (pure span-check; a fake LLM, no network)."""
import asyncio
from types import SimpleNamespace

from api.people_population import assemble_grounded_drafts, build_grounded_drafts

RESUME = ("Jane Doe. Senior Data Engineer at Acme from 2019 to 2024. Built a real-time streaming "
          "pipeline processing two billion events per day on Kafka and Flink. Led a team of six engineers.")


def _item(index, answer, source_quote=""):
    return SimpleNamespace(index=index, answer=answer, source_quote=source_quote)


def test_a_claim_backed_by_a_real_resume_quote_is_grounded():
    qs = [{"label": "Describe your data-pipeline experience."}]
    items = [_item(1, "I built a real-time streaming pipeline on Kafka and Flink.",
                   "Built a real-time streaming pipeline processing two billion events per day")]
    out = assemble_grounded_drafts(qs, items, {}, RESUME)
    assert out[0]["grounded"] is True and out[0]["answer"].startswith("I built")


def test_a_fabricated_claim_whose_quote_is_not_in_the_resume_is_flagged():
    qs = [{"label": "Do you have Kubernetes experience?"}]
    # the model invented a quote that is NOT in the résumé
    items = [_item(1, "Yes, I ran Kubernetes clusters for five years.",
                   "operated Kubernetes clusters at scale for five years")]
    out = assemble_grounded_drafts(qs, items, {}, RESUME)
    assert out[0]["grounded"] is False and "not found" in out[0]["reason"]


def test_a_no_claim_answer_needs_no_quote():
    qs = [{"label": "Why do you want to work here?"}]
    items = [_item(1, "I'm eager to contribute to your mission and grow in this space.", "")]
    out = assemble_grounded_drafts(qs, items, {}, RESUME)
    assert out[0]["grounded"] is True and out[0]["source_quote"] == ""


def test_a_quote_can_be_grounded_in_a_scalar_profile_fact_not_only_the_resume():
    qs = [{"label": "What is your current title?"}]
    items = [_item(1, "Staff Machine Learning Engineer.", "Staff Machine Learning Engineer")]
    out = assemble_grounded_drafts(qs, items, {"current_title": "Staff Machine Learning Engineer"}, "")
    assert out[0]["grounded"] is True


def test_unanswered_questions_are_dropped_and_indexing_is_one_based():
    qs = [{"label": "Q1"}, {"label": "Q2"}]
    items = [_item(2, "Only the second is answered.", "")]   # 1-based: index 2 → Q2
    out = assemble_grounded_drafts(qs, items, {}, RESUME)
    assert [o["label"] for o in out] == ["Q2"]


def test_build_grounded_drafts_runs_the_span_check_over_a_fake_llm():
    qs = [{"label": "Describe your streaming work."}, {"label": "Kubernetes experience?"}]

    class _FakeLLM:
        async def complete(self, **_kw):
            answers = [_item(1, "I built a streaming pipeline on Kafka and Flink.",
                             "Built a real-time streaming pipeline processing two billion events per day"),
                       _item(2, "I ran Kubernetes for years.", "operated Kubernetes clusters for years")]
            return SimpleNamespace(parsed=SimpleNamespace(answers=answers))

    out = asyncio.run(build_grounded_drafts("Eng", "Acme", {}, RESUME, qs, _FakeLLM()))
    by = {o["label"]: o for o in out}
    assert by["Describe your streaming work."]["grounded"] is True
    assert by["Kubernetes experience?"]["grounded"] is False   # fabricated quote → flagged, never auto-filled
