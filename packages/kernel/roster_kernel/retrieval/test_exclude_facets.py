"""exclude_facets: the default (Allopathic) view must keep modality=alternative blocks out WITHOUT
excluding untagged blocks (no null-exclusion trap). Tested on the in-memory source (mirrors the
Postgres `NOT (facets ? key) OR value <> ALL(...)` clause)."""
import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.retrieval.memory import InMemoryRetrievalSource, IndexedBlock, _facets_excluded
from roster_kernel.contract.dto import Locator


def _blk(bid, modality=None):
    facets = {}
    if modality is not None:
        facets["modality"] = modality
    return IndexedBlock(block_id=bid, document_id="d"+bid, tenant_id="acme",
                        text="acupuncture for chronic low back pain "+bid,
                        locator=Locator("block_span", "d"+bid, {"block_id": bid}),
                        source_key="corpus", facets=facets, embedding=[0.0]*8)


def _src():
    s = InMemoryRetrievalSource()
    s.add(_blk("conv"))                     # untagged conventional block
    s.add(_blk("allo", "allopathic"))       # explicitly allopathic
    s.add(_blk("cam", "alternative"))       # CAM block
    return s


def _ids(hits):
    return {h.block_id for h in hits}


def test_default_excludes_alternative_keeps_untagged():
    s = _src()
    req = RetrievalRequest(query="acupuncture", tenant_id="acme", query_embedding=[0.0]*8,
                           exclude_facets={"modality": ("alternative",)}, k=10, fetch_pool=10)
    got = _ids(asyncio.run(s.search(req)))
    assert "cam" not in got                 # CAM excluded from the default view
    assert {"conv", "allo"} <= got          # untagged + allopathic both pass (no null-exclusion trap)


def test_alternative_mode_no_exclusion_sees_everything():
    s = _src()
    req = RetrievalRequest(query="acupuncture", tenant_id="acme", query_embedding=[0.0]*8,
                           exclude_facets={}, k=10, fetch_pool=10)   # Alternative mode → no exclusion
    got = _ids(asyncio.run(s.search(req)))
    assert "cam" in got and "conv" in got   # CAM surfaces alongside conventional context


def test_facets_excluded_unit():
    assert _facets_excluded({"modality": "alternative"}, {"modality": ("alternative",)}) is True
    assert _facets_excluded({"modality": "allopathic"}, {"modality": ("alternative",)}) is False
    assert _facets_excluded({}, {"modality": ("alternative",)}) is False   # untagged passes
    assert _facets_excluded({"modality": "alternative"}, {}) is False       # no exclusion set
