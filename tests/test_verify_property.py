"""Property 1: Verifier soundness and completeness (pytest + Hypothesis).

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6**

The :class:`~claimledger.verify.Verifier` is the single gate every number passes
before it can display as *Verified*. Its contract is a **biconditional**: for a
claim built against a synthetic :class:`~claimledger.evidence.EvidenceStore`,
``Verifier().verify(claim, store)`` returns ``VERIFIED`` **iff** BOTH

* (a) every ``evidence_id`` the claim references is present in the store, AND
* (b) the claim's value equals the value recomputed from the referenced record
  under the per-type rule (scores within :data:`SCORE_ABS_TOLERANCE` = 1e-9,
  counts/PMIDs exact, datatype breakdown equal as an id-keyed mapping).

This module pins that biconditional by computing the *expected* verdict
**independently** of the verifier — a presence check plus a recompute-and-compare
that reuses the same ``recompute_*`` single source of truth and the **same
tolerance constant** the verifier imports (so the oracle and the implementation
cannot disagree merely because they picked different tolerances; the property is
"verify matches an independent presence+recompute check", not "verify matches a
re-implementation with a different tolerance").

The generators produce BOTH matching claims (value = the recomputed value) and
mismatching claims (value perturbed beyond tolerance, count off by >=1, PMID not
in the idlist, a datatype score bumped, a datatype id dropped) as well as claims
that reference a missing Evidence_ID. For every generated ``(claim, store)`` the
test asserts the verifier's verdict equals the independently-computed expected
verdict (soundness ∧ completeness), and that a value-mismatch rejection reports
*both* values while a missing-evidence rejection names the missing id.
"""

from __future__ import annotations

import math
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
    Source,
)
from claimledger.recompute import (
    recompute_association,
    recompute_co_mention_count,
    recompute_datatype_breakdown,
    recompute_literature_count,
    recompute_recent_paper,
)
from claimledger.verify import SCORE_ABS_TOLERANCE, Verifier
from tests.strategies import (
    ot_association_rows,
    ot_associations_payload,
    ot_scores,
    pmids,
    pubmed_esearch_payload,
)

# Test-controlled Evidence_IDs. The PRESENT ids are added to the store; the
# ABSENT id is never added, so a claim wired to it exercises the missing-evidence
# rejection path (Req 6.2).
OT_PRESENT_ID = "ot_associations:present00000001"
GENE_PRESENT_ID = "pubmed_gene:present0000000001"
PAIR_PRESENT_ID = "pubmed_pair:present0000000001"
ABSENT_ID = "ot_associations:absent000000001"

GENE_SYMBOL = "SYNTH"
SUBJECT_SEP = "::"


