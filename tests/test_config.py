"""Unit tests for config defaults (Req 9.6, 2.7, 3.6).

Assert every threshold and config default matches the specified values and that
both ``Thresholds`` and ``Config`` are frozen dataclasses.
"""

from __future__ import annotations

import dataclasses

import pytest

from claimledger.config import Config, Thresholds


def test_thresholds_defaults() -> None:
    """Thresholds defaults match the specified values (Req 2.7, 3.6, 9.6)."""
    t = Thresholds()
    assert t.top_n == 5                       # Req 2.7
    assert t.retmax == 3                      # Req 3.6
    assert t.support_threshold == 0.3         # Req 9.6
    assert t.min_supporting_datatypes == 2    # Req 9.6
    assert t.high_score_threshold == 0.5      # Req 9.6
    assert t.low_co_mention_threshold == 100  # Req 9.6


def test_config_defaults() -> None:
    """Config defaults match the specified values (Req 3.6, 3.10)."""
    c = Config()
    assert c.tool == "claimledger"
    assert c.email  # an email identity is set
    assert c.ot_graphql_url == (
        "https://api.platform.opentargets.org/api/v4/graphql"
    )
    assert c.pubmed_esearch_url == (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    )
    assert c.http_timeout_s == 10.0
    assert c.max_requests_per_second == 3.0


def test_config_thresholds_default_is_defaults() -> None:
    """Config carries a default Thresholds instance equal to Thresholds()."""
    assert Config().thresholds == Thresholds()


def test_config_example_genes_registry() -> None:
    """Example_Gene registry contains the three named genes plus the fourth gene."""
    genes = Config().example_genes
    assert "EGFR" in genes
    assert "TP53" in genes
    assert "BRCA1" in genes
    # The fourth slot is now populated (task 16.1) with the selected
    # lesser-studied gene PANK2 — no placeholder remains.
    assert len(genes) == 4
    assert "PANK2" in genes
    assert genes[3] == "PANK2"
    assert None not in genes


def test_thresholds_is_frozen() -> None:
    """Assigning to a Thresholds attribute raises FrozenInstanceError."""
    t = Thresholds()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.top_n = 99  # type: ignore[misc]


def test_config_is_frozen() -> None:
    """Assigning to a Config attribute raises FrozenInstanceError."""
    c = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.tool = "tampered"  # type: ignore[misc]
