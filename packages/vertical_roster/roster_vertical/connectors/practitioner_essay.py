"""Practitioner-essay connector — first-person writing about hiring and job search (keyless RSS).

The sibling of `expert_feed.py`. That connector curates deep-tech experts; this one curates people
writing about FINDING WORK AND FINDING PEOPLE — how a search actually goes, what a résumé is for,
what an interview loop measures, how candidates and interviews are being faked, what a recruiter sees
from the other side. Same mechanics, different vocabulary.

THIS IS THE STRONGEST LEG, and the measurement says why: podcasts in this genre are 83% title-only and
YouTube gives descriptions, but an essay ships FULL TEXT with an unambiguous author, which is the only
thing here that can be quoted at all. Every feed was kept only after measuring that it ships full text
and that it is actually about this domain — see `voices_sources.py`, where both numbers live.

TIER. `source_kind="essay"` from this connector grades to `practitioner_advice`: a named person's
counsel, on the record, and NEVER controlling. It supports "this author argues X" and never "X works".
The careers genre is full of confident, engagement-optimised advice; the register has to carry that.

`voice_role` records what the writer IS — recruiter, engineer, coach, manager, vendor — because "a
recruiter's view of résumés" and "a candidate coach's view of résumés" are different evidence and the
surface must be able to say which one it is holding.
"""
from __future__ import annotations

import logging

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import expert_feed_doc
from ..voices_sources import by_kind
from ._feed import parse_feed
from ._http import HttpStrategy

# A feed that fails silently turns a whole source off with no trace. One naive datetime once
# emptied every podcast in a run, and the only symptom was a zero in a job result.
log = logging.getLogger(__name__)

VOICES = by_kind("essay")
TOTAL_CAP = 60


class PractitionerEssayConnector:
    key = "practitioner_essay"

    def __init__(self, *, items: list[dict] | None = None, page_size: int = 30):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for r in (items or []):
            if expert_feed_doc.item_id(r):
                self._by_id[expert_feed_doc.item_id(r)] = r

    @staticmethod
    def _facets(rec: dict, writer: str, role: str, audience: str = "", publication: str = "") -> dict:
        f = dict(expert_feed_doc.facets(rec))
        # The feed's own byline wins; the curated writer fills in when a feed omits one (many do).
        if not f.get("author") and writer:
            f["author"] = writer
        if role:
            f["voice_role"] = role
        if audience:
            f["audience"] = audience          # who this material is FOR — the surface's one filter
        if publication and not f.get("publication"):
            f["publication"] = publication
        f["entity_type"] = "essay"
        f["voices"] = "1"                     # this essay entered through Voices, not the research corpus
        return {k: v for k, v in f.items() if v}

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        window = window or {}
        if self._by_id and not (window.get("query") or "").strip():
            items = [(r, r.get("_writer", ""), r.get("_role", "")) for r in self._by_id.values()]
        else:
            limit = int(window.get("limit", self._page_size) or self._page_size)
            per = max(1, limit // max(1, len(VOICES)))
            items = []
            for src in VOICES:
                try:                                       # best-effort: one bad feed ≠ dead batch
                    recs = parse_feed(await self.fetch_strategy.fetch(src.feed))
                except Exception as e:                     # noqa: BLE001
                    log.warning("practitioner_essay: %s failed: %s: %s", src.label, type(e).__name__, e)
                    continue
                for r in recs[:per]:
                    if expert_feed_doc.item_id(r):
                        r["_writer"], r["_role"] = src.author, src.role
                        r["_audience"], r["_publication"] = src.audience, src.label
                        items.append((r, src.author, src.role))
                if len(items) >= TOTAL_CAP:
                    break
            items = items[:TOTAL_CAP]
            for r, _w, _ro in items:
                self._by_id[expert_feed_doc.item_id(r)] = r
        return [EntityRef(source_key=self.key, native_id=expert_feed_doc.item_id(r),
                          title=expert_feed_doc.title(r),
                          facets=self._facets(r, w, ro, r.get("_audience", ""), r.get("_publication", "")))
                for r, w, ro in items if expert_feed_doc.item_id(r)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_id.get(entity.native_id)
        facets = (self._facets(r, r.get("_writer", ""), r.get("_role", ""),
                              r.get("_audience", ""), r.get("_publication", "")) if r else dict(entity.facets))
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown", facets=facets,
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return expert_feed_doc.to_markdown(r).encode("utf-8")
