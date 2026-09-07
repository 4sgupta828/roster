"""GUIDED v3 — the recruiting consultant's VOCABULARY (docs/specs/guided-consultant-v3.md). The kernel (`facets/brief.py`)
owns the brief mechanics, the move parser and the gates; this module names the fields, maps them onto the contract,
says what readiness means, and writes the prompts: the consultant persona, the document reader, the JD interview."""
from __future__ import annotations

from dataclasses import dataclass

from .facet_schema import FACET_SCHEMA, VALUE_LABELS

DIRECTIONS = ("job", "candidate")                 # a seeker looks for a JOB; a hiring manager looks for a CANDIDATE
SEARCH_KIND = {"job": "job", "candidate": "person"}
DOCUMENT_LABEL = {"document": "from your document", "stored": "from your profile", "stated": "you said", "asked": "you said",
                  "inferred": "inferred — confirm?", "skipped": "skipped", "unknown": "unknown"}


@dataclass(frozen=True)
class BriefField:
    key: str                 # brief field name
    label: str               # what the user sees
    contract: tuple | None   # (contract key, mode) or None (artifact-only)
    directions: tuple = ("job", "candidate")
    ask: str = ""            # one line on what a consultant would ask about it (the planner's cue, never the question text)


# order = the consultant's order of impact within each direction (the planner is told it; code never forces it)
FIELDS: tuple[BriefField, ...] = (
    BriefField("direction", "Looking or hiring", None, ask="which side the user is on — never guessed from a bare title list"),
    # ---- hiring first (mission before title) ----
    BriefField("mission", "What this person owns", None, ("candidate",), "what they will own and what fails if unhired — the FIRST hiring question"),
    BriefField("success", "Success in six months", None, ("candidate",), "what good looks like at six months"),
    BriefField("hiring_reason", "Why now", None, ("candidate",), "backfill / new team / urgent gap / exploratory"),
    BriefField("must_have_done", "Must have done before", ("specialty", "prefer"), ("candidate",), "what they must have DONE (not skills lists)"),
    BriefField("trainable", "Can learn on the job", None, ("candidate",), "what is nice-to-have / trainable"),
    BriefField("calibration", "People like", None, ("candidate",), "calibration examples: people from which companies / roles"),
    BriefField("seniority_tradeoff", "Senior vs staff trade-off", None, ("candidate",), "strong senior vs weaker staff at this comp"),
    BriefField("team", "Team and reporting line", None, ("candidate",), "who they report to; team size and stage"),
    BriefField("evidence", "Public proof wanted", ("evidence", "must"), ("candidate",), "repos / papers / talks as a bar — or a preference"),
    BriefField("disqualifiers", "Rules out", None, ("candidate",), "job-relevant disqualifiers only"),
    # ---- seeker first (posture before field) ----
    BriefField("posture", "Step up, lateral or switch", None, ("job",), "the FIRST seeker question: step up, lateral in a better environment, or a switch"),
    BriefField("search_posture", "Active, passive or exploring", None, ("job",), "active / passive / exploratory"),
    BriefField("career_arc", "Career arc", None, ("job",), "the trajectory read from the résumé (tenure, moves, domain) — inferred, shown"),
    BriefField("positioning", "Story to lead with", None, ("job",), "what the résumé should lead with for this target"),
    BriefField("title_flexibility", "Title flexibility", None, ("job",), "titles they would and would not take"),
    BriefField("stage_appetite", "Company stage appetite", ("company_type", "prefer"), ("job",), "startup / public / big tech appetite"),
    BriefField("avoid_domains", "Domains to avoid", ("field", "avoid"), ("job",), "industries or domains to avoid"),
    BriefField("risk", "Risk tolerance", None, ("job",), "seed-stage risk vs stability"),
    BriefField("authorization", "Work authorization", None, ("job",), "sponsorship / authorization — factual, never a proxy"),
    BriefField("confidentiality", "Confidential search", None, ("job",), "whether the search is confidential"),
    # ---- shared ----
    BriefField("role_family", "Role", ("role_family", "prefer"), ask="the title family (backend engineer, CTO, growth marketer)"),
    BriefField("field", "Domain", ("field", "must"), ask="the domain the work belongs to"),
    BriefField("function", "Function", ("function", "prefer"), ask="the kind of work day to day"),
    BriefField("specialties", "Specialties", ("specialty", "prefer"), ask="1–3 specialties"),
    BriefField("skills", "Skills that matter", ("skill", "prefer"), ask="the few skills that would decide it — never a list to tick"),
    BriefField("level", "Level", ("level", "center"), ask="current level (seeker) / the level calibrated for (hiring)"),
    BriefField("work_type", "IC or manager", ("work_type", "prefer"), ask="hands-on IC vs manager vs executive — one question settles level posture too"),
    BriefField("metro", "Place", ("metro", "must"), ask="the metro — or remote; place-or-mode when both are said"),
    BriefField("work_mode", "Work mode", ("work_mode", "must"), ask="remote / hybrid / onsite"),
    BriefField("timezone", "Timezone / commute", None, ask="timezone or commute constraints"),
    BriefField("comp", "Compensation", ("comp", "prefer"), ask="floor vs target (seeker) / range and flexibility (hiring) — propose the market number first"),
    BriefField("comp_flexibility", "Comp flexibility", None, ask="hard cap vs equity offset"),
    BriefField("company_type", "Company type", ("company_type", "prefer"), ask="startup / public / Fortune 500 / big tech"),
    BriefField("employment_type", "Employment type", ("employment_type", "must"), ask="full time / contract / part time"),
    BriefField("timing", "Timing", None, ask="start date / urgency"),
    BriefField("deal_breakers", "Deal-breakers", None, ask="hard constraints, job-relevant only"),
)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}
FIELD_KEYS = set(FIELDS_BY_KEY)


