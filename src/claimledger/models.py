"""Domain models: EvidenceRecord, Claim, ClaimType, DatatypeScore, Association,
ConflictFlag, ClaimStatus, VerificationResult (Req 4, 5, 6, 9).

Library choice: standard-library ``dataclasses``, **not** Pydantic. The domain
logic (recompute, verify, conflict) must be pure and deterministic, and these
models are internal. All external JSON is raw API payload parsed by hand against
saved shapes (steering rule 8), so there is no coercion/validation layer here:
Pydantic-style coercion would add a second, implicit parsing step that could
silently "repair" shapes and mask the very mismatches the verifier exists to
catch (steering rule 3, "never repair").

Frozen dataclasses give value semantics, cheap equality for round-trip tests,
and hash-stability for deterministic ordering, with zero coercion. Tuples are
used wherever the design uses a sequence so instances stay immutable and
hashable. Serialization for export and round-trip tests is explicit
(``to_dict``/``from_dict``) rather than reflective.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .config import Thresholds


class Source(str, Enum):
    """Which interface produced an EvidenceRecord (Req 4.1)."""

    OT_SEARCH = "ot_search"
    OT_ASSOCIATIONS = "ot_associations"
    PUBMED_GENE = "pubmed_gene"
    PUBMED_PAIR = "pubmed_pair"


@dataclass(frozen=True)
class EvidenceRecord:
    """A single cached/live API response and its provenance (Req 4.1-4.3, 11.5-11.7)."""

    evidence_id: str          # stable within run (see Evidence IDs section)   Req 4.1, 4.2
    source: Source            # which interface produced it
    url: str                  # request URL (envelope source_url when cached)  Req 4.1, 11.7
    retrieved_at: str         # ISO-8601 UTC string                            Req 4.1, 11.7
    payload: dict             # raw response body, parsed by hand              Req 4.1, 4.6
    submitted_query: str | None = None     # gene+disease PubMed only          Req 4.3
    query_translation: str | None = None   # esearchresult.querytranslation    Req 4.3
    from_cache: bool = False  # surfaced in UI as "came from cache"            Req 11.5


@dataclass(frozen=True)
class DatatypeScore:
    """One evidence-datatype score within an association (Req 2.4, 2.5).

    ``id`` is one of: clinical, genetic_literature, affected_pathway,
    literature, genetic_association, somatic_mutation.
    """

    id: str
    score: float

    def to_dict(self) -> dict:
        """Explicit serialization (no reflection/coercion)."""
        return {"id": self.id, "score": self.score}

    @classmethod
    def from_dict(cls, data: dict) -> "DatatypeScore":
        """Explicit deserialization; inverse of :meth:`to_dict`."""
        return cls(id=data["id"], score=data["score"])


@dataclass(frozen=True)
class Association:
    """A gene-disease association parsed from an ot_associations record (Req 2.1-2.7)."""

    disease_id: str
    disease_name: str
    overall_score: float                        # Req 2.4
    datatype_scores: tuple[DatatypeScore, ...]  # tuple: immutable + order captured, not relied on
    evidence_id: str                            # the ot_associations record
    incomplete: bool = False                    # Req 2.6 (a required field was missing)


class ClaimType(str, Enum):
    """The five claim types; see the value-type table below (Req 5.1, 5.3-5.7)."""

    ASSOCIATION = "ASSOCIATION"               # value: float (Overall_Score)
    DATATYPE_BREAKDOWN = "DATATYPE_BREAKDOWN"  # value: tuple[DatatypeScore, ...]
    LITERATURE_COUNT = "LITERATURE_COUNT"      # value: int
    CO_MENTION_COUNT = "CO_MENTION_COUNT"      # value: int
    RECENT_PAPER = "RECENT_PAPER"              # value: str (PMID)


class ClaimStatus(str, Enum):
    """Verification verdict surfaced in the ledger (Req 6.4, 7.1)."""

    VERIFIED = "Verified"
    UNSUPPORTED = "Unsupported"


# Value type carried by Claim.value, keyed by claim type (design "Value type per
# claim type" table). Used by the explicit Claim serializer to round-trip each
# type correctly without guessing from the runtime object.
#
#   ASSOCIATION         -> float   (rows[i].score)
#   DATATYPE_BREAKDOWN  -> tuple[DatatypeScore, ...]  (serialized as list of dicts)
#   LITERATURE_COUNT    -> int     (int(esearchresult.count), gene-only)
#   CO_MENTION_COUNT    -> int     (int(esearchresult.count), gene+disease)
#   RECENT_PAPER        -> str     (a PMID from esearchresult.idlist)


@dataclass(frozen=True)
class Claim:
    """A single recomputable assertion about a gene/disease (Req 5.1-5.8, 6.4, 7.1, 13.3).

    ``value`` is typed per ``claim_type`` (float / tuple[DatatypeScore,...] / int
    / int / str PMID). ``original_value`` is set by the Tamper_Control so a reset
    can restore the pre-tamper value (Req 13.3).
    """

    claim_id: str
    claim_type: ClaimType
    subject: str                          # gene symbol or "GENE::DISEASE" key for context
    value: object                         # float | tuple[DatatypeScore,...] | int | str per type
    evidence_ids: tuple[str, ...]         # >= 1 (Req 5.2, steering rule 1)
    status: ClaimStatus = ClaimStatus.VERIFIED
    reason: str | None = None             # populated when UNSUPPORTED (Req 7.1)
    original_value: object = None         # set by Tamper_Control for reset (Req 13.3)

    def to_dict(self) -> dict:
        """Serialize to a JSON-ready dict with explicit per-type value handling.

        The ``DATATYPE_BREAKDOWN`` value is a tuple of :class:`DatatypeScore`,
        serialized as a list of dicts; every other type's value is already a
        JSON scalar. ``original_value`` round-trips with the same per-type rule
        so a tampered-then-exported claim restores correctly.
        """
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type.value,
            "subject": self.subject,
            "value": self._value_to_json(self.claim_type, self.value),
            "evidence_ids": list(self.evidence_ids),
            "status": self.status.value,
            "reason": self.reason,
            "original_value": self._value_to_json(self.claim_type, self.original_value),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        """Deserialize a dict produced by :meth:`to_dict`; exact inverse."""
        claim_type = ClaimType(data["claim_type"])
        return cls(
            claim_id=data["claim_id"],
            claim_type=claim_type,
            subject=data["subject"],
            value=cls._value_from_json(claim_type, data["value"]),
            evidence_ids=tuple(data["evidence_ids"]),
            status=ClaimStatus(data["status"]),
            reason=data.get("reason"),
            original_value=cls._value_from_json(claim_type, data.get("original_value")),
        )

    @staticmethod
    def _value_to_json(claim_type: ClaimType, value: object) -> object:
        """Convert a typed claim value to a JSON-serializable form.

        Only ``DATATYPE_BREAKDOWN`` needs structural conversion (tuple of
        DatatypeScore -> list of dicts). ``None`` passes through so an unset
        ``original_value`` serializes as ``null``.
        """
        if value is None:
            return None
        if claim_type is ClaimType.DATATYPE_BREAKDOWN:
            return [ds.to_dict() for ds in value]  # type: ignore[union-attr]
        return value

    @staticmethod
    def _value_from_json(claim_type: ClaimType, value: object) -> object:
        """Inverse of :meth:`_value_to_json`, reconstructing typed values.

        A ``DATATYPE_BREAKDOWN`` list-of-dicts becomes a tuple of DatatypeScore
        (tuple preserves immutability/hashability); ``None`` passes through.
        """
        if value is None:
            return None
        if claim_type is ClaimType.DATATYPE_BREAKDOWN:
            return tuple(DatatypeScore.from_dict(d) for d in value)  # type: ignore[union-attr]
        return value


@dataclass(frozen=True)
class ConflictFlag:
    """A raised conflict with the detail each named rule needs for its panel.

    ``Single_Source_Dependence`` populates ``supporting_datatypes`` /
    ``supporting_count`` (Req 9.5); ``Literature_Gap`` populates
    ``overall_score`` / ``co_mention_count`` (Req 9.3). ``thresholds`` records
    the thresholds in force when flagged (Req 9.7).
    """

    rule: str                    # "Single_Source_Dependence" | "Literature_Gap"
    disease_id: str
    disease_name: str
    # Single_Source_Dependence detail:
    supporting_datatypes: tuple[DatatypeScore, ...] = ()  # those >= support_threshold (Req 9.5)
    supporting_count: int = 0
    # Literature_Gap detail:
    overall_score: float | None = None
    co_mention_count: int | None = None
    thresholds: Thresholds | None = None   # the thresholds in force when flagged


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of verifying one claim against its evidence (Req 6.3, 6.4, 7.4, 13.2).

    Carries both the claim's value and the recomputed value so the ledger and the
    tamper reason can report both sides of a mismatch.
    """

    status: ClaimStatus
    reason: str | None = None
    claim_value: object = None
    recomputed_value: object = None
