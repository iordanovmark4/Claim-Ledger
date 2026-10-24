"""Property 5: Conflict symmetry and order-independence (pytest + Hypothesis).

**Validates: Requirements 9.2, 9.3**

The two named conflict rules (design "Conflict Detector", Req 9.1-9.3) are pure,
order-independent verdicts over a single
:class:`~claimledger.models.Association` evaluated against a
:class:`~claimledger.config.Thresholds`:

* ``Single_Source_Dependence`` counts how many ``datatype_scores`` reach the
  ``support_threshold`` and flags when that count is below
  ``min_supporting_datatypes`` — a filter over an *unordered* multiset of
  ``(id, score)``. Its verdict, ``supporting_count`` and ``supporting_datatypes``
  (keyed by id) must therefore be invariant to the order of
  ``association.datatype_scores`` (Req 9.2).
* ``Literature_Gap`` compares two scalars (``overall_score`` and
  ``co_mention_count``) against their thresholds, is skipped entirely when the
  Co_Mention_Count is unavailable (``None``) and is never fabricated (Req 9.3).

These properties pin each rule against an *independent* oracle that recomputes
the verdict with the SAME ``>=``/``<=`` comparisons and the SAME threshold
values the implementation uses, so float boundary equality is consistent with no
separate epsilon.
"""

from __future__ import annotations

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.conflicts import (
    LITERATURE_GAP,
    SINGLE_SOURCE_DEPENDENCE,
    ConflictDetector,
)
from claimledger.config import Thresholds
from claimledger.models import Association, ConflictFlag, DatatypeScore
from tests.strategies import ot_scores

# The fixed OT evidence-datatype id set (models.DatatypeScore docstring). A
# datatype_scores tuple draws a *unique* subset so a breakdown keyed by id is
# unambiguous — mirrors tests/strategies._DATATYPE_IDS.
_DATATYPE_IDS = (
    "clinical",
    "genetic_literature",
    "affected_pathway",
    "literature",
    "genetic_association",
    "somatic_mutation",
)

# Co_Mention_Count domain: None (count unavailable -> Literature_Gap skipped) or
# a non-negative int. Including None keeps the skip path in the input space.
co_mention_counts = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=10_000_000),
)


@st.composite
def thresholds_strat(draw: st.DrawFn) -> Thresholds:
    """Draw a :class:`Thresholds` spanning the conflict-rule cut-off domain.

    ``support_threshold`` and ``high_score_threshold`` are floats in [0, 1] (the
    score domain), ``min_supporting_datatypes`` is 0..6 (there are six OT
    datatypes) and ``low_co_mention_threshold`` is a non-negative int. ``top_n``
    and ``retmax`` keep their defaults — the conflict rules never read them.
    """
    return Thresholds(
        support_threshold=draw(ot_scores),
        min_supporting_datatypes=draw(st.integers(min_value=0, max_value=6)),
        high_score_threshold=draw(ot_scores),
        low_co_mention_threshold=draw(st.integers(min_value=0, max_value=10_000_000)),
    )