def fields_for(direction: str) -> list[BriefField]:
    return [f for f in FIELDS if direction in f.directions]


def contract_mapping(direction: str) -> dict:
    """{brief field: (contract key, mode)} for the fields that reach the search — only keys the search kind has."""
    kind = SEARCH_KIND.get(direction, "job")
    legal = {k.key for k in FACET_SCHEMA.for_kind(kind)}
    return {f.key: f.contract for f in fields_for(direction) if f.contract and f.contract[0] in legal}


# readiness (spec §4.4): known or explicitly skipped before the search is proposed
REQUIRED = {"job": ("direction", "posture", "role_family", "level", "metro"),
            "candidate": ("direction", "mission", "role_family", "level", "metro")}
REMOTE_SETTLES = ("metro",)          # a remote role / a remote-only seeker has no metro to ask for
CONTRACT_KEYS_FOR_EFFECTS = ("field", "function", "specialty", "skill", "role_family", "level", "work_type", "metro", "state", "country", "work_mode",
                             "employment_type", "comp", "company_type", "evidence")
MAX_FORKS = 4                  # forks per intake before `ready` is offered anyway
MAX_PLANNER_CALLS = 8
NEVER_ASK = ("current salary", "age", "gender", "race", "religion", "nationality", "family plans", "health", "years of experience as a number",
             "a list of skills to tick", "what field are you in (when the résumé says)", "generic strengths / culture")
JD_INTERVIEW = ("mission", "must_have_done", "trainable", "level", "deal_breakers", "team")   # order of impact; peers supply the market centre


def option_label(key: str, value: str) -> str:
    return VALUE_LABELS.get(str(value)) or str(value).replace("_", " ")


def _vocab(kind: str) -> str:
    return "; ".join(f"{k.key}: {', '.join(k.values)}" for k in FACET_SCHEMA.for_kind(kind) if k.values)


def direction_prompt() -> str:
    """The first read when the words leave the side open: settle it or ask it — nothing else."""
    return ("You are a recruiting consultant meeting someone new. Decide from their words whether they are LOOKING for a role for themselves "
            "(direction \"job\") or HIRING people (direction \"candidate\"). Signals for looking: I'm looking, my next role, my résumé, roles for me. "
            "Signals for hiring: hire, hiring, we need, our team, candidates, a JD. A bare list of titles or skills is AMBIGUOUS — ask. Return ONLY JSON: "
            "{\"move\": \"infer\" | \"fork\", \"say\": ≤ 2 sentences, \"brief_delta\": {\"direction\": {\"value\": \"job\" | \"candidate\", \"source\": \"stated\", \"span\": their words}} "
            "when clear, or \"question\": {\"field\": \"direction\", \"text\": ..., \"options\": [{\"label\": \"Looking for a role\", \"value\": \"job\"}, "
            "{\"label\": \"Hiring\", \"value\": \"candidate\"}]} when ambiguous. Never guess.")


