"""EvidenceStore + stable Evidence_ID minting (Req 4.1-4.3, 11.7).

This module turns a raw cache/live response envelope into a provenance-bearing
:class:`~claimledger.models.EvidenceRecord`, mints its stable ``Evidence_ID``,
and holds the per-run collection of records keyed by that id.

Stable Evidence_IDs (design "Stable Evidence_IDs", Req 4.2, cache/live parity)
------------------------------------------------------------------------------
An ``Evidence_ID`` is a deterministic function of the *logical request*, not of
wall-clock time or object identity, so a cached fetch and a live fetch of the
same request mint the **same** id::

    evidence_id = source.value + ":" + sha256(normalized_request_key)[:16]

The ``normalized_request_key`` is built per source from stable inputs:

* ``OT_SEARCH``:        ``graphql + "|search|" + symbol.strip().upper()``
* ``OT_ASSOCIATIONS``:  ``graphql + "|assoc|" + ensembl_id + "|top_n=" + top_n``
* ``PUBMED_GENE`` / ``PUBMED_PAIR``: the canonicalized esearch URL with its
  query params sorted and ``tool`` / ``email`` **excluded** from the hashed key
  (they identify the caller, not which evidence is returned).

Because the key is normalized (trimmed / upper-cased symbol; sorted query
params; caller identity dropped), the same logical request yields the same id
whether served live or from cache — the fallback is transparent to claims and
verification (Req 11.5, 11.7).

Building an EvidenceRecord from an envelope (Req 11.7)
-----------------------------------------------------
A cache/live envelope is the documented three-key shape
``{source_url, retrieved_at, payload}``. :func:`record_from_envelope` maps it
straight onto the model: ``source_url`` -> ``EvidenceRecord.url``,
``retrieved_at`` -> ``retrieved_at``, ``payload`` -> ``payload`` (Req 11.7), so a
record built from a cache hit carries the *original* provenance of the real
response, not the moment it was re-read.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Config
from .models import EvidenceRecord, Source

# Query parameters that identify the *caller* rather than *which evidence* is
# returned. They are stripped from the normalized PubMed request key so a key
# built with one tool/email matches one built with another for the same logical
# request (design "Stable Evidence_IDs", Req 11.7).
_CALLER_IDENTITY_PARAMS = ("tool", "email")

# Default Open Targets GraphQL endpoint used when a caller does not pass one
# explicitly. Kept in sync with Config so OT keys are stable across the app.
_DEFAULT_OT_GRAPHQL = Config().ot_graphql_url


# --------------------------------------------------------------------------- #
# Per-source normalized request keys                                          #
# --------------------------------------------------------------------------- #
def _normalize_symbol(symbol: str) -> str:
    """Case-fold and Unicode-normalize a gene symbol for a stable key.

    Trims surrounding whitespace, applies NFKC normalization, then
    ``str.casefold`` so the comparison is genuinely case-insensitive across the
    whole Unicode range — not just ASCII. Plain ``.upper()`` is not a stable
    case-insensitive normalizer: characters such as U+0130 (İ) do not round-trip
    through ``.lower()``/``.upper()`` and would mint two different ids for one
    logical symbol. NFKC + casefold collapses those equivalent forms so a "live"
    symbol string and its cached normalized form map to the same Evidence_ID.
    """
    return unicodedata.normalize("NFKC", symbol.strip()).casefold()


def ot_search_request_key(symbol: str, graphql_url: str = _DEFAULT_OT_GRAPHQL) -> str:
    """Normalized key for an OT target-search request (design OT_SEARCH).

    The symbol is trimmed and case-folded (via :func:`_normalize_symbol`) so
    ``" egfr "`` and ``"EGFR"`` mint the same id; the GraphQL endpoint pins the
    key to the Open Targets search interface.
    """
    return f"{graphql_url}|search|{_normalize_symbol(symbol)}"


def ot_associations_request_key(
    ensembl_id: str,
    top_n: int,
    graphql_url: str = _DEFAULT_OT_GRAPHQL,
) -> str:
    """Normalized key for an OT associated-diseases request (design OT_ASSOCIATIONS).

    Keyed on the resolved Ensembl id and the ``top_n`` page size, which together
    determine which association rows the response carries.
    """
    return f"{graphql_url}|assoc|{ensembl_id}|top_n={top_n}"


def pubmed_request_key(url: str) -> str:
    """Canonicalize a PubMed esearch URL into a stable normalized request key.

    The canonicalization is deterministic and order-independent:

    1. parse the URL into scheme/host/path and query params;
    2. drop the caller-identity params (``tool``, ``email``) — they never change
       which PMIDs are returned, so two otherwise-identical requests differing
       only in caller identity must mint the same id;
    3. sort the remaining params (by name, then value) so param ordering in the
       source URL is irrelevant;
    4. rebuild the URL.

    So a live request and a cached capture of the same logical esearch — even if
    their query strings were assembled in a different order, or one carried a
    different ``email`` — produce the identical key and therefore the identical
    ``Evidence_ID`` (design cache/live parity, Req 11.7).
    """
    parts = urlsplit(url)
    # keep_blank_values so a param present with an empty value still participates
    # deterministically rather than silently vanishing.
    params = parse_qsl(parts.query, keep_blank_values=True)
    filtered = [
        (name, value)
        for name, value in params
        if name not in _CALLER_IDENTITY_PARAMS
    ]
    filtered.sort()
    canonical_query = urlencode(filtered)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, canonical_query, parts.fragment)
    )


# --------------------------------------------------------------------------- #
# Evidence_ID minting                                                         #
# --------------------------------------------------------------------------- #
def mint_evidence_id(source: Source, normalized_request_key: str) -> str:
    """Mint the stable ``Evidence_ID`` for a source + normalized request key.

    ``evidence_id = source.value + ":" + sha256(normalized_request_key)[:16]``
    (design "Stable Evidence_IDs", Req 4.2). Pure and deterministic: the same
    inputs always yield the same id, which is what makes a cached fetch and a
    live fetch of the same logical request indistinguishable to downstream
    claims and verification.
    """
    digest = hashlib.sha256(normalized_request_key.encode("utf-8")).hexdigest()
    return f"{source.value}:{digest[:16]}"


# --------------------------------------------------------------------------- #
# Envelope -> EvidenceRecord                                                  #
# --------------------------------------------------------------------------- #
def record_from_envelope(
    source: Source,
    normalized_request_key: str,
    envelope: dict[str, Any],
    *,
    from_cache: bool = False,
    submitted_query: Optional[str] = None,
    query_translation: Optional[str] = None,
) -> EvidenceRecord:
    """Build an :class:`EvidenceRecord` from a cache/live envelope (Req 11.7).

    Maps the three-key envelope onto the model without altering provenance:
    ``source_url`` -> :attr:`EvidenceRecord.url`, ``retrieved_at`` ->
    :attr:`retrieved_at`, ``payload`` -> :attr:`payload` (Req 11.7). The
    ``Evidence_ID`` is minted from ``source`` + ``normalized_request_key`` so it
    is identical for the live and cached forms of the same request.

    ``submitted_query`` / ``query_translation`` are carried through for
    ``PUBMED_PAIR`` evidence (the co-mention provenance view, Req 4.3); they are
    ``None`` for the other sources.
    """
    evidence_id = mint_evidence_id(source, normalized_request_key)
    return EvidenceRecord(
        evidence_id=evidence_id,
        source=source,
        url=envelope["source_url"],
        retrieved_at=envelope["retrieved_at"],
        payload=envelope["payload"],
        submitted_query=submitted_query,
        query_translation=query_translation,
        from_cache=from_cache,
    )


# Logical fixture name -> Source, for the conftest ``evidence_store_factory``
# convenience path. The factory hands this module the committed real envelopes
# keyed by logical name; this maps each to the interface that produced it.
_NAME_TO_SOURCE: dict[str, Source] = {
    "ot_search": Source.OT_SEARCH,
    "ot_associations": Source.OT_ASSOCIATIONS,
    "pubmed_gene": Source.PUBMED_GENE,
    "pubmed_gene_and_disease": Source.PUBMED_PAIR,
    # permissive aliases
    "pubmed_pair": Source.PUBMED_PAIR,
}


def _normalized_key_for_envelope(source: Source, envelope: dict[str, Any]) -> str:
    """Derive a normalized request key from an envelope for the factory path.

    The committed envelopes carry a ``source_url`` but not the original request
    inputs (symbol / ensembl id). For PubMed the full esearch URL *is* the
    request, so it canonicalizes directly. For the Open Targets sources the
    ``source_url`` is only the GraphQL endpoint (the request body held the
    variables), so the endpoint string is used as the key — enough for the
    envelope-building helper the tests exercise, while the real pipeline mints
    OT ids through :func:`ot_search_request_key` /
    :func:`ot_associations_request_key` with the actual inputs.
    """
    source_url = envelope["source_url"]
    if source in (Source.PUBMED_GENE, Source.PUBMED_PAIR):
        return pubmed_request_key(source_url)
    return source_url


class EvidenceStore:
    """Per-run mapping ``Evidence_ID -> EvidenceRecord`` (design "Evidence store").

    A thin, deterministic collection: :meth:`add` a record, :meth:`get` one back
    by id (or ``None``), ask whether the store :meth:`contains` an id. Keying on
    the stable ``Evidence_ID`` means re-adding the same logical evidence (live
    then cached, say) is idempotent rather than duplicative.
    """

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        """Store ``record`` under its ``evidence_id`` and return it.

        Idempotent by id: adding a record whose id already exists overwrites the
        prior entry (the same logical request mints the same id, so this is a
        no-op in content for a live-then-cache re-add).
        """
        self._records[record.evidence_id] = record
        return record

    def get(self, evidence_id: str) -> Optional[EvidenceRecord]:
        """Return the record for ``evidence_id``, or ``None`` if absent."""
        return self._records.get(evidence_id)

    def contains(self, evidence_id: str) -> bool:
        """Return True iff a record with ``evidence_id`` has been added."""
        return evidence_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._records

    # -- envelope construction entry points (used by the conftest factory) -- #
    @classmethod
    def from_envelope(cls, name: str, envelope: dict[str, Any]) -> EvidenceRecord:
        """Build an :class:`EvidenceRecord` from a logical-name + envelope pair.

        ``name`` is one of the logical fixture names (``ot_search``,
        ``ot_associations``, ``pubmed_gene``, ``pubmed_gene_and_disease``); it
        selects the :class:`Source`. The normalized request key is derived from
        the envelope and the record is built via :func:`record_from_envelope`.
        """
        source = _NAME_TO_SOURCE.get(name)
        if source is None:
            raise KeyError(f"unknown evidence envelope name: {name!r}")
        key = _normalized_key_for_envelope(source, envelope)
        return record_from_envelope(source, key, envelope)

    def add_envelope(self, name: str, envelope: dict[str, Any]) -> EvidenceRecord:
        """Build a record from ``(name, envelope)`` and :meth:`add` it."""
        return self.add(self.from_envelope(name, envelope))

    @classmethod
    def from_envelopes(cls, envelopes: dict[str, dict[str, Any]]) -> "EvidenceStore":
        """Build a populated store from a ``{name: envelope}`` mapping.

        Iterated in sorted key order so construction is deterministic regardless
        of the mapping's insertion order.
        """
        store = cls()
        for name in sorted(envelopes):
            store.add_envelope(name, envelopes[name])
        return store
