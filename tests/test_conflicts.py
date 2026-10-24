"""Example-based unit tests for the Conflict Detector (task 12.1, Req 9).

These pin the concrete behaviour of :meth:`ConflictDetector.detect` for the two
named, order-independent rules described in the design "Conflict Detector"
section:

Single_Source_Dependence (Req 9.2, 9.5)
  * flags when fewer than ``min_supporting_datatypes`` reach
    ``support_threshold`` (1 datatype >= 0.3 with the default minimum of 2 ->
    flag); does not flag when the count meets the minimum;
  * the flag exposes the counted supporting datatypes sorted by id and the
    count (Req 9.5);
  * the verdict and the exposed supporting set are invariant to the order of
    ``datatype_scores`` (order-independence; the property test 12.2 generalizes
    this).

Literature_Gap (Req 9.3)
  * flags when ``overall_score >= high_score_threshold`` AND
    ``co_mention_count <= low_co_mention_threshold``; does not flag otherwise;
  * is skipped entirely when ``co_mention_count`` is ``None`` (never fabricated,
    steering rule 3);
  * a NaN ``overall_score`` never spuriously flags.

Re-evaluation on threshold change (Req 9.7, 9.8)
  * the same association yields a different verdict under a different
    ``Thresholds``, and each flag records the thresholds in force.

Real fixture (Req 9.1)
  * at least one case drives the EGFR NSCLC association parsed from the
    committed real OT response via
    :func:`claimledger.clients.opentargets.parse_associations` (six datatypes
    >= 0.3 -> no Single_Source_Dependence; a synthetic co-mention count
    exercises Literature_Gap).
"""

from __future__ import annotations

from typing import Any

from claimledger.clients.opentargets import parse_associations
from claimledger.config import Thresholds
from claimledger.conflicts import (
    LITERATURE_GAP,
    SINGLE_SOURCE_DEPENDENCE,
    ConflictDetector,
)
from claimledger.models import Association, DatatypeScore

# Defaults mirror Thresholds: support 0.3, min datatypes 2, high score 0.5,
# low co-mention 100.
DEFAULTS = Thresholds()

EVIDENCE_ID = "ot_associations:deadbeefdeadbeef"


def _assoc(
    *,
    disease_id: str = "MONDO_0000001",
    disease_name: str = "example disease",
    overall_score: float = 0.9,
    datatype_scores: tuple[DatatypeScore, ...] = (),
) -> Association:
    """Build a minimal Association for a focused conflict case."""
    return Association(
        disease_id=disease_id,
        disease_name=disease_name,
        overall_score=overall_score,
        datatype_scores=datatype_scores,
        evidence_id=EVIDENCE_ID,
    )


# --------------------------------------------------------------------------- #
# Single_Source_Dependence (Req 9.2, 9.5)
# --------------------------------------------------------------------------- #


