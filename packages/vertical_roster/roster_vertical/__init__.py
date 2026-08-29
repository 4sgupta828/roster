"""roster_vertical — the deep-tech research vertical (product #1 on the roster kernel).

ALL tech/finance vocabulary (company/technology/patent/paper/repo/funding, the SEC-EDGAR
+ arXiv connectors, the tech evidence pyramid, persona, sectors, and UI) lives HERE —
never in roster_kernel. Sub-domains (AI, fintech, biotech…) are a `sector` facet + the
`sector_profiles` map, so ONE deployment answers across sectors (see sectors.py).
"""
from .manifest import build_manifest

manifest = build_manifest()
