"""Facets, contracts and the evaluator — the one mechanism behind search, navigation, calibration and
refresh (docs/specs/facet-contract-evaluator.md). Domain-free: keys and vocabularies come from the
vertical manifest; this package knows types, grammar, filtering, ranking and counting."""
from .contract import MODES, Contract, edit, state_of, validate_contract
from .evaluate import FacetWeights, calibrated_pct, diagnose_musts, evaluate, pool_from_counts
from .schema import UNKNOWN, FacetKey, FacetSchema, FacetType
from .store import FacetStore, InMemoryFacetStore, count_rows, matches_must

__all__ = ["UNKNOWN", "Contract", "FacetKey", "FacetSchema", "FacetStore", "FacetType", "FacetWeights", "InMemoryFacetStore",
           "MODES", "calibrated_pct", "count_rows", "diagnose_musts", "edit", "evaluate", "matches_must", "pool_from_counts", "state_of", "validate_contract"]
