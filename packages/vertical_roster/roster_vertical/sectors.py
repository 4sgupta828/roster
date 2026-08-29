"""SECTOR_PROFILES — the sub-vertical seam (threaded via the manifest `sector_profiles` slot).

Each sector (AI, fintech, biotech…) is a per-question SUBJECT scope, NOT a separate vertical
package: one deployment answers across all of them. A profile supplies sector vocabulary
(`vocab_seed`), a retrieval-steering `context_fn(question) -> planner-only context str`, a
compose `directive`, and optionally sector-specific extra `web_domains`. Adding a sector =
one dict entry here — zero kernel/connector change. Mirrors medical `country_profiles`.
"""
from __future__ import annotations

# ---- AI / ML (the seeded beachhead sector) -------------------------------------------------

_AI_VOCAB = (
    "large language model", "foundation model", "transformer", "inference", "training compute",
    "GPU", "benchmark", "MMLU", "agentic", "fine-tuning", "RAG", "open-weights",
    "OpenAI", "Anthropic", "Google DeepMind", "Meta AI", "Mistral", "Nvidia", "Hugging Face",
)

_AI_DOMAINS = ("paperswithcode.com", "huggingface.co", "mlcommons.org", "arxiv.org")


def _ai_context(question: str) -> str:
    """Planner-only steering context for the AI sector (never a citable fact)."""
    return ("SECTOR = AI/ML. Relevant evidence lives in arXiv preprints, peer-reviewed ML papers, "
            "benchmark leaderboards (MMLU, MLPerf, SWE-bench), model/repo traction on GitHub and "
            "Hugging Face, and the SEC filings of public AI companies. When judging a technology "
            "claim, prefer reproducible benchmark numbers and granted patents over marketing.")

_AI_DIRECTIVE = ("For AI/ML subjects: report benchmark results with the EXACT metric, dataset, and "
                 "model size; distinguish a released open-weights model from an API-only one; treat "
                 "compute/parameter and 'state-of-the-art' claims as vendor statements unless a "
                 "third-party benchmark confirms them.")

# ---- Other deep-tech sectors (compact profiles via a shared context factory) ---------------------
# The product is deep-tech intelligence BROADLY. Each profile supplies sector vocabulary + a
# planner-only steering context + a compose directive. Evidence for all of them already flows through
# the domain-general connectors (papers, patents, grants, filings, code, discussion); the profile just
# tells the planner where to look and how to judge a claim in that sector. Adding one = a dict entry.

def _ctx(label: str, where: str, judge: str):
    def _fn(question: str) -> str:
        return f"SECTOR = {label}. Relevant evidence lives in {where}. When judging a claim, {judge}"
    return _fn


