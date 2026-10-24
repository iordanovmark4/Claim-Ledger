"""Unit tests for the Pipeline orchestrator (task 17.1).

These exercise :class:`claimledger.pipeline.Pipeline` end-to-end **offline**,
injecting an offline :class:`HttpFetcher` over the committed ``data/cache`` so
the whole resolve -> retrieve -> build -> verify -> detect -> render sequence runs
from cache with no network. They assert the Pipeline encapsulates exactly the
wiring the hand-written ``run_offline_pipeline`` in ``test_offline.py`` proves
correct:

* EGFR resolves to ENSG00000146648, yields non-empty associations and verified
  claims, zero unsupported, a non-empty dossier carrying the safety notice, and
  reports every record came from cache (Req 1, 2, 5, 6, 8, 11.4);
* PANK2 raises at least one conflict flag at the default thresholds (Req 9, 11.3);
* an unresolvable symbol (no cached search, offline) returns a PipelineResult
  with the NOT_FOUND status and empty results — the UI's stop branch (Req 1.5).

Offline guarantee (two belts, one brace), mirroring test_offline.py:
1. the fetcher is built with ``external_available=lambda: False`` so a present
   cache is served cache-first and the live request is never reached;
2. ``requests.get`` / ``requests.post`` are monkeypatched to raise as a hard
   backstop, so any accidental live call fails loudly rather than hitting the
   network.
"""

from __future__ import annotations

import pytest

from claimledger.cache import ResponseCache
from claimledger.clients.opentargets import ResolveStatus
from claimledger.config import Config
from claimledger.http import HttpFetcher, RateLimiter
from claimledger.pipeline import Pipeline, PipelineResult
from claimledger.render import SAFETY_NOTICE


def _offline_fetcher(config: Config) -> HttpFetcher:
    """An HttpFetcher that serves the committed cache and never sleeps.

    ``external_available=False`` pins the fetcher to cache-first so the live
    ``do_request`` is unreachable; the rate limiter's clock/sleep are faked so
    the test stays fast and deterministic.
    """
    return HttpFetcher(
        cache=ResponseCache(),
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
        config=config,
        external_available=lambda: False,
    )


