"""Podcast-transcript connector — genuine human/expert DISCUSSION (the "human wisdom" gap).

A FEED connector over a CURATED SHOW ALLOWLIST of podcast RSS feeds. Each RSS <item> is an episode; a
transcript exists ONLY if the item carries a Podcasting-2.0 `<podcast:transcript url="..." type="..."/>`
element (namespace https://podcastindex.org/namespace/1.0).

PROVENANCE / ToS (non-negotiable): we ingest ONLY the transcript the PUBLISHER provides via that element
(or the VTT/SRT/plain/JSON URL the feed links). We NEVER machine-transcribe audio and NEVER scrape YouTube.
discover_entities emits one EntityRef per episode THAT HAS a transcript element — episodes without one are
SKIPPED, so the transcript-only contract is enforced at discovery, structurally.

discover_entities → fetch each allowlisted feed, parse <item>s, keep only transcript-bearing ones (or use
injected fixtures). list_documents → one document per episode. fetch_artifact → download the transcript URL,
normalize by type (VTT/SRT/plain/JSON → prose) → podcast_doc.to_markdown bytes. Tests inject `episodes`
(carrying `transcript_text`) so they run fully offline.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import podcast_doc
from ._http import HttpStrategy

# Podcasting-2.0 namespace that carries the <podcast:transcript> element.
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

# --- CURATED SHOW ALLOWLIST -------------------------------------------------------------------------
# Deep-tech podcasts whose PUBLISHERS provide transcripts. Every show here MUST be verified to emit
# <podcast:transcript> before it is trusted — the connector ENFORCES this at discovery (episodes with no
# transcript element are skipped), so an entry that stops shipping transcripts simply yields zero
# episodes rather than bad data. The list is CURATABLE: only transcript-providing deep-tech shows belong;
# add/remove freely. Placeholders (commented) are candidates pending a transcript-tag verification.
SHOWS: list[str] = [
    # Latent Space — AI engineering deep-dives (Substack podcast feed).
    "https://api.substack.com/feed/podcast/1084089.rss",
    # The Changelog network is the reference implementation of the <podcast:transcript> tag — it ships a
    # publisher transcript on every episode. These are safe, verified transcript providers.
    "https://changelog.com/podcast/feed",        # The Changelog — software deep-dives
    "https://changelog.com/practicalai/feed",    # Practical AI — ML/AI in practice
    "https://changelog.com/gotime/feed",         # Go Time — the Go language / systems
    "https://changelog.com/jsparty/feed",        # JS Party — the web platform
    "https://changelog.com/shipit/feed",         # Ship It — platform engineering / infra
    "https://changelog.com/friends/feed",        # Changelog & Friends — engineering culture
    # --- pending transcript-tag verification (uncomment ONLY after confirming <podcast:transcript>) ----
    # "https://api.substack.com/feed/podcast/69345.rss",     # Dwarkesh Podcast (verify tag)
    # "https://thegradientpub.substack.com/feed/podcast",    # The Gradient (verify tag)
    # "https://feeds.megaphone.fm/MLN2155636147",            # a16z / other deep-tech show (verify tag)
    # ... add ONLY shows verified to expose a publisher-provided transcript element.
]

TOTAL_CAP = 30   # cap total episodes emitted across all feeds in one discovery pass


class PodcastConnector:
    key = "podcast"

    def __init__(self, *, episodes: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for ep in (episodes or []):
            eid = podcast_doc.episode_id(ep)
            if eid:
                self._by_id[eid] = ep

    @staticmethod
    def _parse_feed(raw: bytes | str) -> tuple[str, list[dict]]:
        """Parse an RSS feed → (show_title, [episode dict]). KEEPS ONLY items with a
        <podcast:transcript> element (the transcript-only contract, enforced structurally).
        """
        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is None:
            channel = root
        show_title = " ".join((channel.findtext("title") or "").split())
        episodes: list[dict] = []
        for item in channel.findall("item"):
            tr = item.find(f"{{{PODCAST_NS}}}transcript")
            if tr is None:
                continue                              # no publisher transcript → SKIP (ToS/provenance)
            turl = (tr.get("url") or "").strip()
            if not turl:
                continue
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or "").strip()
            episodes.append({
                "guid": guid or link,
                "link": link,
                "title": " ".join((item.findtext("title") or "").split()),
                "show": show_title,
                "pubDate": (item.findtext("pubDate") or "").strip(),
                "transcript_url": turl,
                "transcript_type": (tr.get("type") or "").strip(),
            })
        return show_title, episodes

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        window = window or {}
        if not window.get("query") and self._by_id:
            eps = list(self._by_id.values())
        else:
            cap = int(window.get("limit", TOTAL_CAP))
            feeds = window.get("feeds") or SHOWS
            eps = []
            for url in feeds:
                if len(eps) >= cap:
                    break
                try:                                  # per-feed isolation — one bad feed can't kill discovery
                    _, feed_eps = self._parse_feed(await self.fetch_strategy.fetch(url))
                except Exception:
                    continue
                for ep in feed_eps:
                    eps.append(ep)
                    if len(eps) >= cap:
                        break
            for ep in eps:
                eid = podcast_doc.episode_id(ep)
                if eid:
                    self._by_id[eid] = ep
        return [EntityRef(source_key=self.key, native_id=podcast_doc.episode_id(ep),
                          title=podcast_doc.title(ep), facets=podcast_doc.facets(ep))
                for ep in eps if podcast_doc.episode_id(ep)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        ep = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=podcast_doc.facets(ep) if ep else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        ep = self._by_id.get(doc.native_id) or {"guid": doc.native_id, "title": doc.title}
        # Fixtures carry transcript_text; live use downloads the publisher transcript URL.
        if not (ep.get("transcript_text") or "").strip() and ep.get("transcript_url"):
            try:
                raw = await self.fetch_strategy.fetch(ep["transcript_url"])
                ep = {**ep, "transcript_text": raw.decode("utf-8", "replace")}
            except Exception:
                ep = {**ep, "transcript_text": ""}
        if not (ep.get("transcript_text") or "").strip():
            # Rare race: discovery filtered on the transcript element but the URL failed to fetch.
            # Emit a clearly-labeled metadata-only doc rather than a silent empty artifact.
            return self._unavailable_markdown(ep).encode("utf-8")
        return podcast_doc.to_markdown(ep).encode("utf-8")

    @staticmethod
    def _unavailable_markdown(ep: dict) -> str:
        parts = [f"# {podcast_doc.title(ep)}", ""]
        parts += [
            f"{podcast_doc.show(ep)} — podcast episode  "
            "(publisher-provided transcript UNAVAILABLE at fetch time — episode skipped)",
            "",
        ]
        if ep.get("link"):
            parts += [f"Link: {ep['link']}", ""]
        return "\n".join(parts).strip() + "\n"
