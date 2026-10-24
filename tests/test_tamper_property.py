"""Property 9: Tamper rejection and reset (pytest + Hypothesis).

**Validates: Requirements 12.7, 13.1, 13.2, 13.3, 13.4**

The Tamper_Control (:mod:`claimledger.tamper`) exists to *demonstrate* that the
:class:`~claimledger.verify.Verifier` genuinely re-checks displayed numbers
against saved evidence: deliberately altering one claim value to a genuinely
different value must make verification reject it (``Unsupported``), restoring
the original must make it accept again (``Verified``), and the underlying
evidence / cached response bytes must never change in the process (Req 13.4).

This module pins that round-trip across all five claim types over a store of
synthetic, *genuinely verifiable* claims (each value is ``recompute_*`` from the
store, so the baseline verdict is VERIFIED by construction). For each generated
``(store, claim, tamper_value)`` the main property asserts:

1. baseline verifies VERIFIED (the claim's value really is recomputed from the
   store);
2. ``tamper(claim, tamper_value, store)`` yields ``UNSUPPORTED`` for a tamper
   value that is genuinely DIFFERENT under the claim type's comparison rule,
   with a reason reporting both values (or a not-in-idlist recompute failure for
   RECENT_PAPER), ``tampered.value == tamper_value`` and
   ``tampered.original_value == the genuine original`` (Req 13.1, 13.2);
3. ``reset(tampered, store)`` yields ``VERIFIED`` with ``value == the genuine
   original`` and ``original_value is None`` (Req 13.3);
4. the referenced :class:`~claimledger.models.EvidenceRecord` is the same object
   and its payload bytes are byte-identical before tamper, after tamper, and
   after reset (Req 13.4);
5. (secondary) a tamper value EQUAL to the true value (within the per-type
   tolerance) leaves the claim VERIFIED — tamper does not *force* a rejection,
   it re-verifies.

A DOUBLE-tamper-then-reset example pins Req 13.3's subtlety: reset after two
tampers restores the TRUE original, never the first tamper's value.

Tamper-value construction (genuinely-different under the per-type rule)
-----------------------------------------------------------------------
* ASSOCIATION (score float, 1e-9 tolerance): if the true score ``s <= 0.5`` use
  ``s + 0.5`` else ``s - 0.5``. The result stays in ``[0, 1]`` and differs from
  ``s`` by exactly 0.5 — far beyond the 1e-9 tolerance — regardless of where in
  ``[0, 1]`` the true score sits (so a boundary score 0.0/1.0 is handled).
* DATATYPE_BREAKDOWN (id-keyed mapping): if the breakdown is non-empty, bump one
  entry's score the same +/-0.5 way (a clear >1e-9 change on a shared id);
  if it is empty, *add* a new id — in both cases the id-keyed mapping differs.
* LITERATURE_COUNT / CO_MENTION_COUNT (int, exact): ``true + 1 + k`` for a drawn
  ``k >= 0`` — always a different non-negative int.
* RECENT_PAPER (PMID, exact / idlist membership): a sentinel PMID prefixed with
  a non-digit ("X"), which the digit-only PMID generator can never produce, so
  it is guaranteed absent from the record's idlist (recompute raises and the
  verdict is UNSUPPORTED).
"""

from __future__ import annotations

import copy
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
)
from claimledger.tamper import reset, tamper
from claimledger.verify import Verifier
from tests.strategies import (
    ot_association_rows,
    ot_associations_payload,
    pubmed_esearch_payload,
)

# Stable, test-controlled Evidence_IDs forced onto the synthetic records (same
# record_from_envelope + replace pattern as test_claims_property.py), so the
# claim wiring is deterministic.
OT_ID = "ot_associations:tamper0000000001"
GENE_ID = "pubmed_gene:tamper00000000001"
PAIR_ID = "pubmed_pair:tamper00000000001"

GENE_SYMBOL = "SYNTH"
SUBJECT_SEP = "::"

# A score offset that is guaranteed to produce a genuinely-different score that
# still lives in [0, 1]: 0.5 away from the true score, on whichever side keeps
# it in range. 0.5 >> 1e-9 so the difference is unambiguous.
_SCORE_DELTA = 0.5


def _different_score(true_score: float) -> float:
    """Return a score in [0, 1] differing from ``true_score`` by exactly 0.5.

    Construction (documented in the module docstring): add 0.5 when the true
    score is in the lower half, subtract 0.5 otherwise. The result is always a
    valid score in [0, 1] and differs by 0.5 — far beyond the verifier's 1e-9
    tolerance — even when the true score sits on a boundary (0.0 -> 0.5,
    1.0 -> 0.5).
    """
    if true_score <= 0.5:
        return true_score + _SCORE_DELTA
    return true_score - _SCORE_DELTA


