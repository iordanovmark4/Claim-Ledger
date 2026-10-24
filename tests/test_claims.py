"""Unit tests for ClaimBuilder (Req 5.1-5.8).

These build an :class:`~claimledger.evidence.EvidenceStore` from the committed
*real* cache envelopes (steering rule 8) with known Evidence_IDs, parse the OT
associations with the real :func:`~claimledger.clients.opentargets.parse_associations`,
and assert the builder:

* emits one ASSOCIATION and one DATATYPE_BREAKDOWN per top disease with the
  right recomputed values and the OT Evidence_ID (Req 5.3, 5.4);
* emits a LITERATURE_COUNT for the gene (108097) referencing the gene PubMed id
  (Req 5.5);
* emits a CO_MENTION_COUNT for the wired pair (18950) referencing the pair
  PubMed id (Req 5.6);
* emits one RECENT_PAPER per PMID in the gene idlist (Req 5.7);
* attaches >= 1 Evidence_ID to every claim (Req 5.2);
* omits claims whose referenced record is absent from the store (steering
  rule 6) rather than fabricating values.

Values asserted (NSCLC score, gene/pair counts, real PMIDs, disease ids) were
read from the committed captures in ``data/cache``, not assumed.
"""

from __future__ import annotations

import math

import pytest

from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import parse_associations
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import Claim, ClaimType, DatatypeScore, Source

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
GENE_IDLIST = ["42920723", "42920699", "42920570"]


# --------------------------------------------------------------------------- #
# Store + association fixtures                                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def ot_assoc_record(ot_associations_envelope):
    return record_from_envelope(Source.OT_ASSOCIATIONS, OT_ASSOC_ID, ot_associations_envelope)


@pytest.fixture
def gene_record(pubmed_gene_envelope):
    return record_from_envelope(Source.PUBMED_GENE, GENE_ID, pubmed_gene_envelope)


@pytest.fixture
def pair_record(pubmed_gene_and_disease_envelope):
    return record_from_envelope(Source.PUBMED_PAIR, PAIR_ID, pubmed_gene_and_disease_envelope)


@pytest.fixture
def full_store(ot_assoc_record, gene_record, pair_record) -> EvidenceStore:
    """A store holding the OT assoc, gene PubMed, and pair PubMed records.

    The Evidence_IDs are forced to the known constants above by constructing each
    record with ``record_from_envelope`` and re-wrapping it, so the test can wire
    the builder with ids it controls.
    """
    store = EvidenceStore()
    # record_from_envelope mints an id from the request key; replace the id with
    # our known constant so the test controls the wiring deterministically.
    for record, known_id in (
        (ot_assoc_record, OT_ASSOC_ID),
        (gene_record, GENE_ID),
        (pair_record, PAIR_ID),
    ):
        store.add(_with_id(record, known_id))
    return store


def _with_id(record, evidence_id: str):
    """Return a copy of ``record`` with ``evidence_id`` replaced (frozen model)."""
    from dataclasses import replace

    return replace(record, evidence_id=evidence_id)


@pytest.fixture
def associations(ot_associations_envelope):
    """The parsed top associations from the real OT envelope."""
    result = parse_associations(ot_associations_envelope["payload"], 5, OT_ASSOC_ID)
    return result.associations


@pytest.fixture
def pair_ids_by_disease():
    """Wire the NSCLC pair to the (single) committed pair PubMed record.

    The real capture holds one gene+disease PubMed response; mapping the NSCLC
    disease id to it gives a representative CO_MENTION_COUNT claim.
    """
    return {NSCLC_DISEASE_ID: PAIR_ID}


# --------------------------------------------------------------------------- #
# ASSOCIATION + DATATYPE_BREAKDOWN per pair (Req 5.3, 5.4)                     #
# --------------------------------------------------------------------------- #
def test_one_association_and_breakdown_per_top_disease(
    full_store, associations, pair_ids_by_disease
) -> None:
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease=pair_ids_by_disease,
    )

    disease_ids = [a.disease_id for a in associations]
    assert len(disease_ids) == 5  # top_n from the real capture

    assoc_claims = [c for c in claims if c.claim_type is ClaimType.ASSOCIATION]
    breakdown_claims = [c for c in claims if c.claim_type is ClaimType.DATATYPE_BREAKDOWN]

    # Exactly one of each per top disease.
    assert {c.subject for c in assoc_claims} == {
        f"{GENE_SYMBOL}::{d}" for d in disease_ids
    }
    assert len(assoc_claims) == len(disease_ids)
    assert len(breakdown_claims) == len(disease_ids)

    # The NSCLC association claim carries the real score and the OT evidence id.
    nsclc = next(
        c for c in assoc_claims if c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )
    assert isinstance(nsclc.value, float)
    assert math.isclose(nsclc.value, NSCLC_OVERALL_SCORE, abs_tol=1e-9)
    assert nsclc.evidence_ids == (OT_ASSOC_ID,)

    # Its datatype breakdown is a tuple of DatatypeScore sorted by id.
    nsclc_bd = next(
        c for c in breakdown_claims if c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )
    assert isinstance(nsclc_bd.value, tuple)
    assert all(isinstance(ds, DatatypeScore) for ds in nsclc_bd.value)
    ids = [ds.id for ds in nsclc_bd.value]
    assert ids == sorted(ids)
    assert nsclc_bd.evidence_ids == (OT_ASSOC_ID,)


