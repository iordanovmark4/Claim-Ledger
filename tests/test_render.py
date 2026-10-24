"""Unit tests for DossierRenderer (Req 7.2, 8.1-8.5, 10.1, 10.2, 10.5).

These build real Verified_Claims end-to-end: an :class:`EvidenceStore` from the
committed real EGFR cache envelopes (steering rule 8), the five claim types via
:class:`ClaimBuilder`, and the verdicts via :func:`verify_all`. The renderer is
then exercised against those genuine verified claims — no hand-mocked values.

Coverage:

* determinism — same input and every permutation yield a byte-identical string
  (Req 8.2, 8.3);
* Unsupported claims never contribute to prose (Req 7.2, 8.1);
* approved wording present ("associated with" / "reported in") and the forbidden
  terms absent (Req 10.1, 10.2);
* an Overall_Score is labeled an evidence-strength score, never a probability
  (Req 8.5, 10.5);
* the datatype breakdown is rendered sorted by id (Req 8.3);
* the render-time wording guard raises on genuinely forbidden generated content
  (Req 10.2).
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import parse_associations
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import Claim, ClaimStatus, ClaimType, Source
from claimledger.render import (
    FORBIDDEN_TERMS,
    DossierRenderer,
    ForbiddenWordingError,
)
from claimledger.verify import Verifier, verify_all

GENE_SYMBOL = "EGFR"
OT_ASSOC_ID = "ot_associations:testassoc000001"
GENE_ID = "pubmed_gene:testgene00000001"
PAIR_ID = "pubmed_pair:testpair00000001"

NSCLC_DISEASE_ID = "MONDO_0005233"
NSCLC_DISEASE_NAME = "non-small cell lung carcinoma"


# --------------------------------------------------------------------------- #
# Fixtures: a store of real EGFR evidence and genuine verified claims         #
# --------------------------------------------------------------------------- #
def _with_id(record, evidence_id: str):
    return replace(record, evidence_id=evidence_id)


@pytest.fixture
def full_store(
    ot_associations_envelope,
    pubmed_gene_envelope,
    pubmed_gene_and_disease_envelope,
) -> EvidenceStore:
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
    return parse_associations(
        ot_associations_envelope["payload"], 5, OT_ASSOC_ID
    ).associations


@pytest.fixture
def disease_names(associations):
    """Real disease_id -> name map from the parsed associations."""
    return {a.disease_id: a.disease_name for a in associations}


@pytest.fixture
def verified_claims(full_store, associations):
    """Genuine Verified_Claims for EGFR built and verified against real evidence."""
    claims = ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease={NSCLC_DISEASE_ID: PAIR_ID},
    )
    verdicts = verify_all(Verifier(), claims, full_store)
    assert verdicts.verified, "expected real EGFR evidence to produce verified claims"
    return verdicts.verified


# --------------------------------------------------------------------------- #
# Determinism (Req 8.2, 8.3)                                                   #
# --------------------------------------------------------------------------- #
def test_same_input_is_byte_identical(verified_claims, disease_names) -> None:
    renderer = DossierRenderer()
    a = renderer.render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    b = renderer.render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    assert a == b


def test_permuted_input_is_byte_identical(verified_claims, disease_names) -> None:
    renderer = DossierRenderer()
    baseline = renderer.render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    rng = random.Random(1234)
    for _ in range(10):
        shuffled = list(verified_claims)
        rng.shuffle(shuffled)
        out = renderer.render(
            shuffled, gene_symbol=GENE_SYMBOL, disease_names=disease_names
        )
        assert out == baseline


# --------------------------------------------------------------------------- #
# Unsupported claims excluded from prose (Req 7.2, 8.1)                        #
# --------------------------------------------------------------------------- #
def test_unsupported_claims_excluded_from_prose(
    verified_claims, disease_names
) -> None:
    renderer = DossierRenderer()
    baseline = renderer.render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )

    # Craft an Unsupported claim carrying an obvious sentinel value and feed it
    # in alongside the verified ones; it must not appear anywhere in the prose.
    sentinel = "ZZZ_UNSUPPORTED_SENTINEL_PMID"
    bad = Claim(
        claim_id="RECENT_PAPER:EGFR:unsupported",
        claim_type=ClaimType.RECENT_PAPER,
        subject=GENE_SYMBOL,
        value=sentinel,
        evidence_ids=(GENE_ID,),
        status=ClaimStatus.UNSUPPORTED,
        reason="tampered for the test",
    )
    with_bad = list(verified_claims) + [bad]
    out = renderer.render(
        with_bad, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    assert sentinel not in out
    # The verified-only output is unchanged by the presence of an Unsupported claim.
    assert out == baseline


# --------------------------------------------------------------------------- #
# Approved wording and forbidden-term absence (Req 10.1, 10.2)                 #
# --------------------------------------------------------------------------- #
def test_uses_approved_wording(verified_claims, disease_names) -> None:
    out = DossierRenderer().render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    assert "associated with" in out or "reported in" in out


def test_no_forbidden_terms_in_output(verified_claims, disease_names) -> None:
    out = DossierRenderer().render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    ).lower()
    for term in FORBIDDEN_TERMS:
        assert term not in out


# --------------------------------------------------------------------------- #
# Score labeled evidence-strength, never a probability (Req 8.5, 10.5)         #
# --------------------------------------------------------------------------- #
def test_score_labeled_evidence_strength_not_probability(
    verified_claims, disease_names
) -> None:
    out = DossierRenderer().render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    assert "evidence-strength score" in out
    lowered = out.lower()
    assert "probability" not in lowered
    assert "likelihood" not in lowered
    # A score is never presented as a percentage likelihood.
    assert "% likely" not in lowered


# --------------------------------------------------------------------------- #
# Datatype breakdown rendered sorted by id (Req 8.3)                           #
# --------------------------------------------------------------------------- #
def test_datatype_breakdown_rendered_sorted_by_id(
    verified_claims, disease_names
) -> None:
    out = DossierRenderer().render(
        verified_claims, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    # Collect the datatype ids from the breakdown of the top disease (NSCLC).
    breakdown = next(
        c
        for c in verified_claims
        if c.claim_type is ClaimType.DATATYPE_BREAKDOWN
        and c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )
    ids = [ds.id for ds in breakdown.value]
    assert len(ids) >= 2  # the real NSCLC row has several datatypes

    # Each id appears as a bullet; the order they appear in the text is sorted.
    positions = [out.index(f"- {ds_id}: ") for ds_id in sorted(ids)]
    assert positions == sorted(positions)
    # And that matches rendering ids in ascending order, not the tuple order.
    assert sorted(ids) == [i for _, i in sorted(zip(positions, sorted(ids)))]


# --------------------------------------------------------------------------- #
# Render-time wording guard raises on genuinely forbidden content (Req 10.2)  #
# --------------------------------------------------------------------------- #
def test_wording_guard_raises_on_forbidden_disease_name(full_store) -> None:
    """A disease named with a standalone forbidden word trips the guard.

    This is honest: the forbidden word becomes a standalone word in OUR prose
    ("EGFR is associated with proves ..."), which is exactly what the guard must
    reject — not a contrived injection into the template.
    """
    # Build an association claim whose disease name is a forbidden standalone word.
    from claimledger.clients.opentargets import parse_associations

    # A disease id present in the real associations, renamed for display.
    assoc = parse_associations(
        full_store.get(OT_ASSOC_ID).payload, 5, OT_ASSOC_ID
    ).associations
    disease_id = assoc[0].disease_id

    claim = Claim(
        claim_id=f"ASSOCIATION:{GENE_SYMBOL}::{disease_id}",
        claim_type=ClaimType.ASSOCIATION,
        subject=f"{GENE_SYMBOL}::{disease_id}",
        value=0.5,
        evidence_ids=(OT_ASSOC_ID,),
        status=ClaimStatus.VERIFIED,
    )
    with pytest.raises(ForbiddenWordingError):
        DossierRenderer().render(
            [claim],
            gene_symbol=GENE_SYMBOL,
            # Display name is a bare forbidden word -> standalone word in prose.
            disease_names={disease_id: "proves"},
        )


def test_wording_guard_allows_forbidden_substring_inside_a_word(
    full_store,
) -> None:
    """A forbidden term embedded in a longer token must NOT trip the guard.

    The guard matches whole words only, so a disease name that merely *contains*
    the letters of a forbidden term (e.g. "provesicular") renders fine.
    """
    from claimledger.clients.opentargets import parse_associations

    assoc = parse_associations(
        full_store.get(OT_ASSOC_ID).payload, 5, OT_ASSOC_ID
    ).associations
    disease_id = assoc[0].disease_id

    claim = Claim(
        claim_id=f"ASSOCIATION:{GENE_SYMBOL}::{disease_id}",
        claim_type=ClaimType.ASSOCIATION,
        subject=f"{GENE_SYMBOL}::{disease_id}",
        value=0.5,
        evidence_ids=(OT_ASSOC_ID,),
        status=ClaimStatus.VERIFIED,
    )
    out = DossierRenderer().render(
        [claim],
        gene_symbol=GENE_SYMBOL,
        disease_names={disease_id: "provesicular syndrome"},
    )
    assert "provesicular syndrome" in out