@st.composite
def datatype_score_tuples(draw: st.DrawFn) -> tuple[DatatypeScore, ...]:
    """Draw a variable-length tuple of DatatypeScore with UNIQUE ids.

    0..6 entries, each id a distinct member of the fixed OT datatype set and each
    score a float in [0, 1]. Unique ids keep ``supporting_datatypes`` keyed by id
    unambiguous.
    """
    n = draw(st.integers(min_value=0, max_value=len(_DATATYPE_IDS)))
    chosen_ids = draw(
        st.lists(
            st.sampled_from(_DATATYPE_IDS),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return tuple(DatatypeScore(id=dt_id, score=draw(ot_scores)) for dt_id in chosen_ids)


@st.composite
def associations(draw: st.DrawFn) -> Association:
    """Draw an :class:`Association` with an in-[0,1] overall score and datatypes."""
    return Association(
        disease_id=draw(
            st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", min_size=1, max_size=12)
        ),
        disease_name=draw(st.text(min_size=0, max_size=20)),
        overall_score=draw(ot_scores),
        datatype_scores=draw(datatype_score_tuples()),
        evidence_id="synthetic-evidence-id",
    )


def _flags_by_rule(flags: list[ConflictFlag]) -> dict[str, ConflictFlag]:
    """Index a detect() result by rule name (rules are unique per association)."""
    return {f.rule: f for f in flags}


# --------------------------------------------------------------------------- #
# Independent oracles (same comparisons / thresholds as the implementation).   #
# --------------------------------------------------------------------------- #

def _oracle_ssd_supporting_ids(
    association: Association, thresholds: Thresholds
) -> set[str]:
    """Ids whose score reaches support_threshold (SAME >= comparison)."""
    return {
        d.id
        for d in association.datatype_scores
        if d.score >= thresholds.support_threshold
    }


def _oracle_literature_gap_fires(
    association: Association,
    thresholds: Thresholds,
    co_mention_count: Optional[int],
) -> bool:
    """Recompute the Literature_Gap boolean with the SAME comparisons/thresholds."""
    if co_mention_count is None:
        return False
    high = association.overall_score >= thresholds.high_score_threshold
    low = co_mention_count <= thresholds.low_co_mention_threshold
    return high and low


# --------------------------------------------------------------------------- #
# Property 5.1 — Single_Source_Dependence is order-independent.                 #
# --------------------------------------------------------------------------- #

@settings(max_examples=200)
@given(
    data=st.data(),
    association=associations(),
    thresholds=thresholds_strat(),
    co_mention_count=co_mention_counts,
)
def test_single_source_dependence_order_independent(
    data: st.DataObject,
    association: Association,
    thresholds: Thresholds,
    co_mention_count: Optional[int],
) -> None:
    """Permuting datatype_scores never changes the SSD verdict/count/supporting set.

    detect() on the original association and on one whose datatype_scores are an
    arbitrary permutation must agree on whether Single_Source_Dependence fires,
    on ``supporting_count``, and on the ``supporting_datatypes`` set keyed by id.
    Validates Requirement 9.2.
    """
    detector = ConflictDetector()

    permuted_scores = data.draw(st.permutations(association.datatype_scores))
    permuted = Association(
        disease_id=association.disease_id,
        disease_name=association.disease_name,
        overall_score=association.overall_score,
        datatype_scores=tuple(permuted_scores),
        evidence_id=association.evidence_id,
        incomplete=association.incomplete,
    )

    original = _flags_by_rule(detector.detect(association, thresholds, co_mention_count))
    reordered = _flags_by_rule(detector.detect(permuted, thresholds, co_mention_count))

    # Same flagged/not-flagged verdict for Single_Source_Dependence.
    assert (SINGLE_SOURCE_DEPENDENCE in original) == (SINGLE_SOURCE_DEPENDENCE in reordered)

    if SINGLE_SOURCE_DEPENDENCE in original:
        o = original[SINGLE_SOURCE_DEPENDENCE]
        r = reordered[SINGLE_SOURCE_DEPENDENCE]
        assert o.supporting_count == r.supporting_count
        assert {d.id for d in o.supporting_datatypes} == {d.id for d in r.supporting_datatypes}


# --------------------------------------------------------------------------- #
# Property 5.2 — detect() is fully deterministic under datatype reordering.     #
# --------------------------------------------------------------------------- #

@settings(max_examples=200)
@given(
    data=st.data(),
    association=associations(),
    thresholds=thresholds_strat(),
    co_mention_count=co_mention_counts,
)
def test_detect_is_deterministic_under_reordering(
    data: st.DataObject,
    association: Association,
    thresholds: Thresholds,
    co_mention_count: Optional[int],
) -> None:
    """The whole flag list is invariant to datatype ordering.

    Same set of rule names, and for every rule the same ``supporting_count`` and
    the same ``overall_score``/``co_mention_count`` scalar fields, regardless of
    the order of ``datatype_scores``. Validates Requirements 9.2, 9.3.
    """
    detector = ConflictDetector()

    permuted_scores = data.draw(st.permutations(association.datatype_scores))
    permuted = Association(
        disease_id=association.disease_id,
        disease_name=association.disease_name,
        overall_score=association.overall_score,
        datatype_scores=tuple(permuted_scores),
        evidence_id=association.evidence_id,
        incomplete=association.incomplete,
    )

    original = _flags_by_rule(detector.detect(association, thresholds, co_mention_count))
    reordered = _flags_by_rule(detector.detect(permuted, thresholds, co_mention_count))

    assert set(original) == set(reordered)
    for rule, o in original.items():
        r = reordered[rule]
        assert o.supporting_count == r.supporting_count
        assert o.overall_score == r.overall_score
        assert o.co_mention_count == r.co_mention_count


# --------------------------------------------------------------------------- #
# Property 5.3 — Literature_Gap depends only on the two scalars.                #
# --------------------------------------------------------------------------- #

@settings(max_examples=200)
@given(
    association=associations(),
    thresholds=thresholds_strat(),
    co_mention_count=co_mention_counts,
)
def test_literature_gap_matches_scalar_oracle(
    association: Association,
    thresholds: Thresholds,
    co_mention_count: Optional[int],
) -> None:
    """Literature_Gap fires iff the independent two-scalar oracle says so.

    The flag fires exactly when ``overall_score >= high_score_threshold`` AND
    ``co_mention_count is not None`` AND ``co_mention_count <=
    low_co_mention_threshold``; it is skipped iff the count is None (never
    fabricated). The oracle uses the same comparisons/thresholds so float
    boundary equality is consistent. Validates Requirement 9.3.
    """
    detector = ConflictDetector()
    flags = _flags_by_rule(detector.detect(association, thresholds, co_mention_count))

    expected_fires = _oracle_literature_gap_fires(association, thresholds, co_mention_count)
    assert (LITERATURE_GAP in flags) == expected_fires

    # Never-fabricate: when the count is unavailable the rule is simply skipped.
    if co_mention_count is None:
        assert LITERATURE_GAP not in flags

    if LITERATURE_GAP in flags:
        gap = flags[LITERATURE_GAP]
        # The flag carries exactly the two scalars it was decided from.
        assert gap.overall_score == association.overall_score
        assert gap.co_mention_count == co_mention_count


# --------------------------------------------------------------------------- #
# Property 5.4 — Single_Source_Dependence matches the count-based oracle.       #
# --------------------------------------------------------------------------- #

@settings(max_examples=200)
@given(
    association=associations(),
    thresholds=thresholds_strat(),
    co_mention_count=co_mention_counts,
)
def test_single_source_dependence_matches_count_oracle(
    association: Association,
    thresholds: Thresholds,
    co_mention_count: Optional[int],
) -> None:
    """SSD fires iff fewer than min_supporting_datatypes reach support_threshold.

    When it fires, ``supporting_datatypes`` (by id) equals exactly the set of
    datatypes meeting the threshold and ``supporting_count`` equals that set's
    size. The oracle recomputes the supporting set with the same ``>=``
    comparison/threshold value. Validates Requirement 9.2.
    """
    detector = ConflictDetector()
    flags = _flags_by_rule(detector.detect(association, thresholds, co_mention_count))

    supporting_ids = _oracle_ssd_supporting_ids(association, thresholds)
    expected_fires = len(supporting_ids) < thresholds.min_supporting_datatypes
    assert (SINGLE_SOURCE_DEPENDENCE in flags) == expected_fires

    if SINGLE_SOURCE_DEPENDENCE in flags:
        ssd = flags[SINGLE_SOURCE_DEPENDENCE]
        assert {d.id for d in ssd.supporting_datatypes} == supporting_ids
        assert ssd.supporting_count == len(supporting_ids)
