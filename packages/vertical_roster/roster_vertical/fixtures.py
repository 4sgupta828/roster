"""Load the bundled sample corpus (offline fixtures for tests + a default corpus)."""
from __future__ import annotations

import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"


def sample_papers() -> list[dict]:
    return json.loads((_DATA / "arxiv_sample.json").read_text())


def sample_filings() -> list[dict]:
    return json.loads((_DATA / "filings_sample.json").read_text())
