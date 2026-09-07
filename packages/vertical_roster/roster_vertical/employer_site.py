"""THE EMPLOYER'S OWN WEBSITE, read from the job board it publishes on (owner, 2026-09-07: "the company link is there on
the boards — click on name and it takes to company site").

The boards' public APIs do not return it, but the board PAGE does, in a place that is stable per ATS:
  · Ashby      — `window.__appData` embeds `organization.publicWebsite`
  · Lever      — the header logo links to the company site (the first external href)
  · Greenhouse — the classic board page's header link, when present

Pure functions over page HTML so they can be tested on saved fixtures; the fetching and the storing live in
`scripts/company_sites.py`. Anything uncertain yields None — a board link is never passed off as the company's site."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

# the ATS VENDORS themselves — their product sites as well as their board hosts. Greenhouse's board pages link to
# greenhouse.com in the footer, and without it every Greenhouse employer was given the vendor's site as its own.
_ATS_HOSTS = ("ashbyhq.com", "ashby.hq", "ashbyprd.com", "lever.co", "greenhouse.io", "greenhouse.com", "greenhouse-cdn.com",
              "careerpuck.com", "smartrecruiters.com", "myworkdayjobs.com", "workday.com", "workable.com", "recruitee.com",
              "jobvite.com", "bamboohr.com", "teamtailor.com", "pinpointhq.com", "rippling.com", "eightfold.ai", "icims.com",
              "jazzhr.com", "breezy.hr", "polymer.co", "gem.com", "paylocity.com", "adp.com", "ukg.com", "successfactors.com",
              "taleo.net", "oraclecloud.com", "brassring.com", "avature.net", "phenompeople.com", "jobs.net")
_JUNK_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "github.com",
               "glassdoor.com", "medium.com", "w3.org", "schema.org", "google.com", "apple.com", "gstatic.com",
               "cloudflare.com", "googleapis.com", "cookiebot.com", "onetrust.com", "indeed.com", "crunchbase.com")


def _is_company_site(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host or urlsplit(url).scheme not in ("http", "https"):
        return False
    return not any(host == h or host.endswith("." + h) for h in (*_ATS_HOSTS, *_JUNK_HOSTS))


def _clean(url: str) -> str | None:
    if not _is_company_site(url):
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}" + (p.path.rstrip("/") if p.path.rstrip("/") not in ("", "/") else "")


def from_ashby(html: str) -> str | None:
    """Ashby embeds the whole board state; the organization carries `publicWebsite`."""
    m = re.search(r'window\.__appData\s*=\s*(\{.*?\});?\s*</script>', html or "", re.S)
    if not m:
        m = re.search(r'"publicWebsite"\s*:\s*"([^"]+)"', html or "")
        return _clean(m.group(1)) if m else None
    try:
        data = json.loads(m.group(1))
    except Exception:   # noqa: BLE001
        m2 = re.search(r'"publicWebsite"\s*:\s*"([^"]+)"', m.group(1))
        return _clean(m2.group(1)) if m2 else None
    site = ((data.get("organization") or {}).get("publicWebsite") or "")
    return _clean(site)


def from_header_link(html: str) -> str | None:
    """Lever / Greenhouse and friends: the first external link in the page is the header logo → the company site."""
    for url in re.findall(r'href="(https?://[^"#]+)"', html or ""):
        c = _clean(url)
        if c:
            return c
    return None


_READERS = {"ashbyhq.com": from_ashby, "lever.co": from_header_link, "greenhouse.io": from_header_link,
            "job-boards.greenhouse.io": from_header_link, "boards.greenhouse.io": from_header_link}


def company_site(board_url: str, html: str) -> str | None:
    """The employer's own website from their board page, or None."""
    try:
        host = (urlsplit(board_url or "").hostname or "").lower()
    except ValueError:
        return None
    for suffix, reader in _READERS.items():
        if host == suffix or host.endswith("." + suffix):
            return reader(html)
    return None
