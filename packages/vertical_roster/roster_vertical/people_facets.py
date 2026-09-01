"""People-enumeration facet vocabulary + the facet-PARSE prompt (roster domain vocabulary — Rule 18).

The LLM acts as a QUERY COMPILER: it turns a free-text people-discovery question into a normalized
facet filter (it does NOT process the population). Code owns the filter + grounding; the LLM owns only
this semantic normalization (role synonyms, geo rollups, function synonyms). All domain vocabulary
lives HERE, never in the kernel or the app engine.
"""
from __future__ import annotations

# The closed set of facet KEYS the compiler may emit (all filterable). `role` = the functional JOB
# (what they DO), distinct from `seniority` (the LEVEL) and `function` (the DOMAIN). NOTE: `title` is a
# display-only stored facet (the raw bio) and is DELIBERATELY excluded here — the model kept mis-filing
# job titles into `title` instead of `role`, so it is not an emit option.
PEOPLE_FACET_KEYS = ("role", "seniority", "function", "industry", "metro", "company", "worked_at",
                     "country", "state", "stage", "accelerator", "skill")

_ROLE = ("software_engineer", "ml_engineer", "system_architect", "solutions_architect", "data_scientist",
         "data_engineer", "research_scientist", "product_manager", "sre", "security_engineer",
         "designer", "devops_engineer", "engineering_manager", "researcher")

# Illustrative normalized VALUES per key (the LLM normalizes to snake_case; this guides it, it is not
# an exhaustive enum — new values are allowed as long as they are normalized consistently).
_SENIORITY = ("c_level", "vp", "director", "principal", "senior_manager", "engineering_manager",
              "staff", "senior", "mid", "junior", "founder")
_FUNCTION = ("machine_learning", "infrastructure", "backend", "frontend", "data", "product",
             "security", "research", "hardware", "design")
_METRO = ("bay_area", "nyc", "seattle", "boston", "los_angeles", "austin", "london", "remote")
# Business SECTORS (the employer's industry) — distinct from _FUNCTION (technical domain). ONE
# canonical value per sector (fintech/payments BOTH canonicalize to `payments`, so the query and the
# stored ingest share one vocabulary — the normalization-alignment invariant).
_INDUSTRY = ("payments", "ai", "healthcare", "ecommerce", "crypto", "gaming", "adtech",
             "biotech", "cybersecurity", "automotive", "aerospace_defense", "social_media",
             "cloud_infrastructure", "edtech", "media")

