"""GUIDED INTAKE vocabulary (vertical — docs/specs/guided-intake.md §1, §2.1): the two directions, the artifact
each needs, the completeness CHECKLIST per artifact (which items are required, which contract key an answer
feeds and how), the REQUIRED / OPTIONAL keys per direction, the question words, and the two prompts (the
completeness read; the per-turn answer read). Mechanics (whether to ask, budgets, the state) live in
`roster_kernel.facets.intake`; this module only knows the roster domain."""
from __future__ import annotations

from dataclasses import dataclass

from .facet_schema import FACET_SCHEMA, VALUE_LABELS

DIRECTIONS = ("job", "candidate")                       # I am looking for a role | I am hiring
ARTIFACT_FOR = {"job": "profile", "candidate": "jd"}
SEARCH_KIND = {"job": "job", "candidate": "person"}     # the entity kind the direction searches


@dataclass(frozen=True)
class Item:
    name: str
    required: bool
    key: str | None = None        # the contract key an answer feeds (None = artifact-only)
    mode: str = "must"            # must | prefer
    words: str = ""               # the question, plain
    hint: str = ""                # one line under the question


CHECKLIST: dict[str, tuple[Item, ...]] = {
    "profile": (
        Item("current_role", True, None, "must", "What is your current title, and where?", "e.g. 'senior backend engineer at Acme'"),
        Item("level", True, "level", "prefer", "How senior are you today?", "the level your title carries"),
        Item("years", True, None, "must", "Roughly how many years of professional experience?", ""),
        Item("field", True, "field", "must", "Which field is your work in?", "the domain, not the job title"),
        Item("skills", True, "skill", "prefer", "Which three to five skills or systems do you own?", "as they would appear on a posting"),
        Item("location", True, "metro", "must", "Where should roles be?", "a metro, a state, or remote"),
        Item("comp", True, "comp", "must", "What base salary range are you aiming for?", "a band; you can decline"),
        Item("target_level", False, "level", "prefer", "Should I lean toward roles a step up?", ""),
        Item("company_types", False, "company_type", "prefer", "Any preference on company type?", "startup, public, Fortune 500, big tech"),
        Item("avoid", False, None, "must", "Anything to avoid — industries, on-site only, contract work?", ""),
        Item("notable_work", False, None, "must", "Public work I should know about — repos, papers, talks?", ""),
        Item("authorization", False, None, "must", "Anything on work authorization I should factor in?", ""),
    ),
    "jd": (
        Item("title", True, None, "must", "What is the title of the role?", ""),
        Item("level", True, "level", "must", "What level is this hire?", ""),
        Item("location", True, "metro", "must", "Where is the role — which metro, or remote?", ""),
        Item("must_skills", True, "skill", "must", "Which three or more skills or systems are must-haves?", "the ones you would reject without"),
        Item("responsibilities", True, None, "must", "What will this person actually do — two or three responsibilities?", ""),
        Item("comp", True, None, "must", "What is the base salary range, or is it undisclosed?", "peers' stated ranges are shown as a market signal"),
        Item("team", False, None, "must", "Which team, and who does the role report to?", ""),
        Item("nice_to_have", False, "skill", "prefer", "Any nice-to-haves?", ""),
        Item("evidence", False, "evidence", "must", "Do you want public proof of work — repos, papers, talks?", ""),
        Item("disqualifiers", False, None, "must", "Anything that rules a candidate out?", ""),
        Item("company_context", False, None, "must", "What should candidates know about the company — stage, product, why now?", ""),
    ),
}

# Keys a compiled must may be DEMOTED on when it collapses the pool (spec §12.4.2): open-phrase / many-valued sets.
# Geo keys are set-typed too but are promises the index keeps — they are relaxed only by the place-or-mode rule.
RELAXABLE_KEYS = ("skill", "specialty", "role_family")

# Contract keys the intake must know before a search runs (asked regardless of the corpus) and the keys it may
# ask about when the pool is split on them (spec §0.2).
REQUIRED_KEYS = {"job": ["field", "level", "metro"], "candidate": ["field", "level", "metro"]}
# evidence is OPTIONAL for a hiring flow: proof of work ranks candidates first; as a must it emptied a CRM /
# marketing-engineering search of everyone relevant (owner report 2026-09-05). The rail can still require it.
OPTIONAL_KEYS = {"job": ["company_type", "work_mode", "function", "employment_type"],
                 "candidate": ["evidence", "company_type", "work_type", "function", "years"]}

