"""Property 2: No claim without evidence (pytest + Hypothesis).

**Validates: Requirements 5.2, 6.2**

The :class:`~claimledger.claims.ClaimBuilder` is the only place claims are
constructed, and it must never emit a claim that the evidence store cannot
stand behind. This property pins that invariant across an input space that
deliberately exercises *omission*: the wiring handed to ``build`` sometimes
names Evidence_IDs that are genuinely present in the store and sometimes names
ids that are **absent** (PubMed/OT unavailable, no record minted).

For every generated run the test asserts, for *every* built claim:

* it carries at least one Evidence_ID (a non-empty tuple) — "no claim without
  evidence" (Req 5.2, steering rule 1); and
* every Evidence_ID it references is present in the store
  (``store.contains(eid)``) — no built claim references a missing Evidence_ID,
  which is exactly the precondition the Verifier relies on (Req 6.2): a
  verifiable claim never points at evidence that is not there, because the
  builder *omits* rather than emitting a dangling claim.

It further asserts the omission direction explicitly: when a wiring id is absent
from the store, **no** claim in the output references that absent id. And as a
spot check that the omitted/produced split is value-honest, each produced
ASSOCIATION claim's value equals :func:`recompute_association` for its disease
(the single source of truth), never a fabricated number.
"""

from __future__ import annotations

import math
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.claims import ClaimBuilder
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import Association, Claim, ClaimType, DatatypeScore, Source
from claimledger.recompute import recompute_association
from tests.strategies import (
    ot_association_rows,
    ot_associations_payload,
    pubmed_esearch_payload,
)

# Stable, test-controlled Evidence_IDs. The PRESENT ids are added to the store;
# the ABSENT ids are never added, so wiring the builder with them exercises the
# omission path (steering rule 6, Req 6.2).
OT_PRESENT_ID = "ot_associations:present00000001"
OT_ABSENT_ID = "ot_associations:absent000000001"
GENE_PRESENT_ID = "pubmed_gene:present0000000001"
GENE_ABSENT_ID = "pubmed_gene:absent00000000001"
PAIR_PRESENT_ID = "pubmed_pair:present0000000001"
PAIR_ABSENT_ID = "pubmed_pair:absent00000000001"

GENE_SYMBOL = "SYNTH"


