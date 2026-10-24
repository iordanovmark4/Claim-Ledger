"""Verifier: recompute-and-compare, tolerance, no-repair (Req 6, 7, 13).

``Verifier.verify(claim, store) -> VerificationResult`` is the gate every number
passes before it can reach the screen as *Verified*: it recomputes the claim's
value from the referenced evidence through the shared ``recompute_*`` functions
(the single source of truth, Req 4.5, 6.1) and accepts the claim *only* when the
claimed value matches the recomputed one. It is **pure and side-effect-free** and
it **never mutates, repairs, or substitutes** ``claim.value`` (Req 6.7, steering
rule 3): a mismatch is reported, never silently corrected.

Algorithm (design "Verifier")
------------------------------
1. **Evidence-existence check (Req 6.2).** For each ``evidence_id`` in
   ``claim.evidence_ids``, if ``not store.contains(evidence_id)`` the claim is
   ``UNSUPPORTED`` with a reason naming the missing id. A claim with *no*
   evidence ids is also rejected — there is nothing to recompute from, so it can
   never be supported (Req 5.2, steering rule 1).
2. **Recompute (Req 6.1).** Dispatch on ``claim.claim_type`` to the matching
   ``recompute_*`` on the referenced record. A ``recompute_*`` failure
   (``LookupError`` / ``ValueError``) means the evidence cannot genuinely supply
   the claimed value, which is itself a rejection (Req 6.3, steering rule 3) —
   never a repaired default.
3. **Compare.** Scores (``ASSOCIATION`` / ``DATATYPE_BREAKDOWN``) within an
   absolute tolerance of ``1e-9`` (Req 6.5); the datatype breakdown is compared
   as a mapping keyed by datatype ``id`` so ordering never changes the verdict.
   Counts and PMIDs by exact equality (Req 6.6).
4. **Verdict.** Equal -> ``VERIFIED`` (Req 6.4). Not equal -> ``UNSUPPORTED``
   with a reason reporting *both* the claim value and the recomputed value
   (Req 6.3, 7.4).

The returned :class:`~claimledger.models.VerificationResult` carries ``status``,
``reason``, ``claim_value``, and ``recomputed_value`` so the ledger can show both
sides of a mismatch (Req 7.4) and the tamper control can report both values
(Req 13.2). For the ledger display, :func:`annotate` returns a copy of the claim
with ``status``/``reason`` set via :func:`dataclasses.replace` — and it is
careful to leave ``value`` byte-identical to the input (only status/reason
change), so even the "annotated for display" path cannot repair a value
(Req 6.7, 7.1).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .evidence import EvidenceStore
from .models import (
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
    VerificationResult,
)
from .recompute import (
    recompute_association,
    recompute_co_mention_count,
    recompute_datatype_breakdown,
    recompute_literature_count,
    recompute_recent_paper,
)

# Absolute tolerance for float comparisons of Overall_Score / Datatype_Score
# (Req 6.5). Counts and PMIDs are compared by exact equality (Req 6.6), so the
# tolerance deliberately applies to score claim types only.
SCORE_ABS_TOLERANCE = 1e-9

# The "::" convention from claims.py: a pair claim's subject is "GENE::DISEASE".
# Re-declared here (rather than imported) only as a fallback splitter; the
# disease id is derived from the subject so the verifier can recompute an
# ASSOCIATION / DATATYPE_BREAKDOWN by the same disease_id the builder used.
SUBJECT_SEP = "::"


def _disease_id_from_subject(subject: str) -> str:
    """Extract the ``DISEASE`` half of a ``GENE::DISEASE`` subject (claims.py).

    ASSOCIATION / DATATYPE_BREAKDOWN claims carry a ``GENE::DISEASE`` subject
    (``claims._pair_subject``). The disease id is everything after the first
    ``"::"`` so a gene symbol that itself contains no separator is handled, and a
    disease id is returned unchanged if (defensively) no separator is present.
    """
    _, sep, disease_id = subject.partition(SUBJECT_SEP)
    return disease_id if sep else subject


def _scores_equal(a: float, b: float) -> bool:
    """True iff two scores match within :data:`SCORE_ABS_TOLERANCE` (Req 6.5)."""
    return abs(float(a) - float(b)) <= SCORE_ABS_TOLERANCE


def _breakdowns_equal(
    claimed: Iterable[DatatypeScore],
    recomputed: Iterable[DatatypeScore],
) -> bool:
    """Compare two datatype breakdowns as id-keyed mappings, order-independent.

    The verdict must not depend on the order of the tuples (Req 6.5, design
    "compare as sets keyed by datatype id"): both sides are collapsed to an
    ``{id: score}`` mapping, the two id *sets* must match exactly, and each
    shared id's scores must agree within :data:`SCORE_ABS_TOLERANCE`. A duplicate
    id on either side (which canonical recompute never produces) is treated as a
    mismatch rather than silently coalesced.
    """
    claimed_map = _as_score_map(claimed)
    recomputed_map = _as_score_map(recomputed)
    if claimed_map is None or recomputed_map is None:
        return False
    if claimed_map.keys() != recomputed_map.keys():
        return False
    return all(
        _scores_equal(claimed_map[key], recomputed_map[key])
        for key in claimed_map
    )


def _as_score_map(scores: Iterable[DatatypeScore]) -> dict[str, float] | None:
    """Build an ``{id: score}`` mapping, or ``None`` if the shape is wrong.

    Returns ``None`` when the value is not an iterable of ``DatatypeScore`` or
    carries a duplicate id, so a structurally-off claim value compares unequal
    rather than raising — a tampered breakdown of the wrong type is a rejection,
    not a crash.
    """
    try:
        items = list(scores)
    except TypeError:
        return None
    mapping: dict[str, float] = {}
    for item in items:
        ds_id = getattr(item, "id", None)
        ds_score = getattr(item, "score", None)
        if not isinstance(ds_id, str) or ds_score is None:
            return None
        if ds_id in mapping:  # duplicate id: not a canonical breakdown
            return None
        mapping[ds_id] = ds_score
    return mapping


def _format_value(value: object) -> str:
    """Render a claim/recomputed value for a human-readable rejection reason.

    A ``DATATYPE_BREAKDOWN`` tuple is rendered as an id-sorted ``{id: score}``
    mapping so the reason reads the same regardless of tuple order; every other
    value falls back to ``repr`` so counts, scores, and PMIDs show exactly.
    """
    score_map = _as_score_map(value) if _looks_like_breakdown(value) else None
    if score_map is not None:
        inner = ", ".join(f"{k}={score_map[k]!r}" for k in sorted(score_map))
        return "{" + inner + "}"
    return repr(value)


def _looks_like_breakdown(value: object) -> bool:
    """Heuristic: is ``value`` a sequence of :class:`DatatypeScore`-like items?"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return False
    return all(hasattr(item, "id") and hasattr(item, "score") for item in value)