def _ot_record(rows: list[dict], evidence_id: str = OT_ID):
    """Build an ot_associations EvidenceRecord with a forced id, wrapping rows."""
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


def _pair_subject(disease_id: str) -> str:
    return f"{GENE_SYMBOL}{SUBJECT_SEP}{disease_id}"


# --------------------------------------------------------------------------- #
# Verifiable-claim builders: value = recompute_* from the store (VERIFIED)    #
# --------------------------------------------------------------------------- #
def _verified_association_claim(store: EvidenceStore, disease_id: str) -> Claim:
    """An ASSOCIATION claim whose value = recompute_association (so VERIFIED)."""
    record = store.get(OT_ID)
    score = recompute_association(record, disease_id)
    subject = _pair_subject(disease_id)
    return Claim(
        claim_id=f"ASSOCIATION:{subject}",
        claim_type=ClaimType.ASSOCIATION,
        subject=subject,
        value=score,
        evidence_ids=(OT_ID,),
    )


def _verified_breakdown_claim(store: EvidenceStore, disease_id: str) -> Claim:
    """A DATATYPE_BREAKDOWN claim whose value = recompute_datatype_breakdown."""
    record = store.get(OT_ID)
    breakdown = recompute_datatype_breakdown(record, disease_id)
    subject = _pair_subject(disease_id)
    return Claim(
        claim_id=f"DATATYPE_BREAKDOWN:{subject}",
        claim_type=ClaimType.DATATYPE_BREAKDOWN,
        subject=subject,
        value=breakdown,
        evidence_ids=(OT_ID,),
    )


def _verified_literature_claim(store: EvidenceStore) -> Claim:
    """A LITERATURE_COUNT claim whose value = recompute_literature_count."""
    record = store.get(GENE_ID)
    count = recompute_literature_count(record)
    return Claim(
        claim_id=f"LITERATURE_COUNT:{GENE_SYMBOL}",
        claim_type=ClaimType.LITERATURE_COUNT,
        subject=GENE_SYMBOL,
        value=count,
        evidence_ids=(GENE_ID,),
    )


def _verified_co_mention_claim(store: EvidenceStore, disease_id: str) -> Claim:
    """A CO_MENTION_COUNT claim whose value = recompute_co_mention_count."""
    record = store.get(PAIR_ID)
    count = recompute_co_mention_count(record)
    subject = _pair_subject(disease_id)
    return Claim(
        claim_id=f"CO_MENTION_COUNT:{subject}",
        claim_type=ClaimType.CO_MENTION_COUNT,
        subject=subject,
        value=count,
        evidence_ids=(PAIR_ID,),
    )


def _verified_recent_paper_claim(pmid: str) -> Claim:
    """A RECENT_PAPER claim whose value is a PMID genuinely in the gene idlist."""
    return Claim(
        claim_id=f"RECENT_PAPER:{GENE_SYMBOL}:{pmid}",
        claim_type=ClaimType.RECENT_PAPER,
        subject=GENE_SYMBOL,
        value=pmid,
        evidence_ids=(GENE_ID,),
    )


# --------------------------------------------------------------------------- #
# Immutability helpers (Req 13.4)                                             #
# --------------------------------------------------------------------------- #
def _payload_snapshot(store: EvidenceStore, evidence_id: str) -> object:
    """Deep-copy the referenced record's payload for a byte-identity check."""
    record = store.get(evidence_id)
    return copy.deepcopy(record.payload)


def _assert_evidence_unchanged(
    store: EvidenceStore,
    evidence_id: str,
    record_before: object,
    payload_before: object,
) -> None:
    """Assert the store's record object and payload bytes are unchanged (Req 13.4)."""
    record_after = store.get(evidence_id)
    # Same object: tamper/reset/verify are read-only over the store.
    assert record_after is record_before
    # Payload bytes identical to the pre-tamper deep-copied snapshot.
    assert record_after.payload == payload_before


