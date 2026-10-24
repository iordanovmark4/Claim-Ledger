"""Example-based end-to-end offline test for all four Example_Genes (task 16.3).

Per Requirement 11 the four Example_Genes (EGFR, TP53, BRCA1, and the
lesser-studied PANK2) must resolve, retrieve, build claims, verify, detect
conflicts, and render a dossier using *only* the committed cache in
``data/cache`` — no network. This test exercises the whole pipeline wired from
the real modules (``OTClient`` / ``PubMedClient`` over an ``HttpFetcher`` in
offline mode) against that committed cache and asserts the end-to-end
guarantees:

* every gene resolves to its known Ensembl_ID (Req 11.1, 11.4);
* five associations are retrieved with the target identity populated
  (Req 2.2, 2.7, 11.4);
* the gene-only literature count and every per-disease co-mention count parse
  from cache (Req 3.2, 3.5, 11.4);
* the ClaimBuilder builds claims and ``verify_all`` yields a non-empty Verified
  set with zero Unsupported — the values come straight from the cached
  evidence, so they verify by construction (Req 5, 6, 11.4);
* the DossierRenderer renders a non-empty Markdown dossier carrying the
  mandatory "Evidence summary, not medical advice" notice and approved wording,
  with none of the forbidden terms (the render guard would raise otherwise)
  (Req 8.1, 10.1, 10.2, 10.4);
* every evidence record used is ``from_cache=True`` — proving offline operation
  (Req 11.4, 11.5, 11.7);
* PANK2 raises at least one conflict flag (Single_Source_Dependence or
  Literature_Gap) at the default ``Thresholds()`` (Req 11.3).

Per Req 12.8 this is an example-based test over a small fixed set of
representative cached examples — the four Example_Genes — not a property-based
one.

Offline guarantee (two belts, one brace)
-----------------------------------------
1. The :class:`HttpFetcher` is built with ``external_available=lambda: False``
   so a present cache is served cache-first and the live ``do_request`` is
   never reached (Req 11.4).
2. As a hard backstop, ``requests.get`` / ``requests.post`` are monkeypatched to
   raise, so if any code path *did* attempt a real call the test would fail
   loudly rather than touch the network. The clients import ``requests`` lazily
   *inside* ``do_request``, so patching the module attributes covers them.

If a required cache file is missing so a gene cannot run offline, the pipeline
yields empty results and the assertions below FAIL loudly — that is a real
regression (a missing committed cache), never a skip.
"""

from __future__ import annotations

from typing import Optional

import pytest

from claimledger.cache import ResponseCache
from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import OTClient, ResolveStatus
from claimledger.clients.pubmed import (
    PubMedClient,
    build_esearch_params,
    build_esearch_url,
    gene_term,
    pair_term,
)
from claimledger.conflicts import ConflictDetector
from claimledger.config import Config, Thresholds
from claimledger.evidence import (
    EvidenceStore,
    mint_evidence_id,
    ot_associations_request_key,
    pubmed_request_key,
)
from claimledger.http import HttpFetcher, RateLimiter
from claimledger.models import Source
from claimledger.render import SAFETY_NOTICE, DossierRenderer
from claimledger.verify import Verifier, verify_all

# The four Example_Genes and the Ensembl_IDs they must resolve to offline
# (Req 11.1-11.3). Taken straight from Config().example_genes; the ids are the
# stable OT identities captured in the committed cache.
EXPECTED_ENSEMBL: dict[str, str] = {
    "EGFR": "ENSG00000146648",
    "TP53": "ENSG00000141510",
    "BRCA1": "ENSG00000012048",
    "PANK2": "ENSG00000125779",
}

# The top_n used across the retrieval + claim + conflict steps (Config default).
TOP_N = 5