QUESTION_WORDS: dict[str, dict] = {
    "field": {"job": "Which field should the roles be in?", "candidate": "Which field is the role in?"},
    "level": {"job": "What level of role?", "candidate": "What level is the hire?"},
    "metro": {"job": "Which metro — or remote?", "candidate": "Which metro is the role in?"},
    "evidence": {"candidate": "Should candidates with public work — repos, papers, talks — rank first?", "job": "Should roles favour teams with public work?"},
    "company_type": {"job": "Any preference on company type?", "candidate": "Should candidates come from a particular company type?"},
    "work_mode": {"job": "Remote, hybrid, or on-site?", "candidate": "Remote, hybrid, or on-site?"},
    "function": {"job": "Which kind of work day to day?", "candidate": "Which kind of work day to day?"},
    "employment_type": {"job": "Full-time, contract, or internship?", "candidate": "Full-time, contract, or internship?"},
    "work_type": {"candidate": "An individual contributor, a manager, or a founder?", "job": "IC or manager roles?"},
    "years": {"candidate": "How many years of experience?", "job": "How many years of experience?"},
    "comp": {"decline": "Prefer not to say"},
}

_DIRECTION_WORDS = {"job": "roles", "candidate": "people"}


def required_items(artifact: str) -> list[str]:
    return [it.name for it in CHECKLIST.get(artifact, ()) if it.required]


def item(artifact: str, name: str) -> Item | None:
    return next((it for it in CHECKLIST.get(artifact, ()) if it.name == name), None)


def question_for(kind: str, name: str, ctx: str) -> str:
    """The words for a question: an ITEM (ctx = artifact) or a KEY (ctx = direction)."""
    if kind == "item":
        it = item(ctx, name)
        return it.words if it else f"{name.replace('_', ' ')}?"
    w = QUESTION_WORDS.get(name) or {}
    return w.get(ctx) or w.get("job") or w.get("candidate") or f"{name.replace('_', ' ')}?"


def option_label(key: str, value: str) -> str:
    return VALUE_LABELS.get(value) or str(value).replace("_", " ")


def completeness_prompt(artifact: str) -> str:
    items = CHECKLIST.get(artifact, ())
    names = ", ".join(it.name for it in items)
    what = "a candidate's résumé or self-description" if artifact == "profile" else "a job description or a hiring manager's role notes"
    closed = [(it.name, FACET_SCHEMA.key(it.key)) for it in items if it.key and FACET_SCHEMA.key(it.key) is not None and FACET_SCHEMA.key(it.key).values]
    vocab = "; ".join(f"{name}: {', '.join(k.values)}" for name, k in closed)
    return (f"You read {what} and report which of these items it states: {names}.\n"
            "Return ONLY JSON: {\"present\": {item: the value as stated, short}, \"tokens\": {item: one vocabulary token for a PRESENT "
            "item that has a vocabulary}, \"missing\": [items it does not state at all], "
            "\"weak\": [items it touches but too thinly to search on]}. Every item appears in exactly one of present / missing / weak. "
            "Never infer an item the text does not state; a level is 'missing' when no title word states it; comp is 'missing' "
            "unless a figure or range is written. Keep values verbatim-ish and under 12 words. "
            f"Vocabularies — {vocab}. A token is the vocabulary word the stated value MEANS (a 'Head of' / VP / CTO / director title "
            "carries the leadership level; principal / staff is staff_plus); never a token for an item that is missing or weak.")


def direction_prompt() -> str:
    """The first read: is the user looking for a role for themselves, or hiring people? Unambiguous tokens —
    'candidate' would mean both things here."""
    return ("Read the user's message and return ONLY JSON {\"direction\": \"looking\" | \"hiring\" | null}. "
            "\"looking\" = the user wants a role / job for themselves (their own next role, their résumé, jobs to apply to). "
            "\"hiring\" = the user wants to find or hire PEOPLE for a role (a job description, candidates, a team to staff). "
            "null when the message does not CLEARLY say which — a bare list of titles, skills or a role name (\"Founder CTO or VP "
            "Engineering, ML infra\") is ambiguous: it could be the user's own target or a hire. Signals for looking: I'm looking, "
            "for me, my next role, my résumé, roles I can apply to. Signals for hiring: hire, hiring, we need, our team, candidates, "
            "a JD. Never guess.")


