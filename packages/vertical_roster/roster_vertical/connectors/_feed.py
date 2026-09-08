"""Shared RSS/Atom feed parsing (stdlib only) for the feed-style connectors (expert_feed, eng_blog).

XML is parsed with stdlib xml.etree.ElementTree, matching by tag LOCAL name so RSS (<item>) and Atom
(<entry>) — and their content:/dc:/atom: namespaces — both parse. Full article text lands only if the
feed provides content:encoded (RSS), an inline <content type="html">/CDATA (Atom), or a full-text
<description>; Atom type="xhtml" (nested element children, no .text) yields an EMPTY body here — such
feeds are excluded at curation time, not parsed specially.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def _local(tag: str) -> str:
    """Tag local name, namespace-stripped (`{ns}entry` → `entry`)."""
    return tag.rsplit("}", 1)[-1]


def _find(el, name: str):
    for ch in el:
        if _local(ch.tag) == name:
            return ch
    return None


def _findall(el, name: str) -> list:
    return [ch for ch in el if _local(ch.tag) == name]


def _text(el, name: str) -> str:
    ch = _find(el, name)
    return (ch.text or "").strip() if (ch is not None and ch.text) else ""


def _enclosure(el) -> str:
    """The media URL an item encloses, or "". Several podcast feeds ship NO <link> at all — No
    Priors, 20VC, This Week in Startups and The Startup Ideas Podcast all omit it — so the audio
    enclosure is the only address the publisher gives for an episode."""
    for ch in el:
        if _local(ch.tag) == "enclosure":
            href = ch.get("url") or ch.get("href")
            if href:
                return href.strip()
        if _local(ch.tag) == "link" and (ch.get("rel") or "") == "enclosure" and ch.get("href"):
            return ch.get("href").strip()
    return ""


def _image(el) -> str:
    """The item's own artwork: <itunes:image href> (podcasts) or <media:thumbnail url> (video)."""
    for ch in el:
        n = _local(ch.tag)
        if n in ("image", "thumbnail"):
            href = ch.get("href") or ch.get("url")
            if href:
                return href.strip()
        if n == "group":                       # media:group wraps media:thumbnail on YouTube
            inner = _image(ch)
            if inner:
                return inner
    return ""


def channel_meta(raw: bytes) -> dict:
    """Show-level title and artwork. Every episode of a show shares its cover when it has no own
    artwork, and a card with no picture next to cards with pictures looks broken rather than plain."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    node = _find(root, "channel") or root
    out = {"title": _text(node, "title")}
    img = ""
    for ch in node:
        n = _local(ch.tag)
        if n == "image":
            img = (ch.get("href") or "").strip() or _text(ch, "url")
            if img:
                break
    if img:
        out["image"] = img
    return {k: v for k, v in out.items() if v}


def _rss_item(it, publication: str) -> dict:
    link = _text(it, "link")
    guid = _text(it, "guid")
    author = _text(it, "creator") or _text(it, "author")     # dc:creator local name = "creator"
    content = ""
    for ch in it:
        if _local(ch.tag) == "encoded" and ch.text:          # content:encoded
            content = ch.text
            break
    return {
        "id": guid or link,
        "guid": guid,
        "link": link,
        "title": _text(it, "title"),
        "author": author,
        "publication": publication,
        "published": _text(it, "pubDate") or _text(it, "date"),
        "summary": _text(it, "description"),
        "content": content,
        "enclosure": _enclosure(it),
        "image": _image(it),
    }


def _atom_entry(e, publication: str) -> dict:
    link = ""
    for ch in e:
        if _local(ch.tag) == "link":
            href = ch.get("href")
            if not href:
                continue
            rel = ch.get("rel") or "alternate"
            if rel == "alternate":
                link = href
                break
            if not link:
                link = href
    author = ""
    au = _find(e, "author")
    if au is not None:
        author = _text(au, "name")
    return {
        "id": _text(e, "id") or link,
        "guid": _text(e, "id"),
        "link": link,
        "title": _text(e, "title"),
        "author": author,
        "publication": publication,
        "published": _text(e, "published") or _text(e, "updated"),
        "summary": _text(e, "summary"),
        "content": _text(e, "content"),
        "enclosure": _enclosure(e),
        "image": _image(e),
    }


def parse_feed(raw: bytes) -> list[dict]:
    """Parse RSS (<item>) or Atom (<entry>) into normalized item dicts (stdlib only, best-effort)."""
    root = ET.fromstring(raw)
    if _local(root.tag) == "feed":                            # Atom
        publication = _text(root, "title")
        return [_atom_entry(e, publication) for e in _findall(root, "entry")]
    channel = _find(root, "channel") or root                  # RSS 2.0 / RDF
    publication = _text(channel, "title")
    return [_rss_item(it, publication) for it in _findall(channel, "item")]