# --------------------------------------------------------------------------- #
# Main property: genuinely-different tamper -> UNSUPPORTED; reset -> VERIFIED  #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(
    rows=ot_association_rows(),
    gene_payload=pubmed_esearch_payload(),
    pair_payload=pubmed_esearch_payload(),
    count_bump=st.integers(min_value=0, max_value=1000),
    data=st.data(),
)
def test_tamper_rejects_and_reset_restores(
    rows: list[dict],
    gene_payload: dict,
    pair_payload: dict,
    count_bump: int,
    data: st.DataObject,
) -> None:
    """A genuinely-different tamper rejects; reset restores; evidence unchanged.

    Builds a store of synthetic OT + PubMed evidence and one genuinely-verifiable
    claim per claim type, then for each: snapshots the evidence, confirms the
    baseline VERIFIED, tampers with a genuinely-different value (UNSUPPORTED,
    reason reports both values / recompute failure), resets (VERIFIED again), and
    asserts the referenced evidence record + payload bytes never changed
    (Req 12.7, 13.1, 13.2, 13.3, 13.4).
    """
    store = EvidenceStore()
    store.add(_ot_record(rows))
    store.add(_pubmed_record(gene_payload, Source.PUBMED_GENE, GENE_ID))
    store.add(_pubmed_record(pair_payload, Source.PUBMED_PAIR, PAIR_ID))
    verifier = Verifier()

    # Pick one disease id for the pair/association/breakdown claims.
    disease_id = rows[0]["disease"]["id"]

    # --- Build (claim, tamper_value, evidence_id) cases, one per type. ----- #
    cases: list[tuple[Claim, object, str]] = []

    # ASSOCIATION: tamper to a score 0.5 away (>1e-9 different), still in [0,1].
    assoc = _verified_association_claim(store, disease_id)
    cases.append((assoc, _different_score(float(assoc.value)), OT_ID))

    # DATATYPE_BREAKDOWN: differ as an id-keyed mapping. Bump one entry's score
    # if non-empty; otherwise add a brand-new id so the mapping differs.
    breakdown = _verified_breakdown_claim(store, disease_id)
    bd_value: tuple[DatatypeScore, ...] = breakdown.value  # type: ignore[assignment]
    if bd_value:
        idx = data.draw(st.integers(min_value=0, max_value=len(bd_value) - 1))
        bumped = list(bd_value)
        target = bumped[idx]
        bumped[idx] = DatatypeScore(
            id=target.id, score=_different_score(float(target.score))
        )
        tampered_breakdown: tuple[DatatypeScore, ...] = tuple(bumped)
    else:
        # Empty breakdown: adding any id makes the id set differ.
        tampered_breakdown = (DatatypeScore(id="clinical", score=0.5),)
    cases.append((breakdown, tampered_breakdown, OT_ID))

    # LITERATURE_COUNT: a different non-negative int (true + 1 + k).
    lit = _verified_literature_claim(store)
    cases.append((lit, int(lit.value) + 1 + count_bump, GENE_ID))

    # CO_MENTION_COUNT: a different non-negative int (true + 1 + k).
    com = _verified_co_mention_claim(store, disease_id)
    cases.append((com, int(com.value) + 1 + count_bump, PAIR_ID))

    # RECENT_PAPER: a sentinel PMID the digit-only generator can never produce,
    # so it is guaranteed absent from the gene record's idlist. Only exercisable
    # when the gene idlist has at least one genuine PMID to start VERIFIED from.
    gene_idlist = gene_payload["esearchresult"]["idlist"]
    if gene_idlist:
        rp = _verified_recent_paper_claim(gene_idlist[0])
        cases.append((rp, "X_not_a_pmid_0", GENE_ID))

    for claim, tamper_value, evidence_id in cases:
        # Snapshot evidence identity + bytes before any tamper (Req 13.4).
        record_before = store.get(evidence_id)
        payload_before = _payload_snapshot(store, evidence_id)
        genuine_original = claim.value

        # 1. Baseline verifies VERIFIED (value really recomputes from the store).
        baseline = verifier.verify(claim, store)
        assert baseline.status is ClaimStatus.VERIFIED, (
            f"baseline not VERIFIED for {claim.claim_type}: {baseline.reason}"
        )

        # 2. Tamper to a genuinely-different value -> UNSUPPORTED (Req 13.1, 13.2).
        tampered, t_result = tamper(claim, tamper_value, store, verifier)
        assert t_result.status is ClaimStatus.UNSUPPORTED, (
            f"tamper did not reject {claim.claim_type}: value={tamper_value!r}"
        )
        assert tampered.status is ClaimStatus.UNSUPPORTED
        assert tampered.value == tamper_value
        # original_value preserves the genuine pre-tamper value (Req 13.3).
        assert tampered.original_value == genuine_original
        # Reason reports both values (numeric/score types) or a recompute
        # failure (RECENT_PAPER not-in-idlist). Either way it is informative.
        assert t_result.reason
        if claim.claim_type is ClaimType.RECENT_PAPER:
            assert "recompute" in t_result.reason.lower()
        else:
            assert "!=" in t_result.reason

        # Evidence unchanged after tamper (Req 13.4).
        _assert_evidence_unchanged(store, evidence_id, record_before, payload_before)

        # 3. Reset -> VERIFIED, value restored, original_value cleared (Req 13.3).
        reset_claim, r_result = reset(tampered, store, verifier)
        assert r_result.status is ClaimStatus.VERIFIED, (
            f"reset did not restore VERIFIED for {claim.claim_type}: {r_result.reason}"
        )
        assert reset_claim.status is ClaimStatus.VERIFIED
        assert reset_claim.original_value is None
        if claim.claim_type is ClaimType.ASSOCIATION:
            assert math.isclose(
                float(reset_claim.value), float(genuine_original), abs_tol=1e-9
            )
        else:
            assert reset_claim.value == genuine_original

        # Evidence unchanged after reset too (Req 13.4).
        _assert_evidence_unchanged(store, evidence_id, record_before, payload_before)