PEOPLE_FACET_PARSE_PROMPT = f"""You compile a people-search question into a normalized facet filter.

This is a PEOPLE-SEARCH product: DEFAULT to treating the input as a search for PEOPLE. If it contains
ANY role, job title, expertise, field, seniority, company, or location — even as BARE KEYWORDS with no
verb (e.g. "data science chemistry Google" → data scientists working in chemistry at Google;
"staff engineers NYC" → staff engineers in NYC) — EXTRACT those into facets. Prefer returning facets
over nothing. Output {{}} ONLY when the input is clearly NOT about finding people (a definition request,
a how-to, a yes/no, or an explanation of a concept).

Output ONLY a compact JSON object mapping facet_key → list of normalized values (snake_case). Include a
key ONLY if the question constrains it. Valid keys: {", ".join(PEOPLE_FACET_KEYS)}.

Normalize synonyms to canonical snake_case values, e.g.:
- role (the functional JOB / expertise — WHAT they do, distinct from level and domain): "System
  Architect" → ["system_architect"]; "Data Scientist" → ["data_scientist"]; "ML Engineer" →
  ["ml_engineer"]; "Solutions Architect" → ["solutions_architect"]; "Product Manager" →
  ["product_manager"]; "Research Scientist" → ["research_scientist"]; "SRE" → ["sre"];
  "researcher(s)"/"scientist(s)"/"academic(s)"/"professor(s)" → ["researcher"];
  "doctors"/"physicians"/"clinicians" → ["physician"]. When the ONLY people word is "researchers at
  <place/field>", emit role=["researcher"] plus the field/company — do NOT put "research" in function.
  Examples: {", ".join(_ROLE)}.
- seniority (canonical values): "Directors" → ["director"]; "EMs"/"Engineering Managers" →
  ["engineering_manager"]; "VPs"/"SVP"/"EVP" → ["vp"]; "C-suite"/"CTO"/"CEO"/"Chief X" → ["c_level"];
  "Head of X" → ["head"]; "Principal"/"Distinguished"/"Fellow" → ["principal"]. A GROUP word maps to
  the SET of leadership levels: "leaders"/"leadership"/"heads"/"execs"/"management" → ["c_level", "vp",
  "director", "head", "senior_manager", "engineering_manager", "principal"]. Examples:
  {", ".join(_SENIORITY)}.
- A ranking word ("top", "best", "most senior", "leading") is NOT a facet — DROP it; the results are
  returned unranked. Only translate the actual role/function/place constraints into facets.
- function (the research FIELD / technical DOMAIN): emit the STANDARD academic snake_case name of the
  field so it matches the stored concept vocabulary — do NOT collapse a SPECIFIC field into a generic
  bucket. A specific AI/CS subfield keeps its OWN name: "computer vision"/"vision" → ["computer_vision"];
  "NLP"/"natural language processing" → ["natural_language_processing"]; "reinforcement learning"/"RL"
  → ["reinforcement_learning"]; "deep learning" → ["deep_learning"]; "robotics" → ["robotics"];
  "speech"/"speech recognition" → ["speech_recognition"]; "graphics"/"computer graphics" →
  ["computer_graphics"]; "cryptography" → ["cryptography"]; "bioinformatics"/"computational biology" →
  ["bioinformatics"]; "genomics" → ["genomics"]; "quantum computing" → ["quantum_computing"]. Only the
  GENERIC family words collapse: "machine learning"/"ML" → ["machine_learning"]; "AI"/"artificial
  intelligence"/"LLMs"/"generative AI"/"foundation models" → ["artificial_intelligence",
  "machine_learning"] (emit BOTH — either concept may carry the person). "data science"/"analytics" →
  ["data_science"]; "infra"/"infrastructure" → ["infrastructure"]; "backend" → ["backend"]; "frontend"
  → ["frontend"]; "security" (engineering) → ["security"]. Map an OCCUPATION word to its field:
  "physicists" → ["physics"]; "biologists" → ["biology"]; "chemists" → ["chemistry"]; "economists" →
  ["economics"]; "neuroscientists" → ["neuroscience"]; "mathematicians" → ["mathematics"];
  "computer scientists" → ["computer_science"]; "medicine"/"medical"/"clinicians" → ["medicine"].
  MEDICAL SPECIALTY → its field: "cardiologists" → ["cardiovascular_disease"]; "pediatricians" →
  ["pediatrics"]; "psychiatrists" → ["psychiatry"]; "radiologists" → ["radiology"]; "dermatologists" →
  ["dermatology"]; "neurologists" → ["neurology"]; "oncologists" → ["hematology_oncology"]; "surgeons" →
  ["surgery"]; "gastroenterologists" → ["gastroenterology"]; "anesthesiologists" → ["anesthesiology"].
  NEVER emit "research" as a function — "researcher(s)"/"scientist(s)"/"academic(s)" is a ROLE
  (["researcher"]), NOT a function. BUSINESS FUNCTIONS (the non-technical areas that run a company) are
  ALWAYS `function` values, NEVER a `role`: "sales leaders at Stripe" → {{"function": ["sales"],
  "seniority": ["c_level","vp","director","head"], "company": ["stripe"]}} — do NOT emit role=["sales"];
  "salespeople"/"marketers"/"recruiters"/"marketing team" are `function`, not `role`. The values:
  "sales"/"salespeople"/"account executives"/"revenue"/"GTM" → ["sales"];
  "marketing"/"growth"/"brand"/"comms"/"PR" → ["marketing"]; ANY recruiting/hiring INTENT →
  ["recruiting"] — "recruiters"/"recruiting"/"talent"/"talent acquisition"/"sourcers"/"technical
  recruiters"/"who does hiring"/"who hires"/"people who recruit" all → function=["recruiting"] (NEVER
  role=["recruiter"]/["sourcer"] — understand the intent, don't invent a role); "business
  development"/"BD"/"partnerships"/"corporate development" →
  ["business_development"]; "finance"/"accounting" → ["finance"]; "HR"/"people ops"/"human resources" →
  ["human_resources"]; "operations"/"ops"/"bizops" → ["operations"]; "legal"/"counsel"/"compliance" →
  ["legal"]; "customer success"/"support"/"CX" → ["customer_success"]. Examples: {", ".join(_FUNCTION)}.
- industry (the BUSINESS SECTOR the person's EMPLOYER operates in — distinct from `function`, which
  is a TECHNICAL domain). Emit this for "<sector> industry / space / sector / companies" phrases and
  bare sector words: "payment industry"/"payments"/"fintech"/"financial technology"/"payment
  processing" → ["payments"]; "AI companies"/"AI labs"/"AI industry"/"foundation model companies" →
  ["ai"] (the EMPLOYER is an AI lab — distinct from `function`, the person's technical field);
  "streaming"/"media companies"/"entertainment industry" → ["media"];
  "healthcare industry"/"health tech"/"digital health" → ["healthcare"];
  "e-commerce"/"online retail" → ["ecommerce"]; "crypto"/"web3"/"blockchain industry" → ["crypto"];
  "gaming industry"/"video games" → ["gaming"]; "adtech"/"advertising" → ["adtech"]; "biotech"/
  "biotechnology" → ["biotech"]; "cybersecurity industry" → ["cybersecurity"]; "automotive"/"self-
  driving" → ["automotive"]; "aerospace"/"defense" → ["aerospace_defense"]; "edtech" → ["edtech"];
  "social media" → ["social_media"]; "cloud/infrastructure companies" → ["cloud_infrastructure"].
  A sector word describes WHO the employer is; a `function` describes what the person technically does
  — "ML engineers in fintech" → {{"role": ["ml_engineer"], "industry": ["payments"]}}. Examples:
  {", ".join(_INDUSTRY)}.
- metro: "Bay Area"/"SF"/"San Francisco"/"Silicon Valley"/"Palo Alto" → ["bay_area"];
  "New York" → ["nyc"]. Examples: {", ".join(_METRO)}.
- country (a COUNTRY named in the query — a 2-letter lowercased code): "US"/"USA"/"United States"/
  "America" → ["us"]; "UK"/"United Kingdom"/"Britain" → ["uk"]; "Germany" → ["de"]; "France" → ["fr"];
  "India" → ["in"]; "Canada" → ["ca"]; "Japan" → ["jp"]; "China" → ["cn"]; "Ireland" → ["ie"];
  "Netherlands" → ["nl"]. Emit ONLY when the query explicitly names a country (default scope is applied
  separately by the app, so do NOT infer a country from a city).
- state (a US STATE named in the query — its 2-letter lowercased code): "California"/"Calif" → ["ca"];
  "New York State" → ["ny"]; "Texas" → ["tx"]; "Washington State" → ["wa"]; "Massachusetts" → ["ma"];
  "Illinois" → ["il"]; "Georgia" → ["ga"]; "Colorado" → ["co"]. A CITY is `metro`, not `state`.
- stage (the EMPLOYER's company STAGE — NOT an industry): "startup"/"startups"/"early-stage"/"seed"/
  "pre-seed"/"YC companies"/"early stage" → ["startup"]; "public companies"/"publicly traded"/"Fortune
  500"/"big tech"/"large enterprise"/"public company" → ["public"]. IMPORTANT: "startup"/"startups" is a
  STAGE, NEVER an `industry` — do NOT emit industry=["startups"] (there is no such sector). "CEO"/"chief
  executive"/"chief X officer" is a LEVEL → seniority=["c_level"], NOT a role — do NOT emit
  role=["founder"] for a CEO (a public-company CEO is usually NOT a founder). Only the word
  "founder(s)"/"co-founder(s)" → role=["founder"]. Examples: "CEOs of startups" → {{"seniority":
  ["c_level"], "stage": ["startup"]}}; "CEOs of public companies" → {{"seniority": ["c_level"], "stage":
  ["public"]}}; "startup founders" → {{"role": ["founder"], "stage": ["startup"]}}.
- accelerator (the INCUBATOR / accelerator / venture-studio / fund that BACKED the founder's startup —
  a curated proper noun, NOT an industry). Recognize these KNOWN backers and map "<X> founders" /
  "founders backed by <X>" / "<X> portfolio" / "incubated by <X>" to accelerator=[slug]:
  "Y Combinator"/"YC" → ["yc"]; "AIFund"/"AI Fund" → ["ai_fund"]; "Techstars" → ["techstars"];
  "South Park Commons"/"SPC" → ["south_park_commons"]; "Entrepreneur First"/"EF" → ["entrepreneur_first"];
  "Antler" → ["antler"]; "500 Global"/"500 Startups" → ["500_global"]; "Pioneer" → ["pioneer"];
  "Pear"/"Pear VC" → ["pear"]; "SOSV"/"HAX"/"IndieBio" → ["sosv"].
  CRITICAL: "AIFund"/"AI Fund" is the FUND named "AI Fund" → accelerator=["ai_fund"], it is NOT
  industry=["ai"]. A backer name that looks like keywords is still a proper noun — do NOT turn it into
  an industry/function. (A non-accelerator company like "Google" in "Google founders" stays `company`.)
- skill (a specific TECHNOLOGY / programming language / framework / tool the person works with — NOT a
  research field). Emit the lowercased snake_case tech name: "CUDA" → ["cuda"]; "Kubernetes"/"k8s" →
  ["kubernetes"]; "PyTorch" → ["pytorch"]; "TensorFlow" → ["tensorflow"]; "React" → ["react"]; "Rust" →
  ["rust"]; "Go"/"Golang" → ["go"]; "Python" → ["python"]; "C++" → ["cpp"]; "TypeScript" →
  ["typescript"]; "Kafka" → ["kafka"]; "Terraform" → ["terraform"]; "Solidity" → ["solidity"]. A
  concrete tech/language/framework is a `skill`, NEVER a `function` (a broad field) — "CUDA engineers" →
  {{"role": ["software_engineer"], "skill": ["cuda"]}}; "Rust developers at Stripe" → {{"role":
  ["software_engineer"], "skill": ["rust"], "company": ["stripe"]}}.
- company: a specific employer → its CANONICAL lowercased short name — drop legal suffixes (Inc, LLC,
  Corp, Platforms) and apply known aliases: "Facebook"/"Meta Platforms" → ["meta"]; "Alphabet"/"Google
  LLC" → ["google"]; "Twitter" → ["x"]; "AWS" → ["amazon"]. E.g. "at Stripe" → {{"company": ["stripe"]}}.
- worked_at (PAST employer / work history — DISTINCT from current company): "worked at Google",
  "previously at Google", "ex-Google", "formerly at", "used to work at", "alumni of Google" →
  {{"worked_at": ["google"]}} (same canonicalization as company). "works at" / "at" / "@" = CURRENT →
  use `company`, NOT `worked_at`.

A single facet may list SEVERAL acceptable values (OR within the key): "Directors or EMs" →
{{"seniority": ["director", "engineering_manager"]}}.

If the question is about ONE SPECIFIC NAMED INDIVIDUAL (identity/profile — e.g. "who is Jane Doe",
"Jane Doe who worked at Acme", "tell me about John Smith the ML director"), do NOT emit facets —
instead set "person" to their full name and "person_context" to any employer/role hints that
disambiguate them (e.g. "Tubi, Netflix, ML infrastructure"). Leave the facet lists empty.

If the question is none of these (a definition, a yes/no, not about people), output exactly {{}}.

Question: {{question}}
JSON:"""


