"""USPTO connector — US patents via the USPTO Open Data Portal (ODP / "PatentSearch").

REPLACES the dormant PatentsView connector (PatentsView suspended issuing new API keys). Same
KEYED, fail-safe contract: reads ROSTER_USPTO_KEY and sends it as the `X-API-KEY` header; without
a key the connector WARNS and discover_entities returns [] (inert — never errors an ingest job).

Evidence tiering is unchanged and delegated to the shared `patent_doc` module: a GRANTED patent is
a legal record → `primary_filing`; a pre-grant APPLICATION is intent, not fact → `technical_signal`
(evidence_kind reads facets `source_kind=patent` + `grant_status`). We read the DECLARED grant
status from the record; we never infer meaning (Rule 18).

discover_entities({"query": topic | assignee, "limit": N}) searches live (limit capped at 50).
Tests inject `patents` (already in patent_doc shape) so they run fully offline.
"""
from __future__ import annotations

import logging
import os

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import patent_doc
from ._http import HttpStrategy

# ─── LIVE-API CONTRACT (confirmed against data.uspto.gov ODP, Aug 2026) ─────────────────────────
# USPTO Open Data Portal "Patent File Wrapper" search — a POST-body search API. Confirmed LIVE (200)
# with a real key, Aug 2026:
#   POST https://api.uspto.gov/api/v1/patent/applications/search   header: X-API-KEY: <key>
#   body: {"q": "<query>", "pagination": {"offset": 0, "limit": N}}
#   resp: {"count": N, "patentFileWrapperDataBag": [ {"applicationNumberText": ...,
#            "applicationMetaData": {"inventionTitle","patentNumber","grantDate","filingDate",
#            "applicationStatusDescriptionText","applicantBag":[{"applicantNameText"}], ...}} ]}
# NOTE: the API host is api.uspto.gov — data.uspto.gov/api/... is the web-UI backend and 400s with
# "use the api.uspto.gov endpoint". (Offline contract doesn't depend on this — tests inject fixtures.)
SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
RESULTS_KEY = "patentFileWrapperDataBag"
KEY_HEADER = "X-API-KEY"


def _granted(rec: dict) -> bool:
    """Structural read of the record's declared grant status — NOT a semantic inference."""
    gs = str(rec.get("grant_status") or "").lower()
    return gs in ("granted", "grant", "true", "1", "yes")


def _normalize(raw: dict) -> dict:
    """Map ONE raw USPTO ODP record → patent_doc's expected shape. IDEMPOTENT.

    Injected fixtures are already in patent_doc shape (they carry `patent_title`/`assignees`), so
    they pass straight through unchanged. Live ODP records are mapped here — this mapping is the
    single point of change if ODP field names differ from the assumptions below.
    """
    # Already normalized (fixture or a prior pass) → return as-is (idempotent).
    if "patent_title" in raw and "assignees" in raw:
        return raw

    # ODP nests the descriptive fields under applicationMetaData; the app number sits at the top level.
    md = raw.get("applicationMetaData") if isinstance(raw.get("applicationMetaData"), dict) else {}
    app_no = raw.get("applicationNumberText") or md.get("applicationNumberText")
    num = md.get("patentNumber") or raw.get("patentNumber") or app_no
    title = md.get("inventionTitle") or raw.get("inventionTitle") or raw.get("title")
    abstract = md.get("abstractText") or raw.get("abstractText") or raw.get("abstract")
    grant_date = md.get("grantDate") or raw.get("grantDate")
    date = grant_date or md.get("filingDate") or raw.get("filingDate") or raw.get("patent_date")
    status = str(md.get("applicationStatusDescriptionText")
                 or raw.get("applicationStatusDescriptionText") or "").lower()

    # Applicants/assignees → list[{"assignee_organization": org}] (patent_doc reads that shape).
    assignees: list[dict] = []
    bag = (md.get("applicantBag") or md.get("assigneeBag")
           or raw.get("applicantBag") or raw.get("assigneeBag") or [])
    for a in bag:
        if isinstance(a, dict):
            org = a.get("applicantNameText") or a.get("assignee_organization") \
                or a.get("organizationName") or a.get("name")
            if org:
                assignees.append({"assignee_organization": str(org)})
        elif isinstance(a, str) and a.strip():
            assignees.append({"assignee_organization": a.strip()})
    if not assignees and (md.get("firstApplicantName") or raw.get("firstApplicantName")):
        assignees.append({"assignee_organization":
                          str(md.get("firstApplicantName") or raw.get("firstApplicantName"))})

    # grant vs pre-grant publication: a granted patent has a patentNumber + grantDate, or a
    # "patented"/"granted" status. Otherwise it is a pre-grant application (intent, not fact).
    is_grant = bool(md.get("patentNumber") or raw.get("patentNumber")) and bool(grant_date) \
        or "patent" in status or "granted" in status
    grant_status = "granted" if is_grant else "application"

    rec = {
        "patent_id": str(num or "").strip(),
        "patent_title": str(title or "").strip(),
        "assignees": assignees,
        "grant_status": grant_status,
        "patent_date": str(date or ""),
    }
    if abstract:
        rec["patent_abstract"] = str(abstract).strip()
    return rec


_log = logging.getLogger(__name__)


class UsptoConnector:
    key = "uspto"

    def __init__(self, *, patents: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy(base_delay=2.0, max_retries=4)
        self._page_size = page_size
        self._key = os.environ.get("ROSTER_USPTO_KEY", "")
        self._by_id: dict[str, dict] = {}
        for p in (patents or []):
            rec = _normalize(p)
            pid = patent_doc.patent_id(rec)
            if pid:
                self._by_id[pid] = rec

    async def _search(self, query: str, limit: int) -> list[dict]:
        if not self._key:
            _log.warning("uspto: ROSTER_USPTO_KEY not set — skipping (no patents ingested)")
            return []
        n = min(50, max(1, limit))
        # ODP search is a POST with a JSON body (HttpStrategy is GET-only), so use httpx directly
        # — same as the reddit connector's token POST. Fail-safe: any API error → [] (never error a job).
        import httpx
        body = {"q": query, "pagination": {"offset": 0, "limit": n}}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(SEARCH_URL, json=body,
                                      headers={KEY_HEADER: self._key, "Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
        except Exception as e:   # noqa: BLE001 — transient/API error → skip this query, don't crash ingest
            _log.warning("uspto: search failed (%s) — skipping", str(e)[:120])
            return []
        records = data.get(RESULTS_KEY) or data.get("results") or []
        return [_normalize(rec) for rec in records if isinstance(rec, dict)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        win = window or {}
        q = str(win.get("query") or "").strip()
        if not q and self._by_id:
            recs = list(self._by_id.values())
        elif q:
            limit = int(win.get("limit", self._page_size))
            recs = await self._search(q, limit)
            for r in recs:
                pid = patent_doc.patent_id(r)
                if pid:
                    self._by_id[pid] = r
        else:
            recs = []
        return [EntityRef(source_key=self.key, native_id=patent_doc.patent_id(r),
                          title=patent_doc.title(r),
                          facets=patent_doc.facets(r, granted=_granted(r)))
                for r in recs if patent_doc.patent_id(r)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        facets = patent_doc.facets(p, granted=_granted(p)) if p else dict(entity.facets)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown", facets=facets,
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id) or {"patent_id": doc.native_id}
        return patent_doc.to_markdown(p, granted=_granted(p)).encode("utf-8")