# --------------------------------------------------------------------------- #
# Secondary property: tamper to the TRUE value does not force a rejection      #
# --------------------------------------------------------------------------- #
@settings(max_examples=100)
@given(rows=ot_association_rows())
def test_tamper_to_true_value_stays_verified(rows: list[dict]) -> None:
    """Tamper to a value EQUAL to the true value re-verifies as VERIFIED.

    tamper does not *force* an UNSUPPORTED verdict — it replaces the value and
    re-verifies. Supplying the genuine recomputed value (within tolerance) must
    leave the verdict VERIFIED, confirming tamper's rejection in the main
    property is driven by the value being genuinely different, not by tamper
    itself (Req 13.1, 13.2 contrapositive).
    """
    store = EvidenceStore()
    store.add(_ot_record(rows))
    verifier = Verifier()

    disease_id = rows[0]["disease"]["id"]
    claim = _verified_association_claim(store, disease_id)

    record_before = store.get(OT_ID)
    payload_before = _payload_snapshot(store, OT_ID)

    # Tamper with the exact true value: re-verification must stay VERIFIED.
    tampered, result = tamper(claim, claim.value, store, verifier)
    assert result.status is ClaimStatus.VERIFIED
    assert tampered.status is ClaimStatus.VERIFIED
    _assert_evidence_unchanged(store, OT_ID, record_before, payload_before)


# --------------------------------------------------------------------------- #
# Double-tamper then reset: reset restores the TRUE original (Req 13.3)        #
# --------------------------------------------------------------------------- #
@settings(max_examples=100)
@given(rows=ot_association_rows())
def test_double_tamper_then_reset_restores_true_original(rows: list[dict]) -> None:
    """A second tamper keeps the original; reset restores the TRUE original.

    Tampering an already-tampered claim must NOT overwrite ``original_value`` with
    the first tamper's value — so a reset after two tampers restores the genuine
    pre-any-tamper value, never the intermediate tamper (Req 13.3).
    """
    store = EvidenceStore()
    store.add(_ot_record(rows))
    verifier = Verifier()

    disease_id = rows[0]["disease"]["id"]
    claim = _verified_association_claim(store, disease_id)
    true_value = float(claim.value)

    record_before = store.get(OT_ID)
    payload_before = _payload_snapshot(store, OT_ID)

    # First tamper: a genuinely-different value. original_value becomes the true.
    first_tamper_value = _different_score(true_value)
    tampered1, r1 = tamper(claim, first_tamper_value, store, verifier)
    assert r1.status is ClaimStatus.UNSUPPORTED
    assert math.isclose(float(tampered1.original_value), true_value, abs_tol=1e-9)

    # Second tamper (on the already-tampered claim): a yet-different value. The
    # stored original_value must still be the TRUE value, not the first tamper.
    second_tamper_value = (first_tamper_value + 0.25) % 1.0
    # Ensure the second value is genuinely different from both true and first.
    if math.isclose(second_tamper_value, true_value, abs_tol=1e-9) or math.isclose(
        second_tamper_value, first_tamper_value, abs_tol=1e-9
    ):
        second_tamper_value = (first_tamper_value + 0.5) % 1.0
    tampered2, r2 = tamper(tampered1, second_tamper_value, store, verifier)
    assert r2.status is ClaimStatus.UNSUPPORTED
    assert tampered2.value == second_tamper_value
    # Critical: original_value is still the TRUE original, not first_tamper_value.
    assert math.isclose(float(tampered2.original_value), true_value, abs_tol=1e-9)

    # Reset restores the TRUE original and re-verifies VERIFIED.
    reset_claim, r3 = reset(tampered2, store, verifier)
    assert r3.status is ClaimStatus.VERIFIED
    assert reset_claim.original_value is None
    assert math.isclose(float(reset_claim.value), true_value, abs_tol=1e-9)

    _assert_evidence_unchanged(store, OT_ID, record_before, payload_before)
