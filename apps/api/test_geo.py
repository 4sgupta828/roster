"""Local scope: nearest metro, person/job locality checks (unknown is kept, elsewhere dropped)."""
from __future__ import annotations

from api.geo import (job_geo_status, nearest_us_metro, partition_local, person_geo_status, resolve_scope,
                     scope_statement)


def test_nearest_metro_and_scope_resolution():
    assert nearest_us_metro(37.77, -122.42)[0] == "bay_area"          # San Francisco
    assert nearest_us_metro(37.39, -122.08)[0] == "bay_area"          # Mountain View
    assert nearest_us_metro(47.62, -122.35)[0] == "seattle"
    assert nearest_us_metro(44.06, -121.31)[0] == ""                  # Bend, OR: no metro within 120 km
    s = resolve_scope(lat=37.44, lon=-122.14)
    assert s["metro"] == "bay_area" and s["state"] == "ca" and s["metro_label"] == "Bay Area" and s["source"] == "browser"
    assert resolve_scope(ip="10.0.0.1")["source"] == ""              # private IP: nothing


def _f(**kv):
    return [{"facet_key": k, "value_norm": v} for k, v in kv.items()]


def test_person_locality_keeps_unknown_and_drops_elsewhere():
    m = dict(metro="bay_area")
    assert person_geo_status(_f(metro="san_francisco"), **m) == "in"          # alias → canonical
    assert person_geo_status(_f(metro="bay_area", country="us"), **m) == "in"
    assert person_geo_status(_f(metro="seattle"), **m) == "out"
    assert person_geo_status(_f(state="wa"), **m) == "out"
    assert person_geo_status(_f(state="ca"), **m) == "unknown"                # right state, no metro
    assert person_geo_status(_f(country="us"), **m) == "unknown"
    assert person_geo_status(_f(country="de"), **m) == "out"
    assert person_geo_status(_f(metro="berlin"), **m) == "out"                # metro implies country
    assert person_geo_status([], **m) == "unknown"
    s = dict(state="ca")
    assert person_geo_status(_f(metro="los_angeles"), **s) == "in"
    assert person_geo_status(_f(state="ca"), **s) == "in"
    assert person_geo_status(_f(metro="nyc"), **s) == "out"
    assert person_geo_status(_f(role="x"), **s) == "unknown"
    assert person_geo_status(_f(metro="nyc")) == "in"                         # no scope: everything is in


def test_job_locality():
    m = dict(metro="bay_area")
    assert job_geo_status("San Francisco, CA", **m) == "in"
    assert job_geo_status("Mountain View, California", **m) == "in"
    assert job_geo_status("Seattle, WA", **m) == "out"
    assert job_geo_status("Austin, TX", **m) == "out"
    assert job_geo_status("Remote - US", **m) == "remote"
    assert job_geo_status("", **m) == "unknown"
    assert job_geo_status("London, United Kingdom", **m) == "out"
    assert job_geo_status("Berlin", **m) == "out"
    s = dict(state="ca")
    assert job_geo_status("Los Angeles, CA", **s) == "in" and job_geo_status("New York, NY", **s) == "out"
    assert job_geo_status("California", **s) == "in"


def test_partition_and_statement():
    jobs = [{"location": "Seattle, WA"}, {"location": "Remote"}, {"location": ""}, {"location": "Palo Alto, CA"}]
    rows, c = partition_local(jobs, lambda j: job_geo_status(j["location"], metro="bay_area"))
    assert [r["location"] for r in rows] == ["Palo Alto, CA", "Remote", ""] and c == {"in": 1, "remote": 1, "unknown": 1, "out": 1}
    st = scope_statement("jobs", "bay_area", "", c)
    assert st.startswith("1 roles located there lead") and "Expand to California or all US" in st
    assert scope_statement("people", "", "wa", {"in": 3, "unknown": 2, "out": 1}).startswith("3 people placed there lead")
    assert scope_statement("people", "", "", {}) == ""


def test_apply_job_scope_leads_local_keeps_remote_and_respects_query_location():
    from api.people_population import apply_job_scope
    jobs = [{"location": "Berlin, Germany"}, {"location": "Seattle, WA"}, {"location": "Remote"},
            {"location": "San Jose, CA"}, {"location": ""}]
    rows, gs = apply_job_scope(jobs, country="us", metro="bay_area")
    assert [r["location"] for r in rows] == ["San Jose, CA", "Remote", ""]     # Berlin (country) + Seattle (metro) out
    assert rows[0].get("local") is True and gs["label"] == "Bay Area" and gs["counts"]["in"] == 1
    assert gs["state"] == "ca" and gs["state_label"] == "California" and "1 roles located there lead" in gs["statement"]
    rows2, gs2 = apply_job_scope(jobs, country="us", metro="bay_area", query_location="Seattle")
    assert gs2 is None and len(rows2) == 5                                     # the query's own place wins
    rows3, gs3 = apply_job_scope(jobs, country="de", metro="bay_area")
    assert gs3 is None and [r["location"] for r in rows3] == ["Berlin, Germany", "Remote", ""]   # non-US country: no metro scope


def test_location_regex_matches_scope_only():
    import re
    from api.geo import location_regex
    rx = re.compile(location_regex("bay_area"), re.I)
    assert rx.search("San Francisco, CA") and rx.search("Mountain View, California") and rx.search("SF Bay Area")
    assert not rx.search("Seattle, WA") and not rx.search("Remote - US")
    rs = re.compile(location_regex(state="ca"), re.I)
    assert rs.search("Los Angeles, CA") and rs.search("Sacramento, California")
    assert not rs.search("Toronto, Canada") and not rs.search("New York, NY")
    assert location_regex() == ""
