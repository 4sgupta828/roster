"""SEC EDGAR connector — public-company filings via the official data.sec.gov APIs (free, no key).

REBUILT (was a thin FTS stub that yielded empty header blocks). Flow:
  discover_entities({"query": ticker|CIK|company name, "limit": N, "forms": [...]}) →
    resolve the company to a CIK (company_tickers.json) → GET /submissions/CIK…json →
    pick the N most recent wanted-form filings → one EntityRef per filing.
  fetch_artifact → fetch the filing's PRIMARY DOCUMENT html, parse it into real narrative sections
    (Item 1 Business / 1A Risk Factors / 7 MD&A …), and append audited XBRL Financial Highlights
    from /api/xbrl/companyfacts. → a substantive, span-verifiable markdown filing document.

SEC fair-access: ≤10 req/s + a real User-Agent (ROSTER_HTTP_CONTACT). Tests inject `filings`
(pre-normalized records) so they run offline against the same filing_doc synthesizer.
"""
from __future__ import annotations

import json

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import filing_doc, filing_html, sec_financials
from ._http import HttpStrategy

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
FTS = "https://efts.sec.gov/LATEST/search-index"

_DEFAULT_FORMS = ("10-K", "10-Q", "S-1", "DEF 14A")   # narrative + the proxy (people layer); Form D via param


