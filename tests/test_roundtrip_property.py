"""Property 7: Parse round-trip (pytest + Hypothesis).

**Validates: Requirements 12.6**

Serialization is the only way a Claim leaves the process (the JSON export, task
14.1) and re-enters it (``Claim.from_dict`` on a loaded payload). For the export
to be a faithful artifact, that round-trip must be *lossless*: no field dropped,
no type silently widened or narrowed, no tuple returning as a list. This module
pins that invariant directly against :meth:`Claim.to_dict`/``from_dict``,
:meth:`DatatypeScore.to_dict`/``from_dict``, and the JSON produced by
:meth:`Exporter.export_claims_json`.

A single reusable strategy (:func:`claims`) generates arbitrary *valid* claims
across all five :class:`ClaimType` values with type-appropriate values:

* ``ASSOCIATION`` — a float in ``[0, 1]`` (the OT Overall_Score domain);
* ``DATATYPE_BREAKDOWN`` — a tuple of :class:`DatatypeScore` with unique ids
  drawn from the fixed OT datatype set, scores in ``[0, 1]`` (0..6 entries, may
  be empty);
* ``LITERATURE_COUNT`` / ``CO_MENTION_COUNT`` — a non-negative int;
* ``RECENT_PAPER`` — a digit-string PMID.

Every claim carries a ``claim_id``, a ``subject`` (``GENE`` or
``GENE::DISEASE``), a non-empty tuple of evidence-id strings, a status of
``VERIFIED`` or ``UNSUPPORTED`` (with a ``reason`` string when UNSUPPORTED, else
``None``), and — sometimes — a type-appropriate ``original_value`` (the
tamper/reset field), ``None`` otherwise.

The four properties asserted:

1. **Claim round-trip** — ``Claim.from_dict(c.to_dict()) == c`` (deep equality;
   a DATATYPE_BREAKDOWN tuple reconstructs equal, order included).
2. **JSON round-trip through the exporter** — verified + unsupported lists ->
   JSON string -> ``json.loads`` -> ``Claim.from_dict`` on each item yields the
   same claims; compared order-insensitively (as Counters) because the exporter
   sorts.
3. **DatatypeScore round-trip** — ``DatatypeScore.from_dict(ds.to_dict()) == ds``.
4. **Idempotent serialization** — ``to_dict(from_dict(to_dict(c))) == to_dict(c)``.
"""

from __future__ import annotations

import json
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.export import Exporter
from claimledger.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
)

# The fixed set of OT evidence-datatype ids (models.DatatypeScore docstring /
# design). A breakdown draws a *unique* subset so it is unambiguous by id.
_DATATYPE_IDS = (
    "clinical",
    "genetic_literature",
    "affected_pathway",
    "literature",
    "genetic_association",
    "somatic_mutation",
)

# Scores are finite floats in [0, 1] — the genuine OT score domain (see
# data/cache/preflight_ot_associations.json). NaN/inf are excluded because they
# are outside the real domain AND because NaN != NaN would make equality
# comparison meaningless for a round-trip assertion, not a real counterexample.
_scores = st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)

# Non-negative ints: the LITERATURE_COUNT / CO_MENTION_COUNT domain.
_counts = st.integers(min_value=0, max_value=10_000_000)

# PMIDs: non-empty digit strings, as in esearchresult.idlist.
_pmids = st.text(alphabet="0123456789", min_size=1, max_size=9)

# Id-like strings for claim_id / evidence_ids / subject tokens.
_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_:-",
    min_size=1,
    max_size=24,
)


# --------------------------------------------------------------------------- #
# DatatypeScore strategy (Property 3).                                        #
# --------------------------------------------------------------------------- #
datatype_scores = st.builds(DatatypeScore, id=st.sampled_from(_DATATYPE_IDS), score=_scores)


@st.composite
def datatype_breakdowns(draw: st.DrawFn) -> tuple:
    """Draw a DATATYPE_BREAKDOWN value: 0..6 DatatypeScore with unique ids.

    May be empty. Ids are unique within the tuple so the breakdown is
    unambiguous by id; order is whatever is drawn and is preserved through the
    round-trip (the tuple is canonical/ordered).
    """
    n = draw(st.integers(min_value=0, max_value=len(_DATATYPE_IDS)))
    chosen_ids = draw(
        st.lists(st.sampled_from(_DATATYPE_IDS), min_size=n, max_size=n, unique=True)
    )
    return tuple(DatatypeScore(id=dt_id, score=draw(_scores)) for dt_id in chosen_ids)


def _value_strategy_for(claim_type: ClaimType) -> st.SearchStrategy:
    """A type-appropriate value strategy for ``claim_type``."""
    if claim_type is ClaimType.ASSOCIATION:
        return _scores
    if claim_type is ClaimType.DATATYPE_BREAKDOWN:
        return datatype_breakdowns()
    if claim_type in (ClaimType.LITERATURE_COUNT, ClaimType.CO_MENTION_COUNT):
        return _counts
    # RECENT_PAPER
    return _pmids


