"""Config/Thresholds dataclasses, defaults, Example_Gene registry (Req 9.6, 11.1-11.3).

Both ``Thresholds`` and ``Config`` are frozen dataclasses so that a change
produces a new object rather than mutating an existing one. The ``Conflict_Detector``
re-evaluates against whatever ``Thresholds`` instance it is handed, so immutability
makes "the thresholds in force when flagged" a stable, copyable value (Req 9.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Thresholds:
    """Configurable conflict/retrieval thresholds with their defaults.

    A frozen dataclass: changing a threshold yields a new ``Thresholds`` object
    the Conflict_Detector re-evaluates against (Req 9.7).
    """

    top_n: int = 5                       # Req 2.7  — top-scoring associations retained
    retmax: int = 3                      # Req 3.6  — recent PMIDs requested per query
    support_threshold: float = 0.3       # Req 9.2, 9.6 — Support_Threshold
    min_supporting_datatypes: int = 2    # Req 9.2, 9.6 — Minimum_Supporting_Datatype_Count
    high_score_threshold: float = 0.5    # Req 9.3, 9.6 — High_Score_Threshold
    low_co_mention_threshold: int = 100  # Req 9.3, 9.6 — Low_Co_Mention_Threshold


# --- Example_Gene registry (Req 11.1-11.3) -----------------------------------
#
# EGFR, TP53, and BRCA1 are present (Req 11.1). The fourth, lesser-studied
# Example_Gene (task 16.1) is PANK2 — a single-gene Mendelian target whose real,
# cached Open Targets / PubMed data trips a conflict flag at the *default*
# thresholds (Req 11.3), selected rather than threshold-tuned:
#
#   * Literature_Gap — association with "hypoprebetalipoproteinemia,
#     acanthocytosis, retinitis pigmentosa, and pallidal degeneration"
#     (MONDO_0011798): Overall_Score 0.6584 (>= high_score_threshold 0.5) AND
#     gene+disease Co_Mention_Count 3 (<= low_co_mention_threshold 100). The
#     "dystonic disorder" (MONDO_0003441) association likewise flags
#     Literature_Gap (score 0.6272, co-mention 0).
#   * Single_Source_Dependence — association with "hereditary disease"
#     (MONDO_0003847): only 1 datatype scores >= support_threshold 0.3, below
#     the min_supporting_datatypes of 2.
#
# These numbers come from the responses persisted under data/cache so the fourth
# gene resolves, builds, verifies, and flags identically offline.

FOURTH_EXAMPLE_GENE: str | None = "PANK2"  # task 16.1: lesser-studied Mendelian gene

EXAMPLE_GENES: tuple[str | None, ...] = ("EGFR", "TP53", "BRCA1", FOURTH_EXAMPLE_GENE)


@dataclass(frozen=True)
class Config:
    """Application configuration: endpoints, request identity, limits, thresholds.

    A frozen dataclass; ``thresholds`` uses ``default_factory`` so the frozen
    default never shares a single mutable instance across ``Config`` objects.
    """

    # default_factory keeps the frozen default from binding one shared Thresholds
    # instance at class-definition time.
    thresholds: Thresholds = field(default_factory=Thresholds)

    tool: str = "claimledger"                 # Req 3.10 — PubMed tool parameter
    # "hackathon@example.com" matches the email baked into the committed PubMed
    # cache URLs so cached-vs-live request keys line up (Req 3.10, 11.7).
    email: str = "hackathon@example.com"      # Req 3.10 — PubMed email parameter

    ot_graphql_url: str = "https://api.platform.opentargets.org/api/v4/graphql"
    pubmed_esearch_url: str = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    )

    http_timeout_s: float = 10.0
    max_requests_per_second: float = 3.0      # Req 3.9 — PubMed rate limit

    # The Example_Gene registry (Req 11.1-11.3): EGFR, TP53, BRCA1, and the
    # fourth lesser-studied gene PANK2 selected in task 16.1 (no placeholder).
    example_genes: tuple[str | None, ...] = EXAMPLE_GENES