def test_single_source_flags_when_fewer_than_min_reach_support_threshold() -> None:
    """One datatype >= 0.3 with default min 2 -> Single_Source_Dependence (Req 9.2)."""
    assoc = _assoc(
        datatype_scores=(
            DatatypeScore(id="literature", score=0.95),  # >= 0.3
            DatatypeScore(id="clinical", score=0.10),     # < 0.3
            DatatypeScore(id="genetic_association", score=0.20),  # < 0.3
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=None)

    ssd = [f for f in flags if f.rule == SINGLE_SOURCE_DEPENDENCE]
    assert len(ssd) == 1
    assert ssd[0].disease_id == assoc.disease_id
    assert ssd[0].disease_name == assoc.disease_name


def test_single_source_no_flag_when_count_meets_minimum() -> None:
    """Two datatypes >= 0.3 with default min 2 -> no Single_Source_Dependence (Req 9.2)."""
    assoc = _assoc(
        datatype_scores=(
            DatatypeScore(id="literature", score=0.95),  # >= 0.3
            DatatypeScore(id="clinical", score=0.40),     # >= 0.3
            DatatypeScore(id="genetic_association", score=0.10),  # < 0.3
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=None)

    assert [f for f in flags if f.rule == SINGLE_SOURCE_DEPENDENCE] == []


def test_single_source_exposes_counted_datatypes_sorted_by_id_and_count() -> None:
    """The flag exposes the supporting datatypes sorted by id and the count (Req 9.5)."""
    # Three reach 0.3 (so default min 2 is met) would not flag; use a stricter
    # threshold so exactly two support and the minimum is three -> flag, with a
    # deterministic supporting set to assert the sort-by-id exposure.
    thresholds = Thresholds(support_threshold=0.3, min_supporting_datatypes=3)
    assoc = _assoc(
        datatype_scores=(
            DatatypeScore(id="somatic_mutation", score=0.80),  # >= 0.3
            DatatypeScore(id="affected_pathway", score=0.10),   # < 0.3
            DatatypeScore(id="clinical", score=0.50),           # >= 0.3
        ),
    )

    flags = ConflictDetector().detect(assoc, thresholds, co_mention_count=None)

    ssd = next(f for f in flags if f.rule == SINGLE_SOURCE_DEPENDENCE)
    assert ssd.supporting_count == 2
    # Exposed sorted by id: "clinical" before "somatic_mutation".
    assert [ds.id for ds in ssd.supporting_datatypes] == ["clinical", "somatic_mutation"]
    assert ssd.supporting_datatypes == (
        DatatypeScore(id="clinical", score=0.50),
        DatatypeScore(id="somatic_mutation", score=0.80),
    )
    # The thresholds in force are recorded on the flag (Req 9.8).
    assert ssd.thresholds == thresholds


def test_single_source_is_order_independent_in_verdict_and_supporting_set() -> None:
    """Reordering datatype_scores yields the same verdict and same supporting set."""
    scores = (
        DatatypeScore(id="somatic_mutation", score=0.80),
        DatatypeScore(id="affected_pathway", score=0.10),
        DatatypeScore(id="clinical", score=0.50),
    )
    shuffled = (scores[2], scores[0], scores[1])

    thresholds = Thresholds(support_threshold=0.3, min_supporting_datatypes=3)
    detector = ConflictDetector()

    flags_a = detector.detect(_assoc(datatype_scores=scores), thresholds)
    flags_b = detector.detect(_assoc(datatype_scores=shuffled), thresholds)

    ssd_a = next(f for f in flags_a if f.rule == SINGLE_SOURCE_DEPENDENCE)
    ssd_b = next(f for f in flags_b if f.rule == SINGLE_SOURCE_DEPENDENCE)

    # Same verdict (both flagged), same count, and the same ordered supporting
    # set (sorted by id) regardless of input order.
    assert ssd_a.supporting_count == ssd_b.supporting_count == 2
    assert ssd_a.supporting_datatypes == ssd_b.supporting_datatypes


# --------------------------------------------------------------------------- #
# Literature_Gap (Req 9.3)
# --------------------------------------------------------------------------- #


def test_literature_gap_flags_when_high_score_and_low_co_mention() -> None:
    """overall_score >= 0.5 AND co_mention_count <= 100 -> Literature_Gap (Req 9.3)."""
    assoc = _assoc(
        overall_score=0.85,
        datatype_scores=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.9),  # two supporters -> no SSD
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=42)

    gap = [f for f in flags if f.rule == LITERATURE_GAP]
    assert len(gap) == 1
    assert gap[0].overall_score == 0.85
    assert gap[0].co_mention_count == 42
    assert gap[0].thresholds == DEFAULTS


def test_literature_gap_no_flag_when_co_mention_above_threshold() -> None:
    """A high co_mention_count (> 100) does not raise Literature_Gap (Req 9.3)."""
    assoc = _assoc(
        overall_score=0.85,
        datatype_scores=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.9),
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=5000)

    assert [f for f in flags if f.rule == LITERATURE_GAP] == []


def test_literature_gap_no_flag_when_score_below_threshold() -> None:
    """A low overall_score (< 0.5) does not raise Literature_Gap (Req 9.3)."""
    assoc = _assoc(
        overall_score=0.40,
        datatype_scores=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.9),
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=5)

    assert [f for f in flags if f.rule == LITERATURE_GAP] == []


def test_literature_gap_skipped_when_co_mention_unavailable() -> None:
    """co_mention_count=None skips Literature_Gap entirely -- never fabricated (Req 9.3)."""
    assoc = _assoc(
        overall_score=0.95,  # clearly high
        datatype_scores=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.9),
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=None)

    assert [f for f in flags if f.rule == LITERATURE_GAP] == []


