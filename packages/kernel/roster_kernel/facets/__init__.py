"""Facets, contracts and the evaluator — the one mechanism behind search, navigation, calibration and
refresh (docs/specs/facet-contract-evaluator.md). Domain-free: keys and vocabularies come from the
vertical manifest; this package knows types, grammar, filtering, ranking and counting."""
from .contract import MODES, Contract, edit, state_of, validate_contract
from .evaluate import FacetWeights, calibrated_pct, evaluate
from .schema import UNKNOWN, FacetKey, FacetSchema, FacetType
from .store import FacetStore, InMemoryFacetStore, count_rows, matches_must

__all__ = ["UNKNOWN", "Contract", "FacetKey", "FacetSchema", "FacetStore", "FacetType", "FacetWeights", "InMemoryFacetStore",
           "MODES", "calibrated_pct", "count_rows", "edit", "evaluate", "matches_must", "state_of", "validate_contract"]
