"""Provider-malformation repair on AgentStep (the prod 'claims as string' failure):
a JSON-list-plus-XML-junk string coerces to the list; garbage degrades to [] (recovery
path) instead of hard-failing the run."""
from roster_kernel.research.react import AgentStep


def test_xml_junk_wrapped_claims_string_is_repaired():
    raw = ('[\n{"text":"Apixaban dose is 5 mg BID","atom_id":"a1","quote":"apixaban 5 mg '
           'twice daily"}]</claims>\n</invoke>\n')
    s = AgentStep(action="answer", claims=raw)
    assert len(s.claims) == 1 and s.claims[0].atom_id == "a1"


def test_queries_string_repaired_and_garbage_degrades_to_empty():
    s = AgentStep(action="search", query="q", queries='["a", "b"]</queries>')
    assert s.queries == ["a", "b"]
    s2 = AgentStep(action="answer", claims="totally not json")
    assert s2.claims == []                       # → empty-claims recovery, not a 502


def test_normal_lists_pass_through_unchanged():
    s = AgentStep(action="answer", claims=[
        {"text": "t", "atom_id": "a1", "quote": "q"}])
    assert len(s.claims) == 1
