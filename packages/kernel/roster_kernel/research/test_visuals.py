"""Post-hoc visualizer mechanics: the GROUNDING gate is the correctness surface — an element whose
quote isn't in the answer must be dropped, an edge needs its own basis, and a visual that falls below
its floor is dropped entirely (fail-safe). Scripted LLM (Rule 8: tests the code contract)."""
import asyncio

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.visuals import (
    Visual, VNode, VEdge, VEvent, VisualSet, visualize_answer, _clean_visual)

ANSWER = ("Metformin lowers hepatic glucose production, which reduces blood glucose. "
          "As eGFR falls below 30, metformin is stopped due to lactic acidosis risk.")


def test_flow_drops_ungrounded_node_and_edge():
    v = Visual(kind="flow", title="MoA",
        nodes=[
            VNode(id="a", label="Metformin", quote="Metformin lowers hepatic glucose production"),
            VNode(id="b", label="Less glucose", quote="which reduces blood glucose"),
            VNode(id="c", label="Weight loss", quote="metformin causes significant weight loss"),  # NOT in answer
        ],
        edges=[
            VEdge(src="a", dst="b", label="reduces", quote="Metformin lowers hepatic glucose production, which reduces blood glucose"),
            VEdge(src="a", dst="c", label="causes", quote="metformin causes significant weight loss"),  # ungrounded + dead endpoint
        ])
    out = _clean_visual(v, ANSWER)
    assert out is not None and out["kind"] == "flow"
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"a", "b"}                       # ungrounded node c dropped
    assert len(out["edges"]) == 1 and out["edges"][0]["dst"] == "b"   # ungrounded edge dropped


def test_flow_dropped_when_no_grounded_edges():
    v = Visual(kind="flow", title="x",
        nodes=[VNode(id="a", label="Metformin", quote="Metformin lowers hepatic glucose production"),
               VNode(id="b", label="Glucose", quote="which reduces blood glucose")],
        edges=[VEdge(src="a", dst="b", quote="")])   # no basis → dropped → no edges → whole flow dropped
    assert _clean_visual(v, ANSWER) is None


def test_timeline_requires_two_grounded_events():
    v = Visual(kind="timeline", title="course", events=[
        VEvent(when="eGFR<30", label="Stop metformin", quote="metformin is stopped due to lactic acidosis risk"),
        VEvent(when="later", label="Dialysis", quote="the patient begins dialysis"),   # not in answer
    ])
    assert _clean_visual(v, ANSWER) is None          # only 1 grounded event → below floor


def test_tree_reparents_to_root_when_parent_dropped():
    v = Visual(kind="tree", title="dx", nodes=[
        VNode(id="r", label="Renal function", quote="As eGFR falls below 30"),
        VNode(id="x", label="Ghost", quote="totally fabricated branch not present"),   # dropped
        VNode(id="c", label="Stop metformin", parent="x", quote="metformin is stopped due to lactic acidosis risk"),
    ])
    out = _clean_visual(v, ANSWER)
    assert out is not None and {n["id"] for n in out["nodes"]} == {"r", "c"}
    assert next(n for n in out["nodes"] if n["id"] == "c")["parent"] == ""   # dangling parent → root


def test_label_number_not_in_answer_drops_node():
    v = Visual(kind="timeline", title="t", events=[
        VEvent(when="wk0", label="eGFR below 30", quote="As eGFR falls below 30"),
        VEvent(when="wk1", label="eGFR below 45", quote="which reduces blood glucose"),  # "45" not in answer
    ])
    # second event's quote grounds but its label injects a fabricated number → dropped → floor fails
    assert _clean_visual(v, ANSWER) is None


def test_unknown_kind_dropped():
    assert _clean_visual(Visual(kind="sankey", title="x"), ANSWER) is None


MAPANS = ("Obesity drives insulin resistance, which raises blood glucose. Insulin resistance also "
          "promotes hypertension, and hypertension worsens kidney function. High blood glucose "
          "further damages the kidneys.")