# --------------------------------------------------------------------------- #
# LITERATURE_COUNT (Req 5.5)                                                   #
# --------------------------------------------------------------------------- #
def test_literature_count_for_gene(full_store, associations) -> None:
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL, associations, ot_assoc_evidence_id=OT_ASSOC_ID, gene_evidence_id=GENE_ID
    )
    lit = [c for c in claims if c.claim_type is ClaimType.LITERATURE_COUNT]
    assert len(lit) == 1
    assert lit[0].subject == GENE_SYMBOL
    assert lit[0].value == GENE_LITERATURE_COUNT
    assert lit[0].evidence_ids == (GENE_ID,)


# --------------------------------------------------------------------------- #
# CO_MENTION_COUNT (Req 5.6)                                                   #
# --------------------------------------------------------------------------- #
def test_co_mention_count_for_pair(full_store, associations, pair_ids_by_disease) -> None:
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease=pair_ids_by_disease,
    )
    com = [c for c in claims if c.claim_type is ClaimType.CO_MENTION_COUNT]
    assert len(com) == 1
    assert com[0].subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    assert com[0].value == PAIR_CO_MENTION_COUNT
    assert com[0].evidence_ids == (PAIR_ID,)


# --------------------------------------------------------------------------- #
# RECENT_PAPER per gene idlist PMID (Req 5.7)                                  #
# --------------------------------------------------------------------------- #
def test_recent_paper_claims_for_gene_idlist(full_store, associations) -> None:
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL, associations, ot_assoc_evidence_id=OT_ASSOC_ID, gene_evidence_id=GENE_ID
    )
    recent = [c for c in claims if c.claim_type is ClaimType.RECENT_PAPER]
    # One claim per PMID in the gene idlist, in idlist order, referencing gene id.
    assert [c.value for c in recent] == GENE_IDLIST
    assert all(c.evidence_ids == (GENE_ID,) for c in recent)
    assert all(c.subject == GENE_SYMBOL for c in recent)
    # Stable, deterministic claim ids per PMID.
    assert [c.claim_id for c in recent] == [
        f"RECENT_PAPER:{GENE_SYMBOL}:{pmid}" for pmid in GENE_IDLIST
    ]


# --------------------------------------------------------------------------- #
# Every claim carries >= 1 Evidence_ID (Req 5.2)                              #
# --------------------------------------------------------------------------- #
def test_every_claim_has_at_least_one_evidence_id(
    full_store, associations, pair_ids_by_disease
) -> None:
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease=pair_ids_by_disease,
    )
    assert claims  # non-empty
    for c in claims:
        assert isinstance(c, Claim)
        assert len(c.evidence_ids) >= 1
        # Every referenced id actually resolves in the store it was built from.
        assert all(full_store.contains(eid) for eid in c.evidence_ids)


# --------------------------------------------------------------------------- #
# Omission when the referenced record is absent (steering rule 6)             #
# --------------------------------------------------------------------------- #
def test_claims_omitted_when_ot_record_absent(full_store, associations) -> None:
    """No association/datatype claims when the OT record is not in the store."""
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id="ot_associations:absent0000000",  # not in store
        gene_evidence_id=GENE_ID,
    )
    assert not [c for c in claims if c.claim_type is ClaimType.ASSOCIATION]
    assert not [c for c in claims if c.claim_type is ClaimType.DATATYPE_BREAKDOWN]
    # Gene-scoped claims still built from the present gene record.
    assert [c for c in claims if c.claim_type is ClaimType.LITERATURE_COUNT]


def test_literature_count_omitted_when_gene_record_absent(
    full_store, associations
) -> None:
    """No LITERATURE_COUNT / RECENT_PAPER when the gene record is absent."""
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id="pubmed_gene:absent000000000",  # not in store
    )
    assert not [c for c in claims if c.claim_type is ClaimType.LITERATURE_COUNT]
    assert not [c for c in claims if c.claim_type is ClaimType.RECENT_PAPER]
    # Association claims still present from the OT record.
    assert [c for c in claims if c.claim_type is ClaimType.ASSOCIATION]


def test_co_mention_omitted_when_pair_record_absent(full_store, associations) -> None:
    """A pair wired to an absent record yields no CO_MENTION_COUNT claim."""
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease={NSCLC_DISEASE_ID: "pubmed_pair:absent00000000"},
    )
    assert not [c for c in claims if c.claim_type is ClaimType.CO_MENTION_COUNT]