def _ot_record(rows: list[dict], evidence_id: str):
    """Build an ot_associations EvidenceRecord with a forced id, wrapping rows.

    Uses the same ``record_from_envelope`` the production clients use (so the
    builder reads the real ``data.target.associatedDiseases.rows[]`` path), then
    forces ``evidence_id`` to a test-controlled constant via ``replace`` so the
    wiring is deterministic — mirroring the existing unit tests in
    ``test_claims.py``.
    """
    envelope = {
        "source_url": "https://synthetic.test/graphql",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": ot_associations_payload(rows),
    }
    record = record_from_envelope(Source.OT_ASSOCIATIONS, "synthetic-ot-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _pubmed_record(payload: dict, source: Source, evidence_id: str):
    """Build a PubMed EvidenceRecord with a forced id, wrapping an esearch payload."""
    envelope = {
        "source_url": "https://synthetic.test/esearch.fcgi",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": payload,
    }
    record = record_from_envelope(source, "synthetic-pubmed-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _associations_from_rows(rows: list[dict], evidence_id: str) -> list[Association]:
    """Build Association models directly from the generated OT rows.

    The property concerns the builder + store, not OT selection/parsing, so the
    Association objects are built straight from the generated rows (one per row,
    all rows carried, disease ids unique by construction of the strategy). Each
    carries ``evidence_id`` purely as model provenance; the builder's wiring is
    driven by the explicit ``ot_assoc_evidence_id`` argument, not this field.
    """
    associations: list[Association] = []
    for row in rows:
        disease = row["disease"]
        associations.append(
            Association(
                disease_id=disease["id"],
                disease_name=disease["name"],
                overall_score=row["score"],
                datatype_scores=tuple(
                    DatatypeScore(id=e["id"], score=e["score"])
                    for e in row["datatypeScores"]
                ),
                evidence_id=evidence_id,
            )
        )
    return associations


@settings(max_examples=200)
@given(
    rows=ot_association_rows(),
    gene_payload=pubmed_esearch_payload(),
    pair_payload=pubmed_esearch_payload(),
    # Which of the three wirings point at a PRESENT (True) vs ABSENT (False) id.
    ot_present=st.booleans(),
    gene_present=st.booleans(),
    # Per-disease pair wirings: a subset of the diseases, each present or absent.
    pair_present_flags=st.lists(st.booleans(), min_size=0, max_size=8),
)
def test_no_claim_without_evidence(
    rows: list[dict],
    gene_payload: dict,
    pair_payload: dict,
    ot_present: bool,
    gene_present: bool,
    pair_present_flags: list[bool],
) -> None:
    """Every built claim has >= 1 Evidence_ID, all present in the store.

    Builds a store populated with a synthetic OT associations record, a gene
    PubMed record and a pair PubMed record (each under its PRESENT id), then
    wires ``build`` so each of the OT / gene / per-pair references points at
    either the PRESENT id or a never-added ABSENT id. Validates Requirements
    5.2, 6.2.
    """
    store = EvidenceStore()
    store.add(_ot_record(rows, OT_PRESENT_ID))
    store.add(_pubmed_record(gene_payload, Source.PUBMED_GENE, GENE_PRESENT_ID))
    store.add(_pubmed_record(pair_payload, Source.PUBMED_PAIR, PAIR_PRESENT_ID))

    ot_record = store.get(OT_PRESENT_ID)
    associations = _associations_from_rows(rows, OT_PRESENT_ID)

    # Wire OT + gene to present-or-absent depending on the drawn flags.
    ot_wiring = OT_PRESENT_ID if ot_present else OT_ABSENT_ID
    gene_wiring = GENE_PRESENT_ID if gene_present else GENE_ABSENT_ID

    # Wire a subset of the diseases to the pair record, each present or absent.
    disease_ids = [row["disease"]["id"] for row in rows]
    pair_wiring: dict[str, str] = {}
    for disease_id, present in zip(disease_ids, pair_present_flags):
        pair_wiring[disease_id] = PAIR_PRESENT_ID if present else PAIR_ABSENT_ID

    # The full set of ids the wiring could name, split into present / absent.
    absent_ids = {OT_ABSENT_ID, GENE_ABSENT_ID, PAIR_ABSENT_ID}

    claims = ClaimBuilder(store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=ot_wiring,
        gene_evidence_id=gene_wiring,
        pair_evidence_ids_by_disease=pair_wiring,
    )

    for claim in claims:
        assert isinstance(claim, Claim)

        # Req 5.2 — no claim without evidence: a non-empty tuple of ids.
        assert isinstance(claim.evidence_ids, tuple)
        assert len(claim.evidence_ids) >= 1

        # Req 6.2 — every referenced id is present in the store (no dangling
        # reference a verifier would have to reject for a missing id).
        for eid in claim.evidence_ids:
            assert store.contains(eid), (
                f"claim {claim.claim_id!r} references Evidence_ID {eid!r} "
                f"which is not present in the store"
            )

        # Omission direction: no produced claim references an absent wiring id.
        assert not (set(claim.evidence_ids) & absent_ids), (
            f"claim {claim.claim_id!r} references an absent id: "
            f"{set(claim.evidence_ids) & absent_ids}"
        )

    # Spot check (single source of truth): each produced ASSOCIATION claim's
    # value equals recompute_association for its disease — never fabricated.
    for claim in claims:
        if claim.claim_type is ClaimType.ASSOCIATION:
            disease_id = claim.subject.split("::", 1)[1]
            expected = recompute_association(ot_record, disease_id)
            assert isinstance(claim.value, float)
            assert math.isclose(claim.value, expected, abs_tol=1e-9)


@settings(max_examples=100)
@given(
    rows=ot_association_rows(),
    gene_payload=pubmed_esearch_payload(),
)
def test_absent_ids_are_omitted_entirely(
    rows: list[dict], gene_payload: dict
) -> None:
    """When every wiring id is absent from the store, no claims are produced.

    The hardest omission case: the store holds records, but the wiring points at
    ids that were never added. A correct builder emits nothing rather than
    dangling claims — so the output must be empty (Req 5.2, 6.2, steering
    rule 6).
    """
    store = EvidenceStore()
    # Populate the store with PRESENT records, but wire only ABSENT ids.
    store.add(_ot_record(rows, OT_PRESENT_ID))
    store.add(_pubmed_record(gene_payload, Source.PUBMED_GENE, GENE_PRESENT_ID))

    associations = _associations_from_rows(rows, OT_PRESENT_ID)
    disease_ids = [row["disease"]["id"] for row in rows]

    claims = ClaimBuilder(store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ABSENT_ID,
        gene_evidence_id=GENE_ABSENT_ID,
        pair_evidence_ids_by_disease={d: PAIR_ABSENT_ID for d in disease_ids},
    )

    assert claims == []
