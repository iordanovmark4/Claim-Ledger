"""Unit tests for EvidenceStore and stable Evidence_ID minting (Req 4.1-4.3, 11.7).

These cover the task-5.1 surface:

* ``EvidenceStore`` add / get / contains behavior and idempotent re-add;
* ``mint_evidence_id`` determinism (same inputs -> same id, right shape);
* the PubMed normalized key being invariant to ``tool`` / ``email`` and to query
  param ordering (design cache/live parity), so those requests mint one id;
* building an ``EvidenceRecord`` from a cache envelope preserving the original
  ``source_url`` -> url, ``retrieved_at``, and ``payload`` (Req 11.7).
"""

from __future__ import annotations

import hashlib

from claimledger.evidence import (
    EvidenceStore,
    mint_evidence_id,
    ot_search_request_key,
    pubmed_request_key,
    record_from_envelope,
)
from claimledger.models import EvidenceRecord, Source


def _record(evidence_id: str, source: Source = Source.OT_SEARCH) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        source=source,
        url="https://example.test/req",
        retrieved_at="2024-01-01T00:00:00Z",
        payload={"ok": True},
    )


# --------------------------------------------------------------------------- #
# EvidenceStore                                                               #
# --------------------------------------------------------------------------- #
def test_add_get_contains_round_trip() -> None:
    store = EvidenceStore()
    rec = _record("ot_search:deadbeefdeadbeef")

    assert store.contains(rec.evidence_id) is False
    assert store.get(rec.evidence_id) is None

    returned = store.add(rec)

    assert returned is rec
    assert store.contains(rec.evidence_id) is True
    assert store.get(rec.evidence_id) is rec
    assert rec.evidence_id in store
    assert len(store) == 1


def test_get_missing_returns_none() -> None:
    store = EvidenceStore()
    store.add(_record("ot_search:aaaaaaaaaaaaaaaa"))
    assert store.get("ot_search:ffffffffffffffff") is None
    assert store.contains("ot_search:ffffffffffffffff") is False


def test_add_is_idempotent_by_evidence_id() -> None:
    store = EvidenceStore()
    store.add(_record("pubmed_gene:1234567812345678", Source.PUBMED_GENE))
    store.add(_record("pubmed_gene:1234567812345678", Source.PUBMED_GENE))
    assert len(store) == 1


# --------------------------------------------------------------------------- #
# mint_evidence_id                                                            #
# --------------------------------------------------------------------------- #
def test_mint_is_deterministic_and_well_formed() -> None:
    key = ot_search_request_key("EGFR")
    first = mint_evidence_id(Source.OT_SEARCH, key)
    second = mint_evidence_id(Source.OT_SEARCH, key)

    assert first == second
    prefix, _, digest = first.partition(":")
    assert prefix == Source.OT_SEARCH.value
    assert len(digest) == 16
    expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    assert digest == expected


def test_ot_search_key_trims_and_uppercases_symbol() -> None:
    assert ot_search_request_key("  egfr ") == ot_search_request_key("EGFR")
    assert mint_evidence_id(
        Source.OT_SEARCH, ot_search_request_key("  egfr ")
    ) == mint_evidence_id(Source.OT_SEARCH, ot_search_request_key("EGFR"))


# --------------------------------------------------------------------------- #
# PubMed key canonicalization                                                 #
# --------------------------------------------------------------------------- #
_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def test_pubmed_key_invariant_to_tool_and_email() -> None:
    with_identity = (
        f"{_BASE}?db=pubmed&term=EGFR&retmax=3&retmode=json"
        "&tool=claimledger&email=hackathon@example.com"
    )
    other_identity = (
        f"{_BASE}?db=pubmed&term=EGFR&retmax=3&retmode=json"
        "&tool=someoneelse&email=other@example.org"
    )
    without = f"{_BASE}?db=pubmed&term=EGFR&retmax=3&retmode=json"

    assert pubmed_request_key(with_identity) == pubmed_request_key(without)
    assert pubmed_request_key(with_identity) == pubmed_request_key(other_identity)
    assert mint_evidence_id(
        Source.PUBMED_GENE, pubmed_request_key(with_identity)
    ) == mint_evidence_id(Source.PUBMED_GENE, pubmed_request_key(other_identity))


def test_pubmed_key_invariant_to_param_order() -> None:
    ordering_a = f"{_BASE}?db=pubmed&term=EGFR&retmax=3&retmode=json"
    ordering_b = f"{_BASE}?retmode=json&retmax=3&term=EGFR&db=pubmed"
    assert pubmed_request_key(ordering_a) == pubmed_request_key(ordering_b)


# --------------------------------------------------------------------------- #
# Envelope -> EvidenceRecord (Req 11.7)                                       #
# --------------------------------------------------------------------------- #
def test_record_from_envelope_preserves_provenance() -> None:
    envelope = {
        "source_url": "https://api.platform.opentargets.org/api/v4/graphql",
        "retrieved_at": "2023-11-15T09:30:00Z",
        "payload": {"data": {"search": {"hits": []}}},
    }
    key = ot_search_request_key("EGFR")
    record = record_from_envelope(Source.OT_SEARCH, key, envelope, from_cache=True)

    # source_url -> url, retrieved_at -> retrieved_at, payload -> payload (Req 11.7)
    assert record.url == envelope["source_url"]
    assert record.retrieved_at == envelope["retrieved_at"]
    assert record.payload == envelope["payload"]
    assert record.source is Source.OT_SEARCH
    assert record.from_cache is True
    assert record.evidence_id == mint_evidence_id(Source.OT_SEARCH, key)


def test_record_from_envelope_carries_pubmed_pair_query_fields() -> None:
    envelope = {
        "source_url": f"{_BASE}?db=pubmed&term=x&retmax=3&retmode=json",
        "retrieved_at": "2023-11-15T09:30:00Z",
        "payload": {"esearchresult": {"count": "7"}},
    }
    key = pubmed_request_key(envelope["source_url"])
    record = record_from_envelope(
        Source.PUBMED_PAIR,
        key,
        envelope,
        submitted_query='"EGFR"[Title/Abstract] AND "lung cancer"[Title/Abstract]',
        query_translation='"EGFR"[tiab] AND "lung neoplasms"[MeSH Terms]',
    )
    assert record.submitted_query is not None
    assert record.query_translation == '"EGFR"[tiab] AND "lung neoplasms"[MeSH Terms]'


def test_store_factory_from_real_envelopes(evidence_store_factory) -> None:
    """The conftest factory builds a populated store from the real envelopes."""
    store = evidence_store_factory()
    assert len(store) == len(
        {Source.OT_SEARCH, Source.OT_ASSOCIATIONS, Source.PUBMED_GENE, Source.PUBMED_PAIR}
    )
