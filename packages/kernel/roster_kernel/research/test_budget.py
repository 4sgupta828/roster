"""Offline tests for the per-run cost governor."""
from __future__ import annotations

import pytest

from roster_kernel.research.budget import BudgetExceeded, BudgetState


def test_reserve_and_charge_calls() -> None:
    b = BudgetState(max_calls=2)
    b.reserve(); b.charge()
    b.reserve(); b.charge()
    assert b.exhausted
    with pytest.raises(BudgetExceeded):
        b.reserve()


def test_remaining_calls() -> None:
    b = BudgetState(max_calls=3)
    b.charge(calls=1)
    assert b.remaining_calls() == 2


def test_token_ceiling() -> None:
    b = BudgetState(max_calls=100, max_tokens=1000)
    b.charge(calls=1, tokens=1000)
    assert b.exhausted
    with pytest.raises(BudgetExceeded):
        b.reserve()


def test_reserve_blocks_overshoot() -> None:
    b = BudgetState(max_calls=1)
    with pytest.raises(BudgetExceeded):
        b.reserve(calls=2)
