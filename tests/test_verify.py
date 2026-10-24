"""Unit tests for Verifier (Req 6.1-6.7, 7.1, 7.3, 7.4).

These build an :class:`~claimledger.evidence.EvidenceStore` from the committed
*real* cache envelopes (steering rule 8) with known Evidence_IDs, build claims
with the real :class:`~claimledger.claims.ClaimBuilder`, and assert the verifier:

* accepts every freshly-built claim as VERIFIED (builder/verifier share the same
  ``recompute_*``, so they agree by construction — Req 6.1, 6.4);
* rejects a claim whose value was tampered, reporting BOTH the claim value and
  the recomputed value in the reason, and leaving ``claim.value`` UNCHANGED
  after verify (Req 6.3, 6.7, 7.4);
* rejects a claim referencing a missing Evidence_ID, naming that id (Req 6.2);
* verifies a DATATYPE_BREAKDOWN whose tuple is reordered (order-independence,
  Req 6.5);
* compares counts exactly (an off-by-one count -> UNSUPPORTED, Req 6.6).

Values asserted (NSCLC score, gene/pair counts, real PMIDs, disease ids) were
read from the committed captures in ``data/cache``, not assumed.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import parse_associations
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
    Source,
)
from claimledger.verify import Verifier, annotate, verify_all

# Known Evidence_IDs used to wire records into the store for these tests.
OT_ASSOC_ID = "ot_associations:testassoc000001"
GENE_ID = "pubmed_gene:testgene00000001"
PAIR_ID = "pubmed_pair:testpair00000001"

GENE_SYMBOL = "EGFR"

# Read from the real OT associations capture.
NSCLC_DISEASE_ID = "MONDO_0005233"
NSCLC_OVERALL_SCORE = 0.8503239816603523

# Read from the real PubMed captures.
GENE_LITERATURE_COUNT = 108097
PAIR_CO_MENTION_COUNT = 18950


# --------------------------------------------------------------------------- #
# Store + claim fixtures                                                      #
# --------------------------------------------------------------------------- #
def _with_id(record, evidence_id: str):
    """Return a copy of ``record`` with ``evidence_id`` replaced (frozen model)."""
    return replace(record, evidence_id=evidence_id)


@pytest.fixture
def full_store(
    ot_associations_envelope,
    pubmed_gene_envelope,
    pubmed_gene_and_disease_envelope,
) -> EvidenceStore:
    """Store holding the OT assoc, gene PubMed, and pair PubMed records.

    Ids are forced to the known constants so the builder/verifier wiring is
    controlled deterministically by the test.
    """
    store = EvidenceStore()
    store.add(
        _with_id(
            record_from_envelope(
                Source.OT_ASSOCIATIONS, OT_ASSOC_ID, ot_associations_envelope
            ),
            OT_ASSOC_ID,
        )
    )
    store.add(
        _with_id(
            record_from_envelope(Source.PUBMED_GENE, GENE_ID, pubmed_gene_envelope),
            GENE_ID,
        )
    )
    store.add(
        _with_id(
            record_from_envelope(
                Source.PUBMED_PAIR, PAIR_ID, pubmed_gene_and_disease_envelope
            ),
            PAIR_ID,
        )
    )
    return store


@pytest.fixture
def associations(ot_associations_envelope):
    """The parsed top associations from the real OT envelope."""
    result = parse_associations(ot_associations_envelope["payload"], 5, OT_ASSOC_ID)
    return result.associations


@pytest.fixture
def built_claims(full_store, associations) -> list[Claim]:
    """Every claim type built from the real evidence (builder == verifier)."""
    return ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease={NSCLC_DISEASE_ID: PAIR_ID},
    )


def _claim_of_type(claims: list[Claim], claim_type: ClaimType) -> Claim:
    return next(c for c in claims if c.claim_type is claim_type)


def _nsclc_claim(claims: list[Claim], claim_type: ClaimType) -> Claim:
    return next(
        c
        for c in claims
        if c.claim_type is claim_type
        and c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )


# --------------------------------------------------------------------------- #
# Every built claim verifies (Req 6.1, 6.4)                                   #
# --------------------------------------------------------------------------- #
def test_all_built_claims_verify(full_store, built_claims) -> None:
    verifier = Verifier()
    assert built_claims  # non-empty
    for claim in built_claims:
        result = verifier.verify(claim, full_store)
        assert result.status is ClaimStatus.VERIFIED, (
            f"{claim.claim_type} {claim.subject} unexpectedly {result.status}: "
            f"{result.reason}"
        )
        assert result.reason is None


def test_verify_all_splits_verified_and_unsupported(full_store, built_claims) -> None:
    verdicts = verify_all(Verifier(), built_claims, full_store)
    assert len(verdicts.verified) == len(built_claims)
    assert verdicts.unsupported == []
    assert all(c.status is ClaimStatus.VERIFIED for c in verdicts.verified)


# --------------------------------------------------------------------------- #
# Tampered value -> UNSUPPORTED, both values in reason, value UNCHANGED        #
# (Req 6.3, 6.7, 7.4)                                                          #
# --------------------------------------------------------------------------- #
def test_tampered_association_value_rejected_with_both_values(
    full_store, built_claims
) -> None:
    original = _nsclc_claim(built_claims, ClaimType.ASSOCIATION)
    tampered = replace(original, value=0.12345)  # a different number

    result = Verifier().verify(tampered, full_store)

    assert result.status is ClaimStatus.UNSUPPORTED
    # Reason reports BOTH the claim value and the recomputed value (Req 6.3/7.4).
    assert "0.12345" in result.reason
    assert repr(NSCLC_OVERALL_SCORE) in result.reason
    assert result.claim_value == 0.12345
    assert math.isclose(result.recomputed_value, NSCLC_OVERALL_SCORE, abs_tol=1e-9)
    # No-repair: the claim value is UNCHANGED after verify (Req 6.7).
    assert tampered.value == 0.12345


def test_verify_does_not_mutate_claim_value_on_rejection(
    full_store, built_claims
) -> None:
    """Snapshot value before/after verify for a rejected claim (Req 6.7)."""
    original = _claim_of_type(built_claims, ClaimType.LITERATURE_COUNT)
    tampered = replace(original, value=GENE_LITERATURE_COUNT + 1)
    before = tampered.value

    Verifier().verify(tampered, full_store)

    assert tampered.value == before  # byte-identical, untouched


def test_annotate_sets_status_reason_but_keeps_value(full_store, built_claims) -> None:
    original = _nsclc_claim(built_claims, ClaimType.ASSOCIATION)
    tampered = replace(original, value=0.999)
    result = Verifier().verify(tampered, full_store)

    annotated = annotate(tampered, result)

    assert annotated.status is ClaimStatus.UNSUPPORTED
    assert annotated.reason == result.reason
    # annotate must not repair the value — only status/reason change.
    assert annotated.value == 0.999


# --------------------------------------------------------------------------- #
# Missing Evidence_ID -> UNSUPPORTED naming the id (Req 6.2)                   #
# --------------------------------------------------------------------------- #
def test_missing_evidence_id_rejected_naming_it(full_store, built_claims) -> None:
    missing_id = "ot_associations:absent0000000"
    original = _nsclc_claim(built_claims, ClaimType.ASSOCIATION)
    claim = replace(original, evidence_ids=(missing_id,))

    result = Verifier().verify(claim, full_store)

    assert result.status is ClaimStatus.UNSUPPORTED
    assert missing_id in result.reason
    assert result.recomputed_value is None


def test_empty_evidence_ids_rejected(full_store, built_claims) -> None:
    original = _nsclc_claim(built_claims, ClaimType.ASSOCIATION)
    claim = replace(original, evidence_ids=())

    result = Verifier().verify(claim, full_store)

    assert result.status is ClaimStatus.UNSUPPORTED
    assert "missing evidence" in result.reason


# --------------------------------------------------------------------------- #
# DATATYPE_BREAKDOWN reordered still VERIFIED (order-independence, Req 6.5)    #
# --------------------------------------------------------------------------- #
def test_reordered_datatype_breakdown_still_verified(full_store, built_claims) -> None:
    original = _nsclc_claim(built_claims, ClaimType.DATATYPE_BREAKDOWN)
    assert len(original.value) >= 2  # meaningful reorder requires >= 2 entries

    reordered = replace(original, value=tuple(reversed(original.value)))
    # Sanity: the reordered tuple is genuinely a different ordering.
    assert reordered.value != original.value

    result = Verifier().verify(reordered, full_store)
    assert result.status is ClaimStatus.VERIFIED


def test_datatype_breakdown_rejected_when_a_score_differs(
    full_store, built_claims
) -> None:
    original = _nsclc_claim(built_claims, ClaimType.DATATYPE_BREAKDOWN)
    first = original.value[0]
    bumped = DatatypeScore(id=first.id, score=first.score + 0.5)
    tampered = replace(original, value=(bumped,) + tuple(original.value[1:]))

    result = Verifier().verify(tampered, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED
    assert first.id in result.reason  # names the datatype in the reason


def test_datatype_breakdown_rejected_when_an_id_is_missing(
    full_store, built_claims
) -> None:
    """Dropping a datatype id makes the id sets differ -> UNSUPPORTED (Req 6.5)."""
    original = _nsclc_claim(built_claims, ClaimType.DATATYPE_BREAKDOWN)
    assert len(original.value) >= 2
    tampered = replace(original, value=tuple(original.value[1:]))  # drop one id

    result = Verifier().verify(tampered, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Counts compared exactly (off-by-one -> UNSUPPORTED, Req 6.6)                 #
# --------------------------------------------------------------------------- #
def test_literature_count_off_by_one_rejected(full_store, built_claims) -> None:
    original = _claim_of_type(built_claims, ClaimType.LITERATURE_COUNT)
    assert original.value == GENE_LITERATURE_COUNT
    tampered = replace(original, value=GENE_LITERATURE_COUNT + 1)

    result = Verifier().verify(tampered, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED
    assert str(GENE_LITERATURE_COUNT + 1) in result.reason
    assert str(GENE_LITERATURE_COUNT) in result.reason


def test_co_mention_count_off_by_one_rejected(full_store, built_claims) -> None:
    original = _nsclc_claim(built_claims, ClaimType.CO_MENTION_COUNT)
    assert original.value == PAIR_CO_MENTION_COUNT
    tampered = replace(original, value=PAIR_CO_MENTION_COUNT - 1)

    result = Verifier().verify(tampered, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED


# --------------------------------------------------------------------------- #
# RECENT_PAPER: a PMID not in the idlist -> UNSUPPORTED (Req 6.3)              #
# --------------------------------------------------------------------------- #
def test_recent_paper_not_in_idlist_rejected(full_store, built_claims) -> None:
    original = _claim_of_type(built_claims, ClaimType.RECENT_PAPER)
    tampered = replace(original, value="00000000")  # a PMID not in the idlist

    result = Verifier().verify(tampered, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED
    assert result.recomputed_value is None  # recompute failed (not in idlist)
