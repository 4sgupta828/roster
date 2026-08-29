"""Provider mode resolution + the no-credits-in-tests guard."""
from __future__ import annotations

import enum
import os
import sys


class ProviderMode(str, enum.Enum):
    REPLAY = "replay"  # offline, deterministic; the default
    RECORD = "record"  # real call + persist a cassette
    LIVE = "live"      # real call (production)


class LiveCallForbidden(RuntimeError):
    """A `live` provider call was attempted under a test run.

    Tests must never spend credits. Set ROSTER_ALLOW_LIVE=1 to override for an
    explicit, intentional live smoke test.
    """


def resolve_mode(explicit: ProviderMode | str | None = None) -> ProviderMode:
    """Resolve the active provider mode.

    Precedence: explicit arg → ROSTER_PROVIDER_MODE env → REPLAY (default).
    """
    if explicit is not None:
        return ProviderMode(explicit)
    raw = os.environ.get("ROSTER_PROVIDER_MODE", "replay").strip().lower()
    return ProviderMode(raw)


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def guard_live(mode: ProviderMode) -> None:
    """Raise if a live call is about to happen inside a test without opt-in."""
    if mode is ProviderMode.LIVE and _running_under_pytest():
        if os.environ.get("ROSTER_ALLOW_LIVE") != "1":
            raise LiveCallForbidden(
                "live provider call under pytest — this would spend credits. "
                "Use replay/record, or set ROSTER_ALLOW_LIVE=1 for an "
                "intentional live smoke test."
            )