class Verifier:
    """Recompute-and-compare verifier with a 1e-9 score tolerance and no repair.

    Stateless and reusable: a single instance can verify any number of claims
    against any store. Holds no per-run state so it is safe to share and trivial
    to reason about (design "Verifier": pure, side-effect-free).
    """

    def verify(self, claim: Claim, store: EvidenceStore) -> VerificationResult:
        """Verify one ``claim`` against ``store`` (Req 6.1-6.7).

        Pure: reads ``claim`` and ``store`` only and returns a
        :class:`VerificationResult`. Never writes back to ``claim`` or ``store``;
        in particular ``claim.value`` is never modified, repaired, or substituted
        (Req 6.7). The result carries both the claim value and the recomputed
        value so the caller can show both sides of a mismatch (Req 7.4, 13.2).
        """
        # 1. Evidence-existence check (Req 6.2). Empty evidence_ids -> no
        #    evidence to recompute from, so the claim cannot be supported.
        if not claim.evidence_ids:
            return VerificationResult(
                status=ClaimStatus.UNSUPPORTED,
                reason="missing evidence: claim references no Evidence_ID",
                claim_value=claim.value,
                recomputed_value=None,
            )
        for evidence_id in claim.evidence_ids:
            if not store.contains(evidence_id):
                return VerificationResult(
                    status=ClaimStatus.UNSUPPORTED,
                    reason=f"missing evidence: {evidence_id}",
                    claim_value=claim.value,
                    recomputed_value=None,
                )

        # 2. Recompute via the shared recompute_* (Req 6.1). A recompute failure
        #    is a rejection (the evidence cannot supply the claimed value),
        #    reported with the precise error (Req 6.3; steering rule 3).
        try:
            recomputed = self._recompute(claim, store)
        except (LookupError, ValueError) as exc:
            return VerificationResult(
                status=ClaimStatus.UNSUPPORTED,
                reason=f"cannot recompute from evidence: {exc}",
                claim_value=claim.value,
                recomputed_value=None,
            )

        # 3. Compare with the per-type rule (Req 6.5 / 6.6).
        if self._values_match(claim.claim_type, claim.value, recomputed):
            # 4a. Verdict: VERIFIED (Req 6.4).
            return VerificationResult(
                status=ClaimStatus.VERIFIED,
                reason=None,
                claim_value=claim.value,
                recomputed_value=recomputed,
            )

        # 4b. Verdict: UNSUPPORTED, reason reports BOTH values (Req 6.3, 7.4).
        return VerificationResult(
            status=ClaimStatus.UNSUPPORTED,
            reason=(
                f"claim value {_format_value(claim.value)} != "
                f"recomputed {_format_value(recomputed)}"
            ),
            claim_value=claim.value,
            recomputed_value=recomputed,
        )

    # -- internals ---------------------------------------------------------- #
    def _recompute(self, claim: Claim, store: EvidenceStore) -> object:
        """Dispatch to the matching ``recompute_*`` for ``claim.claim_type``.

        The referenced record is the claim's (single) evidence record; existence
        was already confirmed in :meth:`verify`. Score/breakdown claims recompute
        by the disease id carried in the ``GENE::DISEASE`` subject; the recent
        paper confirms the claimed PMID against the record's idlist. A recompute
        helper raising ``LookupError``/``ValueError`` propagates to
        :meth:`verify`, which turns it into a rejection.
        """
        record = store.get(claim.evidence_ids[0])
        if record is None:  # pragma: no cover - contains() already guarded
            raise LookupError(f"missing evidence: {claim.evidence_ids[0]}")

        claim_type = claim.claim_type
        if claim_type is ClaimType.ASSOCIATION:
            return recompute_association(record, _disease_id_from_subject(claim.subject))
        if claim_type is ClaimType.DATATYPE_BREAKDOWN:
            return recompute_datatype_breakdown(
                record, _disease_id_from_subject(claim.subject)
            )
        if claim_type is ClaimType.LITERATURE_COUNT:
            return recompute_literature_count(record)
        if claim_type is ClaimType.CO_MENTION_COUNT:
            return recompute_co_mention_count(record)
        if claim_type is ClaimType.RECENT_PAPER:
            # recompute_recent_paper returns the pmid iff it is in the idlist,
            # raising LookupError otherwise — so a tampered PMID the evidence
            # does not contain is rejected rather than "corrected".
            return recompute_recent_paper(record, claim.value)
        # Defensive: an unknown claim type cannot be recomputed/supported.
        raise ValueError(f"unknown claim type: {claim_type!r}")

    def _values_match(
        self, claim_type: ClaimType, claim_value: object, recomputed: object
    ) -> bool:
        """Apply the per-type comparison rule (Req 6.5 / 6.6).

        Scores compare within :data:`SCORE_ABS_TOLERANCE`; the datatype breakdown
        compares as an id-keyed mapping (order-independent); counts and PMIDs
        compare by exact equality. A score claim whose value is not numeric
        (e.g. tampered to a string) compares unequal rather than raising.
        """
        if claim_type is ClaimType.ASSOCIATION:
            if isinstance(claim_value, bool) or not isinstance(
                claim_value, (int, float)
            ):
                return False
            return _scores_equal(claim_value, recomputed)  # type: ignore[arg-type]
        if claim_type is ClaimType.DATATYPE_BREAKDOWN:
            return _breakdowns_equal(claim_value, recomputed)  # type: ignore[arg-type]
        # LITERATURE_COUNT / CO_MENTION_COUNT / RECENT_PAPER: exact equality.
        return claim_value == recomputed