class OfflineRun:
    """The pieces produced by one offline pipeline run, for assertions."""

    __slots__ = (
        "gene",
        "ensembl_id",
        "associations",
        "literature_count",
        "co_mention_counts",
        "claims",
        "verified",
        "unsupported",
        "dossier",
        "flags",
        "store",
    )

    def __init__(
        self,
        *,
        gene: str,
        ensembl_id: Optional[str],
        associations: tuple,
        literature_count: Optional[int],
        co_mention_counts: dict[str, Optional[int]],
        claims: list,
        verified: list,
        unsupported: list,
        dossier: str,
        flags: list,
        store: EvidenceStore,
    ) -> None:
        self.gene = gene
        self.ensembl_id = ensembl_id
        self.associations = associations
        self.literature_count = literature_count
        self.co_mention_counts = co_mention_counts
        self.claims = claims
        self.verified = verified
        self.unsupported = unsupported
        self.dossier = dossier
        self.flags = flags
        self.store = store


def _make_offline_fetcher(config: Config) -> HttpFetcher:
    """Build an HttpFetcher that serves the committed cache and never sleeps.

    ``external_available`` is pinned to ``False`` so a present cache is served
    cache-first and the live ``do_request`` is not reached (Req 11.4). The rate
    limiter's clock/sleep are faked so the test stays fast and deterministic even
    though no live call is ever made.
    """
    return HttpFetcher(
        cache=ResponseCache(),  # defaults to the repo data/cache (committed)
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
        config=config,
        external_available=lambda: False,
    )


