# Roster — Download Campaign: BLOCKED items (do after the easy tranches)

Living tracker. The easy/priority tranches run via `scripts/run_downloads.py` (prod-direct
`/admin/corpus/ingest`). These items are **blocked on external access or a missing connector** —
we do NOT block the campaign on them; we keep downloading everything else and come back here.

Status legend: 🔴 blocked (needs access/build) · 🟡 partial (works but throttled/thin) · 🟢 unblocked.

| # | Source | Value | Blocker | Unblock step | Status |
|---|--------|-------|---------|--------------|--------|
| 1 | **Patents (USPTO PatentsView)** | Granted patents = IP-moat / prior-art evidence (`primary_filing` when granted). | PatentsView **suspended new API-key grants** (confirmed Aug 2026); `connectors/patentsview.py` is DORMANT. | Switch source: **Google Patents Public Data** (BigQuery `patents-public-data`, no key, bulk) OR **EPO OPS** (free tier, register app) OR **USPTO bulk data** (PatentsView bulk TSV downloads, no key). Build a `google_patents`/`epo_ops` connector reusing the granted-vs-pending → tier mapping. | 🔴 |
| 2 | **Semantic Scholar (S2 Graph API)** | Citation graph + strong CS/AI coverage; complements OpenAlex. | ~~No connector~~ **BUILT** (`connectors/semantic_scholar.py`). Works KEYLESS with patient backoff (S2's shared pool 429s often); a free `ROSTER_S2_API_KEY` makes it fast/reliable. | Optional: set `ROSTER_S2_API_KEY` (free, instant form at semanticscholar.org) for per-key limits. DOI/arXiv-id facets emitted for later dedup. | 🟢 |
| 3 | **Crossref** | DOI/venue authority metadata + citation counts. | ~~No connector~~ **BUILT** (`connectors/crossref.py`, keyless REST, `mailto` polite pool). Grades journal/proceedings/book-chapter → `verified_structured`, `posted-content` → `technical_signal`; strips JATS abstracts; `doi` facet for dedup. | Done — running via the `crossref` tranche. | 🟢 |
| 4 | **News / sentiment — GDELT** | Market sentiment / news tone (`sentiment_signal`, lowest tier, never controlling). | GDELT hard-throttles bulk (≈1 req/5s, IP bans); connector is best-effort. **Deferred** — the keyless **Hacker News (Algolia)** connector (row 5) now covers the sentiment need. | Optional later: GDELT 2.0 DOC API in paced batches, labeled signal only. | 🟡 |
| 5 | **Hacker News** | Developer sentiment / launches (`sentiment_signal`). | ~~No connector~~ **BUILT** (`connectors/hackernews.py`, KEYLESS Algolia HN Search). Labeled market signal, never controlling. | Done — running via the `hn` tranche. **Keyless sentiment fallback for row 4.** | 🟢 |
| 6 | **Commercial DBs — Crunchbase / PitchBook / Dealroom / CB Insights** | Private funding rounds, cap tables, investor lists — the deepest private-diligence data. | Paid/licensed; no connector can legally fetch. | **Keyless fallbacks now cover the common cases:** **Form D** (private raises, live) + **Wikidata** (`connectors/wikidata.py` — founders, CEO, inception, industry, parent/subsidiary, ownership, ticker; reference tier). Still gated: cap tables, private financials, full investor lists → **recommend-only** in `gaps.py`. | 🟡 |
| 7 | **Product Hunt / Gartner / IDC** | Launch traction / analyst reports. | Paid or ToS-restricted. | Recommend-only. | 🔴 |
| 8 | **Reddit** | Broader-community SENTIMENT signal (r/MachineLearning, r/LocalLLaMA, r/startups, r/venturecapital…) — `sentiment_signal`, never controlling. | **CONNECTOR BUILT** (`connectors/reddit.py`, OAuth app-only). Keyless `.json` is dead — Reddit serves the HTML app shell to datacenter IPs (confirmed). INERT without creds. | Register a free "script" app at reddit.com/prefs/apps, set **`ROSTER_REDDIT_CLIENT_ID`** + **`ROSTER_REDDIT_CLIENT_SECRET`** on roster-api, then run the `reddit` tranche. | 🟡 |
| 9 | **UK Companies House** | Non-US filings + company OFFICERS (the SEC-only geography gap + the team/execution lens) — `primary_filing`. | **CONNECTOR BUILT** (`connectors/companies_house.py`, HTTP Basic, inert without key). | Register a free key at developer.company-information.service.gov.uk, set **`ROSTER_COMPANIES_HOUSE_KEY`** on roster-api, then run the `companies_house` tranche. | 🟡 |
| 10 | **USPTO patents** (PatentsView replacement) | IP-moat / prior-art evidence — `primary_filing` (granted) / `technical_signal` (application). Replaces the DORMANT patentsview (row 1). | **CONNECTOR BUILT + API CONFIRMED** (`connectors/uspto.py`): `POST https://api.uspto.gov/api/v1/patent/applications/search`, header `X-API-KEY`, body `{"q":…,"pagination":{…}}`, results in `patentFileWrapperDataBag[].applicationMetaData`. Reuses `patent_doc.py`; inert without key; `_normalize` tested against the real ODP nested shape. | Register a key at data.uspto.gov (Open Data Portal — note: from 2026-08-18 the profile needs 4 extra fields), set **`ROSTER_USPTO_KEY`** on roster-api, then run the `uspto` tranche. | 🟡 |

## Notes
- **Form D** (private raises) is NOT blocked — the EDGAR connector fetches it via full-text search
  by name (`forms:["D"]`); it runs in the easy tranches.
- **DOI-dedup arXiv↔OpenAlex** is a code quality fix (not a download blocker) — track separately;
  currently the same paper can embed twice at two tiers.
