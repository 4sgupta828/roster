"""Held-out eval gold for the tech vertical (against the bundled sample corpus).

Every case is answerable ONLY from the fixture filings/papers, and the refuse case names a
subject NOT in the corpus — so a pass means the system grounded a real fact or honestly
declined, never memorized. `evidence_floor` names the minimum authority tier the top cited
finding must reach for a factual claim.
"""
from __future__ import annotations

GOLD = {
    "revenue_disclosed": {
        "question": "What revenue did Nimbus AI report for fiscal 2024?",
        "expect": "value",
        "expected_values": ["$42.0 million", "42.0 million"],
        "supporting_quote": "Revenue increased to $42.0 million in fiscal 2024",
        "evidence_floor": "primary_filing",
    },
    "benchmark_result": {
        "question": "What MMLU accuracy did the SparseServe 7B model reach?",
        "expect": "value",
        "expected_values": ["68.4%", "68.4"],
        "supporting_quote": "our 7B-parameter model reaches 68.4% accuracy",
        "evidence_floor": "technical_signal",
    },
    "customer_concentration_risk": {
        "question": "What customer-concentration risk did Corvus Semiconductors disclose?",
        "expect": "value",
        "expected_values": ["38%", "largest customer"],
        "supporting_quote": "Our largest customer accounted for 38% of revenue",
        "evidence_floor": "primary_filing",
    },
    "coverage_gap_unknown_company": {
        "question": "What was the Series C valuation of Helio Robotics in this corpus?",
        "expect": "refuse",   # not in the fixture corpus → honest gap, not a fabricated number
    },
}
