#!/usr/bin/env python3
"""LOCAL→PROD full-text bridge — download arXiv PDFs from a GOOD local IP, ship to prod for docling.

arXiv rate-limits the PROD IP on PDF fetches, so OLD papers (no HTML → PDF+docling path) degrade to
abstract-only in the corpus. This runs on the LOCAL box (good IP): it resolves paper metadata + PDF
from arXiv (paced, polite), base64s the PDF bytes, and POSTs them to prod `POST /admin/corpus/ingest-pdf`
where the prod worker's proven docling parses + ingests them. Because prod builds document_id as
`arxiv:{id}`, this REPLACES the abstract stub with full text (clean-replace) — recovery, not duplicate.

Stdlib only (no project deps) so it runs with any python3. Usage:
  ROSTER_ADMIN_TOKEN=... python scripts/local_pdf_bridge.py --seminal
  ROSTER_ADMIN_TOKEN=... python scripts/local_pdf_bridge.py 2005.11401 1706.03762 1412.6980
  ROSTER_ADMIN_TOKEN=... python scripts/local_pdf_bridge.py --from-file ids.txt   # one arxiv id per line
Options: --prod URL (default prod), --delay SECONDS (default 3, be polite to arXiv), --batch N (docs/POST).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PROD_DEFAULT = "https://roster-api-production.up.railway.app"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_PDF = "https://arxiv.org/pdf/{id}"
UA = "roster-pdf-bridge/1.0 (mailto:sandeepgupta828@gmail.com)"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Curated landmark papers (mostly pre-2023 → no arXiv HTML → the exact set that degrades to abstract).
# Kept in sync with run_downloads.SEMINAL_PAPERS by intent; duplicated here so the driver has no deps.
SEMINAL_PAPERS = [
    "Attention Is All You Need",
    "Deep Residual Learning for Image Recognition",
    "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    "Adam: A Method for Stochastic Optimization",
    "Generative Adversarial Networks",
    "Denoising Diffusion Probabilistic Models",
    "Auto-Encoding Variational Bayes",
    "Playing Atari with Deep Reinforcement Learning",
    "Mastering the game of Go with deep neural networks and tree search",
    "Highly accurate protein structure prediction with AlphaFold",
    "Language Models are Few-Shot Learners",
    "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
    "Dropout: A Simple Way to Prevent Neural Networks from Overfitting",
    "Batch Normalization: Accelerating Deep Network Training",
    "Sequence to Sequence Learning with Neural Networks",
    "ImageNet Classification with Deep Convolutional Neural Networks",
    "Very Deep Convolutional Networks for Large-Scale Image Recognition",
    "U-Net: Convolutional Networks for Biomedical Image Segmentation",
    "Distilling the Knowledge in a Neural Network",
    "Proximal Policy Optimization Algorithms",
]


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 — arXiv https only
        return r.read()


def _resolve(query: str) -> dict | None:
    """query = an arXiv id (digits.digits) or a paper title → metadata dict (id/title/facets)."""
    is_id = bool(query) and query[0].isdigit() and ("." in query or "/" in query)
    q = (f"id_list={query}" if is_id
         else "search_query=ti:%22" + urllib.parse.quote(query) + "%22&max_results=1")
    try:
        raw = _get(f"{ARXIV_API}?{q}")
    except Exception as e:   # noqa: BLE001
        print(f"  ! resolve failed for {query!r}: {str(e)[:80]}", file=sys.stderr); return None
    root = ET.fromstring(raw)
    e = root.find("a:entry", NS)
    if e is None:
        return None
    rid = (e.findtext("a:id", default="", namespaces=NS) or "").rsplit("/", 1)[-1].split("v")[0]
    prim = e.find("arxiv:primary_category", NS)
    return {
        "native_id": rid,
        "title": " ".join((e.findtext("a:title", default="", namespaces=NS) or "").split()),
        "facets": {
            "source_kind": "paper",
            "year": (e.findtext("a:published", default="", namespaces=NS) or "")[:4],
            "category": prim.get("term", "") if prim is not None else "",
        },
    }


def _post(prod: str, token: str, docs: list[dict]) -> dict:
    body = json.dumps({"docs": docs, "source_key": "arxiv"}).encode("utf-8")
    req = urllib.request.Request(f"{prod}/admin/corpus/ingest-pdf", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Admin-Token": token,
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r:   # docling on prod is slow → long timeout
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="arXiv ids or titles")
    ap.add_argument("--seminal", action="store_true", help="ingest the curated landmark-paper set")
    ap.add_argument("--from-file", help="file with one arXiv id per line")
    ap.add_argument("--prod", default=PROD_DEFAULT)
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between arXiv fetches (be polite)")
    ap.add_argument("--batch", type=int, default=2, help="docs per POST (keep each request < timeout)")
    a = ap.parse_args()

    token = os.environ.get("ROSTER_ADMIN_TOKEN", "")
    if not token:
        print("ERROR: set ROSTER_ADMIN_TOKEN", file=sys.stderr); return 2

    queries: list[str] = list(a.ids)
    if a.seminal:
        queries += SEMINAL_PAPERS
    if a.from_file:
        with open(a.from_file) as f:
            queries += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not queries:
        print("nothing to do — pass ids/titles, --seminal, or --from-file", file=sys.stderr); return 2

    print(f"resolving + downloading {len(queries)} papers (delay={a.delay}s) → {a.prod}")
    batch: list[dict] = []
    ok = fail = 0
    for i, query in enumerate(queries, 1):
        meta = _resolve(query)
        time.sleep(a.delay)
        if not meta or not meta["native_id"]:
            print(f"[{i}/{len(queries)}] UNRESOLVED: {query[:60]}"); fail += 1; continue
        try:
            pdf = _get(ARXIV_PDF.format(id=meta["native_id"]))
        except Exception as e:   # noqa: BLE001
            print(f"[{i}/{len(queries)}] PDF DL FAILED {meta['native_id']}: {str(e)[:70]}"); fail += 1
            time.sleep(a.delay); continue
        time.sleep(a.delay)
        meta["pdf_b64"] = base64.b64encode(pdf).decode("ascii")
        print(f"[{i}/{len(queries)}] {meta['native_id']}  {len(pdf)//1024}KB  {meta['title'][:50]}")
        batch.append(meta)
        if len(batch) >= a.batch or i == len(queries):
            try:
                res = _post(a.prod, token, batch)
                for r in res.get("ingested", []):
                    if r.get("error"):
                        print(f"    -> {r['native_id']}: ERROR {r['error']}"); fail += 1
                    else:
                        print(f"    -> {r['native_id']}: {r.get('blocks')} blocks "
                              f"({r.get('body_chars')} chars)"); ok += 1
            except Exception as e:   # noqa: BLE001
                print(f"    -> POST FAILED: {str(e)[:120]}"); fail += len(batch)
            batch = []
    print(f"\nDONE: {ok} ingested full-text, {fail} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
