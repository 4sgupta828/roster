#!/usr/bin/env python3
"""Roster download campaign — prod-direct ingest in PRIORITY ORDER (easy sources first).

Enqueues connector jobs against prod `/admin/corpus/ingest`. Ingest cost is embeddings-only
(~$0.02/1M tokens — pennies); the real limits are source rate-limits + time, so the prod worker
paces the queue serially. We do NOT block on unavailable sources (see docs/downloads-blocked.md).

Usage:
  ROSTER_ADMIN_TOKEN=... python scripts/run_downloads.py <tranche> [--limit N] [--dry]
  tranches: depth | formd | openalex | arxiv | s2 | crossref | wikidata | yc | hn | github | recent | all
  --dry prints the jobs without enqueuing.
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.request

PROD = os.environ.get("ROSTER_PROD_URL", "https://roster-api-production-3405.up.railway.app")

# --- T1a: EDGAR flagship DEPTH (default forms: 10-K/10-Q/S-1/DEF 14A → history + people/comp) ---
# Public leaders across AI, semis, cloud, software, security — deepen the entities we already have.
DEPTH_TICKERS = [
    "NVDA","AMD","INTC","AVGO","MU","QCOM","ARM","MRVL","ON","ADI","TXN","LRCX","AMAT","KLAC","SMCI",
    "MSFT","GOOGL","AMZN","META","AAPL","ORCL","CRM","ADBE","NOW","SNOW","PLTR","AI","PATH","MDB","DDOG",
    "NET","CRWD","PANW","ZS","S","FTNT","OKTA","DELL","HPE","IBM","CSCO","ANET","INTU","WDAY","TEAM",
    # wave 2
    "ADSK","ANSS","CDNS","SNPS","FICO","MSCI","VRSN","AKAM","FFIV","JNPR","HUBS","ZM","DOCU","TWLO",
    "ESTC","GTLB","CFLT","U","RBLX","DASH","ABNB","UBER","SHOP","XYZ","PYPL","COIN","HOOD","SOFI","AFRM",
    "NU","TOST","BILL","MELI","SE","VRT","CRDO","ALAB","TEM","RXRX","SDGR","VEEV","DOCS","HIMS",
]

# --- T1b: EDGAR FORM D (private raises via EDGAR full-text search by name) ---
# Notable US private AI/tech startups that file Reg D. Names matched by EDGAR FTS.
FORMD_NAMES = [
    "OpenAI","Anthropic","Databricks","Scale AI","Anduril Industries","xAI","Cohere","Perplexity AI",
    "Glean Technologies","Together Computer","Groq","SambaNova Systems","Runway AI","Adept AI","Inflection AI",
    "Harvey AI","Sierra","Anysphere","Figure AI","Physical Intelligence","Skild AI","Hippocratic AI","Abridge",
    "Cresta","Writer","Jasper AI","Notion Labs","Rippling","Ramp Business","Brex","Deel","Airtable",
    "Discord","Stripe","Plaid","Chime","Instacart","Canva","Grammarly","Vercel","Retool","Snyk","Wiz",
    # wave 2
    "Mistral AI","Reflection AI","Thinking Machines Lab","Safe Superintelligence","World Labs","Luma AI",
    "Suno","ElevenLabs","Cartesia","Fireworks AI","Baseten","Modal Labs","Replicate","LangChain","LlamaIndex",
    "Pinecone Systems","Chroma","Weaviate","Neon","Supabase","Clerk","Vanta","Mercury Technologies","Column",
    "Modern Treasury","Shield AI","Saronic Technologies","Applied Intuition","Hadrian Automation","Xaira Therapeutics",
    "EvolutionaryScale","Chai Discovery","Cursor","Decagon","Sierra AI","Mercor","Clay","Legora","Cognition AI",
]

# --- T2: OpenAlex topics (peer-reviewed, verified_structured; stamp sector=ai) ---
OPENALEX_QUERIES = [
    "large language model","retrieval augmented generation","transformer architecture",
    "reinforcement learning from human feedback","mixture of experts language model","diffusion model image generation",
    "neural network quantization","approximate nearest neighbor vector search","autonomous language model agents",
    "instruction tuning language model","long context transformer","speculative decoding inference",
    # wave 2
    "model distillation neural network","parameter efficient fine-tuning LoRA","chain of thought reasoning",
    "multimodal large language model","code generation large language model","graph neural network",
    "differential privacy machine learning","dense passage retrieval","knowledge distillation transformer",
    "state space model sequence",
]

# --- T2: Semantic Scholar (citation graph, verified_structured/technical_signal; keyless+backoff) ---
S2_QUERIES = [
    "large language model","retrieval augmented generation","mixture of experts",
    "reinforcement learning from human feedback","parameter efficient fine tuning","chain of thought reasoning",
    "multimodal large language model","transformer efficient attention","llm agents tool use",
    "neural network quantization","dense retrieval","diffusion models generative",
]

# --- T2: Crossref (DOI/venue authority + citations, verified_structured/technical_signal; keyless) ---
CROSSREF_QUERIES = [
    "large language model","retrieval augmented generation","mixture of experts transformer",
    "reinforcement learning from human feedback","parameter efficient fine tuning","vector database search",
    "neural machine translation attention","diffusion model image synthesis","graph neural network",
    "self supervised representation learning","knowledge graph embedding","federated learning privacy",
]

# --- T2: arXiv preprints (technical_signal, unreviewed; keyless; stamp sector=ai) ---
ARXIV_QUERIES = [
    "large language model inference","retrieval augmented generation","llm agents tool use",
    "mixture of experts","parameter efficient fine tuning","llm reasoning chain of thought",
    "long context transformer","speculative decoding","model quantization llm","vector database ann search",
    "multimodal foundation model","diffusion model","reinforcement learning human feedback",
    "code generation llm","llm evaluation benchmark",
]

# --- SEMINAL WORKS: the FOUNDATIONAL trunk of the field. The recency/relevance lanes miss these (an
# ingest query for "RAG" returns 2024 derivatives, not the 2020 original), so the corpus loses to web
# on genesis/history questions. Curated landmark TITLES fetched exact via OpenAlex title.search+cited
# (verified to return the exact paper), so the seminal works are GUARANTEED in the corpus. ---
SEMINAL_PAPERS = [
    "Attention Is All You Need",
    "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    "Language Models are Few-Shot Learners",
    "Deep Residual Learning for Image Recognition",
    "ImageNet Classification with Deep Convolutional Neural Networks",
    "Adam: A Method for Stochastic Optimization",
    "Generative Adversarial Networks",
    "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
    "Denoising Diffusion Probabilistic Models",
    "High-Resolution Image Synthesis with Latent Diffusion Models",
    "Chain-of-Thought Prompting Elicits Reasoning in Large Language Models",
    "LoRA: Low-Rank Adaptation of Large Language Models",
    "Training language models to follow instructions with human feedback",
    "Learning Transferable Visual Models From Natural Language Supervision",
    "Scaling Laws for Neural Language Models",
    "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness",
    "LLaMA: Open and Efficient Foundation Language Models",
    "Efficient Estimation of Word Representations in Vector Space",
    "Sequence to Sequence Learning with Neural Networks",
    "Neural Machine Translation by Jointly Learning to Align and Translate",
    "Playing Atari with Deep Reinforcement Learning",
    "Mastering the game of Go with deep neural networks and tree search",
    "Highly accurate protein structure prediction with AlphaFold",
    "Dropout: A Simple Way to Prevent Neural Networks from Overfitting",
    "Batch Normalization: Accelerating Deep Network Training",
    "Long Short-Term Memory",
    "Distilling the Knowledge in a Neural Network",
    "Mixtral of Experts",
    "Direct Preference Optimization",
    "Segment Anything",
    "Emergent Abilities of Large Language Models",
    "GPT-4 Technical Report",
    "Quantum supremacy using a programmable superconducting processor",
    "Deep learning",
]

# --- DEEP-TECH BREADTH: cross-domain topics beyond AI (the product is deep-tech intelligence broadly,
# not just AI). Grouped BY SECTOR so the `deeptech` tranche stamps each ingested doc with its sector
# (facets.sector) — making the top researched sectors DEEPLY + FINDABLY covered, not just present.
# Fanned across the research connectors (arxiv/openalex/s2/crossref/openreview). ---
DEEPTECH_BY_SECTOR: dict[str, list[str]] = {
    "robotics": ["humanoid robotics control","robot manipulation learning","autonomous driving perception",
                 "SLAM simultaneous localization mapping","legged robot locomotion","drone swarm coordination",
                 "imitation learning robotics","visual servoing grasping","tactile sensing manipulation"],
    "semiconductors": ["chiplet architecture interconnect","RISC-V processor design","silicon photonics computing",
                       "in-memory computing accelerator","AI inference ASIC","advanced lithography EUV",
                       "high bandwidth memory HBM","gallium nitride power semiconductor","3D IC packaging",
                       "neuromorphic computing chip"],
    "quantum": ["quantum error correction","superconducting qubit","trapped ion quantum computing",
                "quantum advantage algorithm","photonic quantum computing","topological qubit",
                "quantum machine learning","fault tolerant quantum computation"],
    "biotech": ["protein structure prediction","CRISPR gene editing","mRNA therapeutics","synthetic biology engineering",
                "single cell RNA sequencing","AI drug discovery","brain computer interface","protein language model",
                "antibody design generative","base editing prime editing","cell therapy CAR-T","spatial transcriptomics"],
    "climate": ["solid state battery","green hydrogen electrolysis","direct air carbon capture",
                "perovskite solar cell","nuclear fusion tokamak","grid scale energy storage","geothermal drilling",
                "lithium extraction recycling","sustainable aviation fuel","long duration energy storage"],
    "space": ["reusable rocket propulsion","satellite constellation broadband","in-space manufacturing",
              "electric propulsion satellite","lunar lander mission","space situational awareness"],
    "materials": ["2D materials graphene","metamaterials photonics","metal additive manufacturing","solid electrolyte",
                  "high entropy alloys","machine learning materials discovery"],
    "security": ["post quantum cryptography","zero knowledge proof","confidential computing enclave",
                 "homomorphic encryption","secure multiparty computation","hardware root of trust"],
    "data_infra": ["vector database indexing","stream processing systems","data lakehouse architecture",
                   "wasm edge computing","query optimization distributed","serverless data platform"],
    "fintech": ["real time payments infrastructure","decentralized finance protocol","stablecoin settlement",
                "fraud detection machine learning","open banking API"],
    "spatial_neuro": ["augmented reality waveguide display","neural rendering 3D reconstruction",
                      "neural interface decoding","gaussian splatting reconstruction"],
}
# Flattened (back-compat: e.g. the wikipedia tranche fans the first N of these).
DEEPTECH_TOPICS = [t for topics in DEEPTECH_BY_SECTOR.values() for t in topics]

# --- T2: Wikidata company profiles (KEYLESS Crunchbase fallback: founders/ownership/M&A; reference tier) ---
WIKIDATA_NAMES = [
    "OpenAI","Anthropic","Databricks","Scale AI","Anduril Industries","xAI","Cohere","Perplexity AI",
    "Mistral AI","Hugging Face","Groq","SambaNova Systems","Cerebras Systems","Together AI","Runway",
    "Stripe","Databricks","Canva","Figma","Notion","Rippling","Ramp","Brex","Plaid","Chime","Discord",
    "NVIDIA","Advanced Micro Devices","Palantir Technologies","CrowdStrike","Snowflake Inc","Datadog",
    "Palo Alto Networks","ServiceNow","Cloudflare","MongoDB","Atlassian","Shopify","Coinbase","Block Inc",
]

# --- T3: Hacker News via Algolia (KEYLESS sentiment fallback; sentiment_signal tier, labeled) ---
HN_QUERIES = [
    "OpenAI","Anthropic","NVIDIA","CrowdStrike","Databricks","Palantir","Snowflake","Datadog",
    "large language model","AI agents","vector database","retrieval augmented generation",
    "Mistral","Perplexity","Cursor","llama","GPU shortage","AI regulation",
]

# --- Reddit: broader-community SENTIMENT signal (needs ROSTER_REDDIT_CLIENT_ID/SECRET in prod).
# (subreddit, query) pairs — scoped search of the high-signal AI/tech/startup communities.
REDDIT_QUERIES = [
    ("MachineLearning", "large language model"), ("MachineLearning", "benchmark"),
    ("LocalLLaMA", "open weights model"), ("LocalLLaMA", "quantization"),
    ("artificial", "AGI"), ("singularity", "frontier model"),
    ("OpenAI", "GPT"), ("StableDiffusion", "image model"),
    ("startups", "AI startup funding"), ("venturecapital", "AI investment"),
    ("hardware", "GPU"), ("datascience", "RAG"),
]

# --- Hugging Face Hub: model-adoption signal (technical_signal); keyless, ROSTER_HF_TOKEN optional ---
HF_QUERIES = [
    "large language model","text generation","embedding","reranker","vision language model",
    "code generation","speech recognition","diffusion","mixture of experts","function calling",
    "llama","mistral","qwen","gemma","phi","deepseek",
]
# --- Stack Overflow: developer-adoption signal (sentiment_signal); keyless ---
SO_QUERIES = [
    "langchain","llama.cpp","vllm","transformers huggingface","openai api","pgvector",
    "retrieval augmented generation","fine-tuning LLM","ollama","llama-index","cuda out of memory",
    "vector database",
]
# --- OpenReview: peer-reviewed research (verified_structured when accepted); keyless ---
OPENREVIEW_QUERIES = [
    "large language models","retrieval augmented generation","mixture of experts","in-context learning",
    "reinforcement learning from human feedback","diffusion models","state space models",
    "long context transformers","efficient inference","agentic reasoning",
]

# --- UK Companies House: non-US filings + officers/team (primary_filing); needs ROSTER_COMPANIES_HOUSE_KEY ---
CH_QUERIES = [
    "DeepMind","Stability AI","Synthesia","Wayve","PolyAI","ElevenLabs","Graphcore","Darktrace",
    "Faculty AI","Tractable","Speechmatics","Builder.ai","Improbable","Quantexa","Cohere UK",
]
# --- YC company directory: the startup POPULATION seed (source_kind=reference → verified_structured).
# The AI slice — a text query over AI subfields + an explicit `industry`/`tag` facet filter — pulls
# the ~1500 AI-relevant YC companies with founders. Deep per-query limits page the Algolia index. ---
YC_QUERIES = [
    "AI","machine learning","large language models","AI agents","generative AI","computer vision",
    "natural language processing","AI infrastructure","robotics","AI developer tools","LLM",
    "AI healthcare","AI security","voice AI","data infrastructure",
]
# --- USPTO patents (primary_filing granted / technical_signal application); needs ROSTER_USPTO_KEY ---
USPTO_QUERIES = [
    # AI/ML core
    "large language model","transformer neural network","attention mechanism","retrieval augmented generation",
    "neural network accelerator","AI inference chip","speech recognition model","diffusion image generation",
    "reinforcement learning","vector similarity search",
    # FRONTIER DEEP-TECH (the patent gap is corpus-wide, not just AI — 174 blocks total): a spread per sector
    # so the moat/IP axis has real coverage across quantum/semis/robotics/bio/climate/space/security/energy.
    "quantum computing qubit","quantum error correction","semiconductor lithography","chiplet packaging",
    "gallium nitride power device","autonomous robot navigation","surgical robotics","solid state battery",
    "carbon capture materials","direct air capture","satellite propulsion","reusable rocket",
    "mRNA vaccine platform","CRISPR gene editing","synthetic biology","post-quantum cryptography",
    "confidential computing enclave","lidar sensor","solid oxide fuel cell","brain computer interface",
]

# --- NEWS / MARKET SIGNAL via GDELT (keyless; the connector RESTRICTS to reputable tech/business press —
# TechCrunch, Bloomberg, Reuters, FT, The Information, The Verge, Ars Technica, Wired, Axios, Forbes, CNBC,
# NYT). This IS the "startup news" layer (analysis tier, labeled): recent funding rounds, launches,
# acquisitions, sector developments. news=288 blocks today → the market-signal gap. ---
GDELT_QUERIES = [
    # NOTE: GDELT rejects any <3-char keyword ("AI"/"ML") → spell them out. Keep queries 2-3 words so a
    # full page (75) returns and enough survive the connector's reputable-domain post-filter.
    # funding / deal flow
    "startup funding round","venture capital funding","startup Series funding","startup acquisition",
    "startup valuation billion","artificial intelligence funding",
    # AI landscape
    "foundation model company","coding assistant startup","enterprise artificial intelligence",
    "open source language model","artificial intelligence chip","generative artificial intelligence",
    # frontier sectors
    "quantum computing company","semiconductor startup","robotics company funding","climate tech startup",
    "fintech startup funding","biotech startup funding","defense tech startup","space startup launch",
    "autonomous vehicle company","cybersecurity startup",
]

# --- T3: GitHub org traction (technical_signal) ---
GITHUB_ORGS = [
    "openai","anthropics","google-deepmind","meta-llama","huggingface","nvidia","pytorch","tensorflow",
    "langchain-ai","run-llama","vllm-project","ggml-org","mistralai","databricks","triton-lang","microsoft",
    "google-research","facebookresearch","EleutherAI","allenai","stanfordnlp","deepset-ai","qdrant","weaviate",
    # wave 2
    "vercel","supabase","pinecone-io","chroma-core","modal-labs","BerriAI","stanford-crfm","unslothai",
    "ollama","comfyanonymous","Lightning-AI","ray-project",
]

def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(PROD + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "X-Admin-Token": os.environ.get("ROSTER_ADMIN_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def build(tranche: str, limit: int | None) -> list[dict]:
    jobs: list[dict] = []
    if tranche in ("depth","all"):
        for t in DEPTH_TICKERS:
            jobs.append({"connector":"edgar","query":t,"limit":limit or 12})
    if tranche in ("formd","all"):
        for n in FORMD_NAMES:
            jobs.append({"connector":"edgar","query":n,"limit":limit or 6,"params":{"forms":["D"]}})
    if tranche in ("openalex","all"):
        for qy in OPENALEX_QUERIES:
            jobs.append({"connector":"openalex","query":qy,"limit":limit or 20,"facets":{"sector":"ai"}})
    if tranche in ("arxiv","all"):
        for qy in ARXIV_QUERIES:
            jobs.append({"connector":"arxiv","query":qy,"limit":limit or 20,"facets":{"sector":"ai"}})
    if tranche in ("s2","all"):
        for qy in S2_QUERIES:
            jobs.append({"connector":"semantic_scholar","query":qy,"limit":limit or 20,"facets":{"sector":"ai"}})
    if tranche in ("crossref","all"):
        for qy in CROSSREF_QUERIES:
            jobs.append({"connector":"crossref","query":qy,"limit":limit or 20,"facets":{"sector":"ai"}})
    if tranche in ("wikidata","all"):
        for nm in WIKIDATA_NAMES:
            jobs.append({"connector":"wikidata","query":nm,"limit":limit or 1})
    if tranche in ("hn","all"):
        for qy in HN_QUERIES:
            jobs.append({"connector":"hackernews","query":qy,"limit":limit or 25})
    if tranche in ("github","all"):
        for o in GITHUB_ORGS:
            jobs.append({"connector":"github","query":o,"limit":limit or 8})
    if tranche in ("reddit","all"):
        for sub, qy in REDDIT_QUERIES:
            jobs.append({"connector":"reddit","query":qy,"limit":limit or 25,
                         "params":{"subreddit":sub}})
    if tranche in ("lobsters","all"):
        # feed connector pulls curated tech tags; the query loosely filters. A few passes catch breadth.
        for qy in ("machine learning","LLM","hardware","security","distributed systems","databases",
                   "programming languages","performance"):
            jobs.append({"connector":"lobsters","query":qy,"limit":limit or 30})
    if tranche in ("hf","all"):
        for qy in HF_QUERIES:
            jobs.append({"connector":"huggingface","query":qy,"limit":limit or 30,"facets":{"sector":"ai"}})
    if tranche in ("stackoverflow","all"):
        for qy in SO_QUERIES:
            jobs.append({"connector":"stackoverflow","query":qy,"limit":limit or 25})
        # fan across the technical Stack Exchange sites (AI / Data Science / Cross-Validated / CS /
        # Security / SWE / Quantum / …) — broader human deep-tech discussion, same connector.
        for site in ("ai","datascience","stats","cs","security","softwareengineering","quantumcomputing","robotics"):
            for qy in ("machine learning","neural network","optimization","algorithm","cryptography"):
                jobs.append({"connector":"stackoverflow","query":qy,"limit":limit or 15,
                             "params":{"site":site}})
    if tranche in ("openreview","all"):
        for qy in OPENREVIEW_QUERIES:
            jobs.append({"connector":"openreview","query":qy,"limit":limit or 30,"facets":{"sector":"ai"}})
    if tranche in ("companies_house","all"):
        for qy in CH_QUERIES:
            jobs.append({"connector":"companies_house","query":qy,"limit":limit or 10})
    if tranche in ("yc","all"):
        # AI slice of the YC startup population + one industry-filtered pass for breadth.
        for qy in YC_QUERIES:
            jobs.append({"connector":"yc","query":qy,"limit":limit or 100,"facets":{"sector":"ai"}})
        jobs.append({"connector":"yc","query":"","tag":"Artificial Intelligence",
                     "limit":limit or 500,"facets":{"sector":"ai"}})
    # DEEP-TECH BREADTH: fan the cross-domain topics across the research connectors so the corpus
    # covers the whole frontier (robotics/semi/quantum/bio/climate/space/materials/security/…), DEEPLY
    # and STAMPED with each doc's sector (facets.sector). NOT in "all" (a large, deliberate breadth
    # pass). Run all sectors: `deeptech` — or one at a time (tranches): `deeptech:quantum`, etc.
    if tranche == "deeptech" or tranche.startswith("deeptech:"):
        only = tranche.split(":", 1)[1].strip() if ":" in tranche else None
        for sector, topics in DEEPTECH_BY_SECTOR.items():
            if only and sector != only:
                continue
            for qy in topics:
                f = {"sector": sector}
                jobs.append({"connector":"arxiv","query":qy,"limit":limit or 25,"facets":f,"priority":500})
                jobs.append({"connector":"openalex","query":qy,"limit":limit or 25,"facets":f,"priority":500})
                jobs.append({"connector":"semantic_scholar","query":qy,"limit":limit or 20,"facets":f,"priority":500})
                jobs.append({"connector":"crossref","query":qy,"limit":limit or 20,"facets":f,"priority":500})
                jobs.append({"connector":"openreview","query":qy,"limit":limit or 10,"facets":f,"priority":500})
    if tranche in ("uspto","patents","all"):
        for qy in USPTO_QUERIES:
            jobs.append({"connector":"uspto","query":qy,"limit":limit or 25})
    if tranche in ("gdelt","news","all"):
        # reputable-press news (analysis tier); 6-month window so it's CURRENT market signal.
        for qy in GDELT_QUERIES:
            jobs.append({"connector":"gdelt","query":qy,"limit":limit or 40,"params":{"timespan":"6m"}})
    if tranche in ("wikipedia","all"):
        # tech + company/ecosystem history/genesis pages
        for qy in (list(WIKIDATA_NAMES) + DEEPTECH_TOPICS[:30]):
            jobs.append({"connector":"wikipedia","query":qy,"limit":limit or 5})
    if tranche in ("nsf","all"):
        for qy in DEEPTECH_TOPICS:
            jobs.append({"connector":"nsf","query":qy,"limit":limit or 20})
    if tranche in ("nih","all"):
        for qy in ("mRNA","CRISPR gene editing","protein structure prediction","synthetic biology",
                   "AI drug discovery","single cell sequencing","brain computer interface","genomics",
                   "cancer immunotherapy","neurotechnology"):
            jobs.append({"connector":"nih_reporter","query":qy,"limit":limit or 25})
    if tranche in ("expert","all"):
        # feed connector ignores the query text (pulls its curated allowlist) — a few passes catch
        # recent items across all feeds. Broad terms keep the ingest question meaningful in logs.
        for qy in ("deep tech expert analysis","AI trends","semiconductors","research commentary"):
            jobs.append({"connector":"expert_feed","query":qy,"limit":limit or 40})
    if tranche in ("podcast","all"):
        for qy in ("deep tech","AI","engineering"):
            jobs.append({"connector":"podcast","query":qy,"limit":limit or 30})
    if tranche in ("eng_blog","all"):
        # feed connector ignores the query text (pulls its curated SOTA allowlist of company eng blogs);
        # a few passes catch recent full-text items across all feeds. corp_eng → technical_signal tier.
        for qy in ("distributed systems architecture","infrastructure at scale","reliability performance",
                   "databases storage","machine learning platform"):
            jobs.append({"connector":"eng_blog","query":qy,"limit":limit or 40})
    if tranche == "fulltext":
        # FULL-TEXT depth: re-ingest arXiv papers with the WHOLE body (HTML-first, docling PDF fallback)
        # instead of the abstract → answers ground in methods/results, not just the abstract. Runs on
        # the ingest worker (docling); arXiv rate-limits, so the connector paces. Not part of "all".
        for qy in (ARXIV_QUERIES + DEEPTECH_TOPICS):
            jobs.append({"connector":"arxiv","query":qy,"limit":limit or 6,"params":{"fulltext":True}})
        # the seminal landmark papers, full-text (fetched by exact title via arXiv)
        for title in SEMINAL_PAPERS:
            jobs.append({"connector":"arxiv","query":title,"limit":1,"params":{"fulltext":True}})
    if tranche in ("seminal","all"):
        # curated landmark papers, exact-title → the exact seminal work (closes the genesis/history gap)
        for title in SEMINAL_PAPERS:
            jobs.append({"connector":"openalex","query":title,"limit":2,"params":{"sort":"cited"}})
        # + citation-ranked breadth: the most-cited titled works per deep-tech topic (foundational trunk)
        for qy in DEEPTECH_TOPICS:
            jobs.append({"connector":"openalex","query":qy,"limit":limit or 12,"params":{"sort":"cited"}})
    # RECENT lane: re-pull the paper sources newest-first / floored at >=2010 so the corpus isn't
    # relevance-skewed to old highly-cited work. Not part of "all" (it re-queries the same topics with
    # a freshness filter) — run explicitly: `run_downloads.py recent`.
    if tranche == "recent":
        # arXiv dates are reliable → newest-first is clean. OpenAlex/Crossref have bogus "forthcoming"
        # future dates, so we FLOOR at >=2010 WITHOUT a date sort (relevance within the recent window)
        # to avoid surfacing 2050/2114 junk. S2 floors by year.
        _floor = {"from_year": "2010"}
        for qy in ARXIV_QUERIES:
            jobs.append({"connector":"arxiv","query":qy,"limit":limit or 30,"facets":{"sector":"ai"},"params":{"sort":"recent"}})
        for qy in OPENALEX_QUERIES:
            jobs.append({"connector":"openalex","query":qy,"limit":limit or 30,"facets":{"sector":"ai"},"params":_floor})
        for qy in S2_QUERIES:
            jobs.append({"connector":"semantic_scholar","query":qy,"limit":limit or 30,"facets":{"sector":"ai"},"params":_floor})
        for qy in CROSSREF_QUERIES:
            jobs.append({"connector":"crossref","query":qy,"limit":limit or 30,"facets":{"sector":"ai"},"params":_floor})
    return jobs

def main() -> int:
    _TRANCHES = ["depth","formd","openalex","arxiv","s2","crossref","wikidata","hn","reddit","lobsters",
                 "hf","stackoverflow","openreview","companies_house","yc","uspto","patents","gdelt","news",
                 "wikipedia","nsf","nih","expert","podcast","eng_blog","seminal","fulltext","github",
                 "recent","deeptech","all"]

    def _tranche(v: str) -> str:
        # allow `deeptech:<sector>` (e.g. deeptech:quantum) for one-sector-at-a-time runs
        if v in _TRANCHES or (v.startswith("deeptech:") and v.split(":", 1)[1] in DEEPTECH_BY_SECTOR):
            return v
        raise argparse.ArgumentTypeError(
            f"invalid tranche {v!r}; choose from {_TRANCHES} or deeptech:<{'|'.join(DEEPTECH_BY_SECTOR)}>")

    ap = argparse.ArgumentParser()
    ap.add_argument("tranche", type=_tranche)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--priority", type=int, default=0,
                    help="claim priority (higher = jumps the FIFO backlog; e.g. 500 for strategic sources)")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    jobs = build(a.tranche, a.limit)
    if a.priority:
        for jb in jobs:
            jb["priority"] = a.priority
    print(f"tranche={a.tranche}  jobs={len(jobs)}  priority={a.priority}  (limit/job default applied)")
    if a.dry:
        print(json.dumps(jobs[:5], indent=2)); print(f"... ({len(jobs)} total)"); return 0
    if not os.environ.get("ROSTER_ADMIN_TOKEN"):
        print("ERROR: set ROSTER_ADMIN_TOKEN", file=sys.stderr); return 2
    res = _post("/admin/corpus/ingest", {"jobs": jobs})
    print("enqueued:", res)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