DIRECTION_TOKENS = {"looking": "job", "hiring": "candidate"}


def turn_prompt(direction: str) -> str:
    keys = ", ".join(REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction])
    vocab = "; ".join(f"{k.key}: {', '.join(k.values)}" for k in FACET_SCHEMA.for_kind(SEARCH_KIND[direction]) if k.values and k.key in set(REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction]))
    return (f"You are the intake for a {_DIRECTION_WORDS[direction]} search. The user was asked ONE question (given) and replied. "
            "Map the reply onto the question and onto any other item or key the reply CLEARLY states. Return ONLY JSON: "
            "{\"answers\": {name: value | null}, \"free_text\": the reply's own words for anything that fits no key, "
            "\"direction\": \"job\" | \"candidate\" | null}. `value` for a schema key must be one of its vocabulary tokens "
            f"(keys: {keys}; vocabulary — {vocab}); for an open key (metro, skill) a lowercase token; null means the user declined "
            "or the reply does not answer. Never guess; never add an answer the reply does not state.")


def understood_words(direction: str, contract: dict, answers: dict | None = None) -> str:
    """The plain 'what I understood' line, code-built from the contract (no model call)."""
    c = contract or {}
    L = lambda k, v: option_label(k, v).replace("us", "US") if k == "country" else option_label(k, v)
    musts = [f"{L(k, v)}" for k, vs in (c.get("must") or {}).items() if isinstance(vs, list) for v in vs]
    prefers = [f"{L(k, v)}" for k, vs in (c.get("prefer") or {}).items() if isinstance(vs, list) for v in vs]
    parts = []
    noun = _DIRECTION_WORDS[direction]
    a0 = answers or {}
    who = str(a0.get("current_role") or a0.get("title") or "").strip()
    base = (f"{noun.capitalize()} for a {who}" if who and direction == "job" else f"{noun.capitalize()} for the {who} role" if who else f"{noun.capitalize()} matching your {'profile' if direction == 'job' else 'job description'}")
    parts.append(base + (" that must be " + ", ".join(musts) if musts else ""))
    if prefers:
        parts.append("preferring " + ", ".join(prefers))
    ctr = c.get("center") or {}
    if ctr.get("key"):
        parts.append(f"centred on {option_label(ctr['key'], str(ctr.get('value')))}" + (f" {ctr['key'].replace('_', ' ')}" if ctr['key'] != "level" else ""))
    a = answers or {}
    comp = a.get("comp")
    comp = comp[0] if isinstance(comp, (list, tuple)) and comp else comp
    if comp and "comp" not in (c.get("must") or {}):
        parts.append(f"around {option_label('comp', str(comp))}")
    yrs = str(a.get("years") or "").strip()
    if yrs and yrs.replace("+", "").strip().isdigit():
        parts.append(f"{yrs} years in")
    return "; ".join(parts) + "."


# ---------------- JD drafting from the centre of peer postings (spec §4) and improve-and-save (§2.2) ----------------
JD_SECTIONS = ("summary", "responsibilities", "must_have", "nice_to_have")
JD_PEER_SHARE = 0.30          # a requirement group is the CENTRE when it recurs in ≥ this share of peers
JD_PEERS = 10                 # peers kept after dedupe by company
JD_PEER_MUST_KEYS = ("field", "level")   # a role's peers share its DOMAIN and its TIER; everything else only ranks
JD_PEERS_MIN = 5              # fewer relevant peers than this at the stated tier → the tier is relaxed (domain only)
# a stated tier rules out work types that contradict it (a CTO's peers are never individual-contributor postings)
LEVEL_WORK_TYPES = {"leadership": ("executive", "founder", "manager")}


