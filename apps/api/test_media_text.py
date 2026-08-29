"""Pasted-text attachments route to document context (not vision), UTF-8 safe and length-capped."""
import base64

from api.media import _MAX_DOC_CHARS, _kind, attachments_to_media, session_previews


def _att(text, name="Pasted text", mt="text/plain"):
    return {"name": name, "media_type": mt, "data": base64.b64encode(text.encode("utf-8")).decode()}


def test_text_kind_detected():
    assert _kind(_att("hello")) == "text"
    assert _kind({"name": "notes.txt", "media_type": "", "data": ""}) == "text"
    assert _kind({"name": "notes.md", "media_type": "", "data": ""}) == "text"


def test_pasted_text_becomes_a_doc_not_an_image():
    images, docs, notes = attachments_to_media([_att("A clinical history paragraph.")])
    assert images == [] and notes == []
    assert len(docs) == 1
    assert docs[0]["name"] == "Pasted text"
    assert docs[0]["text"] == "A clinical history paragraph."


def test_utf8_text_round_trips():
    s = "Blóðþrýstingur 140/90 — café μg/dL β-blocker"
    docs = attachments_to_media([_att(s)])[1]
    assert docs[0]["text"] == s


def test_long_text_is_capped():
    docs = attachments_to_media([_att("x" * (_MAX_DOC_CHARS + 5000))])[1]
    assert len(docs[0]["text"]) == _MAX_DOC_CHARS


def test_text_preview_is_a_document_chip():
    docs = attachments_to_media([_att("note")])[1]
    prev = session_previews([], docs)
    assert prev == [{"name": "Pasted text", "kind": "document"}]


def test_text_not_blocked_when_images_full():
    imgs = [{"name": f"i{i}", "media_type": "image/png",
             "data": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()} for i in range(4)]
    # even with 4 images already, a trailing text attachment must still land as a doc
    _, docs, _ = attachments_to_media(imgs + [_att("still attached")])
    assert any(d["text"] == "still attached" for d in docs)