def test_map_keeps_grounded_web_and_drops_isolated_node():
    v = Visual(kind="map", title="web",
        nodes=[VNode(id="ob", label="Obesity", quote="Obesity drives insulin resistance"),
               VNode(id="ir", label="Insulin resistance", quote="Obesity drives insulin resistance"),
               VNode(id="glu", label="Blood glucose", quote="which raises blood glucose"),
               VNode(id="htn", label="Hypertension", quote="promotes hypertension"),
               VNode(id="iso", label="Statins", quote="patient takes a high-dose statin")],  # not in answer
        edges=[
            VEdge(src="ob", dst="ir", label="drives", quote="Obesity drives insulin resistance"),
            VEdge(src="ir", dst="glu", label="raises", quote="which raises blood glucose"),
            VEdge(src="ir", dst="htn", label="promotes", quote="Insulin resistance also promotes hypertension"),
        ])
    out = _clean_visual(v, MAPANS)
    assert out is not None and out["kind"] == "map"
    assert {n["id"] for n in out["nodes"]} == {"ob", "ir", "glu", "htn"}   # ungrounded 'iso' dropped
    assert len(out["edges"]) == 3


def test_map_drops_ungrounded_edge_relationship():
    v = Visual(kind="map", title="web",
        nodes=[VNode(id="ob", label="Obesity", quote="Obesity drives insulin resistance"),
               VNode(id="ir", label="Insulin resistance", quote="Obesity drives insulin resistance"),
               VNode(id="htn", label="Hypertension", quote="promotes hypertension")],
        edges=[
            VEdge(src="ob", dst="ir", label="drives", quote="Obesity drives insulin resistance"),
            VEdge(src="ob", dst="htn", label="cures", quote="obesity cures hypertension"),   # fabricated relation
        ])
    # only 1 grounded edge survives → below the map edge floor (2) → whole map dropped
    assert _clean_visual(v, MAPANS) is None


def test_map_below_web_floor_dropped():
    # 2 nodes, 1 edge — a single link is a flow, not a map
    v = Visual(kind="map", title="thin",
        nodes=[VNode(id="ob", label="Obesity", quote="Obesity drives insulin resistance"),
               VNode(id="ir", label="Insulin resistance", quote="Obesity drives insulin resistance")],
        edges=[VEdge(src="ob", dst="ir", label="drives", quote="Obesity drives insulin resistance")])
    assert _clean_visual(v, MAPANS) is None


class _LLM:
    def __init__(self, parsed): self._p = parsed
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._p, output_tokens=5, model="scripted")


def test_visualize_answer_empty_answer_skips_llm():
    called = {"n": 0}
    class _Spy(_LLM):
        async def complete(self, **k): called["n"] += 1; return LLMResult(parsed=VisualSet(), output_tokens=1)
    out = asyncio.run(visualize_answer(llm=_Spy(None), visuals_prompt="p", question="q", answer="   "))
    assert out == [] and called["n"] == 0


def test_visualize_answer_filters_to_grounded():
    good = Visual(kind="flow", title="MoA",
        nodes=[VNode(id="a", label="Metformin", quote="Metformin lowers hepatic glucose production"),
               VNode(id="b", label="Glucose", quote="which reduces blood glucose")],
        edges=[VEdge(src="a", dst="b", quote="Metformin lowers hepatic glucose production, which reduces blood glucose")])
    bad = Visual(kind="flow", title="junk",
        nodes=[VNode(id="a", label="x", quote="not in the answer at all here"),
               VNode(id="b", label="y", quote="also totally absent")],
        edges=[VEdge(src="a", dst="b", quote="absent")])
    out = asyncio.run(visualize_answer(
        llm=_LLM(VisualSet(visuals=[good, bad])), visuals_prompt="p", question="q", answer=ANSWER))
    assert len(out) == 1 and out[0]["title"] == "MoA"
