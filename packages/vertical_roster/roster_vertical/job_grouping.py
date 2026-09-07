"""HOW A JOB SEEKER SEGMENTS A RESULT SET (docs/specs/result-grouping.md §4) — the vocabulary and the guidance; the
kernel (`facets/grouping.py`) owns the shape rules and the free fallback's arithmetic.

Two things live here: the words we give the model when it segments a set, and the tokens we fall back on when no model
answers. Both encode the same judgement — a seeker cuts the space by the WORK they would own."""
from __future__ import annotations

import re

# the dimensions offered as GROUPINGS, in the order they are considered; each names the row field or facet it reads
# (key, label, where to read it, what kind of dimension it is — see `facets.grouping.eligible`)
GROUP_DIMENSIONS: tuple[tuple[str, str, str, str], ...] = (
    ("auto", "Auto", "", "auto"),               # the model segments this result set (opt-in: it costs one call)
    ("company", "Company", "company", "identity"),
    ("company_industry", "Industry", "facet:company_industry", "categorical"),
    ("level", "Seniority", "facet:level", "categorical"),
    ("metro", "Location", "location", "identity"),
    ("work_mode", "Work mode", "facet:work_mode", "categorical"),
    ("work_type", "IC / manager", "facet:work_type", "categorical"),
)

# words that describe the posting's packaging, not the work; they must never form a group
STOPWORDS = set("""the a an and or of for to in on with at by as is are be we you your our their new senior junior staff
lead principal manager engineer engineering developer specialist analyst associate director head chief officer vp full
time part remote hybrid onsite job jobs role roles position positions hiring team teams work works working experience
years year level i ii iii iv sr jr mid entry contract intern internship""".split())


def tokens(row: dict) -> set:
    """The words that could distinguish this posting from its neighbours: what it is about, never who posts it."""
    out: set = set()
    facets = (row or {}).get("facets") or {}
    for key in ("role_family", "specialty", "skill"):
        for v in (facets.get(key) or []):
            t = str(v).strip().lower()
            if t and t not in STOPWORDS:
                out.add(t)
    for w in re.split(r"[^a-z0-9+#.]+", str((row or {}).get("title") or "").lower()):
        if len(w) > 2 and w not in STOPWORDS:
            out.add(w)
    return out


def row_line(i: int, row: dict, *, cap: int = 70) -> str:
    """One compact line per posting for the segmenting call: the title and what it is about. No company, no location,
    no salary — the model must not be able to segment by them."""
    facets = (row or {}).get("facets") or {}
    bits = [str((row or {}).get("title") or "")[:cap]]
    for key in ("specialty", "skill"):
        vals = [str(v) for v in (facets.get(key) or [])][:4]
        if vals:
            bits.append(", ".join(vals))
    return f"{i}| " + " · ".join(b for b in bits if b)


def segment_prompt(max_groups: int = 7, kind: str = "job") -> str:
    who = ("a list of CANDIDATES the way a HIRING MANAGER reads a shortlist: by the WORK each person does"
           if kind == "person" else "a list of job postings the way a JOB SEEKER decides where to apply: by the WORK they would own")
    return (f"You segment {who}. "
            "Good cuts name the craft or the domain — building infrastructure · applied ML · model training · inference "
            "and serving · integrations and solutions · data platform · frontend · security · new business sales · "
            "account management · sales engineering · long-haul driving · bedside care. NEVER segment by employer, by "
            "seniority, by location, by employment type, or by a word nearly every row shares — that word is the search "
            "itself, not a distinction. Return ONLY JSON: {\"groups\": [{\"name\": 2–4 words a seeker would recognise, "
            "\"why\": ≤ 8 words on what unites them, \"ids\": [the row ids]}]}. "
            f"Between 3 and {max_groups} groups; every id in AT MOST ONE group; leave a row out rather than forcing it "
            "(the leftovers are shown as 'Everything else'). Order groups by how many rows they hold.")


def resegment_prompt(name: str, n: int, total: int, kind: str = "job") -> str:
    """One retry when a single group swallowed the set: split THAT group, keep the rest."""
    return (segment_prompt(kind=kind) + f"\n\nYour previous answer put {n} of {total} rows in \"{name}\", which is not a "
            "segmentation. Split that group by what those people actually build or sell, and keep the smaller groups as "
            "they were. No group may hold more than half the rows.")
