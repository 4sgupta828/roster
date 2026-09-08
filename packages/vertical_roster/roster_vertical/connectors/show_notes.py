"""Show-notes connector — timestamped CHAPTER POINTERS from talent/careers podcasts (keyless RSS).

The companion to `podcast.py`, not a replacement. That connector ingests publisher TRANSCRIPTS and
skips any episode without one. The recruiting and job-search genre publishes neither transcripts nor,
mostly, anything else: measured 2026-09-08 over 98 shows found by topic search, 83% are TITLE-ONLY and
only 11% write a chapter list. What the good ones write is a timestamped marker — "01:59 What shaped
Victor's approach to solving TA problems at scale" — which is a real lesson-level unit and deep-links
to the exact moment.

So this connector ingests the chapter list and nothing else. `source_kind="chapter_pointer"` grades to
the "pointer" tier, which is NOT EVIDENCE: a chapter title is written by the show's producer and says
where to listen, never what was said. Audio is never fetched, never transcribed, never scraped.

The allowlist lives in `voices_sources.py` with the measurement that admitted each show, because in
this genre yield and relevance are different questions and both have to be answered.
"""
from __future__ import annotations

import logging

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import voices_doc
from ..voices_sources import by_kind
from ._feed import channel_meta, parse_feed
from ._http import HttpStrategy

# A feed that fails silently turns a whole source off with no trace. One naive datetime once
# emptied every podcast in a run, and the only symptom was a zero in a job result.
log = logging.getLogger(__name__)

SHOWS = by_kind("podcast")
TOTAL_CAP = 60          # cap episodes emitted per discovery pass


class ShowNotesConnector:
    key = "show_notes"

    def __init__(self, *, episodes: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for r in (episodes or []):
            if voices_doc.episode_id(r):
                self._by_id[voices_doc.episode_id(r)] = r

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        """One EntityRef per episode THAT HAS a chapter list. Episodes without one are skipped, so
        the chapter-only contract is enforced at discovery — structurally, not by later filtering."""
        window = window or {}
        if self._by_id and not (window.get("query") or "").strip():
            items = list(self._by_id.values())
        else:
            limit = int(window.get("limit", self._page_size) or self._page_size)
            per = max(1, limit // max(1, len(SHOWS)))
            items = []
            for src in SHOWS:
                try:                                        # best-effort: one bad feed ≠ dead batch
                    raw = await self.fetch_strategy.fetch(src.feed)
                    recs = parse_feed(raw)
                    meta = channel_meta(raw)
                    cover = meta.get("image", "")
                except Exception as e:                      # noqa: BLE001
                    log.warning("show_notes: %s failed: %s: %s", src.label, type(e).__name__, e)
                    continue
                kept = 0
                for r in recs:
                    if kept >= per:
                        break
                    if not voices_doc.episode_id(r):
                        continue
                    if not voices_doc.chapters(str(r.get("summary") or r.get("content") or "")):
                        continue                            # no chapter list → nothing to index
                    if cover and not r.get("image"):
                        r["image"] = cover                  # every episode of a show has its cover
                    r.setdefault("publication", src.label or meta.get("title", ""))
                    # the source card travels with the episode: WHO it is for and WHAT the speaker is
                    r["audience"], r["voice_role"], r["writer"] = src.audience, src.role, src.author
                    items.append(r)
                    kept += 1
                if len(items) >= TOTAL_CAP:
                    break
            items = items[:TOTAL_CAP]
            for r in items:
                self._by_id[voices_doc.episode_id(r)] = r
        # The chapter-only contract is enforced HERE, on every path — fixtures included. An episode
        # with no chapter list has nothing to point at, so it never becomes an entity.
        return [EntityRef(source_key=self.key, native_id=voices_doc.episode_id(r),
                          title=voices_doc.title(r), facets=voices_doc.facets(r))
                for r in items
                if voices_doc.episode_id(r)
                and voices_doc.chapters(str(r.get("summary") or r.get("content") or ""))]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=voices_doc.facets(r) if r else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return voices_doc.to_markdown(r).encode("utf-8")