def test_literature_gap_nan_overall_score_does_not_spuriously_flag() -> None:
    """A NaN overall_score fails >= high_score_threshold, so no Literature_Gap."""
    assoc = _assoc(
        overall_score=float("nan"),
        datatype_scores=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.9),
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=0)

    assert [f for f in flags if f.rule == LITERATURE_GAP] == []


# --------------------------------------------------------------------------- #
# Re-evaluation on threshold change (Req 9.7, 9.8)
# --------------------------------------------------------------------------- #


def test_threshold_change_re_evaluates_to_a_different_verdict() -> None:
    """The same association flips Single_Source_Dependence when the minimum changes (Req 9.7)."""
    assoc = _assoc(
        datatype_scores=(
            DatatypeScore(id="literature", score=0.95),  # >= 0.3
            DatatypeScore(id="clinical", score=0.40),     # >= 0.3
            DatatypeScore(id="genetic_association", score=0.10),  # < 0.3
        ),
    )
    detector = ConflictDetector()

    # Default min is 2: two supporters meet it -> no flag.
    at_default = detector.detect(assoc, DEFAULTS, co_mention_count=None)
    assert [f for f in at_default if f.rule == SINGLE_SOURCE_DEPENDENCE] == []

    # Raise the minimum to 3: two supporters now fall short -> flag.
    stricter = Thresholds(min_supporting_datatypes=3)
    at_stricter = detector.detect(assoc, stricter, co_mention_count=None)
    ssd = [f for f in at_stricter if f.rule == SINGLE_SOURCE_DEPENDENCE]
    assert len(ssd) == 1
    assert ssd[0].supporting_count == 2
    assert ssd[0].thresholds == stricter


def test_detect_returns_both_rules_in_fixed_order_when_both_fire() -> None:
    """When both rules fire, Single_Source_Dependence precedes Literature_Gap."""
    assoc = _assoc(
        overall_score=0.9,  # high -> Literature_Gap eligible
        datatype_scores=(
            DatatypeScore(id="literature", score=0.95),  # only one supporter -> SSD
        ),
    )

    flags = ConflictDetector().detect(assoc, DEFAULTS, co_mention_count=10)

    assert [f.rule for f in flags] == [SINGLE_SOURCE_DEPENDENCE, LITERATURE_GAP]


# --------------------------------------------------------------------------- #
# Real EGFR fixture via parse_associations (Req 9.1)
# --------------------------------------------------------------------------- #


def test_egfr_nsclc_from_real_fixture_no_single_source_but_literature_gap(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """EGFR/NSCLC from the committed real OT response: no SSD; a synthetic low
    co-mention count exercises Literature_Gap (Req 9.1, 9.2, 9.3)."""
    parsed = parse_associations(
        ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID
    )
    nsclc = parsed.associations[0]
    # Sanity: six datatype scores, all comfortably >= 0.3 at default threshold.
    assert len(nsclc.datatype_scores) == 6
    assert all(ds.score >= DEFAULTS.support_threshold for ds in nsclc.datatype_scores)
    assert nsclc.overall_score >= DEFAULTS.high_score_threshold

    detector = ConflictDetector()

    # Six supporters >= 0.3 easily clears the minimum of 2 -> no SSD flag.
    no_gap = detector.detect(nsclc, DEFAULTS, co_mention_count=None)
    assert [f for f in no_gap if f.rule == SINGLE_SOURCE_DEPENDENCE] == []
    # co-mention unavailable -> Literature_Gap skipped.
    assert [f for f in no_gap if f.rule == LITERATURE_GAP] == []

    # Feed a synthetic low co-mention count: high overall_score + low count -> gap.
    with_gap = detector.detect(nsclc, DEFAULTS, co_mention_count=7)
    rules = {f.rule for f in with_gap}
    assert SINGLE_SOURCE_DEPENDENCE not in rules
    assert LITERATURE_GAP in rules
    gap = next(f for f in with_gap if f.rule == LITERATURE_GAP)
    assert gap.disease_id == nsclc.disease_id
    assert gap.co_mention_count == 7
