"""LOCAL SCOPE — results default to where the user is (their metro), expandable to their state or
all of the US. Two halves:

  1. Where is the user? `/geo/me`: the browser's lat/lon when it offers one (nearest US metro by
     centroid), else the request IP via a best-effort IP-geolocation lookup (cached per IP). Never
     stored server-side beyond the cache; the choice lives in the user's browser.
  2. What is local? Code-owned checks over what we HOLD: a person's metro/state/country facets, a
     job's location text. The recall rule from the country scope carries over — a person or job we
     CANNOT place is KEPT (unknown ≠ elsewhere); only the clearly-elsewhere are dropped, and the
     confirmed-local lead. A location the query itself names always wins over the selector.

App-level module (metro/state vocabulary comes from the vertical); the kernel stays domain-free.
"""
from __future__ import annotations

import logging
import math
import re
import time

from roster_vertical.people_facets import METRO_ALIAS, METRO_COUNTRY, US_METROS, US_STATES

_log = logging.getLogger("roster.geo")

_STATE_CODE_RX = re.compile(r",\s*([A-Z]{2})\b")
_REMOTE_RX = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)
_US_STATE_NAMES = {v.lower(): k for k, v in US_STATES.items()}


# --------------------------------------------------------------------------- #
# Where is the user?                                                          #
# --------------------------------------------------------------------------- #
def nearest_us_metro(lat: float, lon: float, *, max_km: float = 120.0) -> tuple[str, float]:
    """(metro key, distance km) of the closest US metro centroid, or ('', d) when none is within
    `max_km` (the user is in the US but not near a metro we name → state-level scope)."""
    best, bd = "", 1e9
    for k, m in US_METROS.items():
        d = _haversine(lat, lon, m["lat"], m["lon"])
        if d < bd:
            best, bd = k, d
    return (best if bd <= max_km else ""), round(bd, 1)


def _haversine(a1, o1, a2, o2) -> float:
    r = 6371.0
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = math.radians(a2 - a1), math.radians(o2 - o1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


_IP_CACHE: dict[str, tuple[float, dict]] = {}
_IP_TTL = 24 * 3600


def client_ip(headers, fallback: str = "") -> str:
    xff = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (headers.get("x-real-ip") or "") or fallback


def _is_private(ip: str) -> bool:
    return (not ip or ip.startswith(("10.", "192.168.", "127.", "::1", "fc", "fd", "100.64."))
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip) is not None)