# ---- contract search step 2 (spec §12.5 / §12.6): the judge's vocabulary and weights ------------------------------
# the DEFAULT recipe: the compile's own reading of these keys ranks instead of filtering (the user's chips never move)
LADDER_DEFAULT_KEYS = ("field", "function", "work_type", "company_type", "employment_type", "level")
JUDGE_HEAD = 40               # fused rows graded by the blind judge
JUDGE_BATCHES = 3             # the head is graded in this many concurrent calls (wall time, not cost)
MERGE_RRF_K = 60
JUDGE_PARTIAL = 0.4           # a partial fit's weight in head precision (inflation guard)
JUDGE_WEAK_YES = 0.8          # a 'yes' on a row whose evidence is only self-stated
WEAK_FITS = 3                 # fewer judged fits than this in the head → WEAK: say so, ask one clarifying question
SELF_STATED_EVIDENCE = ("", "self_stated", "profile")     # evidence kinds that carry no public proof


def judge_prompt(kind: str) -> str:
    rows = "PEOPLE (a candidate each)" if kind == "person" else "OPEN POSTINGS (a role each)"
    brief = "a hiring manager's need" if kind == "person" else "a job seeker's ask"
    return (f"You judge, row by row, whether each of these {rows} FITS the BRIEF ({brief}: the person's own words, then what is REQUIRED "
            "and what is merely PREFERRED). Return ONLY JSON: "
            "{\"verdicts\": [{\"id\": the row id exactly as shown in brackets (e.g. \"r3\"), \"fit\": \"yes\" | \"partial\" | \"no\", \"why\": ≤ 8 words}]}, one verdict per row. "
            "yes = the row's role family, domain and level tier agree with the brief and nothing REQUIRED is contradicted. "
            "partial = the same kind of role but one dimension is off or unstated: a level one step away, an adjacent specialty, a PREFERRED "
            "skill or place missing, a level or place shown as —. "
            "no = a different role family or domain, a different level tier (an individual contributor for an executive brief and the reverse), "
            "something REQUIRED contradicted, or a resemblance that is keyword-only (the same word in another sense). "
            "Skills are PREFERENCES unless listed as required: a row of the right role and tier that lacks a preferred skill is 'partial', never 'no'. "
            "Judge ONLY from the facts shown on the row; — means unknown and unknown is never a 'no' on its own. Never judge from a name.")


def judge_brief(kind: str, text: str, contract: dict | None) -> str:
    """The BRIEF the judge reads: the person's own words plus what the ratified contract REQUIRES and PREFERS (the same
    for every recipe, so the judge stays blind to the recipe while knowing a skill is a wish, not a bar)."""
    c = contract or {}
    L = lambda k, vs: ", ".join(option_label(k, str(v)) for v in (vs if isinstance(vs, list) else [vs]))
    req = "; ".join(f"{k.replace('_', ' ')}: {L(k, v)}" for k, v in (c.get("must") or {}).items() if v)
    pref = "; ".join(f"{k.replace('_', ' ')}: {L(k, v)}" for k, v in (c.get("prefer") or {}).items() if v)
    ctr = c.get("center") or {}
    centre = f"{str(ctr.get('key') or '').replace('_', ' ')} around {option_label(str(ctr.get('key') or ''), str(ctr.get('value') or ''))}" if ctr.get("key") and ctr.get("value") else ""
    parts = [f"WORDS: {(text or '').strip()[:1200]}", f"REQUIRED: {req or '(nothing beyond the words)'}", f"PREFERRED: {pref or '(none stated)'}"]
    if centre:
        parts.append(f"LEVEL: {centre} (a step away is partial)")
    return "\n".join(parts)


def judge_row(kind: str, bid: str, row: dict, line: str = "") -> str:
    """One normalized line per row, the same shape for every row (spec §12.5): role line · company · field ·
    function · level · work type · metro · skills / specialties ≤ 5 · snippet ≤ 140 · evidence kind. Missing = —."""
    f = row.get("facets") or {}
    first = lambda k: str((f.get(k) or ["—"])[0] or "—").replace("_", " ")
    many = lambda k, n: ", ".join(str(x).replace("_", " ") for x in (f.get(k) or [])[:n]) or "—"
    if kind == "person":
        role = (line or "").split(" — ")[0].strip() or "—"
        snippet = (line or "")[:140] or "—"
        company = "—"
    else:
        role = str(row.get("title") or "—")
        company = str(row.get("company") or "—").replace("_", " ")
        snippet = str(row.get("location") or "—")
    ev = many("evidence", 3)
    skills = ", ".join(x for x in (many("skill", 3), many("specialty", 2)) if x != "—") or "—"
    return (f"[{bid}] {role} · {company} · field {first('field')} · function {first('function')} · level {first('level')} · {first('work_type')} "
            f"· {first('metro')} · skills: {skills} · {snippet} · evidence: {ev}")