def annotate(claim: Claim, result: VerificationResult) -> Claim:
    """Return ``claim`` with ``status``/``reason`` set from ``result`` — value intact.

    For the ledger display (Req 7.1, 7.4): produces a copy carrying the verdict's
    ``status`` and ``reason`` via :func:`dataclasses.replace`. The ``value`` (and
    ``original_value``) field is left exactly as it was on the input claim — this
    function never repairs or substitutes a value, so even the display path
    honors the no-repair invariant (Req 6.7). Returning a copy keeps the original
    frozen claim unmutated.
    """
    return replace(claim, status=result.status, reason=result.reason)


class _Verdicts:
    """Lightweight pair of (verified, unsupported) claim lists.

    Returned by :meth:`Verifier.verify_all` so the pipeline/UI can split the
    ledger without re-deriving status, each list holding claims annotated with
    their verdict (value untouched). Kept minimal — a named tuple-like holder —
    rather than a new public model.
    """

    __slots__ = ("verified", "unsupported", "results")

    def __init__(
        self,
        verified: list[Claim],
        unsupported: list[Claim],
        results: list[tuple[Claim, VerificationResult]],
    ) -> None:
        self.verified = verified
        self.unsupported = unsupported
        self.results = results


def verify_all(
    verifier: Verifier,
    claims: Iterable[Claim],
    store: EvidenceStore,
) -> _Verdicts:
    """Verify every claim, returning annotated (verified, unsupported) lists.

    Each claim is verified independently and :func:`annotate`-d with its verdict
    (status/reason set, value untouched). ``results`` pairs each annotated claim
    with its full :class:`VerificationResult` (both values) for the provenance
    and tamper views (Req 7.4, 13.2). No claim is dropped: a rejected claim is
    retained in ``unsupported`` with its reason (Req 7.3).
    """
    verified: list[Claim] = []
    unsupported: list[Claim] = []
    results: list[tuple[Claim, VerificationResult]] = []
    for claim in claims:
        result = verifier.verify(claim, store)
        annotated = annotate(claim, result)
        results.append((annotated, result))
        if result.status is ClaimStatus.VERIFIED:
            verified.append(annotated)
        else:
            unsupported.append(annotated)
    return _Verdicts(verified, unsupported, results)


__all__ = [
    "Verifier",
    "annotate",
    "verify_all",
    "SCORE_ABS_TOLERANCE",
]