SECTOR_PROFILES: dict[str, dict] = {
    "ai": {
        "context_fn": _ai_context,
        "directive": _AI_DIRECTIVE,
        "vocab_seed": _AI_VOCAB,
        "web_domains": _AI_DOMAINS,
    },
    "semiconductors": {
        "context_fn": _ctx("Semiconductors / compute hardware",
                           "IEEE/arXiv papers, USPTO patents, foundry & fabless SEC filings, standards "
                           "(JEDEC, PCI-SIG), and process/node roadmaps",
                           "prefer measured PPA (power/performance/area), process node, and yield data "
                           "over marketing TOPS; separate a taped-out chip from a roadmap slide."),
        "directive": ("For semiconductor subjects: name the process node and foundry; treat TOPS/FLOPS "
                      "and 'performance-per-watt' as vendor claims unless independently benchmarked; "
                      "distinguish an announced part from a shipping one."),
        "vocab_seed": ("chiplet", "RISC-V", "EUV lithography", "process node", "TSMC", "foundry",
                       "HBM", "interconnect", "photonics", "ASIC", "TOPS", "yield"),
    },
    "robotics": {
        "context_fn": _ctx("Robotics / autonomy",
                           "arXiv (cs.RO), IEEE, patents, and demo/benchmark results",
                           "prefer measured task success rates and real-world (not sim-only) results; "
                           "separate a teleoperated demo from autonomous operation."),
        "directive": ("For robotics: distinguish autonomy from teleoperation; report task success rates "
                      "with the task and environment; treat a staged demo as a demo, not a capability."),
        "vocab_seed": ("humanoid", "manipulation", "SLAM", "locomotion", "teleoperation", "end-effector",
                       "sim-to-real", "autonomy stack", "lidar", "actuator"),
    },
    "quantum": {
        "context_fn": _ctx("Quantum computing",
                           "arXiv (quant-ph), Nature/Science papers, and patents",
                           "prefer qubit count WITH fidelity/coherence and error-corrected logical "
                           "qubits over raw physical-qubit headlines; note 'quantum advantage' claims' "
                           "problem scope."),
        "directive": ("For quantum: always pair qubit count with gate fidelity, coherence time, and "
                      "whether qubits are physical or error-corrected logical; scope any 'advantage' "
                      "claim to its specific problem."),
        "vocab_seed": ("qubit", "error correction", "superconducting", "trapped ion", "gate fidelity",
                       "coherence time", "logical qubit", "quantum advantage", "photonic qubit"),
    },
    "biotech": {
        "context_fn": _ctx("Biotech / health tech",
                           "peer-reviewed papers (OpenAlex/Crossref), NIH RePORTER grants, clinical "
                           "evidence, and patents",
                           "prefer peer-reviewed and clinical-stage evidence over preprints and press; "
                           "note the development stage (discovery vs clinical vs approved)."),
        "directive": ("For biotech: state the development stage (preclinical/clinical phase/approved); "
                      "treat mechanism and in-vitro results as early signal, not efficacy; prefer "
                      "peer-reviewed and trial evidence."),
        "vocab_seed": ("CRISPR", "mRNA", "protein folding", "synthetic biology", "clinical trial",
                       "preclinical", "assay", "therapeutic", "genomics", "bioinformatics"),
    },
    "climate": {
        "context_fn": _ctx("Climate / energy tech",
                           "engineering papers, DOE/NSF grants, patents, and project/pilot data",
                           "prefer measured efficiency, cost-per-unit ($/kWh, $/ton CO2), and pilot-scale "
                           "results over lab-cell records; separate a pilot from commercial deployment."),
        "directive": ("For climate/energy: report efficiency and cost ($/kWh, $/ton) with the scale "
                      "(lab cell vs pilot vs commercial); treat a record lab-cell efficiency as a lab "
                      "result, not a deployable product."),
        "vocab_seed": ("solid-state battery", "green hydrogen", "carbon capture", "perovskite",
                       "fusion", "grid storage", "electrolyzer", "energy density", "LCOE", "geothermal"),
    },
    "space": {
        "context_fn": _ctx("Space tech",
                           "engineering papers, patents, launch/mission records, and SEC filings",
                           "prefer demonstrated launch/flight-heritage and mission data over renderings; "
                           "separate a test article from operational hardware."),
        "directive": ("For space: cite flight heritage and demonstrated mission results; distinguish a "
                      "test flight from operational service; treat payload/cost figures as targets "
                      "unless flown."),
        "vocab_seed": ("reusable rocket", "satellite constellation", "in-space manufacturing", "payload",
                       "launch cadence", "propulsion", "flight heritage", "smallsat"),
    },
    "security": {
        "context_fn": _ctx("Cybersecurity / cryptography",
                           "papers (IACR/arXiv), NIST standards, CVE/advisory data, patents, and filings",
                           "prefer standardized/peer-reviewed schemes and NIST selections over vendor "
                           "claims; note whether a scheme is standardized vs proposed."),
        "directive": ("For security/crypto: cite the standard (NIST/IETF) and peer review; distinguish a "
                      "standardized scheme from a proposal; treat 'unbreakable'/'quantum-safe' vendor "
                      "claims skeptically without a named standard."),
        "vocab_seed": ("post-quantum cryptography", "zero-knowledge proof", "confidential computing",
                       "homomorphic encryption", "zero trust", "NIST PQC", "enclave", "CVE"),
    },
    # Add a sector = one dict entry here — zero kernel/connector change.
}