def reading_alternatives(notes: list[dict] | None) -> list[dict]:
    """The index-aware step's co-occurrence readings as alternative (key, value) recipes for the ladder."""
    out, seen = [], set()
    for n in (notes or []):
        if n.get("rule") != "readings":
            continue
        for r in (n.get("readings") or []):
            fld = str(r.get("field") or "")
            if fld and fld not in seen:
                seen.add(fld)
                out.append({"key": "field", "value": fld, "why": f"'{r.get('value')}' lives {int(float(r.get('share') or 0) * 100)} % in {fld}"})
    return out


def peer_work_types(levels: list[str] | None, work_types: list[str] | None) -> list[str]:
    """The preferred work types that agree with the stated level(s); unconstrained levels keep them all."""
    allowed = None
    for lv in (levels or []):
        if lv in LEVEL_WORK_TYPES:
            allowed = set(LEVEL_WORK_TYPES[lv]) if allowed is None else allowed | set(LEVEL_WORK_TYPES[lv])
    if allowed is None:
        return list(work_types or [])
    return [w for w in (work_types or []) if w in allowed]

DRAFT_CONTEXT_WORDS = ("Three things so the draft is yours, not generic: the title and level, where the role is (metro or remote), "
                       "and any must-have from your side. Add team or company context if you like.")


def group_prompt() -> str:
    """One model call: requirement lines from several postings → groups of EQUIVALENT lines (meaning, not string
    match). Each input line is tagged `[p<peer>] <kind>: <text>`; the output names the peers each group covers."""
    return ("You group job-posting requirement lines that say the SAME thing in different words. Input lines are tagged "
            "[p<n>] followed by a kind (must | nice | responsibility) and the text. Return ONLY JSON: {\"groups\": [{\"label\": a short "
            "neutral phrasing of the requirement (≤ 14 words), \"kind\": \"must\" | \"nice\" | \"responsibility\", \"peers\": [the distinct "
            "peer numbers whose lines belong here]}]}. A group must contain lines from at least one peer; never invent a requirement; "
            "keep company names and product names out of labels; a line may belong to one group only.")


def draft_prompt() -> str:
    return ("You write a job description from (a) the CENTRE — requirement groups that recur across peer postings for this role, each "
            "with the count of peers asking for it — (b) the hiring manager's OWN context and must-haves, and (c) optional groups some "
            "peers also ask for. Return ONLY JSON: {\"title\": str, \"summary\": one paragraph (≤ 70 words) of what the role is, "
            "\"responsibilities\": [{\"text\": str, \"source\": \"g<id>\" | \"you\"}], \"must_have\": [same shape], \"nice_to_have\": [same shape]}. "
            "EVERY line cites its source: a centre / optional group id (g<id>) or \"you\" (the manager's own words). Never write a line "
            "that has neither. Put the manager's must-haves first. Plain, specific language; no company boilerplate; no pay figures "
            "(they are shown separately as a market signal).")


def improve_prompt(artifact: str) -> str:
    what = "résumé" if artifact == "profile" else "job description"
    return (f"You produce a fuller {what} from ONLY two sources: the ORIGINAL text and the ANSWERS the person gave in a short conversation. "
            "Return ONLY JSON: {\"text\": the improved document in plain text with short section headings, \"added\": [each sentence or bullet "
            "that comes from the ANSWERS rather than the original, verbatim as it appears in `text`]}. Never add an employer, a date, a "
            "number, a skill, a location or a credential that appears in neither source. Keep the original's facts and order; tighten wording; "
            "do not flatter.")


def role_key(title: str, facets: dict | None) -> str:
    """The key a hiring manager's JD is filed under: <field>/<function>/<level>/<title slug> from the JD's own facets."""
    import re
    f = facets or {}
    first = lambda k: (f.get(k) or ["_"])[0] if isinstance(f.get(k), list) else (f.get(k) or "_")
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:48] or "role"
    return f"{first('field')}/{first('function')}/{first('level')}/{slug}"
