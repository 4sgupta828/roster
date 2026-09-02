"""Public artifacts (evidence-model-v2 step 1): parsers, summary, packet folding, CSV, fail-safety."""
from __future__ import annotations

import asyncio
import csv
import io

from api.artifacts import (apply_artifacts_to_packet, artifact_lines, attach_artifacts,
                           footprint_coverage, github_org_artifact, github_repo_artifact,
                           openalex_work_artifact, select_repos, summarize_artifacts)
from api.evidence import evidence_packet
from api.maps import build_csv

_WORK = {
    "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/abc",
    "display_name": "Attention Is Enough", "publication_year": 2023, "publication_date": "2023-06-01",
    "type": "article", "cited_by_count": 42,
    "primary_location": {"source": {"display_name": "NeurIPS"}, "landing_page_url": "https://x"},
    "authorships": [
        {"author": {"id": "https://openalex.org/A1"}, "author_position": "first",
         "institutions": [{"display_name": "Acme Research"}]},
        {"author": {"id": "https://openalex.org/A2"}, "author_position": "last",
         "institutions": [{"display_name": "Other U"}]},
    ],
}


def test_openalex_work_first_author_affiliation_and_date():
    a = openalex_work_artifact(_WORK, "A1")
    assert a["kind"] == "paper" and a["artifact_key"] == "W123"
    assert a["role"] == "first_author" and a["detail"]["affiliations"] == ["Acme Research"]
    assert a["date"] == "2023-06-01" and a["venue"] == "NeurIPS" and a["url"].startswith("https://doi.org")
    b = openalex_work_artifact(_WORK, "A2")
    assert b["role"] == "author" and b["detail"]["affiliations"] == ["Other U"]
    assert openalex_work_artifact({"id": "https://openalex.org/W9"}, "A1") is None   # no title → nothing


def test_github_repo_skips_forks_and_private_and_ranks_by_stars():
    own = {"full_name": "ada/vecdb", "name": "vecdb", "owner": {"login": "ada"}, "html_url": "https://github.com/ada/vecdb",
           "stargazers_count": 120, "pushed_at": "2025-03-01T00:00:00Z", "language": "Rust", "fork": False}
    small = {**own, "full_name": "ada/notes", "name": "notes", "stargazers_count": 0, "pushed_at": "2025-08-01T00:00:00Z"}
    fork = {**own, "full_name": "ada/linux", "name": "linux", "fork": True, "stargazers_count": 999}
    priv = {**own, "full_name": "ada/secret", "name": "secret", "private": True}
    assert github_repo_artifact(fork, "ada") is None and github_repo_artifact(priv, "ada") is None
    kept = select_repos([small, fork, own, priv], "ada")
    assert [k["artifact_key"] for k in kept] == ["ada/vecdb", "ada/notes"]
    assert kept[0]["role"] == "owner" and kept[0]["venue"] == "Rust" and kept[0]["date"] == "2025-03-01"
    o = github_org_artifact({"login": "acme-ai", "description": "Acme"})
    assert o["kind"] == "org" and o["url"] == "https://github.com/acme-ai" and o["date"] is None


def test_summary_counts_newest_items_affiliations_and_scanned():
    rows = [openalex_work_artifact(_WORK, "A1"),
            openalex_work_artifact({**_WORK, "id": "https://openalex.org/W2", "display_name": "Older",
                                    "publication_date": "2019-01-01", "publication_year": 2019,
                                    "cited_by_count": 3}, "A1"),
            github_repo_artifact({"full_name": "ada/vecdb", "name": "vecdb", "owner": {"login": "ada"},
                                  "stargazers_count": 5, "pushed_at": "2025-03-01T00:00:00Z"}, "ada")]
    s = summarize_artifacts(rows, [{"source": "openalex", "n_total": 40}, {"source": "github", "n_total": 7}])
    assert s["counts"] == {"paper": 2, "repo": 1} and s["total"] == 3
    assert s["newest"] == "2025-03-01" and s["scanned"] == ["github", "openalex"]
    assert s["items"][0]["title"] == "Attention Is Enough" and s["items"][0]["stat"] == "42 citations"
    assert s["affiliations"][0] == {"name": "Acme Research", "n": 2, "years": [2019, 2023]}
    assert s["reported"] == {"openalex": 40, "github": 7}
    empty = summarize_artifacts([], [{"source": "github", "n_total": 0}])
    assert empty["total"] == 0 and empty["scanned"] == ["github"] and empty["newest"] is None