# --------------------------------------------------------------------------- #
# Record builders (same record_from_envelope the production path uses)        #
# --------------------------------------------------------------------------- #
def _ot_record(rows: list[dict], evidence_id: str):
    """Build an ot_associations record with a forced id, wrapping ``rows``."""
    envelope = {
        "source_url": "https://synthetic.test/graphql",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": ot_associations_payload(rows),
    }
    record = record_from_envelope(Source.OT_ASSOCIATIONS, "synthetic-ot-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _pubmed_record(payload: dict, source: Source, evidence_id: str):
    """Build a PubMed record with a forced id, wrapping an esearch ``payload``."""
    envelope = {
        "source_url": "https://synthetic.test/esearch.fcgi",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": payload,
    }
    record = record_from_envelope(source, "synthetic-pubmed-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _pair_subject(disease_id: str) -> str:
    return f"{GENE_SYMBOL}{SUBJECT_SEP}{disease_id}"


# --------------------------------------------------------------------------- #
# Independent oracle: presence + recompute-and-compare (NOT the verifier)     #
# --------------------------------------------------------------------------- #
def _recompute_for(claim: Claim, store: EvidenceStore):
    """Recompute the claim's value from its referenced record, or raise.

    Mirrors the verifier's dispatch but is written here independently so the
    oracle does not call into ``Verifier``. Raises ``LookupError``/``ValueError``
    exactly as the shared ``recompute_*`` functions do when the evidence cannot
    supply the value.
    """
    record = store.get(claim.evidence_ids[0])
    disease_id = claim.subject.partition(SUBJECT_SEP)[2] or claim.subject
    ct = claim.claim_type
    if ct is ClaimType.ASSOCIATION:
        return recompute_association(record, disease_id)
    if ct is ClaimType.DATATYPE_BREAKDOWN:
        return recompute_datatype_breakdown(record, disease_id)
    if ct is ClaimType.LITERATURE_COUNT:
        return recompute_literature_count(record)
    if ct is ClaimType.CO_MENTION_COUNT:
        return recompute_co_mention_count(record)
    if ct is ClaimType.RECENT_PAPER:
        return recompute_recent_paper(record, claim.value)
    raise ValueError(f"unknown claim type: {ct!r}")  # pragma: no cover


def _value_matches(claim_type: ClaimType, claim_value, recomputed) -> bool:
    """Independent per-type comparison (Req 6.5 / 6.6), reusing the 1e-9 const.

    Uses :data:`SCORE_ABS_TOLERANCE` imported from the verifier so the oracle and
    the implementation share the same tolerance (the property is parity with an
    independent presence+recompute check, not with a different tolerance).
    """
    if claim_type is ClaimType.ASSOCIATION:
        if isinstance(claim_value, bool) or not isinstance(claim_value, (int, float)):
            return False
        return abs(float(claim_value) - float(recomputed)) <= SCORE_ABS_TOLERANCE
    if claim_type is ClaimType.DATATYPE_BREAKDOWN:
        claimed_map = {ds.id: ds.score for ds in claim_value}
        recomputed_map = {ds.id: ds.score for ds in recomputed}
        # duplicate ids collapse in a dict; guard that the claimed side had none.
        if len(claimed_map) != len(tuple(claim_value)):
            return False
        if claimed_map.keys() != recomputed_map.keys():
            return False
        return all(
            abs(float(claimed_map[k]) - float(recomputed_map[k])) <= SCORE_ABS_TOLERANCE
            for k in claimed_map
        )
    # LITERATURE_COUNT / CO_MENTION_COUNT / RECENT_PAPER: exact equality.
    return claim_value == recomputed


def _expected_verdict(claim: Claim, store: EvidenceStore) -> ClaimStatus:
    """Independently compute the verdict: presence (a) AND value-match (b).

    Returns ``VERIFIED`` iff every referenced id is present AND the value matches
    the recompute under the per-type rule; otherwise ``UNSUPPORTED``. This is the
    biconditional oracle the verifier is asserted to agree with.
    """
    if not claim.evidence_ids:
        return ClaimStatus.UNSUPPORTED
    if not all(store.contains(eid) for eid in claim.evidence_ids):
        return ClaimStatus.UNSUPPORTED
    try:
        recomputed = _recompute_for(claim, store)
    except (LookupError, ValueError):
        # Evidence cannot supply the value -> cannot match -> UNSUPPORTED.
        return ClaimStatus.UNSUPPORTED
    return (
        ClaimStatus.VERIFIED
        if _value_matches(claim.claim_type, claim.value, recomputed)
        else ClaimStatus.UNSUPPORTED
    )


def _assert_agrees(claim: Claim, store: EvidenceStore) -> None:
    """Assert verify agrees with the independent oracle for ``(claim, store)``."""
    expected = _expected_verdict(claim, store)
    result = Verifier().verify(claim, store)
    assert result.status is expected, (
        f"verdict mismatch for {claim.claim_type} value={claim.value!r}: "
        f"verify={result.status} expected={expected} reason={result.reason}"
    )


# --------------------------------------------------------------------------- #
# A store with one OT assoc record + gene/pair PubMed records.                #
# --------------------------------------------------------------------------- #
@st.composite
def _store_and_rows(draw):
    """Draw a populated store plus the OT rows / PubMed payloads behind it."""
    rows = draw(ot_association_rows())
    gene_payload = draw(pubmed_esearch_payload())
    pair_payload = draw(pubmed_esearch_payload())
    store = EvidenceStore()
    store.add(_ot_record(rows, OT_PRESENT_ID))
    store.add(_pubmed_record(gene_payload, Source.PUBMED_GENE, GENE_PRESENT_ID))
    store.add(_pubmed_record(pair_payload, Source.PUBMED_PAIR, PAIR_PRESENT_ID))
    return store, rows, gene_payload, pair_payload


# --------------------------------------------------------------------------- #
# Biconditional over a mixed generator of matching + mismatching claims.      #
# --------------------------------------------------------------------------- #
@st.composite
def _claim_and_store(draw):
    """Draw a ``(claim, store)`` that is deliberately matching OR mismatching.

    A claim type is chosen, a well-formed MATCHING claim is built from the store
    (value = the recomputed value, ids present), then one of several mutations is
    optionally applied to push it into the mismatching region:

    * value perturbed beyond tolerance (score), off-by->=1 (count), not-in-idlist
      (PMID), a datatype score bumped, or a datatype id dropped;
    * the evidence id swapped for an ABSENT id (missing-evidence case).

    Returning both regions from one generator lets the single biconditional
    assertion cover soundness (VERIFIED => a ∧ b) and completeness (a ∧ b =>
    VERIFIED) together.
    """
    store, rows, gene_payload, _pair_payload = draw(_store_and_rows())
    disease_ids = [row["disease"]["id"] for row in rows]
    disease_id = draw(st.sampled_from(disease_ids))
    record = store.get(OT_PRESENT_ID)

    claim_type = draw(
        st.sampled_from(
            [
                ClaimType.ASSOCIATION,
                ClaimType.DATATYPE_BREAKDOWN,
                ClaimType.LITERATURE_COUNT,
                ClaimType.CO_MENTION_COUNT,
                ClaimType.RECENT_PAPER,
            ]
        )
    )

    # Build the MATCHING claim from the single source of truth.
    if claim_type is ClaimType.ASSOCIATION:
        subject = _pair_subject(disease_id)
        value = recompute_association(record, disease_id)
        eid = OT_PRESENT_ID
    elif claim_type is ClaimType.DATATYPE_BREAKDOWN:
        subject = _pair_subject(disease_id)
        value = recompute_datatype_breakdown(record, disease_id)
        eid = OT_PRESENT_ID
    elif claim_type is ClaimType.LITERATURE_COUNT:
        subject = GENE_SYMBOL
        value = recompute_literature_count(store.get(GENE_PRESENT_ID))
        eid = GENE_PRESENT_ID
    elif claim_type is ClaimType.CO_MENTION_COUNT:
        subject = _pair_subject(disease_id)
        value = recompute_co_mention_count(store.get(PAIR_PRESENT_ID))
        eid = PAIR_PRESENT_ID
    else:  # RECENT_PAPER
        subject = GENE_SYMBOL
        idlist = gene_payload["esearchresult"]["idlist"]
        # Only a PMID genuinely in the idlist is a matching claim; if the drawn
        # payload has no PMIDs, fall back to a count claim so we always have a
        # well-defined matching base (the empty-idlist RECENT_PAPER mutation is
        # covered via the mismatch branch below when PMIDs do exist).
        if not idlist:
            subject = GENE_SYMBOL
            value = recompute_literature_count(store.get(GENE_PRESENT_ID))
            eid = GENE_PRESENT_ID
            claim_type = ClaimType.LITERATURE_COUNT
        else:
            value = draw(st.sampled_from(idlist))
            eid = GENE_PRESENT_ID

    claim = Claim(
        claim_id=f"{claim_type.value}:{subject}",
        claim_type=claim_type,
        subject=subject,
        value=value,
        evidence_ids=(eid,),
    )

    # Pick a mutation (or keep it matching).
    mutation = draw(
        st.sampled_from(["none", "value", "missing_evidence"])
    )
    if mutation == "missing_evidence":
        return replace(claim, evidence_ids=(ABSENT_ID,)), store
    if mutation == "value":
        claim = _perturb_value(draw, claim)
    return claim, store


def _perturb_value(draw, claim: Claim) -> Claim:
    """Push a matching claim's value into the mismatching region, per type."""
    ct = claim.claim_type
    if ct is ClaimType.ASSOCIATION:
        # Clearly beyond tolerance: >= 1e-6 (never a flaky near-boundary delta).
        delta = draw(st.floats(min_value=1e-6, max_value=1.0, allow_nan=False))
        return replace(claim, value=float(claim.value) + delta)
    if ct is ClaimType.DATATYPE_BREAKDOWN:
        breakdown = tuple(claim.value)
        if not breakdown:
            # No datatypes to bump/drop -> perturb into a non-breakdown shape so
            # the id-set differs (empty vs a single bogus id).
            bogus = (DatatypeScore(id="__bogus__", score=0.5),)
            return replace(claim, value=bogus)
        how = draw(st.sampled_from(["bump", "drop"]))
        if how == "bump":
            first = breakdown[0]
            bumped = DatatypeScore(id=first.id, score=first.score + 0.5)
            return replace(claim, value=(bumped,) + breakdown[1:])
        # drop one id so the id-sets differ
        return replace(claim, value=breakdown[1:])
    if ct in (ClaimType.LITERATURE_COUNT, ClaimType.CO_MENTION_COUNT):
        delta = draw(st.integers(min_value=1, max_value=1000))
        sign = draw(st.sampled_from([1, -1]))
        new = int(claim.value) + sign * delta
        if new < 0:
            new = int(claim.value) + delta  # keep non-negative but still off
        return replace(claim, value=new)
    # RECENT_PAPER: a PMID not in the idlist. Prefix a non-digit marker so it can
    # never collide with a genuine idlist PMID.
    return replace(claim, value="X" + draw(pmids))


@settings(max_examples=300)
@given(data=_claim_and_store())
def test_verifier_soundness_and_completeness(data) -> None:
    """verify's verdict equals the independent presence+recompute oracle.

    Covers BOTH directions of the biconditional at once (Req 6.1-6.6):

    * **Soundness** — if verify says VERIFIED, the oracle (which it must equal)
      required every id present AND value == recompute under the rule.
    * **Completeness** — if the oracle says VERIFIED (all present, value matches)
      then verify must also say VERIFIED.
    """
    claim, store = data
    _assert_agrees(claim, store)


# --------------------------------------------------------------------------- #
# Explicit soundness: VERIFIED => presence ∧ value-matches-recompute.         #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=_claim_and_store())
def test_verified_implies_present_and_recompute_match(data) -> None:
    """If verify returns VERIFIED, both biconditional conjuncts hold (Req 6.1-6.6)."""
    claim, store = data
    result = Verifier().verify(claim, store)
    if result.status is ClaimStatus.VERIFIED:
        # (a) every evidence id present.
        assert claim.evidence_ids
        assert all(store.contains(eid) for eid in claim.evidence_ids)
        # (b) value matches the independent recompute under the per-type rule.
        recomputed = _recompute_for(claim, store)
        assert _value_matches(claim.claim_type, claim.value, recomputed)


# --------------------------------------------------------------------------- #
# Explicit completeness: present ∧ value-matches-recompute => VERIFIED.        #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(st_rows=_store_and_rows(), which=st.data())
def test_present_and_match_implies_verified(st_rows, which) -> None:
    """A claim built straight from the store (value = recompute) is VERIFIED.

    Constructs only MATCHING claims (every id present, value = the recomputed
    value) and asserts the verifier accepts every one — the completeness half of
    the biconditional (Req 6.4).
    """
    store, rows, gene_payload, _pair_payload = st_rows
    record = store.get(OT_PRESENT_ID)
    disease_id = which.draw(st.sampled_from([row["disease"]["id"] for row in rows]))

    matching: list[Claim] = [
        Claim(
            claim_id=f"ASSOCIATION:{_pair_subject(disease_id)}",
            claim_type=ClaimType.ASSOCIATION,
            subject=_pair_subject(disease_id),
            value=recompute_association(record, disease_id),
            evidence_ids=(OT_PRESENT_ID,),
        ),
        Claim(
            claim_id=f"DATATYPE_BREAKDOWN:{_pair_subject(disease_id)}",
            claim_type=ClaimType.DATATYPE_BREAKDOWN,
            subject=_pair_subject(disease_id),
            value=recompute_datatype_breakdown(record, disease_id),
            evidence_ids=(OT_PRESENT_ID,),
        ),
        Claim(
            claim_id=f"LITERATURE_COUNT:{GENE_SYMBOL}",
            claim_type=ClaimType.LITERATURE_COUNT,
            subject=GENE_SYMBOL,
            value=recompute_literature_count(store.get(GENE_PRESENT_ID)),
            evidence_ids=(GENE_PRESENT_ID,),
        ),
        Claim(
            claim_id=f"CO_MENTION_COUNT:{_pair_subject(disease_id)}",
            claim_type=ClaimType.CO_MENTION_COUNT,
            subject=_pair_subject(disease_id),
            value=recompute_co_mention_count(store.get(PAIR_PRESENT_ID)),
            evidence_ids=(PAIR_PRESENT_ID,),
        ),
    ]
    idlist = gene_payload["esearchresult"]["idlist"]
    if idlist:
        pmid = which.draw(st.sampled_from(idlist))
        matching.append(
            Claim(
                claim_id=f"RECENT_PAPER:{GENE_SYMBOL}:{pmid}",
                claim_type=ClaimType.RECENT_PAPER,
                subject=GENE_SYMBOL,
                value=pmid,
                evidence_ids=(GENE_PRESENT_ID,),
            )
        )

    verifier = Verifier()
    for claim in matching:
        result = verifier.verify(claim, store)
        assert result.status is ClaimStatus.VERIFIED, (
            f"matching {claim.claim_type} unexpectedly {result.status}: "
            f"{result.reason}"
        )
        assert result.reason is None
        # Verified also carries the recomputed value for the ledger.
        assert result.claim_value == claim.value


# --------------------------------------------------------------------------- #
# ASSOCIATION tolerance boundary (Req 6.5).                                    #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(
    base=ot_scores,
    within=st.floats(min_value=0.0, max_value=1e-10, allow_nan=False),
    outside=st.floats(min_value=1e-6, max_value=0.5, allow_nan=False),
    sign=st.sampled_from([1.0, -1.0]),
)
def test_association_tolerance_boundary(base, within, outside, sign) -> None:
    """A <= 1e-10 perturbation is VERIFIED; a >= 1e-6 one is UNSUPPORTED (Req 6.5).

    The store's single row carries ``score = base``; a claim whose value is
    ``base + sign*within`` (within tolerance) must verify, while ``base +
    sign*outside`` (clearly outside) must be rejected. Deltas are kept firmly on
    each side of 1e-9 (within <= 1e-10, outside >= 1e-6) so the test is robust to
    float rounding and never flaky at the boundary.
    """
    disease_id = "SYNTH_DISEASE"
    rows = [
        {
            "score": float(base),
            "datatypeScores": [{"id": "clinical", "score": 0.5}],
            "disease": {"id": disease_id, "name": "synthetic"},
        }
    ]
    store = EvidenceStore()
    store.add(_ot_record(rows, OT_PRESENT_ID))
    record = store.get(OT_PRESENT_ID)
    recomputed = recompute_association(record, disease_id)
    subject = _pair_subject(disease_id)
    verifier = Verifier()

    within_claim = Claim(
        claim_id=f"ASSOCIATION:{subject}",
        claim_type=ClaimType.ASSOCIATION,
        subject=subject,
        value=recomputed + sign * within,
        evidence_ids=(OT_PRESENT_ID,),
    )
    # Guard: the within perturbation genuinely stays within tolerance (float
    # math can collapse base+tiny back to base, which is still within).
    assert abs(within_claim.value - recomputed) <= SCORE_ABS_TOLERANCE
    assert verifier.verify(within_claim, store).status is ClaimStatus.VERIFIED

    outside_claim = replace(within_claim, value=recomputed + sign * outside)
    assert abs(outside_claim.value - recomputed) > SCORE_ABS_TOLERANCE
    result = verifier.verify(outside_claim, store)
    assert result.status is ClaimStatus.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Rejection reason: value-mismatch reports BOTH values (Req 6.3, 7.4).         #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(
    base=ot_scores,
    delta=st.floats(min_value=1e-6, max_value=0.5, allow_nan=False),
)
def test_value_mismatch_reason_reports_both_values(base, delta) -> None:
    """A score mismatch rejection names both the claim and recomputed value."""
    disease_id = "SYNTH_DISEASE"
    rows = [
        {
            "score": float(base),
            "datatypeScores": [{"id": "clinical", "score": 0.5}],
            "disease": {"id": disease_id, "name": "synthetic"},
        }
    ]
    store = EvidenceStore()
    store.add(_ot_record(rows, OT_PRESENT_ID))
    recomputed = recompute_association(store.get(OT_PRESENT_ID), disease_id)
    subject = _pair_subject(disease_id)
    tampered_value = recomputed + delta

    claim = Claim(
        claim_id=f"ASSOCIATION:{subject}",
        claim_type=ClaimType.ASSOCIATION,
        subject=subject,
        value=tampered_value,
        evidence_ids=(OT_PRESENT_ID,),
    )
    result = Verifier().verify(claim, store)
    assert result.status is ClaimStatus.UNSUPPORTED
    # Both the claim value and the recomputed value appear in the reason.
    assert repr(tampered_value) in result.reason
    assert repr(recomputed) in result.reason
    # And the result object carries both sides.
    assert result.claim_value == tampered_value
    assert math.isclose(result.recomputed_value, recomputed, abs_tol=1e-12)


# --------------------------------------------------------------------------- #
# Rejection reason: missing-evidence names the missing id (Req 6.2).           #
# --------------------------------------------------------------------------- #
@settings(max_examples=100)
@given(data=_store_and_rows())
def test_missing_evidence_reason_names_the_id(data) -> None:
    """A claim wired to an absent id is UNSUPPORTED and the reason names it."""
    store, rows, _gene_payload, _pair_payload = data
    disease_id = rows[0]["disease"]["id"]
    subject = _pair_subject(disease_id)
    value = recompute_association(store.get(OT_PRESENT_ID), disease_id)

    claim = Claim(
        claim_id=f"ASSOCIATION:{subject}",
        claim_type=ClaimType.ASSOCIATION,
        subject=subject,
        value=value,
        evidence_ids=(ABSENT_ID,),
    )
    result = Verifier().verify(claim, store)
    assert result.status is ClaimStatus.UNSUPPORTED
    assert ABSENT_ID in result.reason
    assert result.recomputed_value is None