# --------------------------------------------------------------------------- #
# Claim strategy (Properties 1, 2, 4) — all five types, both statuses.        #
# --------------------------------------------------------------------------- #
@st.composite
def claims(draw: st.DrawFn) -> Claim:
    """Draw an arbitrary valid :class:`Claim` across all five claim types.

    The value matches the chosen ``claim_type``; the ``subject`` is a bare gene
    token or a ``GENE::DISEASE`` key; ``evidence_ids`` is a non-empty tuple of id
    strings; status is VERIFIED or UNSUPPORTED (with a ``reason`` iff
    UNSUPPORTED); and ``original_value`` is sometimes a second type-appropriate
    value (the tamper/reset field), ``None`` otherwise.
    """
    claim_type = draw(st.sampled_from(list(ClaimType)))
    value = draw(_value_strategy_for(claim_type))

    gene = draw(_ids)
    subject = draw(
        st.one_of(
            st.just(gene),
            st.builds(lambda g, d: f"{g}::{d}", st.just(gene), _ids),
        )
    )

    evidence_ids = tuple(draw(st.lists(_ids, min_size=1, max_size=4)))

    status = draw(st.sampled_from([ClaimStatus.VERIFIED, ClaimStatus.UNSUPPORTED]))
    reason = draw(st.text(min_size=1, max_size=80)) if status is ClaimStatus.UNSUPPORTED else None

    # original_value: sometimes set to a type-appropriate value, else None.
    original_value = draw(
        st.one_of(st.none(), _value_strategy_for(claim_type))
    )

    return Claim(
        claim_id=draw(_ids),
        claim_type=claim_type,
        subject=subject,
        value=value,
        evidence_ids=evidence_ids,
        status=status,
        reason=reason,
        original_value=original_value,
    )


# --------------------------------------------------------------------------- #
# Property 1 — Claim round-trip: from_dict(to_dict(c)) == c.                   #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(claim=claims())
def test_claim_round_trip(claim: Claim) -> None:
    """``Claim.from_dict(c.to_dict()) == c`` for any generated claim (Req 12.6).

    Deep equality follows from frozen-dataclass value semantics; for
    DATATYPE_BREAKDOWN the reconstructed tuple of DatatypeScore must equal the
    original, including order as stored.
    """
    restored = Claim.from_dict(claim.to_dict())
    assert restored == claim
    # Explicit breakdown deep-check: tuple of DatatypeScore, order preserved.
    if claim.claim_type is ClaimType.DATATYPE_BREAKDOWN:
        assert isinstance(restored.value, tuple)
        assert all(isinstance(ds, DatatypeScore) for ds in restored.value)
        assert restored.value == claim.value


# --------------------------------------------------------------------------- #
# Property 2 — JSON round-trip through the exporter (order-insensitive).      #
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(
    verified=st.lists(claims(), max_size=6),
    unsupported=st.lists(claims(), max_size=6),
    gene_symbol=st.one_of(st.none(), _ids),
)
def test_json_round_trip_through_exporter(verified, unsupported, gene_symbol) -> None:
    """Export -> json.loads -> from_dict reconstructs the input claims (Req 12.6).

    The exporter sorts claims deterministically, so the reconstructed claims are
    compared to the inputs as multisets (``Counter``) — claims are frozen and
    hashable — rather than by position.
    """
    exporter = Exporter()
    document = json.loads(
        exporter.export_claims_json(verified, unsupported, gene_symbol=gene_symbol)
    )

    restored_verified = [Claim.from_dict(d) for d in document["verified"]]
    restored_unsupported = [Claim.from_dict(d) for d in document["unsupported"]]

    assert Counter(restored_verified) == Counter(verified)
    assert Counter(restored_unsupported) == Counter(unsupported)

    if gene_symbol is not None:
        assert document["gene"] == gene_symbol
    else:
        assert "gene" not in document


# --------------------------------------------------------------------------- #
# Property 3 — DatatypeScore round-trip.                                      #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(ds=datatype_scores)
def test_datatype_score_round_trip(ds: DatatypeScore) -> None:
    """``DatatypeScore.from_dict(ds.to_dict()) == ds`` (Req 12.6)."""
    assert DatatypeScore.from_dict(ds.to_dict()) == ds


# --------------------------------------------------------------------------- #
# Property 4 — Idempotent serialization.                                      #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(claim=claims())
def test_serialization_is_idempotent(claim: Claim) -> None:
    """``to_dict(from_dict(to_dict(c))) == to_dict(c)`` (Req 12.6).

    The serialized form is stable under a second round-trip: once a claim has
    been through ``to_dict`` once, re-parsing and re-serializing reproduces the
    identical dict (and it survives a JSON string round-trip too).
    """
    once = claim.to_dict()
    twice = Claim.from_dict(once).to_dict()
    assert twice == once
    # And it is genuinely JSON-stable (no non-serializable residue).
    assert json.loads(json.dumps(once)) == once
