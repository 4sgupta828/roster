"""v3-P0 expander contract: masquerade legs (incoming mimics/underlies edges) outrank
comorbidity edges when the question names the cover story; per-relation templates carry the
hidden topic + discriminator; manifests_as stays dark; outgoing masquerade edges are skipped."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roster_kernel.graph.store import GraphStore, build_adjacency, edge_identity  # noqa: E402


def _e(subj, rel, obj, *, ctx="", dx="", conf=1.0):
    return {"id": edge_identity(subj, rel, obj, ctx), "subject": subj,
            "subject_norm": " ".join(subj.lower().split()), "relation": rel,
            "object": obj, "object_norm": " ".join(obj.lower().split()),
            "context_topic": ctx, "distinguished_by": dx, "label": "established",
            "provenance": "curated", "confidence": conf}


EDGES = [
    _e("hypertension", "increases_risk_of", "heart failure"),                # comorbidity noise
    _e("coronary artery disease", "causes", "heart failure"),
    _e("atrial fibrillation", "comorbid_with", "heart failure"),
    _e("cardiac amyloidosis", "underlies_presentation_of", "heart failure",
       ctx="HFpEF phenotype", dx="LVH with low-voltage ECG"),
    _e("cardiac sarcoidosis", "underlies_presentation_of", "heart failure",
       dx="AV block in a young patient"),
    _e("heart failure", "manifests_as", "fatigue"),                          # dark relation
]


def _fake_store():
    g = GraphStore.__new__(GraphStore)
    adj = build_adjacency(EDGES)

    async def _adj():
        return adj
    g._adjacency = _adj
    return g


def _expander(monkeypatch_env):
    os.environ["ROSTER_GRAPH"] = "1"
    os.environ["ROSTER_GRAPH_EXPAND"] = "late"
    import api.app as appmod
    appmod._GRAPH_STORE = _fake_store()
    return appmod._make_graph_expander()


def test_masquerade_legs_win_the_cap_with_discriminated_templates():
    exp = _expander(None)
    got = asyncio.run(exp("Patient with heart failure with preserved ejection fraction "
                          "not responding to standard therapy"))
    queries = [leg["query"] for leg in got["legs"]]
    assert len(queries) == 2 and got["late"] is True
    assert queries[0].startswith("cardiac amyloidosis presenting as heart failure")
    assert "low-voltage ECG" in queries[0]                       # discriminator in the query
    assert queries[1].startswith("cardiac sarcoidosis presenting as heart failure")
    # comorbidity edges (hypertension/CAD/AF) were outranked; manifests_as never legs
    assert not any("fatigue" in q for q in queries)


def test_outgoing_masquerade_edge_is_skipped_for_the_masquerader_itself():
    exp = _expander(None)
    got = asyncio.run(exp("management of cardiac amyloidosis"))
    # asking about the masquerader: its cover-story leg adds nothing — no legs from
    # the outgoing underlies edge (and nothing else is adjacent)
    assert got is None or all(
        not leg["query"].startswith("cardiac amyloidosis presenting")
        for leg in got["legs"])


class _FakeMapLLM:
    """Returns a scripted topic mapping; counts calls (containment-first must not call it)."""
    def __init__(self, topics):
        self.topics = topics
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=150,
                       temperature=None):
        self.calls += 1
        assert "TOPIC LIST" in system            # vocabulary must be shown

        class R:
            parsed = response_format(topics=self.topics)
        return R()


def test_llm_mapping_fires_only_on_containment_miss_and_validates_vocabulary():
    os.environ["ROSTER_GRAPH_MAP"] = "llm"
    import api.app as appmod
    exp = _expander(None)
    fake = _FakeMapLLM(["heart failure", "not-a-real-topic"])
    appmod._GRAPH_MAP_LLM = fake
    try:
        # containment miss ("HFpEF" only) → mapper fires, non-vocab label filtered out,
        # masquerade legs come from the mapped topic
        got = asyncio.run(exp("Refractory HFpEF workup?"))
        assert fake.calls == 1
        assert got and got["legs"][0]["query"].startswith("cardiac amyloidosis presenting")
        # containment HIT → mapper must NOT be called
        asyncio.run(exp("worsening heart failure management"))
        assert fake.calls == 1
        # mapper returns only garbage → filtered → no legs, fail-safe
        appmod._GRAPH_MAP_LLM = _FakeMapLLM(["made-up condition"])
        assert asyncio.run(exp("Refractory HFpEF workup?")) is None
    finally:
        os.environ.pop("ROSTER_GRAPH_MAP", None)
        appmod._GRAPH_MAP_LLM = None


def test_mapping_off_by_default_no_llm_touched():
    os.environ.pop("ROSTER_GRAPH_MAP", None)
    import api.app as appmod
    exp = _expander(None)
    appmod._GRAPH_MAP_LLM = None
    assert asyncio.run(exp("Refractory HFpEF workup?")) is None   # containment miss → no legs
    assert appmod._GRAPH_MAP_LLM is None                          # and no client was built