def run_offline_pipeline(gene: str, config: Config) -> OfflineRun:
    """Run resolve -> retrieve -> build -> verify -> detect -> render offline.

    Wires the real ``OTClient`` / ``PubMedClient`` over an offline
    :class:`HttpFetcher` and drives the full pipeline from the committed cache
    only, mirroring what task 17's Pipeline will orchestrate. Returns an
    :class:`OfflineRun` holding each stage's output for the shared assertions.

    The Evidence_IDs are minted the same way the clients mint them (via the
    shared ``*_request_key`` + :func:`mint_evidence_id`), so the ids wired into
    the ClaimBuilder are exactly the ids the clients stored in the
    EvidenceStore.
    """
    store = EvidenceStore()
    fetcher = _make_offline_fetcher(config)
    ot = OTClient(fetcher, store, config)
    pubmed = PubMedClient(fetcher, store, config)

    # 1. Resolve the symbol to an Ensembl_ID (Req 1.3, 11.4).
    outcome = ot.resolve(gene)
    assert outcome.status is ResolveStatus.RESOLVED, (
        f"{gene} did not resolve offline (status={outcome.status}); a committed "
        f"ot_search cache is likely missing"
    )
    ensembl_id = outcome.ensembl_id
    symbol = outcome.resolved_symbol or gene

    # 2a. Associated diseases (Req 2.1-2.7, 11.4).
    target_assoc = ot.associated_diseases(ensembl_id, top_n=TOP_N)
    associations = target_assoc.associations
    ot_assoc_evidence_id = mint_evidence_id(
        Source.OT_ASSOCIATIONS,
        ot_associations_request_key(ensembl_id, TOP_N, config.ot_graphql_url),
    )

    # 2b. PubMed gene-only literature count (Req 3.1-3.3, 11.4).
    gene_result = pubmed.esearch_gene(symbol)
    gene_url = build_esearch_url(
        config.pubmed_esearch_url,
        build_esearch_params(gene_term(symbol), config=config),
    )
    gene_evidence_id = mint_evidence_id(
        Source.PUBMED_GENE, pubmed_request_key(gene_url)
    )

    # 2c. Per-disease gene+disease co-mention counts (Req 3.4, 3.5, 11.4).
    pair_evidence_ids: dict[str, str] = {}
    co_mention_counts: dict[str, Optional[int]] = {}
    for assoc in associations:
        pair_result = pubmed.esearch_pair(symbol, assoc.disease_name)
        pair_url = build_esearch_url(
            config.pubmed_esearch_url,
            build_esearch_params(
                pair_term(symbol, assoc.disease_name), config=config
            ),
        )
        pair_evidence_ids[assoc.disease_id] = mint_evidence_id(
            Source.PUBMED_PAIR, pubmed_request_key(pair_url)
        )
        co_mention_counts[assoc.disease_id] = pair_result.count

    # 3. Build claims from the gathered evidence (Req 5).
    builder = ClaimBuilder(store)
    claims = builder.build(
        symbol,
        associations,
        ot_assoc_evidence_id=ot_assoc_evidence_id,
        gene_evidence_id=gene_evidence_id,
        pair_evidence_ids_by_disease=pair_evidence_ids,
    )

    # 4. Verify every claim (Req 6).
    verdicts = verify_all(Verifier(), claims, store)

    # 5. Detect conflicts at the default thresholds (Req 9).
    flags = ConflictDetector().detect_all(
        associations, config.thresholds, co_mention_counts
    )

    # 6. Render the dossier from Verified_Claims only (Req 8).
    disease_names = {a.disease_id: a.disease_name for a in associations}
    dossier = DossierRenderer().render(
        verdicts.verified, gene_symbol=symbol, disease_names=disease_names
    )

    return OfflineRun(
        gene=gene,
        ensembl_id=ensembl_id,
        associations=associations,
        literature_count=gene_result.count,
        co_mention_counts=co_mention_counts,
        claims=claims,
        verified=verdicts.verified,
        unsupported=verdicts.unsupported,
        dossier=dossier,
        flags=flags,
        store=store,
    )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard backstop: make any real ``requests`` call fail the test.

    The clients import ``requests`` lazily inside their ``do_request`` builders,
    so patching ``requests.get`` / ``requests.post`` to raise guarantees that any
    accidental live call (a cache miss that fell through to the network) fails
    loudly instead of reaching the internet. In offline mode with the committed
    cache present, these are never invoked.
    """
    import requests

    def _forbidden(*_args: object, **_kwargs: object):
        raise AssertionError(
            "network access attempted during an offline test (a committed "
            "cache file is missing or the cache-first path regressed)"
        )

    monkeypatch.setattr(requests, "get", _forbidden)
    monkeypatch.setattr(requests, "post", _forbidden)


# --------------------------------------------------------------------------- #
# Shared end-to-end assertions over all four Example_Genes (Req 11.1, 11.2,   #
# 11.4, 11.5, 12.8)                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("gene", list(EXPECTED_ENSEMBL))
def test_example_gene_runs_fully_offline(gene: str, no_network: None) -> None:
    """Each Example_Gene resolves, retrieves, builds, verifies, and renders offline."""
    run = run_offline_pipeline(gene, Config())

    # Resolution to the known Ensembl_ID (Req 1.3, 11.1, 11.4).
    assert run.ensembl_id == EXPECTED_ENSEMBL[gene]

    # Associations retrieved with the target identity populated (Req 2.2, 2.7).
    assert len(run.associations) == TOP_N
    assert all(a.evidence_id for a in run.associations)
    assert all(a.disease_id and a.disease_name for a in run.associations)

    # PubMed counts parse from cache (Req 3.2, 3.5): gene-only and every pair.
    assert isinstance(run.literature_count, int) and run.literature_count >= 0
    assert set(run.co_mention_counts) == {a.disease_id for a in run.associations}
    assert all(
        isinstance(c, int) and c >= 0 for c in run.co_mention_counts.values()
    ), run.co_mention_counts

    # Claims build and verify: non-empty Verified set, zero Unsupported — the
    # values come straight from the cached evidence (Req 5, 6, 11.4).
    assert run.claims, f"{gene}: no claims were built offline"
    assert run.verified, f"{gene}: no claims verified offline"
    assert run.unsupported == [], f"{gene}: unexpected Unsupported claims"
    assert len(run.verified) == len(run.claims)

    # Dossier renders non-empty, with the mandatory notice and approved wording
    # (Req 8.1, 10.1, 10.4). The renderer's own guard raises on forbidden terms,
    # so a successful render already proves none are present; we assert anyway.
    assert run.dossier.strip(), f"{gene}: empty dossier"
    assert SAFETY_NOTICE in run.dossier
    assert f"# Evidence dossier: {gene}" in run.dossier
    assert "associated with" in run.dossier or "reported in" in run.dossier
    lowered = run.dossier.lower()
    for forbidden in ("causes", "proves", "validated", "promising"):
        assert forbidden not in lowered, f"{gene}: forbidden wording {forbidden!r}"


@pytest.mark.parametrize("gene", list(EXPECTED_ENSEMBL))
def test_example_gene_evidence_all_from_cache(gene: str, no_network: None) -> None:
    """Every evidence record used is from_cache=True, proving offline (Req 11.4, 11.5, 11.7).

    The committed envelopes were captured live and persisted; served under the
    offline fetcher they come back with ``from_cache=True`` and their *original*
    source/url/retrieved_at provenance (Req 11.7). A record not from cache would
    mean a live call slipped through — the core offline guarantee failing.
    """
    run = run_offline_pipeline(gene, Config())

    records = list(run.store._records.values())
    # A full run stores: ot_search + ot_associations + gene PubMed + 5 pair
    # PubMed = 8 records. At minimum every one must be from cache.
    assert records, f"{gene}: evidence store is empty"
    assert all(r.from_cache for r in records), [
        (r.source.value, r.from_cache) for r in records
    ]
    # And every record carries its original provenance from the envelope.
    assert all(r.url and r.retrieved_at for r in records)


# --------------------------------------------------------------------------- #
# PANK2-specific: at least one conflict flag at default thresholds (Req 11.3) #
# --------------------------------------------------------------------------- #
def test_pank2_raises_a_conflict_flag_at_default_thresholds(
    no_network: None,
) -> None:
    """PANK2 trips >= 1 Single_Source_Dependence / Literature_Gap flag (Req 11.3).

    The fourth, lesser-studied Example_Gene was selected (not tuned) so its real
    cached Open Targets / PubMed data raises a conflict at the *default*
    ``Thresholds()`` (support 0.3, min datatypes 2, high score 0.5, low
    co-mention 100). Evaluating the committed cache through the real
    ConflictDetector must therefore produce at least one flag, each a named
    Single_Source_Dependence or Literature_Gap.
    """
    run = run_offline_pipeline("PANK2", Config())

    assert run.flags, "PANK2 raised no conflict flag at default thresholds"
    rules = {flag.rule for flag in run.flags}
    assert rules <= {"Single_Source_Dependence", "Literature_Gap"}
    assert rules, "PANK2 flags carry no named rule"

    # Every flag names a disease among PANK2's top associations, and carries the
    # default thresholds it was evaluated against (Req 9.8).
    disease_ids = {a.disease_id for a in run.associations}
    defaults = Thresholds()
    for flag in run.flags:
        assert flag.disease_id in disease_ids
        assert flag.thresholds == defaults


def test_egfr_well_studied_gene_raises_no_conflict_flag(no_network: None) -> None:
    """A well-studied gene (EGFR) trips no flag — PANK2's flags are not universal.

    Confirms the fourth gene was chosen to be distinctive: EGFR's strongly
    multi-datatype, heavily co-mentioned associations raise neither rule at the
    defaults, so the PANK2 assertion above is meaningful rather than trivially
    true for any gene.
    """
    run = run_offline_pipeline("EGFR", Config())
    assert run.flags == [], f"EGFR unexpectedly flagged: {run.flags}"


# --------------------------------------------------------------------------- #
# Cache-fallback path: live failure + cache present -> from_cache (Req 11.5)  #
# --------------------------------------------------------------------------- #
def test_cache_fallback_indicates_came_from_cache_on_live_failure(
    no_network: None,
) -> None:
    """A live failure with a cached response falls back and flags from_cache (Req 11.5).

    Distinct from the offline (``external_available=False``) path: here external
    access is *available* but the live ``do_request`` raises, modelling a failed
    or timed-out call. With a committed cache present for the request, the
    fetcher must return it with ``available=True`` and ``from_cache=True`` (the
    "came from cache" indication) rather than failing — and carry the envelope's
    original provenance (Req 11.5, 11.7).
    """
    config = Config()
    fetcher = HttpFetcher(
        cache=ResponseCache(),
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
        config=config,
        external_available=lambda: True,  # online, but the call will fail
    )

    from claimledger.evidence import ot_search_request_key

    request_key = ot_search_request_key("EGFR", config.ot_graphql_url)

    def failing_request():
        raise RuntimeError("simulated live failure / timeout")

    result = fetcher.fetch(request_key, config.ot_graphql_url, failing_request)

    assert result.available is True
    assert result.from_cache is True  # the "came from cache" indication
    assert result.source_url and result.retrieved_at  # original provenance
    assert result.payload is not None
