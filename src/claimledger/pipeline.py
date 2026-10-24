"""Pipeline orchestrator wiring all layers (Req 1.1, 2.1, 3.1, 4.1, 5.1, 6.1, 8.1, 9.1, 11.4).

``Pipeline`` is the single reusable orchestrator the UI (task 18) and the tests
call instead of hand-wiring the layers. It holds a per-run
:class:`~claimledger.evidence.EvidenceStore` and sequences the whole flow the
design's "End-to-end data flow" describes:

    resolve
      -> OT associated_diseases
      -> PubMed gene-only literature count
      -> PubMed gene+disease co-mention count per top disease (rate-limited,
         cache-first)
      -> build claims
      -> verify
      -> detect conflicts
      -> render dossier

returning the Verified_Claims, Unsupported_Claims, the conflict flags, and the
dossier Markdown in one :class:`PipelineResult`.

This mirrors exactly the wiring the offline end-to-end test exercises in its
``run_offline_pipeline`` helper (``tests/test_offline.py``): the Evidence_IDs
are minted the same way the clients mint them â€” via the shared ``*_request_key``
helpers + :func:`~claimledger.evidence.mint_evidence_id` and the pure
``build_esearch_*`` URL/param builders â€” so the ids wired into the
:class:`~claimledger.claims.ClaimBuilder` are *exactly* the ids the clients
stored in the :class:`EvidenceStore`, and a freshly built claim verifies by
construction.

Construction / dependency injection
------------------------------------
``Pipeline(config=Config(), fetcher=None)`` builds its own
:class:`~claimledger.http.HttpFetcher` (hence its own
:class:`~claimledger.cache.ResponseCache` and :class:`~claimledger.http.RateLimiter`)
from the config when none is injected. A caller â€” notably a test â€” may inject an
*offline* fetcher so the whole run is served from the committed cache with no
network::

    fetcher = HttpFetcher(
        cache=ResponseCache(),
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
        config=config,
        external_available=lambda: False,
    )
    result = Pipeline(config, fetcher=fetcher).run("EGFR")

The :class:`EvidenceStore` is **per run**: :meth:`run` / :meth:`run_resolved`
create a fresh store (and fresh clients over it) each call, so two runs never
share evidence.

Resolution as a separate step (Req 1.6)
---------------------------------------
:meth:`resolve` exposes just the resolution step so the UI can present a
disambiguation list before committing to retrieval. :meth:`run` resolves and
then, only when the outcome is ``RESOLVED``, proceeds through the full sequence;
on ``NOT_FOUND`` / ``DISAMBIGUATE`` it returns a :class:`PipelineResult`
carrying the :class:`ResolveOutcome` and empty results so the UI can branch
without the pipeline crashing. :meth:`run_resolved` is the entry point for the
UI once the user has chosen a disambiguation hit (Req 1.7): it runs the full
sequence against an already-resolved Ensembl_ID / symbol.

Resilience (Req 11.4)
---------------------
Partial availability never crashes the run. If OT associations is unavailable
the pipeline still returns a :class:`PipelineResult` (with empty associations);
if a pair fetch is unavailable that disease's co-mention count is ``None`` and
it simply gets no co-mention claim and no Literature_Gap flag. The underlying
clients already degrade to empty/None rather than raising, and the builder /
detector / renderer tolerate the gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .cache import ResponseCache
from .claims import ClaimBuilder
from .clients.opentargets import (
    OTClient,
    ResolveOutcome,
    ResolveStatus,
    SearchHit,
)
from .clients.pubmed import (
    PubMedClient,
    build_esearch_params,
    build_esearch_url,
    gene_term,
    pair_term,
)
from .conflicts import ConflictDetector
from .config import Config
from .evidence import (
    EvidenceStore,
    mint_evidence_id,
    ot_associations_request_key,
    pubmed_request_key,
)
from .http import HttpFetcher, RateLimiter
from .models import Association, Claim, ConflictFlag, Source
from .render import DossierRenderer
from .verify import Verifier, verify_all


@dataclass(frozen=True)
class PipelineResult:
    """The complete outcome of a single pipeline run (Req 1.1-11.4).

    A plain frozen value object so a run's result is immutable and cheap to pass
    to the UI / tests. On a resolved run every field is populated; on a
    ``NOT_FOUND`` / ``DISAMBIGUATE`` resolution the retrieval fields are empty and
    ``resolve_outcome`` carries the status (and, for ``DISAMBIGUATE``, the target
    hits the UI must present).

    Fields
    ------
    gene_symbol:
        The symbol the run was requested for (what the user typed / chose).
    ensembl_id:
        The resolved Ensembl_ID, or ``None`` when resolution did not succeed.
    resolve_outcome:
        The :class:`ResolveOutcome` from the resolution step â€” the discriminant
        the UI branches on (``RESOLVED`` / ``NOT_FOUND`` / ``DISAMBIGUATE``,
        Req 1.5, 1.6).
    associations:
        The parsed top-``top_n`` OT associations (Req 2). Empty when OT was
        unavailable or resolution failed.
    literature_count:
        The gene-only PubMed literature count, or ``None`` when unavailable
        (Req 3.2, 3.8).
    co_mention_counts:
        ``disease_id -> Co_Mention_Count`` for each top disease; a value is
        ``None`` when that pair fetch was unavailable (Req 3.5, 3.8, 11.4).
    verified_claims / unsupported_claims:
        The verification verdict split (Req 6). Each list holds claims annotated
        with their status/reason, value untouched.
    conflict_flags:
        Every :class:`ConflictFlag` raised across the associations at the config
        thresholds (Req 9).
    dossier_markdown:
        The rendered dossier Markdown from Verified_Claims only (Req 8).
    any_from_cache / all_from_cache:
        Whether any / all evidence records used in the run came from cache
        (Req 11.4, 11.5). ``all_from_cache`` is ``True`` for a fully offline run;
        both are ``False`` when no evidence was gathered at all.
    """

    gene_symbol: str
    ensembl_id: Optional[str]
    resolve_outcome: ResolveOutcome
    associations: tuple[Association, ...] = ()
    literature_count: Optional[int] = None
    co_mention_counts: dict[str, Optional[int]] = field(default_factory=dict)
    verified_claims: list[Claim] = field(default_factory=list)
    unsupported_claims: list[Claim] = field(default_factory=list)
    conflict_flags: list[ConflictFlag] = field(default_factory=list)
    dossier_markdown: str = ""
    any_from_cache: bool = False
    all_from_cache: bool = False
    # The per-run evidence store and the disease_id -> pair Evidence_ID map are
    # exposed so the UI (task 18) can show per-claim provenance (Evidence tab),
    # resolve a claim's EvidenceRecord for the Tamper_Control, and indicate
    # "from cache" per record (Req 4.7, 11.5, 13). Optional + default None so
    # existing callers/tests that build a PipelineResult without them are
    # unaffected.
    store: Optional[EvidenceStore] = None
    pair_evidence_ids: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> ResolveStatus:
        """Convenience accessor for the resolution status (the UI branch key)."""
        return self.resolve_outcome.status


class Pipeline:
    """Sequence every layer for one gene, returning a :class:`PipelineResult`.

    Parameters
    ----------
    config:
        The :class:`~claimledger.config.Config` supplying endpoints, caller
        identity, thresholds (``top_n``, conflict cut-offs) and limits. Defaults
        to :class:`Config`.
    fetcher:
        An optional :class:`~claimledger.http.HttpFetcher` to use for every run.
        Inject an offline fetcher (``external_available=lambda: False`` over the
        committed :class:`ResponseCache`) to run entirely from cache with no
        network. When ``None`` the pipeline builds its own fetcher from
        ``config`` (a default :class:`ResponseCache` over ``data/cache`` and a
        :class:`RateLimiter` at ``config.max_requests_per_second``).

    The :class:`EvidenceStore` is created fresh on each :meth:`run` /
    :meth:`run_resolved`, so runs never share evidence; the fetcher (and its
    cache / rate limiter) is shared across runs by design, so the rate limit is
    honored across a sequence of runs.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        fetcher: Optional[HttpFetcher] = None,
    ) -> None:
        self.config = config if config is not None else Config()
        self.fetcher = fetcher if fetcher is not None else self._default_fetcher()

    def _default_fetcher(self) -> HttpFetcher:
        """Build the default online fetcher from the config.

        A :class:`ResponseCache` over the repo ``data/cache`` and a
        :class:`RateLimiter` at ``config.max_requests_per_second`` (Req 3.9), with
        external access available (live-then-cache). A caller wanting offline
        behavior injects its own fetcher instead.
        """
        return HttpFetcher(
            cache=ResponseCache(),
            rate_limiter=RateLimiter(
                max_requests_per_second=self.config.max_requests_per_second
            ),
            config=self.config,
        )

    # -- resolution (exposed so the UI can disambiguate, Req 1.6) ----------- #
    def resolve(self, gene_symbol: str) -> ResolveOutcome:
        """Resolve ``gene_symbol`` to an Ensembl_ID (just the resolution step).

        Runs the OT target search + resolution against a throwaway store and
        returns the :class:`ResolveOutcome` so the UI can present a
        disambiguation list (Req 1.6) before committing to retrieval. The search
        evidence recorded here is discarded; :meth:`run_resolved` re-fetches
        everything (cache-first, so this is cheap) into the real per-run store.
        """
        store = EvidenceStore()
        ot = OTClient(self.fetcher, store, self.config)
        return ot.resolve(gene_symbol)

    def resolve_chosen(self, hit: SearchHit) -> ResolveOutcome:
        """Turn a user's chosen disambiguation hit into a RESOLVED outcome (Req 1.7).

        A thin pass-through to the OT client's pure ``resolve_chosen`` so the UI
        can go straight from a picked :class:`SearchHit` to a resolved outcome it
        feeds into :meth:`run_resolved`.
        """
        from .clients.opentargets import resolve_chosen

        return resolve_chosen(hit)

    # -- full run ----------------------------------------------------------- #
    def run(self, gene_symbol: str) -> PipelineResult:
        """Resolve ``gene_symbol`` then run the full sequence when resolved.

        Resolves first (Req 1.1). On ``RESOLVED`` it proceeds through the full
        retrieve -> build -> verify -> detect -> render sequence via
        :meth:`run_resolved`. On ``NOT_FOUND`` (Req 1.5) or ``DISAMBIGUATE``
        (Req 1.6) it returns a :class:`PipelineResult` carrying the
        :class:`ResolveOutcome` and empty results, so the UI can stop the flow or
        present the disambiguation hits without the pipeline crashing.
        """
        outcome = self.resolve(gene_symbol)
        if outcome.status is not ResolveStatus.RESOLVED:
            # NOT_FOUND / DISAMBIGUATE: hand the outcome back with empty results.
            return PipelineResult(
                gene_symbol=gene_symbol,
                ensembl_id=outcome.ensembl_id,
                resolve_outcome=outcome,
            )
        assert outcome.ensembl_id is not None  # RESOLVED always carries an id
        return self.run_resolved(
            outcome.ensembl_id,
            resolved_symbol=outcome.resolved_symbol or gene_symbol,
            resolve_outcome=outcome,
            requested_symbol=gene_symbol,
        )

    def run_resolved(
        self,
        ensembl_id: str,
        *,
        resolved_symbol: str,
        resolve_outcome: Optional[ResolveOutcome] = None,
        requested_symbol: Optional[str] = None,
    ) -> PipelineResult:
        """Run the full sequence for an already-resolved gene (Req 2-11.4).

        This is the entry point the UI uses once a disambiguation choice has been
        made (Req 1.7): it skips re-resolution and drives retrieval -> claims ->
        verify -> conflicts -> render against a fresh per-run store, minting the
        Evidence_IDs exactly as the clients do so builder/store ids line up.

        Parameters
        ----------
        ensembl_id:
            The resolved Ensembl_ID to retrieve associations for.
        resolved_symbol:
            The resolved approved symbol â€” the subject of every claim and the
            gene half of each ``GENE::DISEASE`` pair, and the gene term for the
            PubMed queries.
        resolve_outcome:
            The resolution outcome to carry on the result. Defaults to a
            synthesized ``RESOLVED`` outcome when called directly without one.
        requested_symbol:
            The symbol the user originally requested (for ``PipelineResult.gene_symbol``).
            Defaults to ``resolved_symbol``.

        Returns
        -------
        PipelineResult
            The full run outcome. Resilient to partial availability (Req 11.4):
            unavailable OT associations yield empty associations; an unavailable
            pair yields a ``None`` co-mention count and no claim/flag for it.
        """
        symbol = resolved_symbol
        top_n = self.config.thresholds.top_n
        outcome = resolve_outcome or ResolveOutcome(
            status=ResolveStatus.RESOLVED,
            ensembl_id=ensembl_id,
            resolved_symbol=symbol,
        )

        # Fresh per-run store + clients over the shared fetcher.
        store = EvidenceStore()
        ot = OTClient(self.fetcher, store, self.config)
        pubmed = PubMedClient(self.fetcher, store, self.config)

        # 1. OT associated diseases (Req 2). Mint the ot_associations id the same
        #    way the client does so the builder's id matches the stored record.
        target_assoc = ot.associated_diseases(ensembl_id, top_n=top_n)
        associations = target_assoc.associations
        ot_assoc_evidence_id = mint_evidence_id(
            Source.OT_ASSOCIATIONS,
            ot_associations_request_key(
                ensembl_id, top_n, self.config.ot_graphql_url
            ),
        )

        # 2. PubMed gene-only literature count (Req 3.1-3.3).
        gene_result = pubmed.esearch_gene(symbol)
        gene_url = build_esearch_url(
            self.config.pubmed_esearch_url,
            build_esearch_params(gene_term(symbol), config=self.config),
        )
        gene_evidence_id = mint_evidence_id(
            Source.PUBMED_GENE, pubmed_request_key(gene_url)
        )

        # 3. Per-disease gene+disease co-mention counts (Req 3.4, 3.5), rate-
        #    limited + cache-first through the fetcher. An unavailable pair yields
        #    a None count and no claim/flag for that disease (Req 11.4).
        pair_evidence_ids: dict[str, str] = {}
        co_mention_counts: dict[str, Optional[int]] = {}
        for assoc in associations:
            pair_result = pubmed.esearch_pair(symbol, assoc.disease_name)
            pair_url = build_esearch_url(
                self.config.pubmed_esearch_url,
                build_esearch_params(
                    pair_term(symbol, assoc.disease_name), config=self.config
                ),
            )
            pair_evidence_ids[assoc.disease_id] = mint_evidence_id(
                Source.PUBMED_PAIR, pubmed_request_key(pair_url)
            )
            co_mention_counts[assoc.disease_id] = pair_result.count

        # 4. Build claims from the gathered evidence (Req 5).
        builder = ClaimBuilder(store)
        claims = builder.build(
            symbol,
            associations,
            ot_assoc_evidence_id=ot_assoc_evidence_id,
            gene_evidence_id=gene_evidence_id,
            pair_evidence_ids_by_disease=pair_evidence_ids,
        )

        # 5. Verify every claim (Req 6).
        verdicts = verify_all(Verifier(), claims, store)

        # 6. Detect conflicts at the config thresholds (Req 9). A disease with an
        #    unavailable (None) co-mention count skips Literature_Gap (Req 9.3).
        available_counts = {
            disease_id: count
            for disease_id, count in co_mention_counts.items()
            if count is not None
        }
        flags = ConflictDetector().detect_all(
            associations, self.config.thresholds, available_counts
        )

        # 7. Render the dossier from Verified_Claims only (Req 8).
        disease_names = {a.disease_id: a.disease_name for a in associations}
        dossier = DossierRenderer().render(
            verdicts.verified, gene_symbol=symbol, disease_names=disease_names
        )

        any_from_cache, all_from_cache = self._cache_summary(store)

        return PipelineResult(
            gene_symbol=requested_symbol or symbol,
            ensembl_id=ensembl_id,
            resolve_outcome=outcome,
            associations=associations,
            literature_count=gene_result.count,
            co_mention_counts=co_mention_counts,
            verified_claims=verdicts.verified,
            unsupported_claims=verdicts.unsupported,
            conflict_flags=flags,
            dossier_markdown=dossier,
            any_from_cache=any_from_cache,
            all_from_cache=all_from_cache,
            store=store,
            pair_evidence_ids=pair_evidence_ids,
        )

    @staticmethod
    def _cache_summary(store: EvidenceStore) -> tuple[bool, bool]:
        """Summarize the from_cache flags of every record in ``store`` (Req 11.4).

        Returns ``(any_from_cache, all_from_cache)``. With no records gathered
        (everything unavailable) both are ``False`` â€” there is no cached evidence
        to have come from.
        """
        records = list(store._records.values())
        if not records:
            return False, False
        return (
            any(r.from_cache for r in records),
            all(r.from_cache for r in records),
        )


__all__ = ["Pipeline", "PipelineResult"]
