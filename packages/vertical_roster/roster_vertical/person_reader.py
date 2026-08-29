"""ROSTER_DEEP_PEOPLE_READER — the tech-vertical config for the deep PERSON reader.

Parallel to company_reader.py, but for an individual. LEGAL/TECHNICAL basis: LinkedIn + X cannot be
deep-scraped (login-walled, ToS-forbidden, LinkedIn litigates — Proxycurl was sued+shut down in 2025).
So this reads ONLY legitimately-public sources: the person's own site/blog, GitHub, press/interviews/
podcasts/talks, Crunchbase-style profiles, company team pages, and whatever public LinkedIn/X SNIPPETS a
search engine has already indexed. LinkedIn/X serve as the profile LINK, never a scrape target.

Facet queries are NAME-based (a person rarely has one canonical domain). `{person}` is substituted.
The kernel mechanic (research/deep_person.py) treats all of this as opaque data (kernel litmus).
"""

# Profile hosts in the user's preference order — harvested from the read pages/results as LINKS.
PROFILE_PREFERENCE = ("linkedin.com", "x.com", "twitter.com", "github.com")

ATTRIBUTION_ADDENDUM = (
    "\n\n[PERSON DOSSIER — sourcing discipline: state what is INDEPENDENTLY reported (press, interviews, "
    "filings, Crunchbase) as fact with [n]; label the person's OWN self-description (their site/bio/posts) "
    "as self-reported ('by his own account', 'he describes himself as'). LinkedIn/X are links, not deep "
    "sources — do not assert LinkedIn/X facts beyond an indexed public snippet. If the public record is "
    "THIN for this person, say so plainly as a gap — never fabricate a bio, role, or investment.]")

PERSON_READER = {
    # NAME-based facet queries. INTERNAL = the person's own public surfaces (github, personal site);
    # EXTERNAL = independent coverage (press, interviews, funding DBs, company pages).
    "internal": {
        "identity_site": "{person} personal website about bio",
        "github": "{person} github",
        "views_posts": "{person} blog essays talks interview quotes views",
    },
    "external": {
        "roles_companies": "{person} founder CEO company role site:crunchbase.com OR site:techcrunch.com "
                           "OR site:bloomberg.com OR site:forbes.com",
        "background": "{person} background education career history",
        "investments_board": "{person} investor investments board member portfolio "
                              "site:crunchbase.com OR site:techcrunch.com",
        "notable_work_press": "{person} profile interview feature site:techcrunch.com OR "
                              "site:forbes.com OR site:theinformation.com OR site:businessinsider.com",
        "recent": "{person} 2026 latest news announcement",
    },
    # profile-link resolution (LinkedIn>X>GitHub>self) is harvested from results; a query to surface them:
    "profile_query_template": "{person} linkedin OR twitter OR x.com OR github profile",
    "attribution_addendum": ATTRIBUTION_ADDENDUM,
    "profile_preference": PROFILE_PREFERENCE,
    "max_queries": 8,
    "max_pages": 20,
    "max_results_per_query": 3,
    "max_chars": 12000,
    "max_chunks_per_page": 5,
    "concurrency": 3,
    "deadline_s": 28.0,
}
