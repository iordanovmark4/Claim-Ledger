"""Unit tests for the Tamper / Reset control (Req 13.1-13.4).

These build an :class:`~claimledger.evidence.EvidenceStore` from the committed
*real* EGFR cache envelopes (steering rule 8), build claims with the real
:class:`~claimledger.claims.ClaimBuilder`, pick a genuinely-verified ASSOCIATION
claim, and assert that :func:`claimledger.tamper.tamper` /
:func:`claimledger.tamper.reset`:

* tamper -> ``UNSUPPORTED`` with a reason reporting BOTH the tampered value and
  the recomputed value; ``value`` becomes the new value; ``original_value``
  holds the genuine original (Req 13.1, 13.2);
* reset -> ``VERIFIED`` with ``value`` restored and ``original_value`` cleared
  (Req 13.3);
* tamper-then-reset round-trips to the original value and VERIFIED;
* a double tamper keeps the genuine original (reset restores the true original,
  not the first tamper's value);
* reset of a never-tampered claim re-verifies VERIFIED and leaves value
  unchanged;
* the referenced Evidence_Record and cached payload bytes are unchanged across
  tamper+reset (Req 13.4).

Values asserted (NSCLC score, disease id) were read from the committed captures
in ``data/cache``, not assumed.
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace

import pytest

from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import parse_associations
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import Claim, ClaimStatus, ClaimType, Source
from claimledger.tamper import reset, tamper
from claimledger.verify import Verifier

# Known Evidence_IDs used to wire records into the store for these tests.
OT_ASSOC_ID = "ot_associations:testassoc000001"
GENE_ID = "pubmed_gene:testgene00000001"
PAIR_ID = "pubmed_pair:testpair00000001"

GENE_SYMBOL = "EGFR"

# Read from the real OT associations capture.
NSCLC_DISEASE_ID = "MONDO_0005233"
NSCLC_OVERALL_SCORE = 0.8503239816603523


def _with_id(record, evidence_id: str):
    """Return a copy of ``record`` with ``evidence_id`` replaced (frozen model)."""
    return replace(record, evidence_id=evidence_id)


@pytest.fixture
def full_store(
    ot_associations_envelope,
    pubmed_gene_envelope,
    pubmed_gene_and_disease_envelope,
) -> EvidenceStore:
    """Store holding the OT assoc, gene PubMed, and pair PubMed records."""
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


@pytest.fixture
def nsclc_association(built_claims) -> Claim:
    """A genuinely-verified ASSOCIATION claim (EGFR::NSCLC)."""
    claim = next(
        c
        for c in built_claims
        if c.claim_type is ClaimType.ASSOCIATION
        and c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )
    assert claim.original_value is None  # starts untampered
    return claim


# --------------------------------------------------------------------------- #
# Tamper -> UNSUPPORTED with both values; value + original_value set           #
# (Req 13.1, 13.2)                                                            #
# --------------------------------------------------------------------------- #
def test_tamper_rejects_with_both_values(full_store, nsclc_association) -> None:
    new_value = 0.12345  # a distinct value
    tampered, result = tamper(nsclc_association, new_value, full_store)

    assert result.status is ClaimStatus.UNSUPPORTED
    # Reason reports BOTH the tampered value and the recomputed value (Req 13.2).
    assert "0.12345" in result.reason
    assert repr(NSCLC_OVERALL_SCORE) in result.reason

    # The tampered claim carries the new value and is annotated Unsupported.
    assert tampered.value == new_value
    assert tampered.status is ClaimStatus.UNSUPPORTED
    assert tampered.reason == result.reason
    # original_value holds the genuine original so reset can restore the truth.
    assert math.isclose(
        tampered.original_value, NSCLC_OVERALL_SCORE, abs_tol=1e-9
    )


def test_tamper_does_not_mutate_input_claim(full_store, nsclc_association) -> None:
    """Claim is frozen: tamper returns a new claim, input is untouched."""
    before_value = nsclc_association.value
    before_original = nsclc_association.original_value

    tamper(nsclc_association, 0.999, full_store)

    assert nsclc_association.value == before_value
    assert nsclc_association.original_value == before_original


# --------------------------------------------------------------------------- #
# Reset -> VERIFIED, value restored, original_value cleared (Req 13.3)         #
# --------------------------------------------------------------------------- #
def test_reset_restores_verified(full_store, nsclc_association) -> None:
    tampered, _ = tamper(nsclc_association, 0.12345, full_store)

    reset_claim, result = reset(tampered, full_store)

    assert result.status is ClaimStatus.VERIFIED
    assert result.reason is None
    assert math.isclose(reset_claim.value, NSCLC_OVERALL_SCORE, abs_tol=1e-9)
    assert reset_claim.original_value is None
    assert reset_claim.status is ClaimStatus.VERIFIED


def test_tamper_then_reset_round_trips(full_store, nsclc_association) -> None:
    """tamper then reset returns to the original value and VERIFIED."""
    original_value = nsclc_association.value

    tampered, _ = tamper(nsclc_association, 0.0, full_store)
    reset_claim, result = reset(tampered, full_store)

    assert result.status is ClaimStatus.VERIFIED
    assert reset_claim.value == original_value
    assert reset_claim.original_value is None


# --------------------------------------------------------------------------- #
# Double tamper keeps the genuine original (Req 13.3)                          #
# --------------------------------------------------------------------------- #
def test_double_tamper_keeps_genuine_original(full_store, nsclc_association) -> None:
    original_value = nsclc_association.value

    first, _ = tamper(nsclc_association, 0.111, full_store)
    assert math.isclose(first.original_value, original_value, abs_tol=1e-9)

    # Tampering again must NOT overwrite the stored genuine original.
    second, result = tamper(first, 0.222, full_store)
    assert result.status is ClaimStatus.UNSUPPORTED
    assert second.value == 0.222
    assert math.isclose(second.original_value, original_value, abs_tol=1e-9)

    # Reset after two tampers restores the TRUE original, not the first tamper.
    reset_claim, reset_result = reset(second, full_store)
    assert reset_result.status is ClaimStatus.VERIFIED
    assert math.isclose(reset_claim.value, original_value, abs_tol=1e-9)
    assert reset_claim.value != 0.111
    assert reset_claim.original_value is None


# --------------------------------------------------------------------------- #
# Reset of a never-tampered claim re-verifies VERIFIED, value unchanged        #
# --------------------------------------------------------------------------- #
def test_reset_of_never_tampered_claim_is_noop_verify(
    full_store, nsclc_association
) -> None:
    before_value = nsclc_association.value
    assert nsclc_association.original_value is None

    reset_claim, result = reset(nsclc_association, full_store)

    assert result.status is ClaimStatus.VERIFIED
    assert reset_claim.value == before_value
    assert reset_claim.original_value is None


# --------------------------------------------------------------------------- #
# Evidence / cache immutability across tamper + reset (Req 13.4)               #
# --------------------------------------------------------------------------- #
def test_tamper_reset_leaves_evidence_bytes_unchanged(
    full_store, nsclc_association
) -> None:
    evidence_id = nsclc_association.evidence_ids[0]
    record_before = full_store.get(evidence_id)
    # Deep snapshot of the payload bytes to compare after the operations.
    payload_before = copy.deepcopy(record_before.payload)

    tampered, _ = tamper(nsclc_association, 0.42, full_store)
    reset(tampered, full_store)

    record_after = full_store.get(evidence_id)
    # Same object identity (nothing replaced it) and identical payload bytes.
    assert record_after is record_before
    assert record_after.payload == payload_before


def test_verifier_instance_is_reused_when_supplied(
    full_store, nsclc_association
) -> None:
    """An injected verifier is used (no evidence/store side effects)."""
    verifier = Verifier()
    tampered, result = tamper(nsclc_association, 0.5, full_store, verifier=verifier)
    assert result.status is ClaimStatus.UNSUPPORTED
    reset_claim, reset_result = reset(tampered, full_store, verifier=verifier)
    assert reset_result.status is ClaimStatus.VERIFIED
    assert math.isclose(reset_claim.value, NSCLC_OVERALL_SCORE, abs_tol=1e-9)