def _formd_section(raw: bytes) -> str:
    """Parse a Form D primary XML into a readable private-offering summary (namespace-agnostic)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)

    def find(tag: str) -> str:
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == tag and (el.text or "").strip():
                return el.text.strip()
        return ""

    def money(v: str) -> str:
        return f"${int(v):,}" if v.isdigit() else v

    fields = [
        ("Issuer", find("entityName")),
        ("Industry group", find("industryGroupType")),
        ("Total offering amount", money(find("totalOfferingAmount"))),
        ("Total amount sold", money(find("totalAmountSold"))),
        ("Amount remaining", money(find("totalRemaining"))),
        ("Minimum investment", money(find("minimumInvestmentAccepted"))),
        ("Investors already invested", find("totalNumberAlreadyInvested")),
        ("Non-accredited investors", find("hasNonAccreditedInvestors")),
        ("Date of first sale", find("dateOfFirstSale")),
    ]
    lines = [f"- {label}: {val}" for label, val in fields if val]
    return "This is a Form D notice of an exempt private securities offering.\n" + "\n".join(lines) if lines else ""


class EdgarConnector:
    key = "edgar"

    def __init__(self, *, filings: list[dict] | None = None, page_size: int = 10):
        self.fetch_strategy = HttpStrategy(base_delay=1.0, max_retries=4)   # SEC allows ~10 req/s
        self._page_size = page_size
        self._by_acc: dict[str, dict] = {}          # accession → normalized filing meta
        self._ticker_map: dict | None = None        # {TICKER: {cik, title}}
        self._facts_cache: dict[str, dict] = {}      # cik10 → companyfacts
        for f in (filings or []):
            self._by_acc[filing_doc.accession(f)] = f

    async def _tickers(self) -> dict:
        if self._ticker_map is None:
            raw = json.loads(await self.fetch_strategy.fetch(TICKERS))
            m: dict[str, dict] = {}
            for v in raw.values():
                t = str(v.get("ticker", "")).upper()
                if t:
                    m[t] = {"cik": str(v.get("cik_str", "")).zfill(10), "title": v.get("title", "")}
            self._ticker_map = m
        return self._ticker_map

    async def _resolve(self, query: str) -> tuple[str, str, str] | None:
        """query → (cik10, ticker, title). Accepts a CIK (digits), a ticker, or a company-name substring."""
        q = (query or "").strip()
        if not q:
            return None
        if q.isdigit():
            return (q.zfill(10), "", "")
        tm = await self._tickers()
        if q.upper() in tm:
            e = tm[q.upper()]
            return (e["cik"], q.upper(), e["title"])
        ql = q.lower()
        for t, e in tm.items():
            if ql in (e["title"] or "").lower():
                return (e["cik"], t, e["title"])
        return None

    async def _fts_formd(self, name: str, limit: int) -> list[EntityRef]:
        """Find a PRIVATE issuer's Form D (private-raise) filings via EDGAR full-text search."""
        import urllib.parse
        url = f"{FTS}?q=%22{urllib.parse.quote(name)}%22&forms=D"
        try:
            data = json.loads(await self.fetch_strategy.fetch(url))
        except Exception:   # noqa: BLE001 — FTS is best-effort
            return []
        refs: list[EntityRef] = []
        for hit in (data.get("hits", {}).get("hits", []) or [])[:limit]:
            src = hit.get("_source", {}) or {}
            acc, _, pdoc = (hit.get("_id", "") or "").partition(":")
            # The accession's first 10 digits ARE the filer's CIK (more reliable than _source.cik).
            cik = acc.split("-")[0] if acc else ""
            if not cik.isdigit():
                cik = src.get("cik")
                cik = (cik[0] if isinstance(cik, list) else cik) or ""
            names = src.get("display_names") or [""]
            meta = {
                "cik": str(cik).zfill(10), "ticker": "", "company": names[0], "sic": "",
                "form_type": "D", "accession": acc, "filed": src.get("file_date", ""),
                "primary_doc": pdoc or "primary_doc.xml",
            }
            self._by_acc[acc] = meta
            refs.append(EntityRef(source_key=self.key, native_id=acc,
                                  title=filing_doc.title(meta), facets=filing_doc.facets(meta)))
        return refs

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_acc:
            filings = list(self._by_acc.values())
            return [EntityRef(source_key=self.key, native_id=filing_doc.accession(f),
                              title=filing_doc.title(f), facets=filing_doc.facets(f)) for f in filings]
        query = (window or {}).get("query", "")
        limit = int((window or {}).get("limit", self._page_size))
        wanted = tuple((window or {}).get("forms") or _DEFAULT_FORMS)
        resolved = await self._resolve(query)
        if not resolved:
            # Private issuers (Form D filers) aren't in the public ticker map → full-text search by name.
            if "D" in wanted:
                return await self._fts_formd(query, limit)
            return []
        cik10, ticker, title = resolved
        sub = json.loads(await self.fetch_strategy.fetch(SUBMISSIONS.format(cik10=cik10)))
        company = sub.get("name", title)
        sic = str(sub.get("sic") or "")          # numeric SIC code (for sector classification + facet)
        sic_desc = sub.get("sicDescription", "")
        rec = (sub.get("filings") or {}).get("recent") or {}
        forms = rec.get("form", []); accs = rec.get("accessionNumber", [])
        docs = rec.get("primaryDocument", []); dates = rec.get("filingDate", [])
        refs: list[EntityRef] = []
        for i, form in enumerate(forms):
            if form not in wanted:
                continue
            acc = accs[i]
            meta = {
                "cik": cik10, "ticker": ticker, "company": company, "sic": sic, "sic_desc": sic_desc,
                "form_type": form, "accession": acc, "filed": dates[i] if i < len(dates) else "",
                "primary_doc": docs[i] if i < len(docs) else "", "sector": (window or {}).get("_sector", ""),
            }
            self._by_acc[acc] = meta
            refs.append(EntityRef(source_key=self.key, native_id=acc,
                                  title=filing_doc.title(meta), facets=filing_doc.facets(meta)))
            if len(refs) >= limit:
                break
        return refs

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        f = self._by_acc.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=filing_doc.facets(f) if f else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def _companyfacts(self, cik10: str) -> dict:
        if cik10 not in self._facts_cache:
            try:
                self._facts_cache[cik10] = json.loads(
                    await self.fetch_strategy.fetch(COMPANYFACTS.format(cik10=cik10)))
            except Exception:   # noqa: BLE001 — a company may have no XBRL facts
                self._facts_cache[cik10] = {}
        return self._facts_cache[cik10]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        f = self._by_acc.get(doc.native_id) or {"accession": doc.native_id}
        # Offline fixtures already carry `sections` — synthesize directly.
        if f.get("sections"):
            return filing_doc.to_markdown(f).encode("utf-8")
        # LIVE: fetch the primary document HTML → real sections.
        rec = dict(f)
        rec["sections"] = {}
        pd = f.get("primary_doc"); cik = str(f.get("cik", "")).lstrip("0"); acc = f.get("accession", "")
        # Form D = a private-raise notice: the primary doc is XML with structured offering amounts.
        if f.get("form_type") == "D" and cik and acc:
            xml_url = ARCHIVE.format(cik=cik, acc_nodash=acc.replace("-", ""), doc=pd or "primary_doc.xml")
            try:
                rec["sections"]["Private Offering (Form D)"] = _formd_section(
                    await self.fetch_strategy.fetch(xml_url))
            except Exception:   # noqa: BLE001
                rec["sections"] = {}
            return filing_doc.to_markdown(rec).encode("utf-8")
        if pd and cik and acc:
            url = ARCHIVE.format(cik=cik, acc_nodash=acc.replace("-", ""), doc=pd)
            try:
                rec["sections"] = filing_html.sections_from_html(await self.fetch_strategy.fetch(url))
            except Exception:   # noqa: BLE001 — a filing may be un-parseable; still emit financials + header
                rec["sections"] = {}
        # Append audited XBRL financial highlights (10-K/10-Q).
        if f.get("cik") and f.get("form_type") in ("10-K", "10-Q"):
            fin = sec_financials.highlights_markdown(await self._companyfacts(str(f["cik"]).zfill(10)))
            if fin:
                # a title→body dict; strip the leading "## " so filing_doc renders it as a section
                rec["sections"]["Financial Highlights (XBRL)"] = fin.split("\n", 1)[1] if "\n" in fin else fin
        return filing_doc.to_markdown(rec).encode("utf-8")