def facet_parse_prompt(question: str) -> str:
    return PEOPLE_FACET_PARSE_PROMPT.replace("{question}", question)


# ── REFINEMENT compile (conversation turns 2+): the model applies the user's change to the RUNNING
# filter — narrow (add a key), expand (add values to a key), replace a dimension, or remove one —
# and returns the FULL updated filter. The user leads; code never guesses merge semantics (Rule 18).
_REFINE_SUFFIX = """

REFINEMENT MODE: the user is refining an ONGOING people search. The CURRENT filter (JSON) is:
{current}

Apply the user's utterance to that filter and output the FULL UPDATED filter (same JSON shape,
same valid keys and normalization rules as above):
- narrowing ("only the ones who worked at Google", "just staff+") → ADD/REPLACE those keys, keep the rest;
- expansion ("also include Munich", "add data scientists too") → APPEND the new values to that key;
- removal ("any location", "drop the company filter", "ignore seniority") → REMOVE that key, keep the rest;
- replacement ("in Munich instead") → REPLACE that key's values;
- a fresh unrelated people search → output the NEW search's filter alone.
Output {} ONLY when the utterance is clearly not about this people search at all.

Utterance: {question}
JSON:"""


def facet_refine_prompt(question: str, current_filter_json: str) -> str:
    return (PEOPLE_FACET_PARSE_PROMPT.split("Question: {question}")[0]
            + _REFINE_SUFFIX.replace("{current}", current_filter_json)
                            .replace("{question}", question))
