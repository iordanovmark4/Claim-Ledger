"""Unit tests for model construction and serialization (Req 5.1, 5.2).

Assert frozen-ness, value semantics/equality, and ``to_dict``/``from_dict``
symmetry for each of the five claim-value types, plus the nested
``DatatypeScore`` serializer, ``original_value`` round-trip, and the
``evidence_ids`` tuple invariant (Req 5.2).
"""

from __future__ import annotations

import dataclasses

import pytest

from claimledger.models import (
    Association,
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
    EvidenceRecord,
    Source,
)


# --- helpers ----------------------------------------------------------------


def _claim(
    claim_type: ClaimType,
    value: object,
    *,
    original_value: object = None,
) -> Claim:
    """Build a Claim of ``claim_type`` with a single Evidence_ID."""
    return Claim(
        claim_id="c1",
        claim_type=claim_type,
        subject="EGFR",
        value=value,
        evidence_ids=("ev:abc123",),
        original_value=original_value,
    )


# A DATATYPE_BREAKDOWN value: variable-length tuple of DatatypeScore.
_BREAKDOWN = (
    DatatypeScore(id="genetic_association", score=0.42),
    DatatypeScore(id="literature", score=0.17),
    DatatypeScore(id="clinical", score=0.9),
)

# (ClaimType, value) for each of the five claim-value types.
_CLAIM_CASES = [
    (ClaimType.ASSOCIATION, 0.73),             # float Overall_Score
    (ClaimType.DATATYPE_BREAKDOWN, _BREAKDOWN),  # tuple[DatatypeScore, ...]
    (ClaimType.LITERATURE_COUNT, 12345),       # int
    (ClaimType.CO_MENTION_COUNT, 42),          # int
    (ClaimType.RECENT_PAPER, "39123456"),      # str PMID
]


# --- DatatypeScore round-trip ----------------------------------------------


def test_datatype_score_to_dict_from_dict_symmetry() -> None:
    """DatatypeScore.from_dict(to_dict(x)) == x (Req 5.1)."""
    ds = DatatypeScore(id="genetic_association", score=0.42)
    assert ds.to_dict() == {"id": "genetic_association", "score": 0.42}
    assert DatatypeScore.from_dict(ds.to_dict()) == ds


# --- Claim round-trip per value type ---------------------------------------


@pytest.mark.parametrize("claim_type,value", _CLAIM_CASES)
def test_claim_round_trip_per_value_type(claim_type: ClaimType, value: object) -> None:
    """Each of the 5 claim-value types round-trips and equals the original (Req 5.1)."""
    claim = _claim(claim_type, value)
    restored = Claim.from_dict(claim.to_dict())
    assert restored == claim


def test_datatype_breakdown_serializes_as_list_and_restores_as_tuple() -> None:
    """DATATYPE_BREAKDOWN -> list of dicts in JSON, back to a tuple of DatatypeScore."""
    claim = _claim(ClaimType.DATATYPE_BREAKDOWN, _BREAKDOWN)
    data = claim.to_dict()
    # Serialized form is a plain list of dicts (JSON-ready).
    assert data["value"] == [ds.to_dict() for ds in _BREAKDOWN]
    assert isinstance(data["value"], list)
    # Restored form is a tuple of DatatypeScore (immutable + hashable).
    restored = Claim.from_dict(data)
    assert isinstance(restored.value, tuple)
    assert all(isinstance(ds, DatatypeScore) for ds in restored.value)
    assert restored.value == _BREAKDOWN


@pytest.mark.parametrize("claim_type,value", _CLAIM_CASES)
def test_original_value_round_trips(claim_type: ClaimType, value: object) -> None:
    """A tampered-style original_value survives the round-trip (Req 5.1, 13.3)."""
    # original_value uses the same per-type rule as value.
    claim = _claim(claim_type, value, original_value=value)
    restored = Claim.from_dict(claim.to_dict())
    assert restored.original_value == value
    assert restored == claim


def test_unset_original_value_round_trips_as_none() -> None:
    """An unset original_value serializes as null and restores as None."""
    claim = _claim(ClaimType.ASSOCIATION, 0.5)
    data = claim.to_dict()
    assert data["original_value"] is None
    assert Claim.from_dict(data).original_value is None


