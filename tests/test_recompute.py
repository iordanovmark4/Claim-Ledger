"""Unit tests for the shared recompute_* functions (Req 4.5, 6.1).

These exercise the single-source-of-truth recompute functions against the
committed *real* cache envelopes (steering rule 8), turning each envelope into
an :class:`~claimledger.models.EvidenceRecord` with the same
:func:`~claimledger.evidence.record_from_envelope` the clients use. The values
asserted below (NSCLC overall score, its datatype breakdown, the gene / pair
counts, the real PMIDs) were read from the committed captures, not assumed.

The failure tests pin the design intent that a recompute *failure is a precise
exception*, which the verifier reads as a mismatch/rejection: a disease id or
PMID not actually in the evidence must raise rather than return a fabricated
value (Req 6.3; steering rule 3).
"""

from __future__ import annotations

import math

import pytest

from claimledger.evidence import record_from_envelope
from claimledger.models import DatatypeScore, EvidenceRecord, Source
from claimledger.recompute import (
    recompute_association,
    recompute_co_mention_count,
    recompute_datatype_breakdown,
    recompute_literature_count,
    recompute_recent_paper,
)

# The NSCLC association in the real OT capture (preflight_ot_associations.json).
NSCLC_DISEASE_ID = "MONDO_0005233"
NSCLC_OVERALL_SCORE = 0.8503239816603523

# The NSCLC row's six datatype scores, read from the real capture, keyed by id.
NSCLC_DATATYPES = {
    "clinical": 0.9959475193895682,
    "genetic_literature": 0.607930797611621,
    "affected_pathway": 0.8209966976180988,
    "literature": 0.9996584498625893,
    "genetic_association": 0.6991204172533643,
    "somatic_mutation": 0.8315878016229145,
}


# --------------------------------------------------------------------------- #
# EvidenceRecord fixtures built from the committed real envelopes             #
# --------------------------------------------------------------------------- #
@pytest.fixture
def ot_assoc_record(ot_associations_envelope) -> EvidenceRecord:
    """An ot_associations EvidenceRecord built from the real envelope."""
    return record_from_envelope(
        Source.OT_ASSOCIATIONS,
        "test-assoc-key",
        ot_associations_envelope,
    )


@pytest.fixture
def pubmed_gene_record(pubmed_gene_envelope) -> EvidenceRecord:
    """A gene-only PubMed EvidenceRecord built from the real envelope."""
    return record_from_envelope(
        Source.PUBMED_GENE,
        "test-gene-key",
        pubmed_gene_envelope,
    )


@pytest.fixture
def pubmed_pair_record(pubmed_gene_and_disease_envelope) -> EvidenceRecord:
    """A gene+disease PubMed EvidenceRecord built from the real envelope."""
    return record_from_envelope(
        Source.PUBMED_PAIR,
        "test-pair-key",
        pubmed_gene_and_disease_envelope,
    )


# --------------------------------------------------------------------------- #
# recompute_association                                                       #
# --------------------------------------------------------------------------- #
def test_recompute_association_nsclc_score(ot_assoc_record: EvidenceRecord) -> None:
    """The NSCLC row recomputes to its real Overall_Score within 1e-9."""
    score = recompute_association(ot_assoc_record, NSCLC_DISEASE_ID)
    assert isinstance(score, float)
    assert math.isclose(score, NSCLC_OVERALL_SCORE, abs_tol=1e-9)


def test_recompute_association_missing_disease_raises(
    ot_assoc_record: EvidenceRecord,
) -> None:
    """A disease_id not present in the rows is a recompute failure, not a guess."""
    with pytest.raises(LookupError):
        recompute_association(ot_assoc_record, "MONDO_9999999")


# --------------------------------------------------------------------------- #
# recompute_datatype_breakdown                                                #
# --------------------------------------------------------------------------- #
def test_recompute_datatype_breakdown_sorted_by_id(
    ot_assoc_record: EvidenceRecord,
) -> None:
    """The breakdown is parsed by id, returned sorted by id, matching reality."""
    breakdown = recompute_datatype_breakdown(ot_assoc_record, NSCLC_DISEASE_ID)

    assert isinstance(breakdown, tuple)
    assert all(isinstance(ds, DatatypeScore) for ds in breakdown)

    ids = [ds.id for ds in breakdown]
    # Canonical/deterministic ordering: ascending by datatype id.
    assert ids == sorted(ids)
    # Every real datatype is present exactly once.
    assert set(ids) == set(NSCLC_DATATYPES)
    assert len(breakdown) == len(NSCLC_DATATYPES)
    # Each score matches the real value keyed by id within 1e-9.
    for ds in breakdown:
        assert math.isclose(ds.score, NSCLC_DATATYPES[ds.id], abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# recompute_literature_count / recompute_co_mention_count                     #
# --------------------------------------------------------------------------- #
def test_recompute_literature_count(pubmed_gene_record: EvidenceRecord) -> None:
    """The gene-only string count converts to the real int 108097."""
    assert recompute_literature_count(pubmed_gene_record) == 108097


def test_recompute_co_mention_count(pubmed_pair_record: EvidenceRecord) -> None:
    """The gene+disease string count converts to the real int 18950."""
    assert recompute_co_mention_count(pubmed_pair_record) == 18950


def test_recompute_literature_count_missing_raises() -> None:
    """A missing/unparseable count raises rather than fabricating a number."""
    record = EvidenceRecord(
        evidence_id="pubmed_gene:deadbeefdeadbeef",
        source=Source.PUBMED_GENE,
        url="https://example.test/esearch",
        retrieved_at="2024-01-01T00:00:00Z",
        payload={"esearchresult": {"idlist": []}},
    )
    with pytest.raises(ValueError):
        recompute_literature_count(record)


# --------------------------------------------------------------------------- #
# recompute_recent_paper                                                      #
# --------------------------------------------------------------------------- #
def test_recompute_recent_paper_present(pubmed_gene_record: EvidenceRecord) -> None:
    """A PMID genuinely in the idlist recomputes back to itself."""
    present_pmid = pubmed_gene_record.payload["esearchresult"]["idlist"][0]
    assert recompute_recent_paper(pubmed_gene_record, present_pmid) == present_pmid


def test_recompute_recent_paper_absent_raises(
    pubmed_gene_record: EvidenceRecord,
) -> None:
    """A PMID not in the idlist is a recompute failure (rejection), not a repair."""
    with pytest.raises(LookupError):
        recompute_recent_paper(pubmed_gene_record, "00000000")
