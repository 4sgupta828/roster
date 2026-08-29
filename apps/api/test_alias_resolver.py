"""Offline tests for the LLM alias-resolver — validation logic (pure) + persistence (fakes).

The load-bearing guarantee is ANTI-POLLUTION: a wrong/uncertain merge must never happen, and a
hallucinated member must never be written. These test that guarantee without any network/LLM.
"""
import asyncio

from api.alias_resolver import cluster_kind_aliases, validate_clusters


# --------------------------------------------------------------------------- #
# validate_clusters — pure, deterministic (the anti-pollution guard)           #
# --------------------------------------------------------------------------- #
FORMS = ["a16z", "Andreessen Horowitz", "Accel", "Accel-KKR", "Sequoia", "TTS", "Text-to-Speech"]


def test_confident_abbreviation_merges():
    out = validate_clusters(
        [{"canonical": "Andreessen Horowitz", "members": ["a16z", "Andreessen Horowitz"],
          "confidence": 0.95}], FORMS)
    assert out == [{"canonical": "Andreessen Horowitz", "members": ["a16z", "Andreessen Horowitz"]}]


def test_low_confidence_is_dropped():
    out = validate_clusters(
        [{"canonical": "Andreessen Horowitz", "members": ["a16z", "Andreessen Horowitz"],
          "confidence": 0.6}], FORMS)
    assert out == []                          # below 0.8 floor → no merge (fail-safe)


def test_hallucinated_member_dropped_and_cluster_collapses():
    # 'Founders Fund' is NOT in FORMS → dropped; leaves <2 real members → whole cluster rejected.
    out = validate_clusters(
        [{"canonical": "a16z", "members": ["a16z", "Founders Fund"], "confidence": 0.99}], FORMS)
    assert out == []


def test_canonical_not_in_input_falls_back_to_longest_member():
    out = validate_clusters(
        [{"canonical": "AH Capital Management LLC",   # not an input form
          "members": ["a16z", "Andreessen Horowitz"], "confidence": 0.9}], FORMS)
    assert out[0]["canonical"] == "Andreessen Horowitz"   # longest surviving member


def test_share_a_word_but_distinct_stays_split():
    # the LLM correctly returns them as separate singletons → nothing to merge.
    out = validate_clusters(
        [{"canonical": "Accel", "members": ["Accel"], "confidence": 0.9},
         {"canonical": "Accel-KKR", "members": ["Accel-KKR"], "confidence": 0.9}], FORMS)
    assert out == []                          # singletons never merge


def test_cross_cluster_ambiguous_member_dropped_everywhere():
    # 'Accel' claimed by two clusters → ambiguous → removed from both; both collapse to <2.
    out = validate_clusters(
        [{"canonical": "Accel", "members": ["Accel", "Sequoia"], "confidence": 0.9},
         {"canonical": "Accel-KKR", "members": ["Accel", "Accel-KKR"], "confidence": 0.9}], FORMS)
    assert out == []


def test_multiple_valid_clusters():
    out = validate_clusters(
        [{"canonical": "Andreessen Horowitz", "members": ["a16z", "Andreessen Horowitz"], "confidence": 0.95},
         {"canonical": "Text-to-Speech", "members": ["TTS", "Text-to-Speech"], "confidence": 0.9}], FORMS)
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# cluster_kind_aliases — persistence via fakes (no LLM/DB)                      #
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self, forms):
        self._forms = forms        # [(surface, norm, quote)]
        self.entities = {}
        self.aliases = []          # (alias, entity_id)

    async def distinct_object_forms(self, predicates, *, tenant_id="demo"):
        return list(self._forms)

    async def upsert_entity(self, entity_id, kind, name, *, tenant_id="demo", **kw):
        self.entities[entity_id] = {"kind": kind, "name": name}
        return entity_id

    async def add_alias(self, alias, entity_id, source=""):
        self.aliases.append((alias, entity_id))
        return alias


class _FakeLLM:
    """Answers BOTH calls: the cluster proposer (user carries 'SURFACE FORMS') and the pairwise
    same-entity verifier (else). `verify` toggles whether the verifier confirms a merge."""
    def __init__(self, clusters, verify=True, conf=0.95):
        self._clusters = clusters
        self._verify = verify
        self._conf = conf
    async def complete_json(self, *, model, system, user):
        if "SURFACE FORMS" in user:
            return {"clusters": self._clusters}
        return {"same": self._verify, "confidence": self._conf, "why": "test"}


def test_apply_persists_canonical_and_aliases():
    forms = [("a16z", "a16z", "backed by a16z"),
             ("Andreessen Horowitz", "andreessen horowitz", "led by Andreessen Horowitz"),
             ("Sequoia", "sequoia", "from Sequoia")]
    store = _FakeStore(forms)
    llm = _FakeLLM([{"canonical": "Andreessen Horowitz",
                     "members": ["a16z", "Andreessen Horowitz"], "confidence": 0.95}], verify=True)
    out = asyncio.run(cluster_kind_aliases(store, kind="investor",
                                           predicates=["has_investor"], llm=llm))
    assert out and out[0]["canonical_id"] == "investor:andreessen horowitz"
    assert "investor:andreessen horowitz" in store.entities
    assert ("a16z", "investor:andreessen horowitz") in store.aliases
    assert ("Andreessen Horowitz", "investor:andreessen horowitz") in store.aliases
    assert all(a[0] != "Sequoia" for a in store.aliases)


def test_pairwise_verifier_rejects_blocks_the_merge():
    # clustering PROPOSES the merge, but the pairwise verifier says NOT same → nothing is written.
    forms = [("a16z", "a16z", "q1"), ("Andreessen Horowitz", "andreessen horowitz", "q2")]
    store = _FakeStore(forms)
    llm = _FakeLLM([{"canonical": "Andreessen Horowitz",
                     "members": ["a16z", "Andreessen Horowitz"], "confidence": 0.99}], verify=False)
    out = asyncio.run(cluster_kind_aliases(store, kind="investor",
                                           predicates=["has_investor"], llm=llm))
    assert out == [] and not store.entities and not store.aliases


def test_no_llm_client_writes_nothing():
    store = _FakeStore([("a16z", "a16z", "q"), ("Andreessen Horowitz", "andreessen horowitz", "q")])
    out = asyncio.run(cluster_kind_aliases(store, kind="investor",
                                           predicates=["has_investor"], llm=None))
    assert out == [] and not store.entities and not store.aliases


def test_fewer_than_two_forms_is_noop():
    store = _FakeStore([("a16z", "a16z", "q")])
    out = asyncio.run(cluster_kind_aliases(store, kind="investor",
                                           predicates=["has_investor"], llm=_FakeLLM([])))
    assert out == [] and not store.aliases