def _offline_pipeline(config: Config | None = None) -> Pipeline:
    cfg = config if config is not None else Config()
    return Pipeline(cfg, fetcher=_offline_fetcher(cfg))


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard backstop: make any real ``requests`` call fail the test.

    The clients import ``requests`` lazily inside their ``do_request`` builders,
    so patching ``requests.get`` / ``requests.post`` guarantees an accidental
    live call fails loudly instead of reaching the internet.
    """
    import requests

    def _forbidden(*_args: object, **_kwargs: object):
        raise AssertionError(
            "network access attempted during an offline pipeline test "
            "(a committed cache file is missing or the cache-first path regressed)"
        )

    monkeypatch.setattr(requests, "get", _forbidden)
    monkeypatch.setattr(requests, "post", _forbidden)


# --------------------------------------------------------------------------- #
# EGFR: full resolved run offline                                             #
# --------------------------------------------------------------------------- #
def test_pipeline_runs_egfr_fully_offline(no_network: None) -> None:
    """EGFR resolves, retrieves, builds, verifies, and renders entirely from cache."""
    result = _offline_pipeline().run("EGFR")

    # Resolution succeeded to the known Ensembl_ID (Req 1.3, 11.1).
    assert isinstance(result, PipelineResult)
    assert result.status is ResolveStatus.RESOLVED
    assert result.ensembl_id == "ENSG00000146648"
    assert result.gene_symbol == "EGFR"

    # Associations non-empty with populated identities (Req 2).
    assert result.associations, "no associations retrieved offline"
    assert all(a.disease_id and a.disease_name for a in result.associations)

    # PubMed counts parsed from cache (Req 3).
    assert isinstance(result.literature_count, int)
    assert result.literature_count >= 0
    assert set(result.co_mention_counts) == {
        a.disease_id for a in result.associations
    }

    # Claims build and verify: non-empty Verified, zero Unsupported — the values
    # come straight from the cached evidence (Req 5, 6, 11.4).
    assert result.verified_claims, "no claims verified offline"
    assert result.unsupported_claims == [], "unexpected Unsupported claims"

    # Dossier renders non-empty with the mandatory safety notice (Req 8, 10.4).
    assert result.dossier_markdown.strip(), "empty dossier"
    assert SAFETY_NOTICE in result.dossier_markdown
    assert "# Evidence dossier: EGFR" in result.dossier_markdown

    # Every record came from cache — proving the run was fully offline (Req 11.4).
    assert result.any_from_cache is True
    assert result.all_from_cache is True


def test_pipeline_run_resolved_matches_run(no_network: None) -> None:
    """run_resolved(ensembl_id, symbol) reproduces run()'s resolved results.

    The UI reaches run_resolved after a disambiguation choice (Req 1.7); it must
    produce the same retrieval/verify/render output as the resolve-then-run path.
    """
    pipeline = _offline_pipeline()
    via_run = pipeline.run("EGFR")
    via_resolved = pipeline.run_resolved(
        "ENSG00000146648", resolved_symbol="EGFR"
    )

    assert via_resolved.ensembl_id == via_run.ensembl_id
    assert len(via_resolved.associations) == len(via_run.associations)
    assert len(via_resolved.verified_claims) == len(via_run.verified_claims)
    assert via_resolved.dossier_markdown == via_run.dossier_markdown


# --------------------------------------------------------------------------- #
# PANK2: at least one conflict flag at default thresholds (Req 9, 11.3)       #
# --------------------------------------------------------------------------- #
def test_pipeline_pank2_raises_conflict_flag_at_defaults(no_network: None) -> None:
    """PANK2 trips >= 1 Single_Source_Dependence / Literature_Gap flag (Req 11.3)."""
    result = _offline_pipeline().run("PANK2")

    assert result.status is ResolveStatus.RESOLVED
    assert result.ensembl_id == "ENSG00000125779"
    assert result.conflict_flags, "PANK2 raised no conflict flag at defaults"
    rules = {flag.rule for flag in result.conflict_flags}
    assert rules <= {"Single_Source_Dependence", "Literature_Gap"}
    # Each flag names a disease among PANK2's top associations.
    disease_ids = {a.disease_id for a in result.associations}
    assert all(flag.disease_id in disease_ids for flag in result.conflict_flags)


def test_pipeline_egfr_raises_no_conflict_flag(no_network: None) -> None:
    """A well-studied gene (EGFR) trips no flag — PANK2's flags are not universal."""
    result = _offline_pipeline().run("EGFR")
    assert result.conflict_flags == []


# --------------------------------------------------------------------------- #
# Unresolvable symbol: NOT_FOUND branch with empty results (Req 1.5, 11.4)    #
# --------------------------------------------------------------------------- #
def test_pipeline_unresolvable_symbol_returns_not_found(no_network: None) -> None:
    """A symbol with no cached search resolves NOT_FOUND offline, empty results.

    Offline, a symbol whose OT search is not in the committed cache yields an
    unavailable fetch -> empty hits -> NOT_FOUND. The pipeline must return a
    PipelineResult carrying that status and empty results rather than crashing or
    proceeding to retrieval (Req 1.5, 11.4).
    """
    result = _offline_pipeline().run("NOTAGENE_ZZZ_404")

    assert isinstance(result, PipelineResult)
    assert result.status is ResolveStatus.NOT_FOUND
    assert result.ensembl_id is None
    assert result.associations == ()
    assert result.literature_count is None
    assert result.co_mention_counts == {}
    assert result.verified_claims == []
    assert result.unsupported_claims == []
    assert result.conflict_flags == []
    assert result.dossier_markdown == ""
    # No evidence gathered, so nothing came from cache.
    assert result.any_from_cache is False
    assert result.all_from_cache is False


def test_pipeline_resolve_exposes_outcome(no_network: None) -> None:
    """resolve() exposes the resolution step for the UI's disambiguation flow."""
    pipeline = _offline_pipeline()
    outcome = pipeline.resolve("EGFR")
    assert outcome.status is ResolveStatus.RESOLVED
    assert outcome.ensembl_id == "ENSG00000146648"

    missing = pipeline.resolve("NOTAGENE_ZZZ_404")
    assert missing.status is ResolveStatus.NOT_FOUND