def test_packet_gains_artifact_backed_rung_freshness_and_gap():
    pk = evidence_packet([{"facet_key": "company", "value_norm": "acme", "document_id": "https://github.com/ada"}], "github:ada")
    assert pk["strength"] == "self_stated"
    s = summarize_artifacts([github_repo_artifact({"full_name": "ada/vecdb", "name": "vecdb",
                                                    "owner": {"login": "ada"}, "stargazers_count": 5,
                                                    "pushed_at": "2025-03-01T00:00:00Z"}, "ada")],
                            [{"source": "github", "n_total": 7}])
    apply_artifacts_to_packet(pk, s)
    assert pk["per_key"]["public_artifacts"]["type"] == "artifact_backed"
    assert "artifact_backed" in pk["types"] and pk["strength"] == "mixed"   # self-stated profile + real work
    assert pk["freshness"]["newest_artifact"] == "2025-03-01"
    assert "public_artifacts" not in pk["gaps"]
    # scanned, nothing found → an explicit gap; org-only membership is affiliation, not capability
    pk2 = evidence_packet([{"facet_key": "company", "value_norm": "acme", "document_id": "https://github.com/bo"}], "github:bo")
    apply_artifacts_to_packet(pk2, summarize_artifacts([], [{"source": "github", "n_total": 0}]))
    assert pk2["strength"] == "self_stated" and "public_artifacts" in pk2["gaps"]
    pk3 = evidence_packet([{"facet_key": "company", "value_norm": "acme", "document_id": "https://github.com/cy"}], "github:cy")
    apply_artifacts_to_packet(pk3, summarize_artifacts([github_org_artifact({"login": "acme"})], [{"source": "github"}]))
    assert pk3["strength"] == "self_stated" and "public_artifacts" not in pk3["gaps"]
    # unscanned → unknown: no gap, no freshness
    pk4 = evidence_packet([], "github:dd")
    apply_artifacts_to_packet(pk4, None)
    assert "public_artifacts" not in pk4["gaps"] and pk4["freshness"]["newest_artifact"] is None


def test_footprint_coverage_distinguishes_unscanned():
    rows = [{"artifacts": {"scanned": ["github"], "total": 2, "counts": {"repo": 2}, "newest": "2024-01-01"}},
            {"artifacts": {"scanned": ["github"], "total": 0, "counts": {}, "newest": None}},
            {"artifacts": {"scanned": [], "total": 0, "counts": {}, "newest": None}},
            {}]
    c = footprint_coverage(rows)
    assert c == {"people": 4, "with_artifacts": 1, "scanned": 2, "unscanned": 2,
                 "kinds": {"repo": 2}, "newest": "2024-01-01"}


def test_csv_carries_artifact_columns():
    m = {"rows": [{"entity_id": "github:ada", "name": "Ada", "attributes": [], "links": [],
                   "evidence": {"strength": "mixed", "types": [], "families": [], "corroborated_keys": [], "gaps": []},
                   "artifacts": {"scanned": ["github"], "counts": {"repo": 2, "paper": 1}, "total": 3,
                                 "newest": "2025-03-01",
                                 "items": [{"url": "https://github.com/ada/vecdb"}, {"url": "https://doi.org/10.1/abc"}]}},
                  {"entity_id": "github:bo", "name": "Bo", "attributes": [], "links": [], "evidence": {},
                   "artifacts": {"scanned": ["github"], "counts": {}, "total": 0, "newest": None, "items": []}}],
         "reviews": {}}
    rows = list(csv.DictReader(io.StringIO(build_csv(m))))
    assert rows[0]["public_artifacts"] == "paper:1|repo:2" and rows[0]["newest_artifact"] == "2025-03-01"
    assert "https://doi.org/10.1/abc" in rows[0]["artifact_links"]
    assert rows[1]["public_artifacts"] == "none found" and rows[1]["newest_artifact"] == ""


def test_attach_is_a_noop_without_a_pool_and_lines_render():
    rows = [{"entity_id": "github:ada", "evidence": {"strength": "self_stated"}}]
    # a stub store: nothing to attach, no raise. (A fresh loop, kept as current — sibling test
    # modules drive coroutines via get_event_loop(); asyncio.run() would leave none set.)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(attach_artifacts(object(), rows))
    assert "artifacts" not in rows[0]
    s = summarize_artifacts([openalex_work_artifact(_WORK, "A1")], [{"source": "openalex", "n_total": 1}])
    lines = artifact_lines(s)
    assert lines[0].startswith("- public artifacts") and "1 papers" in lines[0] and "newest 2023-06-01" in lines[0]
    assert any("Attention Is Enough" in l and "NeurIPS" in l for l in lines)
    assert any("affiliation on published work: Acme Research (2023, 1 works)" in l for l in lines)
    assert artifact_lines(None) == []