def consultant_prompt(direction: str) -> str:
    kind = SEARCH_KIND.get(direction, "job")
    who = "a job seeker" if direction == "job" else "a hiring manager"
    fields = "\n".join(f"- {f.key}: {f.label} — {f.ask}" for f in fields_for(direction) if f.key != "direction")
    first = ("the seeker's FIRST question is posture — step up, lateral in a better environment, or a switch — read from the career arc; "
             "never 'what field are you in' when the résumé says it" if direction == "job" else
             "the hiring manager's FIRST question is the mission — what this person will own and what fails if they are not hired; "
             "never title, years or a skills list first")
    return (f"You are a senior recruiting consultant working with {who}. You keep a BRIEF (structured understanding) and make ONE MOVE per turn. "
            "A consultant turn is: an OBSERVATION from the evidence → the DECISION POINT that changes the search most → its CONSEQUENCE → "
            "2–3 concrete options. Hypothesis-led: propose a default with the market number and ask for variance ('Staff ML in SF clusters "
            "$200–250k base — does that match, or higher?'); consolidate intent ('hands-on IC or management track?' settles level posture, "
            f"role and work type together). {first}. Never ask what the brief already knows (source document / stored / stated / asked); "
            f"never ask: {', '.join(NEVER_ASK)}. Never write a line that has no source. Be direct and brief; say WHY when you ask.\n"
            "Return ONLY JSON: {\"move\": one of infer | confirm | fork | challenge | trade_off | draft | split | ready | restart, "
            "\"say\": ≤ 2 sentences to the user, \"brief_delta\": {field: {\"value\", \"source\": \"inferred\" | \"stated\", \"span\": the words it came from}}, "
            "\"question\": null | {\"field\": the brief field it resolves, \"text\", \"why\": why it matters for the search, "
            "\"options\": [{\"label\", \"value\", \"effect\": {\"must\" | \"prefer\" | \"avoid\": {key: [values]}, \"center\": {\"key\", \"value\"}}}], \"multi\": bool}, "
            "\"contract_delta\": same effect shape, \"ready\": bool}. ONE question at most; its options MUST come from the LEVERAGE given "
            "(measured splits of the current pool) or be a confirm / posture / mission question. An option's `effect` is a SEARCH effect and uses ONLY "
            f"these contract keys: {', '.join(CONTRACT_KEYS_FOR_EFFECTS)} — never a brief field name; every option of one question uses the SAME shape "
            "(all `center` on level, or all `must` on one key). A brief-only question (direction, posture, mission, deal_breakers, timing …) carries "
            f"`value` only and NO effect. Vocabularies — {_vocab(kind)}; open keys (skill, specialty, role_family) take lowercase tokens; metro takes a "
            "token from METRO TOKENS given below, never free text. A REMOTE role has no metro: record work_mode = remote and never ask which city. "
            "If any REQUIRED field is still open, your move is a question on the most "
            "impactful open one (or `ready` when the user asks to search). "
            "Every turn, RECORD every fact the user states into brief_delta (comp, skills, must_have_done, work_mode, timing, deal_breakers, team …) — "
            "nothing the user said may be lost. A free-text question has \"options\": [] (never a placeholder option). "
            "`stated` only for what the user actually wrote; `inferred` for your reading (it ranks, never filters, until confirmed). "
            "`ready` when the required fields are settled or the user asks to search: `say` STATES the search in one line and the assumptions "
            "you are making (never 'I will now summarize'). "
            "`restart` ONLY when the user literally asks to start over or says they are on the other side after all; `split` when there are two roles. `draft` to build or revise the "
            f"document.\nBRIEF FIELDS for {who}:\n{fields}")


def document_reader_prompt(kind: str, direction: str) -> str:
    """Reads a résumé / JD / profile INTO the brief: value + the exact span it came from; nothing inferred beyond the words."""
    what = {"resume": "a résumé or self-description", "jd": "a job description or a hiring manager's role notes", "profile": "a stored profile record"}.get(kind, "a document")
    fields = "\n".join(f"- {f.key}: {f.label}" for f in fields_for(direction) if f.key != "direction")
    arc = ("Also write `career_arc`: two sentences on tenure, trajectory and domain (e.g. 'six years in ML infrastructure, from senior engineer to "
           "head of a 12-person team; fintech throughout') — this is the one field you may compose; mark it source inferred. "
           if kind == "resume" else "Also write `mission` from the responsibilities if the text states what the person owns. ")
    return (f"You read {what} into a recruiting brief. Return ONLY JSON: {{\"fields\": {{field: {{\"value\": short, \"span\": the exact sentence or "
            f"phrase it comes from}}}}}}. Only fields the text STATES; never invent; keep values under 12 words; for level use the title's own words. "
            f"{arc}Fields:\n{fields}\nVocabulary hints — {_vocab(SEARCH_KIND.get(direction, 'job'))}; for metro write the index token "
            "(new_york, bay_area, seattle, los_angeles, boston, chicago, austin, london, bangalore, remote) when the place is one of them.")


def jd_assemble_prompt() -> str:
    return ("You write a complete job description from (a) the BRIEF (the hiring manager's own answers — cite as \"you\") and (b) the market "
            "CENTRE (requirement groups that recur across peer postings, each with its count). Return ONLY JSON: {\"title\": str, \"summary\": "
            "≤ 70 words, \"sections\": [{\"heading\": str, \"lines\": [str], \"source\": \"you\" | \"peers\" | \"both\"}]}. Sections: What you will "
            "own; What you must have done; Nice to have; Level and team; Location and mode; Compensation (only if the manager gave a range — "
            "peers' pay is a market signal shown separately). Every section's source is honest; never a line that neither the manager nor the "
            "peers said. Plain, specific language; no boilerplate.")