def lookup_ip(ip: str) -> dict | None:
    """Best-effort IP geolocation (ipapi.co, keyless, cached 24h). None for private/unknown."""
    if _is_private(ip):
        return None
    hit = _IP_CACHE.get(ip)
    if hit and time.time() - hit[0] < _IP_TTL:
        return hit[1]
    import json
    import urllib.request
    try:
        req = urllib.request.Request(f"https://ipapi.co/{ip}/json/", headers={"User-Agent": "roster-geo/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            d = json.load(r)
        if d.get("error"):
            return None
        out = {"country": (d.get("country_code") or "").lower(), "state": (d.get("region_code") or "").lower(),
               "city": d.get("city") or "", "lat": d.get("latitude"), "lon": d.get("longitude")}
        _IP_CACHE[ip] = (time.time(), out)
        return out
    except Exception as e:  # noqa: BLE001 — geolocation is a convenience, never a failure
        _log.info("ip lookup failed: %s", e)
        return None


def resolve_scope(*, lat=None, lon=None, ip: str = "") -> dict:
    """{country, state, metro, metro_label, state_label, city, source}. Metro only inside the US."""
    out = {"country": "", "state": "", "metro": "", "metro_label": "", "state_label": "", "city": "", "source": ""}
    if lat is not None and lon is not None:
        try:
            la, lo = float(lat), float(lon)
            m, _d = nearest_us_metro(la, lo)
            if m:
                out.update({"country": "us", "metro": m, "state": US_METROS[m]["state"], "source": "browser"})
        except (TypeError, ValueError):
            pass
    if not out["source"] and ip:
        g = lookup_ip(ip)
        if g:
            out.update({"country": g["country"], "state": g["state"] if g["country"] == "us" else "",
                        "city": g["city"], "source": "ip"})
            if g["country"] == "us" and g.get("lat") is not None:
                m, _d = nearest_us_metro(float(g["lat"]), float(g["lon"]))
                out["metro"] = m
                if m and not out["state"]:
                    out["state"] = US_METROS[m]["state"]
    if out["metro"]:
        out["metro_label"] = US_METROS[out["metro"]]["label"]
    if out["state"]:
        out["state_label"] = US_STATES.get(out["state"], out["state"].upper())
    return out


# --------------------------------------------------------------------------- #
# What is local? (code-owned checks over what we hold)                         #
# --------------------------------------------------------------------------- #
def canon_metro(v: str) -> str:
    v = (v or "").strip().lower().replace(" ", "_")
    return METRO_ALIAS.get(v, v)


def person_geo_status(facet_rows: list[dict], *, metro: str = "", state: str = "") -> str:
    """'in' (confirmed local), 'out' (clearly elsewhere), or 'unknown' (nothing placeable — KEPT)."""
    if not metro and not state:
        return "in"
    metros, states, countries = set(), set(), set()
    for f in facet_rows or []:
        k = f.get("facet_key"); v = (f.get("value_norm") or f.get("facet_value_norm") or "").strip().lower()
        if not v:
            continue
        if k == "metro":
            metros.add(canon_metro(v))
        elif k == "state":
            states.add(v)
        elif k == "country":
            countries.add(v)
    for m in metros:
        c = METRO_COUNTRY.get(m)
        if c:
            countries.add(c)
    want_state = state or (US_METROS.get(metro, {}).get("state") if metro else "")
    if countries and "us" not in countries:
        return "out"
    if metro:
        if metro in metros:
            return "in"
        if any(m in US_METROS and m != metro for m in metros):
            return "out"                                   # a different known US metro
        if states and want_state and want_state not in states:
            return "out"
        if states and want_state in states:
            return "unknown"                               # right state, metro not recorded
        return "unknown"
    # state scope
    if want_state in states:
        return "in"
    if any(US_METROS.get(m, {}).get("state") == want_state for m in metros):
        return "in"
    if states or any(m in US_METROS for m in metros):
        return "out"                                       # placed in another state
    return "unknown"


def job_geo_status(location: str, *, metro: str = "", state: str = "") -> str:
    """'in' | 'out' | 'remote' | 'unknown' for a job's free-text location."""
    if not metro and not state:
        return "in"
    loc = " " + re.sub(r"\s+", " ", (location or "").lower()) + " "
    want_state = state or (US_METROS.get(metro, {}).get("state") if metro else "")
    codes = {c.lower() for c in _STATE_CODE_RX.findall(location or "")} & set(US_STATES)
    names = {k for n, k in _US_STATE_NAMES.items() if re.search(r"\b" + re.escape(n) + r"\b", loc)}
    found_states = codes | names
    if metro:
        cities = US_METROS[metro]["cities"]
        if any(re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", loc) for c in cities):
            return "in"
        for k, m in US_METROS.items():
            if k != metro and any(re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", loc) for c in m["cities"] if len(c) > 3):
                return "out"
    elif want_state in found_states:
        return "in"
    if found_states and want_state not in found_states:
        return "out"
    for cc, rx in _foreign_rx():
        if rx.search(location or ""):
            return "out"
    if _REMOTE_RX.search(location or ""):
        return "remote"
    return "unknown"


_FOREIGN = None


def _foreign_rx():
    global _FOREIGN
    if _FOREIGN is None:
        from api.people_population import _COUNTRY_RX
        _FOREIGN = [(c, rx) for c, rx in _COUNTRY_RX.items() if c != "us"]
    return _FOREIGN


def partition_local(items: list, status_of) -> tuple[list, dict]:
    """Drop the clearly-elsewhere; lead with the confirmed-local; keep remote/unknown after.
    Returns (items, counts{in, remote, unknown, out})."""
    inn, rem, unk, out = [], [], [], 0
    for it in items or []:
        s = status_of(it)
        if s == "in":
            inn.append(it)
        elif s == "remote":
            rem.append(it)
        elif s == "out":
            out += 1
        else:
            unk.append(it)
    return inn + rem + unk, {"in": len(inn), "remote": len(rem), "unknown": len(unk), "out": out}


def scope_label(metro: str = "", state: str = "") -> str:
    if metro and metro in US_METROS:
        return US_METROS[metro]["label"]
    if state:
        return US_STATES.get(state, state.upper())
    return ""


def scope_statement(kind: str, metro: str, state: str, counts: dict) -> str:
    """Plain coverage sentence for the local scope (people or jobs)."""
    lab = scope_label(metro, state)
    if not lab:
        return ""
    st = US_STATES.get(US_METROS.get(metro, {}).get("state", ""), "") if metro else ""
    wider = f"Expand to {st} or all US from the scope selector." if metro and st else "Expand to all US from the scope selector."
    if kind == "jobs":
        return (f"{counts.get('in', 0)} roles located there lead; {counts.get('remote', 0)} remote and "
                f"{counts.get('unknown', 0)} unplaced follow; {counts.get('out', 0)} elsewhere were left out. {wider}")
    return (f"{counts.get('in', 0)} people placed there lead; {counts.get('unknown', 0)} with no "
            f"location on record are kept (they may be local); {counts.get('out', 0)} placed elsewhere were left out. {wider}")


def location_regex(metro: str = "", state: str = "") -> str:
    """A PostgreSQL case-insensitive regex matching job locations INSIDE the scope (metro city names,
    or the state code / name) — the recall query for local roles. '' when no scope."""
    parts: list[str] = []
    if metro and metro in US_METROS:
        cities = [re.escape(c) for c in US_METROS[metro]["cities"] if len(c) >= 4]
        parts.append(r"(^|[^a-z])(" + "|".join(cities) + r")([^a-z]|$)")
    st = state or (US_METROS.get(metro, {}).get("state") if metro else "")
    if st and not metro:
        parts.append(r",\s*" + re.escape(st.upper()) + r"([^a-z]|$)")
        name = US_STATES.get(st, "")
        if name:
            parts.append(r"(^|[^a-z])" + re.escape(name) + r"([^a-z]|$)")
    return "|".join(parts)
