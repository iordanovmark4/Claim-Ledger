"""ConflictDetector: Single_Source_Dependence, Literature_Gap (Req 9).

Two named, order-independent conflict rules evaluated for a single
:class:`~claimledger.models.Association` against a :class:`~claimledger.config.Thresholds`
instance. ``detect`` is a pure function of ``(association, co_mention_count,
thresholds)``: given the same inputs it returns the same list of
:class:`~claimledger.models.ConflictFlag`, and re-running it with a *different*
``Thresholds`` re-evaluates both rules (Req 9.7). Because ``Thresholds`` is a
frozen dataclass, "the thresholds in force when flagged" is captured as a stable
copy on each flag (Req 9.8).

Order-independence (design "Conflict Detector", Property 5, Req 9.2)
--------------------------------------------------------------------
Neither rule depends on the order of ``association.datatype_scores``:

* ``Single_Source_Dependence`` counts how many datatypes reach
  ``support_threshold`` — a filter over an unordered multiset of ``(id, score)``.
  The verdict is ``count < min_supporting_datatypes`` and so depends only on the
  count, never the order. The counted datatypes are exposed sorted by ``id`` so
  the flag's ``supporting_datatypes`` is itself deterministic regardless of the
  input order (Req 9.5).
* ``Literature_Gap`` compares two scalars (``overall_score`` and
  ``co_mention_count``); there is no sequence to order.

Never-fabricate (steering rule 3, Req 9.3)
------------------------------------------
``Literature_Gap`` needs the association's ``Co_Mention_Count``, which comes from
a PubMed gene+disease query, not from the Open Targets :class:`Association`. It is
supplied to :meth:`ConflictDetector.detect` as an explicit ``co_mention_count``
parameter. When it is unavailable (``None``) the rule is simply *not evaluated*
for that association — no flag is fabricated.

NaN-safety
----------
An incomplete association may carry ``overall_score = float('nan')`` (see the OT
parser's missing-score sentinel). ``nan >= high_score_threshold`` is ``False``,
so a Literature_Gap is never spuriously raised for such a row. Datatype scores
are real floats, so the ``>=`` comparisons there are ordinary.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .config import Thresholds
from .models import Association, ConflictFlag

# The two rule names, used both on the flags and in the UI (Req 9.8).
SINGLE_SOURCE_DEPENDENCE = "Single_Source_Dependence"
LITERATURE_GAP = "Literature_Gap"


class ConflictDetector:
    """Evaluate the two named conflict rules for an association (Req 9.1-9.3, 9.7, 9.8).

    Stateless: it holds no thresholds of its own. Every evaluation is driven by
    the ``thresholds`` passed to :meth:`detect`, so re-running with a new
    ``Thresholds`` re-evaluates both rules against the updated values (Req 9.7).
    """

    def detect(
        self,
        association: Association,
        thresholds: Thresholds,
        co_mention_count: Optional[int] = None,
    ) -> list[ConflictFlag]:
        """Return every conflict flag raised for ``association`` (Req 9.1).

        Evaluates both rules and returns a list with zero, one, or two
        :class:`ConflictFlag` objects, in the fixed order
        ``[Single_Source_Dependence, Literature_Gap]`` so the result is
        deterministic.

        Parameters
        ----------
        association:
            The association to evaluate. Its ``datatype_scores`` drive
            ``Single_Source_Dependence`` and its ``overall_score`` drives
            ``Literature_Gap``.
        thresholds:
            The :class:`~claimledger.config.Thresholds` in force; captured on each
            raised flag (Req 9.8) and the sole source of the cut-offs (Req 9.7).
        co_mention_count:
            The association's PubMed Co_Mention_Count, supplied externally because
            it is not part of the OT :class:`Association`. ``None`` means the count
            is unavailable, and ``Literature_Gap`` is then skipped — never
            fabricated (steering rule 3, Req 9.3).
        """
        flags: list[ConflictFlag] = []

        ssd = self._single_source_dependence(association, thresholds)
        if ssd is not None:
            flags.append(ssd)

        gap = self._literature_gap(association, thresholds, co_mention_count)
        if gap is not None:
            flags.append(gap)

        return flags

    def detect_all(
        self,
        associations: tuple[Association, ...],
        thresholds: Thresholds,
        co_mention_counts: Optional[Mapping[str, int]] = None,
    ) -> list[ConflictFlag]:
        """Evaluate every association, returning all raised flags in input order.

        ``co_mention_counts`` maps ``disease_id`` -> Co_Mention_Count; a disease
        absent from the mapping (or a ``None`` mapping) has an unavailable count,
        so its ``Literature_Gap`` rule is skipped (Req 9.3). The flags are
        returned in the order the associations are supplied, with each
        association's own flags in the fixed
        ``[Single_Source_Dependence, Literature_Gap]`` order.
        """
        counts = co_mention_counts or {}
        out: list[ConflictFlag] = []
        for assoc in associations:
            out.extend(
                self.detect(assoc, thresholds, counts.get(assoc.disease_id))
            )
        return out

    @staticmethod
    def _single_source_dependence(
        association: Association, thresholds: Thresholds
    ) -> Optional[ConflictFlag]:
        """Single_Source_Dependence rule (Req 9.2, 9.5).

        Count the datatypes whose score reaches ``support_threshold``; flag when
        that count is below ``min_supporting_datatypes``. The counted datatypes
        are exposed sorted by ``id`` so the flag is order-independent (Req 9.5).
        """
        supporting = [
            d
            for d in association.datatype_scores
            if d.score >= thresholds.support_threshold
        ]
        supporting_count = len(supporting)

        if supporting_count >= thresholds.min_supporting_datatypes:
            return None

        # Sort the counted datatypes by id for a deterministic, order-independent
        # exposure the UI names one by one (Req 9.5).
        supporting_sorted = tuple(sorted(supporting, key=lambda d: d.id))

        return ConflictFlag(
            rule=SINGLE_SOURCE_DEPENDENCE,
            disease_id=association.disease_id,
            disease_name=association.disease_name,
            supporting_datatypes=supporting_sorted,
            supporting_count=supporting_count,
            thresholds=thresholds,
        )

    @staticmethod
    def _literature_gap(
        association: Association,
        thresholds: Thresholds,
        co_mention_count: Optional[int],
    ) -> Optional[ConflictFlag]:
        """Literature_Gap rule (Req 9.3).

        Skip entirely when ``co_mention_count`` is unavailable (``None``) — never
        fabricate a count (steering rule 3). Otherwise flag when the
        ``overall_score`` reaches ``high_score_threshold`` AND the
        ``co_mention_count`` is at or below ``low_co_mention_threshold``. A NaN
        ``overall_score`` fails ``>=`` and so is never spuriously flagged.
        """
        if co_mention_count is None:
            return None

        high = association.overall_score >= thresholds.high_score_threshold
        low = co_mention_count <= thresholds.low_co_mention_threshold
        if not (high and low):
            return None

        return ConflictFlag(
            rule=LITERATURE_GAP,
            disease_id=association.disease_id,
            disease_name=association.disease_name,
            overall_score=association.overall_score,
            co_mention_count=co_mention_count,
            thresholds=thresholds,
        )


__all__ = [
    "ConflictDetector",
    "SINGLE_SOURCE_DEPENDENCE",
    "LITERATURE_GAP",
]
