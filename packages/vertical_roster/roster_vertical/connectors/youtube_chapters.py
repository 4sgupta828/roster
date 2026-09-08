"""YouTube connector — timestamped CHAPTER POINTERS from interview/careers channels (keyless).

WHY THIS EXISTS, having first concluded YouTube was closed. YouTube's caption endpoint is gated:
`captionTracks` is still in the watch page, but every format of its `baseUrl` returns zero bytes
without a signed token, so transcripts are genuinely unavailable and we do not scrape them. That is a
fact about CAPTIONS and says nothing about DESCRIPTIONS — which are public in the keyless channel feed
and, on these channels, carry the creator's own timestamped chapter list. Measured 2026-09-08 over the
newest 15 videos per channel: Engineering with Utsav 11, interviewing.io 10, ThinkSoftware 10,
Ken Jee 9, A Life Engineered 6.

This matters more here than for the sibling product: interview preparation is video-native. The
channels that teach system-design or behavioural interviews publish on YouTube, not in newsletters.

The unit is identical to a podcast's show notes — a creator-written marker and an offset — so it
grades the same way: `source_kind="chapter_pointer"`, tier "pointer", never evidence, never quoted. A
chapter deep-links to `watch?v=<id>&t=<seconds>`, which opens the creator's own video at that moment.
No audio, no video and no captions are ever fetched.

The channel feed is `https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>` — public, keyless,
and the newest 15 videos only. There is no back catalogue without the Data API, so the index grows as
the channels publish. Channel IDs are pinned in `voices_sources.py` because a handle can be
reassigned while an ID cannot.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import voices_doc
from ..voices_sources import by_kind
from ._http import HttpStrategy

# A feed that fails silently turns a whole source off with no trace. One naive datetime once
# emptied every podcast in a run, and the only symptom was a zero in a job result.
log = logging.getLogger(__name__)

CHANNELS = by_kind("youtube")
TOTAL_CAP = 90


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_channel(raw: bytes, channel_name: str = "") -> list[dict]:
    """Atom entries → the record shape `voices_doc` already understands.

    The description lives at `media:group/media:description`, NOT at the entry's own `description`,
    which is why an earlier probe of these feeds reported them empty and nearly cost us the source.
    """
    root = ET.fromstring(raw)
    feed_title = ""
    for ch in root:
        if _local(ch.tag) == "title" and ch.text:
            feed_title = ch.text.strip()
            break
    out: list[dict] = []
    for e in root:
        if _local(e.tag) != "entry":
            continue
        rec: dict = {"publication": channel_name or feed_title}
        vid = ""
        for ch in e:
            n = _local(ch.tag)
            if n == "videoId" and ch.text:
                vid = ch.text.strip()
            elif n == "title" and ch.text:
                rec["title"] = ch.text.strip()
            elif n == "published" and ch.text:
                rec["published"] = ch.text.strip()
            elif n == "group":
                for g in ch:
                    gn = _local(g.tag)
                    if gn == "description" and g.text:
                        rec["summary"] = g.text
                    elif gn == "thumbnail" and g.get("url"):
                        rec["image"] = g.get("url").strip()
                    elif gn == "community":
                        for cm in g:
                            if _local(cm.tag) == "statistics" and cm.get("views"):
                                rec["views"] = cm.get("views").strip()
        if not vid:
            continue
        rec["guid"] = f"youtube:{vid}"
        rec["link"] = f"https://www.youtube.com/watch?v={vid}"
        # every video has this thumbnail, whether or not the feed spelled one out
        rec.setdefault("image", f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
        out.append(rec)
    return out


class YoutubeChaptersConnector:
    key = "youtube_chapters"

    def __init__(self, *, videos: list[dict] | None = None, page_size: int = 15):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for r in (videos or []):
            if voices_doc.episode_id(r):
                self._by_id[voices_doc.episode_id(r)] = r

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        window = window or {}
        if self._by_id and not (window.get("query") or "").strip():
            items = list(self._by_id.values())
        else:
            limit = int(window.get("limit", self._page_size) or self._page_size)
            per = max(1, limit // max(1, len(CHANNELS)))
            items = []
            for src in CHANNELS:
                try:                                # best-effort: one dead channel ≠ dead batch
                    recs = parse_channel(await self.fetch_strategy.fetch(src.feed), src.label)
                except Exception as e:              # noqa: BLE001
                    log.warning("youtube_chapters: %s failed: %s: %s", src.label, type(e).__name__, e)
                    continue
                kept = 0
                for r in recs:
                    if kept >= per:
                        break
                    if voices_doc.chapters(str(r.get("summary") or "")):
                        # the source card travels with the video: WHO it is for and WHAT the speaker is
                        r["audience"], r["voice_role"], r["writer"] = src.audience, src.role, src.author
                        items.append(r)
                        kept += 1
                if len(items) >= TOTAL_CAP:
                    break
            items = items[:TOTAL_CAP]
            for r in items:
                self._by_id[voices_doc.episode_id(r)] = r
        # the chapter-only contract, enforced here on every path exactly as the podcast side does
        return [EntityRef(source_key=self.key, native_id=voices_doc.episode_id(r),
                          title=voices_doc.title(r), facets=voices_doc.facets(r))
                for r in items
                if voices_doc.episode_id(r) and voices_doc.chapters(str(r.get("summary") or ""))]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=voices_doc.facets(r) if r else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return voices_doc.to_markdown(r).encode("utf-8")
