# Roster — A "shadow LinkedIn" built entirely from public data, with a citation for every fact

*A LinkedIn post. Repo: https://github.com/4sgupta828/roster*

---

**The problem with finding people and companies today:**

Recruiters, founders, BD, investors — everyone needs the same thing: *"find the right people, understand how they're connected, and know it's true."* The tools we have force a bad trade. LinkedIn is a walled garden. Data brokers sell stale, opaque enrichment with no provenance. And general chatbots will happily invent a plausible bio, a job that never existed, or a connection that isn't there — with total confidence and zero sources. For people-data, a confident fabrication isn't a glitch; it's the whole risk.

**What I explored: Roster — a *grounded* platform for searching professionals and companies, reconstructed from publicly available information.**

The core insight: **"people questions" are not one thing.** "Find all ML directors in the Bay Area," "who is this specific person," and "how is X connected to Y" are three completely different problems, and a single web-search answers none of them well. So Roster **classifies** each question with an LLM query-compiler and routes it to a purpose-built engine:
- an **enumeration engine** (faceted graph queries: role × function × company × location, intersected in SQL),
- a **profile-card engine** (a grounded bio + real GitHub/X/LinkedIn links),
- a **connection engine** (a path where every hop cites its evidence).

Every person, attribute, and edge ties back to a real public source (GitHub, company pages, scholarly graphs, filings, the open web) — and it's honest about what it doesn't know yet. The moat isn't a prettier answer than ChatGPT; it's **grounded, current, structured** intelligence you can act on.

**What AI solves well:**
- Understanding messy natural-language intent and compiling it into structured facets — "VPs of ML in the Bay Area" → role × function × location.
- Reconciling entities across noisy public sources (canonicalizing company names, resolving the same person across profiles).

**What AI does NOT solve — and where code must own it:**
- The graph itself. Enumeration, intersection, and connection paths are *queries* over a structured index, not something to hallucinate in prose. The LLM parses intent; SQL and the graph produce the answer.
- Grounding. Every claim needs a real source attached, or it doesn't ship. "The model is pretty sure" is not evidence.

**What stays genuinely hard:**
- Entity resolution at scale: same name, different people; same person, five profiles. This is the classic record-linkage problem and it's *the* hard core of people-data.
- Coverage honesty: the hardest UX is saying "here's what I found, and here's what I don't know yet" instead of fabricating to fill the gap.
- Freshness and ethics: public data changes, and building on it responsibly (public-only, consent-aware, honest) is a first-class constraint, not an afterthought.

**How to take it from here:**
- Treat the people/company graph as the durable asset; every surface (search, profile, connections) is a view over it.
- Grounded coverage-driven retrieval so a *sample* of results never silently becomes "the answer."
- Provenance on every edge, so a connection path is auditable end to end.

**Products this could become:**
- Grounded sourcing/recruiting where every candidate fact is cited.
- Relationship intelligence for BD and fundraising ("who do we know who knows them, and why do we believe it").
- A public-data enrichment API that ships *sources*, not just fields.

**To go deeper, look up:** entity resolution / record linkage, knowledge-graph construction, hybrid retrieval, and the people-data landscape (Clearbit, Apollo, People Data Labs) — then ask what changes when every fact must cite a public source.

The takeaway: **the winning people-search product isn't the one with the most data — it's the one where every fact is grounded, current, and honest about its gaps.**

#KnowledgeGraphs #EntityResolution #Recruiting #DataEngineering #AI #PeopleAnalytics
