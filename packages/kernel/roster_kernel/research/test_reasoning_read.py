"""Reasoning-Read grounding gate: an interpretation item survives ONLY if it (a) is a valid kind,
(b) rests on ≥1 real finding, and (c) introduces no number/dose/date/% absent from its basis findings.
The safety boundary — a fabricated inference (a number the evidence never stated) must never ship."""
from roster_kernel.research.react import (
    InterpretationItem, VerifiedClaim, _frame_grounded, _validate_interpretation, extract_hard_tokens,
)

V = [
    VerifiedClaim(text="Drug A had a 53% response rate in the phase 3 trial", atom_id="a1",
                  quote="53% response rate"),
    VerifiedClaim(text="Drug B response was 41% but the study was observational", atom_id="a2",
                  quote="41%"),
    VerifiedClaim(text="Median follow-up was 12 months", atom_id="a3", quote="12 months"),
]


def _item(**kw):
    return InterpretationItem(**kw)


def test_grounded_interpretation_survives():
    items = [_item(text="The two response figures conflict and rest on different study designs",
                   kind="tension", basis_findings=[1, 2])]
    out = _validate_interpretation(items, V)
    assert len(out) == 1 and out[0]["kind"] == "tension" and out[0]["basis_findings"] == [1, 2]


def test_grounded_item_reusing_a_basis_number_survives():
    # 53% appears in finding 1 → allowed to cite it in the interpretation text
    items = [_item(text="A 53% response is only from one trial, limiting confidence",
                   kind="assumption", basis_findings=[1])]
    assert len(_validate_interpretation(items, V)) == 1


def test_fabricated_number_drops_item():
    # 70% appears in NO basis finding → no-new-facts drop
    items = [_item(text="Response could reach 70% in practice", kind="implication", basis_findings=[1])]
    assert _validate_interpretation(items, V) == []


def test_dangling_basis_drops_item():
    items = [_item(text="This is unsupported", kind="gap", basis_findings=[9])]
    assert _validate_interpretation(items, V) == []


def test_no_basis_drops_item():
    items = [_item(text="Floating interpretation", kind="implication", basis_findings=[])]
    assert _validate_interpretation(items, V) == []


def test_invalid_kind_drops_item():
    items = [InterpretationItem.model_construct(text="x", kind="editorial", basis_findings=[1])]
    assert _validate_interpretation(items, V) == []


def test_empty_text_drops_item():
    items = [_item(text="   ", kind="gap", basis_findings=[1])]
    assert _validate_interpretation(items, V) == []


def test_bad_basis_indices_are_clamped_not_fatal():
    # 9 is invalid, 1 is valid → item survives on finding 1, basis normalized to [1]
    items = [_item(text="Rests partly on a real finding", kind="implication", basis_findings=[9, 1])]
    out = _validate_interpretation(items, V)
    assert len(out) == 1 and out[0]["basis_findings"] == [1]


def test_dose_token_grounding():
    v = [VerifiedClaim(text="The regimen used 5 mg daily", atom_id="d", quote="5 mg daily")]
    ok = [_item(text="The 5 mg dose is the only one studied", kind="gap", basis_findings=[1])]
    bad = [_item(text="A 10 mg dose might work better", kind="implication", basis_findings=[1])]
    assert len(_validate_interpretation(ok, v)) == 1
    assert _validate_interpretation(bad, v) == []


# ---- Reasoning-Read FRAME grounding (purpose / conclusion = the "Informed judgment") ----
# The allowance is the union of the verified findings AND the grounded composed answer the frame sums up.

def _allow(claims_src: str, answer: str) -> set[str]:
    from roster_kernel.research.react import _REF_MARK_RE
    return extract_hard_tokens(claims_src + " " + _REF_MARK_RE.sub(" ", answer))


def test_frame_survives_when_figure_is_in_the_answer_not_a_claim_atom():
    # REGRESSION: the Informed judgment cited "1 hour/day" — present in the ANSWER prose but not verbatim
    # in any claim atom. Old guard (claims-only) blanked the whole conclusion; it must now survive.
    claims_src = "150-300 minutes of moderate aerobic activity; strength training on 2 days"
    answer = "Guideline dose is 150–300 min/week; cognitive benefit appears from as little as 1 hour/day."
    allowed = _allow(claims_src, answer)
    concl = "A single regimen (150–300 min/week plus 2 days of strength) serves both goals, with cognitive gains from ~1 hour/day."
    assert _frame_grounded(concl, allowed) == concl.strip()


def test_frame_drops_a_fabricated_figure_in_neither_answer_nor_claims():
    allowed = _allow("150-300 minutes weekly", "Do 150–300 minutes of moderate activity weekly.")
    assert _frame_grounded("Aim for 500 minutes weekly for maximal benefit.", allowed) == ""


def test_frame_ignores_citation_markers():
    # a bare [3] reference index is not a fact — stripped before the token check
    allowed = _allow("moderate activity weekly", "Exercise helps mood and cognition.")
    assert _frame_grounded("Exercise supports mental health [3].", allowed) == "Exercise supports mental health [3]."


def test_number_free_purpose_passes_trivially():
    allowed = _allow("some findings", "some grounded answer")
    p = "Whether current guidance supports a specific weekly exercise dose for health and mood."
    assert _frame_grounded(p, allowed) == p


def test_empty_frame_stays_empty():
    assert _frame_grounded("", {"1", "2"}) == ""
    assert _frame_grounded("   ", {"1", "2"}) == ""


def test_empty_is_noop():
    assert _validate_interpretation([], V) == []


def test_extract_hard_tokens_normalizes():
    toks = extract_hard_tokens("53% response, 5 mg dose, on 2026-07-01, and $4.2M")
    assert "53" in toks and "5mg" in toks and "2026-07-01" in toks


def test_extract_hard_tokens_ignores_letter_adjacent_digits():
    # drug/identifier names must NOT yield spurious figure tokens (the prod PCSK9 false-positive)
    toks = extract_hard_tokens("PCSK9 inhibitors, vitamin B12, COVID19, and CoQ10")
    assert toks == set(), toks
    # a real figure next to a name is still caught
    assert "40" in extract_hard_tokens("PCSK9 inhibitors cut LDL by 40%")


# --- control-tag bleed regression (the "Informed judgment" UI leak) ---
from roster_kernel.research.react import strip_control_tags


def test_strip_control_tags_truncates_bled_serialization():
    # a completion bled its structured-output serialization into the frame value
    dirty = ('The evidence supports two strategic camps of VCs.'
             '</reasoning_conclusion> <parameter name="confidence">{"factual":{"level":"moderate"}}')
    assert strip_control_tags(dirty) == 'The evidence supports two strategic camps of VCs.'


def test_frame_grounded_after_strip_keeps_clean_text():
    # clean prefix is grounded (tokens subset of allowed) → survives; the bled tail is gone
    allowed = extract_hard_tokens("two camps of VCs pivot to AI in 2026")
    dirty = ('Top VCs form two camps, with AI as a shared pivot in 2026.'
             '</reasoning_conclusion> <parameter name="confidence">{"x":1}')
    assert _frame_grounded(strip_control_tags(dirty), allowed).endswith("2026.")
    assert "confidence" not in _frame_grounded(strip_control_tags(dirty), allowed)


def test_validate_interpretation_strips_bled_tag_in_item_text():
    items = [_item(text=('The two figures conflict</interpretation> <parameter name="charts">[]'),
                   kind="tension", basis_findings=[1, 2])]
    out = _validate_interpretation(items, V)
    assert len(out) == 1 and out[0]["text"] == "The two figures conflict"
