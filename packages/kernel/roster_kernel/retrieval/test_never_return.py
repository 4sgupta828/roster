"""NEVER-RETURN: facet values a source must exclude from every request, whatever the caller asked.

A vertical may index text that is displayable and searchable but can NEVER support a claim. Making
that bar advisory is how it gets forgotten, so the retrieval source itself carries it and merges it
into the exclusion predicate of every query.
"""
from dataclasses import dataclass, field

from roster_kernel.retrieval.postgres import PostgresRetrievalSource


@dataclass
class _Req:
    tenant_id: str = "demo"
    workspace_id: str | None = None
    facets: dict = field(default_factory=dict)
    exclude_facets: dict = field(default_factory=dict)


def _sql(never=None, req=None):
    src = PostgresRetrievalSource("postgresql://x/y", never_return=never)
    return src._filter_sql(req or _Req())


def test_no_never_return_leaves_the_query_untouched():
    where, params = _sql()
    assert "facets ?" not in where and params == ["demo"]


def test_a_never_return_value_is_excluded_even_when_the_caller_excludes_nothing():
    where, params = _sql({"source_kind": ("chapter_pointer",)})
    assert "NOT (facets ? $2)" in where and "<> ALL($3)" in where
    assert params == ["demo", "source_kind", ["chapter_pointer"]]


def test_a_string_value_is_accepted_like_a_sequence():
    _, params = _sql({"source_kind": "chapter_pointer"})
    assert params[-1] == ["chapter_pointer"]


def test_the_callers_exclusions_are_merged_never_replaced():
    """The request asks to drop one kind; the source's own bar must survive alongside it."""
    where, params = _sql({"source_kind": ("chapter_pointer",)},
                         _Req(exclude_facets={"source_kind": ["podcast"], "lang": "de"}))
    assert params[1] == "source_kind" and params[2] == ["chapter_pointer", "podcast"]
    assert "lang" in params and where.count("NOT (facets ?") == 2


def test_a_request_cannot_switch_the_bar_off_by_asking_for_it():
    """Even a caller that FILTERS FOR the barred value gets the exclusion — the bar is not negotiable."""
    where, params = _sql({"source_kind": ("chapter_pointer",)},
                         _Req(facets={"source_kind": ["chapter_pointer"]}))
    assert "= ANY($3)" in where                                  # the caller's filter is still applied
    assert params[3] == "source_kind" and params[4] == ["chapter_pointer"]
    assert "NOT (facets ? $4)" in where                           # and so is the bar: the result is empty