def test_unsupported_claim_reason_round_trips() -> None:
    """status and reason survive the round-trip for an Unsupported claim (Req 7.1)."""
    claim = Claim(
        claim_id="c9",
        claim_type=ClaimType.LITERATURE_COUNT,
        subject="EGFR",
        value=7,
        evidence_ids=("ev:abc123",),
        status=ClaimStatus.UNSUPPORTED,
        reason="claim value 7 != recomputed 8",
    )
    restored = Claim.from_dict(claim.to_dict())
    assert restored.status is ClaimStatus.UNSUPPORTED
    assert restored.reason == "claim value 7 != recomputed 8"
    assert restored == claim


# --- evidence_ids tuple invariant (Req 5.2) --------------------------------


@pytest.mark.parametrize("claim_type,value", _CLAIM_CASES)
def test_evidence_ids_is_a_tuple_with_at_least_one_id(
    claim_type: ClaimType, value: object
) -> None:
    """Every Claim carries evidence_ids as a tuple with >= 1 id (Req 5.2)."""
    claim = _claim(claim_type, value)
    assert isinstance(claim.evidence_ids, tuple)
    assert len(claim.evidence_ids) >= 1
    # The invariant survives serialization (list in JSON, tuple on restore).
    data = claim.to_dict()
    assert isinstance(data["evidence_ids"], list)
    restored = Claim.from_dict(data)
    assert isinstance(restored.evidence_ids, tuple)
    assert len(restored.evidence_ids) >= 1


# --- value semantics / equality / hashing ----------------------------------


@pytest.mark.parametrize("claim_type,value", _CLAIM_CASES)
def test_two_claims_with_equal_fields_are_equal_and_hash_equal(
    claim_type: ClaimType, value: object
) -> None:
    """Value semantics: equal fields => == and hash-equal (tuples keep it hashable)."""
    a = _claim(claim_type, value)
    b = _claim(claim_type, value)
    assert a == b
    assert hash(a) == hash(b)
    # Hashable instances can live in a set keyed by value.
    assert len({a, b}) == 1


def test_claims_with_differing_value_are_not_equal() -> None:
    """Distinct values produce distinct (unequal) claims."""
    a = _claim(ClaimType.ASSOCIATION, 0.73)
    b = _claim(ClaimType.ASSOCIATION, 0.74)
    assert a != b


def test_datatype_score_value_semantics() -> None:
    """Two DatatypeScores with equal fields are == and hash-equal."""
    a = DatatypeScore(id="clinical", score=0.9)
    b = DatatypeScore(id="clinical", score=0.9)
    assert a == b
    assert hash(a) == hash(b)


# --- frozen-ness ------------------------------------------------------------


def test_claim_is_frozen() -> None:
    """Assigning to a Claim attribute raises FrozenInstanceError."""
    claim = _claim(ClaimType.ASSOCIATION, 0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.value = 0.6  # type: ignore[misc]


def test_datatype_score_is_frozen() -> None:
    """Assigning to a DatatypeScore attribute raises FrozenInstanceError."""
    ds = DatatypeScore(id="clinical", score=0.9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ds.score = 0.1  # type: ignore[misc]


def test_evidence_record_is_frozen() -> None:
    """Assigning to an EvidenceRecord attribute raises FrozenInstanceError."""
    rec = EvidenceRecord(
        evidence_id="ev:abc123",
        source=Source.OT_SEARCH,
        url="https://example.test/graphql",
        retrieved_at="2024-01-01T00:00:00Z",
        payload={"data": {}},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.url = "https://tampered.test"  # type: ignore[misc]


def test_association_is_frozen() -> None:
    """Assigning to an Association attribute raises FrozenInstanceError."""
    assoc = Association(
        disease_id="EFO_0000001",
        disease_name="example disease",
        overall_score=0.73,
        datatype_scores=_BREAKDOWN,
        evidence_id="ev:assoc",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        assoc.overall_score = 0.1  # type: ignore[misc]
