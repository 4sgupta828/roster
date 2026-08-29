"""Tests for render_contract_directive — voice ⟂ shape assembly (the contract-rendered compose core)."""
from roster_kernel.research.contract import render_contract_directive

VOICE = "VOICE: plain, grounded."
SHAPES = {"enumerative": "SHAPE: build the table.", "exploratory": "SHAPE: map the landscape."}
DEFAULT = "SHAPE: answer directly."


def test_default_mode_uses_default_shape():
    out = render_contract_directive(voice=VOICE, shapes=SHAPES, default=DEFAULT, mode="")
    assert out == "VOICE: plain, grounded.\n\nSHAPE: answer directly."


def test_decision_mode_unmapped_falls_to_default():
    out = render_contract_directive(voice=VOICE, shapes=SHAPES, default=DEFAULT, mode="decision")
    assert "SHAPE: answer directly." in out and "build the table" not in out


def test_exploratory_maps_to_default_shape():
    # the tech vertical intentionally does NOT map exploratory → a landscape shape (that would turn
    # normal analytical questions into surveys); exploratory falls through to the default.
    from roster_vertical.golden_answer import CONTRACT_SHAPES, SHAPE_DEFAULT
    out = render_contract_directive(voice=VOICE, shapes=CONTRACT_SHAPES, default=SHAPE_DEFAULT,
                                    mode="exploratory")
    assert "ANSWER THE QUESTION DIRECTLY" in out and "ENUMERATE THE COMPLETE SET" not in out


def test_enumerative_no_entities_renders_dimensions_only():
    # discovered-entity enumerative: no ITEMS line (rows discovered from evidence), DIMENSIONS present.
    from roster_vertical.golden_answer import CONTRACT_SHAPES, SHAPE_DEFAULT
    out = render_contract_directive(voice=VOICE, shapes=CONTRACT_SHAPES, default=SHAPE_DEFAULT,
                                    mode="enumerative", entities=[], axes=["Value", "ROI"])
    assert "ENUMERATE THE COMPLETE SET" in out
    assert "ITEMS to enumerate" not in out
    assert "DIMENSIONS (one column each): Value; ROI." in out


def test_enumerative_appends_items_and_dimensions():
    out = render_contract_directive(
        voice=VOICE, shapes=SHAPES, default=DEFAULT, mode="enumerative",
        entities=["Frontier agents", "Vertical agents", "Infra"], axes=["Moat", "ROI", "Risk"])
    assert "SHAPE: build the table." in out
    assert "ITEMS to enumerate (one row each): Frontier agents; Vertical agents; Infra." in out
    assert "DIMENSIONS (one column each): Moat; ROI; Risk." in out


def test_enumerative_without_grid_is_shape_only():
    out = render_contract_directive(voice=VOICE, shapes=SHAPES, default=DEFAULT,
                                    mode="enumerative", entities=[], axes=[])
    assert "SHAPE: build the table." in out and "ITEMS to enumerate" not in out


def test_voice_alone_when_no_shape():
    out = render_contract_directive(voice=VOICE, shapes={}, default="", mode="whatever")
    assert out == VOICE


def test_voice_carries_no_shape_verdict_wiring():
    # the tech vertical's real constants: VOICE must not contain the "single straight answer" verdict
    # (that belongs to the default SHAPE), else a shape would contradict it.
    from roster_vertical.golden_answer import GOLDEN_VOICE, SHAPE_DEFAULT, CONTRACT_SHAPES
    assert "single most useful, straight answer" not in GOLDEN_VOICE
    assert "single straight answer" not in GOLDEN_VOICE.lower()
    assert "ENUMERATE THE COMPLETE SET" in CONTRACT_SHAPES["enumerative"]
    assert "ANSWER THE QUESTION DIRECTLY" in SHAPE_DEFAULT
