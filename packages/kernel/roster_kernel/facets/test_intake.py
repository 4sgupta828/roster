"""The intake gates (kernel — docs/specs/guided-intake.md §3, §2.1): WHETHER to ask is code's decision, over
opaque item names, contract keys and counts. No vocabulary here: the caller supplies the required items / keys."""
from roster_kernel.facets import Contract
from roster_kernel.facets.intake import (IntakeState, Question, apply_answer, constrained, next_question, search_now, spread,
                                          worth_asking)

REQUIRED_ITEMS = ["title", "level", "location", "comp"]
REQUIRED_KEYS = ["level", "metro"]
OPTIONAL_KEYS = ["company_type", "work_mode", "field"]


def _state(**kw) -> IntakeState:
    base = dict(direction="a", artifact={"kind": "x", "status": "present"},
                checklist={"title": "present", "level": "missing", "location": "missing", "comp": "weak", "team": "missing"},
                contract=Contract(kind="k", text="t").to_dict())
    base.update(kw)
    return IntakeState(**base)


def test_spread_and_known_share_over_counts():
    s, known = spread({"a": 40, "b": 60})
    assert abs(s - 0.4) < 1e-9 and known == 1.0
    s2, known2 = spread({"a": 90, "b": 10, "unknown": 100})
    assert abs(s2 - 0.1) < 1e-9 and known2 == 0.5
    assert spread({}) == (0.0, 0.0) and spread({"unknown": 5}) == (0.0, 0.0)


def test_worth_asking_needs_spread_known_share_and_freedom():
    assert worth_asking({"a": 40, "b": 60}, constrained=False)
    assert not worth_asking({"a": 95, "b": 5}, constrained=False)                       # nearly single-valued
    assert not worth_asking({"a": 40, "b": 60, "unknown": 300}, constrained=False)       # unknown-heavy
    assert not worth_asking({"a": 40, "b": 60}, constrained=True)                        # already constrained
    assert not worth_asking({}, constrained=False)


def test_constrained_reads_every_section_of_the_contract():
    c = Contract(kind="k", must={"level": ["senior"]}, prefer={"metro": ["x"]}, avoid={"field": ["y"]}, center={"key": "years", "value": "3_5", "span": 1})
    assert constrained(c, "level") and constrained(c, "metro") and constrained(c, "field") and constrained(c, "years")
    assert not constrained(c, "company_type")


def test_gaps_come_first_then_required_keys_then_optional_and_budgets_hold():
    counts = {"level": {"senior": 50, "leadership": 50}, "metro": {"bay_area": 60, "nyc": 40}, "company_type": {"c1": 45, "c2": 55},
              "work_mode": {"remote": 98, "onsite": 2}, "field": {"software": 50, "data_ml": 50}}
    st = _state()
    q1 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert isinstance(q1, Question) and q1.kind == "item" and q1.name == "level"            # first missing REQUIRED item ("team" is not required)
    st = apply_answer(st, q1, "senior", contract_key="level")                                # an item that is also a key lands in the contract
    assert st.contract["must"]["level"] == ["senior"] and st.checklist["level"] == "answered"
    q2 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert q2.kind == "item" and q2.name == "location"
    st = apply_answer(st, q2, "bay_area", contract_key="metro")
    q3 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    # comp is 'weak' (never asked), team is not required, level / metro are now constrained → first spread optional key
    assert q3.kind == "key" and q3.name == "company_type" and q3.klass == "optional"


def test_weak_items_are_never_asked_and_required_keys_fill_from_counts():
    counts = {"level": {"senior": 50, "leadership": 50}, "metro": {"bay_area": 60, "nyc": 40}, "company_type": {"c1": 45, "c2": 55},
              "work_mode": {"remote": 98, "onsite": 2}, "field": {"software": 50, "data_ml": 50}}
    st = _state(checklist={"title": "present", "level": "present", "location": "present", "comp": "weak"})
    q = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert q.kind == "key" and q.name == "level"                                             # required key unknown in the contract
    assert [o[0] for o in q.options] == ["senior", "leadership"] and q.options[0][1] == 50   # options = the counts, unknown excluded
    st = apply_answer(st, q, "leadership")
    q2 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert q2.kind == "key" and q2.name == "metro"
    st = apply_answer(st, q2, "nyc")
    q3 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert q3.kind == "key" and q3.name == "company_type"                                    # spread → optional; work_mode (98/2) never
    st = apply_answer(st, q3, "c1", mode="prefer")
    assert st.contract["prefer"]["company_type"] == ["c1"]
    q4 = next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts)
    assert q4.kind == "key" and q4.name == "field"
    st = apply_answer(st, q4, "software")
    assert next_question(st, required_items=REQUIRED_ITEMS, required_keys=REQUIRED_KEYS, optional_keys=OPTIONAL_KEYS, counts=counts) is None
    assert st.stage == "ready"


def test_budgets_cap_each_class_of_question():
    counts = {k: {"a": 50, "b": 50} for k in ("level", "metro", "company_type", "work_mode", "field", "k5", "k6")}
    st = _state(checklist={f"i{n}": "missing" for n in range(6)}, budgets={"gaps": 2, "required": 1, "optional": 1})
    items = [f"i{n}" for n in range(6)]
    asked = []
    while True:
        q = next_question(st, required_items=items, required_keys=["level", "metro"], optional_keys=["company_type", "field", "k5"], counts=counts)
        if q is None:
            break
        asked.append((q.kind, q.name)); st = apply_answer(st, q, "a")
    assert asked == [("item", "i0"), ("item", "i1"), ("key", "level"), ("key", "company_type")]
    assert st.stage == "ready"


def test_a_skipped_answer_never_constrains_and_is_not_asked_again():
    counts = {"level": {"senior": 50, "leadership": 50}}
    st = _state(checklist={"comp": "missing"})
    q = next_question(st, required_items=["comp"], required_keys=[], optional_keys=[], counts=counts)
    st = apply_answer(st, q, None, contract_key="comp")                                       # "prefer not to say"
    assert st.checklist["comp"] == "skipped" and "comp" not in st.contract["must"]
    assert next_question(st, required_items=["comp"], required_keys=[], optional_keys=[], counts=counts) is None


def test_search_now_forces_ready_from_any_stage():
    st = _state(checklist={"level": "missing"})
    assert search_now(st).stage == "ready"
    assert st.stage != "ready"                                                              # pure


def test_state_round_trips_through_dicts():
    st = _state(); st = apply_answer(st, Question(kind="key", name="level", options=[]), "senior")
    d = st.to_dict(); back = IntakeState.from_dict(d)
    assert back.to_dict() == d and back.contract["must"]["level"] == ["senior"] and back.asked == ["level"]
