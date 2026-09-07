"""Grouping mechanics: a dimension earns its place on THESE rows, a segmentation is made safe, and the free fallback
finds the distinctions inside the set rather than the query itself."""
from roster_kernel.facets.grouping import eligible, enforce, token_groups


def test_a_dimension_is_judged_on_the_rows_in_front_of_the_user():
    good = ["a"] * 8 + ["b"] * 7 + ["c"] * 5
    e = eligible(good)
    assert e.ok and e.groups == 3 and e.largest == 0.4 and e.score > 0
    assert not eligible(["a"] * 19 + [""]).ok                                  # one group only
    assert "holds" in eligible(["a"] * 15 + ["b"] * 5).reason                  # 75 % in one group
    assert "not stated" in eligible(["a"] * 7 + ["b"] * 7 + [""] * 6).reason   # 30 % unstated: a junk drawer, hidden
    assert "too many" in eligible([str(i) for i in range(20)]).reason
    assert not eligible([]).ok
    # the same dimension can be ineligible corpus-wide and eligible here — that is the point
    assert eligible(["remote"] * 9 + ["hybrid"] * 6 + ["onsite"] * 5).ok


def test_a_proposed_segmentation_is_made_safe():
    proposed = [{"name": "Infra", "ids": [0, 1, 2, 99, "x"]},          # out-of-range and junk ids dropped
                {"name": "ML", "ids": [2, 3, 4]},                       # 2 already taken → single membership
                {"name": "", "ids": [5, 6]},                            # nameless → its rows go back
                {"name": "Tiny", "ids": [7]}]                           # below min_size → back
    groups, leftovers, notes = enforce(proposed, 10)
    assert [(g.name, g.ids) for g in groups] == [("Infra", [0, 1, 2]), ("ML", [3, 4])]
    assert leftovers == [5, 6, 7, 8, 9]
    assert any("out-of-range" in n for n in notes) and any("non-numeric" in n for n in notes)
    # a dominant group is reported, not silently accepted
    _g, _l, n2 = enforce([{"name": "Everything", "ids": list(range(9))}], 10)
    assert any("dominant" in x for x in n2)
    # more groups than allowed: the extras' rows become leftovers
    many = [{"name": f"g{i}", "ids": [2 * i, 2 * i + 1]} for i in range(9)]
    g3, l3, n3 = enforce(many, 18)
    assert len(g3) == 7 and len(l3) == 4 and any("kept 7" in x for x in n3)


def test_the_fallback_ignores_the_query_and_finds_the_distinctions():
    # every row shares "learning" (the query); the real cuts are infra / vision / nlp
    sets = ([{"learning", "infrastructure", "kubernetes"}] * 6
            + [{"learning", "vision"}] * 5
            + [{"learning", "nlp", "transformers"}] * 4
            + [{"learning"}] * 5)
    groups, rest = token_groups(sets)
    names = [g.name for g in groups]
    assert "learning" not in names                                   # the query is not a distinction
    # three groups, each holding exactly the rows that share a real distinction (the token that NAMES a group may be any
    # of the ones those rows share — 'nlp' and 'transformers' describe the same four rows)
    assert [g.ids for g in groups] == [[0, 1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14]]
    assert names[0] == "infrastructure" and names[1] == "vision" and names[2] in ("nlp", "transformers")
    assert sum(len(g.ids) for g in groups) + len(rest) == 20 and len(rest) == 5
    assert token_groups([]) == ([], [])
    # deterministic
    assert [g.ids for g in token_groups(sets)[0]] == [g.ids for g in groups]


def test_an_identity_dimension_is_useful_with_many_groups_and_a_categorical_one_is_not():
    """43 backend roles across 30 companies is exactly when grouping by company helps — spotting that one employer holds
    five of them. The same shape for a categorical dimension is thirty lines of noise."""
    many = [f"co{i // 2}" for i in range(30)] + [f"solo{i}" for i in range(6)]     # 15 pairs + 6 singletons
    assert eligible(many, kind="identity").ok and eligible(many, kind="identity").groups == 21
    assert not eligible(many).ok and "too many" in eligible(many).reason
    # an identity dimension where every row stands alone is just the flat list again
    assert not eligible([f"u{i}" for i in range(20)], kind="identity").ok
    assert "stands alone" in eligible([f"u{i}" for i in range(20)], kind="identity").reason
    # concentration scores higher than dust
    tight = eligible(["a"] * 5 + ["b"] * 5 + ["c"] * 5 + ["d"] * 5, kind="identity").score
    loose = eligible(["a"] * 2 + ["b"] * 2 + ["c"] * 2 + [f"u{i}" for i in range(14)], kind="identity").score
    assert tight > loose
