"""Live-people leg — offline tests (FakeRecordSearch + deterministic embed, no network/spend)."""
from __future__ import annotations

import asyncio

import api.live_people as lp
from roster_kernel.providers.record_search import ExternalRecord, FakeRecordSearch
from roster_vertical.live_people import normalize_record


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- normalizer ------------------------------------------------------------------------------------
def test_normalize_pdl_record_to_person_card():
    rec = ExternalRecord(id="p1", source="pdl", title="Ada Lovelace",
                         fields={"full_name": "Ada Lovelace", "job_title": "Staff ML Engineer",
                                 "job_company_name": "Acme", "location_locality": "san francisco",
                                 "location_country": "united states", "skills": ["python", "pytorch"],
                                 "linkedin_url": "https://linkedin.com/in/ada"})
    c = normalize_record(rec)
    assert c["entity_id"] == "live:pdl:p1"
    assert c["name"] == "Ada Lovelace"
    assert c["citation"] is None                      # never a grounded row
    assert c["source"] == "pdl" and c["found_by"] == "live:pdl"
    keys = {(a["key"], a["display"]) for a in c["attributes"]}
    assert ("title", "Staff ML Engineer") in keys and ("company", "Acme") in keys
    assert any(l["kind"] == "linkedin" for l in c["links"])


def test_normalize_exa_record_and_dropped_when_no_name():
    ok = normalize_record(ExternalRecord(id="u", source="exa", title="Grace Hopper — Compilers",
                                         url="https://ex.com/grace", text="systems"))
    assert ok["name"] == "Grace Hopper" and ok["citation"] is None and ok["links"][0]["kind"] == "web"
    assert normalize_record(ExternalRecord(id="x", source="exa", title="", url="", text="")) is None


# ---- merge / scoring -------------------------------------------------------------------------------
def _patch_embed(monkeypatch, mapping):
    def fake_embed_texts(texts, **kw):
        return [mapping.get((t or "").strip(), None) for t in texts]
    monkeypatch.setattr(lp, "embed_texts", fake_embed_texts)


def test_merge_scores_and_labels_live_rows(monkeypatch):
    _patch_embed(monkeypatch, {"python pytorch": "[1,0,0]"})
    rec = ExternalRecord(id="p1", source="pdl", title="Ada",
                         fields={"full_name": "Ada", "job_title": "senior ml engineer",
                                 "job_company_name": "Acme", "skills": ["python", "pytorch"],
                                 "summary": "python pytorch"})
    # normalize sets _live_fields.text = "senior ml engineer · Acme · python pytorch"; align embed key
    _patch_embed(monkeypatch, {"senior ml engineer · Acme · python pytorch".strip(): "[1,0,0]"})
    client = FakeRecordSearch({"python pytorch": [rec]})
    prefs = {"skills": ["python"], "seniorities": ["senior"], "search_text": "python pytorch"}
    out = _run(lp.merge_live_candidates("[1,0,0]", prefs, [], search_client=client))
    assert len(out) == 1
    c = out[0]
    assert c["live"] is True and c["citation"] is None
    assert c["_score"] > 0                                   # cosine(1,0,0 · 1,0,0)=1 → weighted+bonuses
    assert any("live" in r for r in c["reasons"])            # source label present
    assert any("senior" in r for r in c["reasons"]) and any("skills" in r for r in c["reasons"])


def test_merge_conservative_dedupe_drops_name_plus_company_collision(monkeypatch):
    _patch_embed(monkeypatch, {})   # embeds irrelevant here; dedupe happens before embed
    rec = ExternalRecord(id="p1", source="pdl", title="Ada",
                         fields={"full_name": "Ada", "job_company_name": "Acme", "skills": []})
    client = FakeRecordSearch({"senior ml engineer": [rec]})
    existing = [{"name": "Ada", "attributes": [{"key": "company", "display": "Acme"}]}]
    out = _run(lp.merge_live_candidates("[1,0,0]", {}, existing, search_client=client, query_text="senior ml engineer"))
    assert out == []                                         # same name+company as a corpus row → dropped


def test_merge_country_hard_filter(monkeypatch):
    _patch_embed(monkeypatch, {})
    rec = ExternalRecord(id="p1", source="pdl", title="Bob",
                         fields={"full_name": "Bob", "location_country": "canada", "skills": []})
    client = FakeRecordSearch({"senior ml engineer": [rec]})
    out = _run(lp.merge_live_candidates("[1,0,0]", {"country": "us"}, [], search_client=client, query_text="senior ml engineer"))
    assert out == []                                         # known-foreign dropped


def test_normalize_exa_structured_person_entity():
    rec = ExternalRecord(id="https://linkedin.com/in/adil", source="exa", title="Adil Mubeen",
        url="https://linkedin.com/in/adil", text="ML engineer",
        fields={"person": {"name": "Adil Mubeen", "location": "San Francisco, California, United States",
                           "workHistory": [{"title": "Senior Machine Learning Engineer",
                                            "company": {"name": "Aozic"}}]}})
    c = normalize_record(rec)
    attrs = {a["key"]: a["display"] for a in c["attributes"]}
    assert c["name"] == "Adil Mubeen"                 # clean name from the entity, not a page title
    assert attrs.get("title") == "Senior Machine Learning Engineer"
    assert attrs.get("company") == "Aozic"
    assert attrs.get("country") == "us"               # parsed from "…United States" → geo filter can act
    assert c["citation"] is None


def test_exa_foreign_profile_gets_country_and_is_droppable(monkeypatch):
    _patch_embed(monkeypatch, {})
    rec = ExternalRecord(id="https://linkedin.com/in/x", source="exa", title="Someone",
        url="https://linkedin.com/in/x",
        fields={"person": {"name": "Someone", "location": "Chennai, Tamil Nadu, India", "workHistory": []}})
    client = FakeRecordSearch({"senior ml engineer": [rec]})
    out = _run(lp.merge_live_candidates("[1,0,0]", {"country": "us"}, [], search_client=client, query_text="senior ml engineer"))
    assert out == []                                  # India profile now carries country=in → dropped for a US search


def test_merge_source_filter_restricts_to_picked_provider(monkeypatch):
    _patch_embed(monkeypatch, {"a": "[1,0,0]", "b": "[1,0,0]"})
    exa = ExternalRecord(id="u", source="exa", title="Grace", url="https://x",
                         fields={"person": {"name": "Grace", "location": "New York, United States"}})
    pdl = ExternalRecord(id="p", source="pdl", title="Ada",
                         fields={"full_name": "Ada", "job_company_name": "Acme"})
    client = FakeRecordSearch({"senior ml engineer": [exa, pdl]})
    # only exa requested → pdl row filtered out even though the client returned it
    out = _run(lp.merge_live_candidates("[1,0,0]", {}, [], search_client=client, sources=["exa"], query_text="senior ml engineer"))
    assert {c["source"] for c in out} == {"exa"}


def test_geo_detection_foreign_and_us():
    from roster_vertical.live_people import _country_from_location as cc
    assert cc("San Francisco, California, United States") == "us"
    assert cc("San Francisco Bay Area") == "us"
    assert cc("Greater Houston") == "us"
    assert cc("Manchester, New Hampshire") == "us"      # US state beats the UK-city name
    assert cc("Tehran, Tehran Province, Iran") == "ir"
    assert cc("Greater Bengaluru Area") == "in"
    assert cc("Argentina") == "ar"
    assert cc("") == ""


def test_flag_default_off():
    import os
    assert lp.live_people_enabled() == (os.environ.get("ROSTER_LIVE_PEOPLE", "").lower()
                                        in ("1", "true", "yes"))
