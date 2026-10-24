"""DossierRenderer: deterministic Markdown from Verified_Claims (Req 8, 10).

``DossierRenderer.render(verified_claims, *, context...) -> str`` produces the
dossier Markdown. It is a **pure function of its inputs** with **no language
model** at runtime (Req 8.4, steering rule 5): every line is plain string
templating over the supplied claims.

What it renders (and what it refuses to)
----------------------------------------
* **Verified claims only (Req 8.1, 7.2, steering rule 4).** The renderer filters
  defensively to ``status == VERIFIED``; any claim that is not Verified is
  excluded from the prose. An Unsupported claim can never contribute a single
  character to the narrative (Req 7.2) — Unsupported handling lives in the UI
  ledger, not here.
* **Deterministic order (Req 8.2, 8.3).** Claims are sorted by the fixed
  composite key from the design's "Deterministic ordering" section::

      sort_key = (disease_rank, claim_type_rank, disease_id, claim_id)

  Nothing depends on dict/set iteration or input order, so the same set of
  Verified_Claims yields a byte-identical string under any permutation.
* **Approved wording (Req 10.1, 10.2, 8.5; steering rule 9).** Gene-disease
  relationships are described only as "associated with" / "reported in". An
  Overall_Score is always labeled an *evidence-strength score*, never a
  probability or a likelihood. The forbidden terms "causes", "proves",
  "validated", "promising" never appear in the generated prose.

Ordering keys (documented, fixed)
---------------------------------
``disease_rank``
    Per-disease claims rank by the disease's ASSOCIATION Overall_Score,
    *descending* (the strongest-evidence disease first), tie-broken by
    ``disease_id`` ascending. The ranking is derived from the ASSOCIATION claims
    themselves (their value is the Overall_Score, their subject carries the
    disease id), so the renderer needs no external score map. A gene-scoped
    claim with no disease (``LITERATURE_COUNT``, and any ``RECENT_PAPER`` whose
    subject is the bare gene) is given rank ``+inf`` so the whole gene-level
    section sorts **after** all per-disease sections — a single, documented,
    stable position.
``claim_type_rank``
    A fixed ordinal over the five claim types, chosen so each disease reads
    headline-first: ASSOCIATION, then DATATYPE_BREAKDOWN, then CO_MENTION_COUNT,
    then LITERATURE_COUNT, then RECENT_PAPER.
``disease_id`` / ``claim_id``
    Final lexicographic tie-breakers, so two claims that are otherwise equal
    still order deterministically.

The wording guard (Req 10.2)
----------------------------
After the Markdown is built, :func:`_assert_no_forbidden_wording` scans the
generated text for the forbidden terms as **whole words** (case-insensitive,
Unicode word boundaries) and raises :class:`ForbiddenWordingError` if any is
present. Using word boundaries means a forbidden term that happens to appear as
a *substring* of a legitimate token (a disease name, say) does not trip the
guard, while the forbidden term standing alone as a word in our prose always
does. The guard is a defensive backstop: the templates never emit these words,
so it firing at all signals a bug — surfaced as an exception rather than shipped
to the screen.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Optional

from .claims import SUBJECT_SEP
from .models import Claim, ClaimStatus, ClaimType, DatatypeScore

# --------------------------------------------------------------------------- #
# Approved / forbidden wording (Req 10.1, 10.2; steering rule 9)              #
# --------------------------------------------------------------------------- #

# Terms that must never appear as standalone words in generated prose
# (Req 10.2, steering rule 9). Matched case-insensitively on whole-word
# boundaries by the render-time guard below.
FORBIDDEN_TERMS: tuple[str, ...] = ("causes", "proves", "validated", "promising")

# Precompiled whole-word, case-insensitive matcher for the forbidden terms. The
# ``\b`` boundaries ensure a forbidden term embedded inside a longer token (a
# disease/gene name) is not a false positive, while the term as its own word is
# always caught.
_FORBIDDEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in FORBIDDEN_TERMS) + r")\b",
    re.IGNORECASE,
)

# Fixed ordinal over the five claim types for ``claim_type_rank`` (documented in
# the module docstring). Any type not present defaults after the known ones.
_CLAIM_TYPE_RANK: dict[ClaimType, int] = {
    ClaimType.ASSOCIATION: 0,
    ClaimType.DATATYPE_BREAKDOWN: 1,
    ClaimType.CO_MENTION_COUNT: 2,
    ClaimType.LITERATURE_COUNT: 3,
    ClaimType.RECENT_PAPER: 4,
}

# The mandatory safety notice. Primarily a UI concern (Req 10.4), included in the
# dossier header so an exported dossier never stands alone without it.
SAFETY_NOTICE = "Evidence summary, not medical advice"


class ForbiddenWordingError(AssertionError):
    """Raised when generated prose contains a forbidden term (Req 10.2).

    A subclass of :class:`AssertionError` because it is an internal invariant
    failure (a renderer bug), not a user-input error. Carries the offending
    terms so the bug is diagnosable.
    """

    def __init__(self, found: Iterable[str]) -> None:
        self.found = tuple(found)
        super().__init__(
            "rendered dossier contains forbidden wording "
            f"{list(self.found)!r}; approved phrasing is 'associated with' / "
            "'reported in' and an Overall_Score is an evidence-strength score"
        )


def _assert_no_forbidden_wording(text: str) -> None:
    """Raise :class:`ForbiddenWordingError` if ``text`` holds a forbidden word.

    Scans for the forbidden terms as whole words (case-insensitive). This is the
    render-time guard of Req 10.2: it never rewrites the text, it only refuses to
    emit prose that violates the wording rules.
    """
    found = sorted({m.group(0).lower() for m in _FORBIDDEN_RE.finditer(text)})
    if found:
        raise ForbiddenWordingError(found)


# --------------------------------------------------------------------------- #
# Subject helpers                                                             #
# --------------------------------------------------------------------------- #
def _disease_id_of(claim: Claim) -> str:
    """Disease id from a ``GENE::DISEASE`` subject, or ``""`` for gene-scoped.

    ASSOCIATION / DATATYPE_BREAKDOWN / CO_MENTION_COUNT claims carry a
    ``GENE::DISEASE`` subject; the disease id is everything after the first
    ``"::"``. A gene-scoped claim (LITERATURE_COUNT, or a RECENT_PAPER whose
    subject is the bare gene) has no separator and yields ``""``.
    """
    _, sep, disease_id = claim.subject.partition(SUBJECT_SEP)
    return disease_id if sep else ""


def _is_gene_scoped(claim: Claim) -> bool:
    """True iff the claim has no disease (its subject carries no ``"::"``)."""
    return SUBJECT_SEP not in claim.subject


# --------------------------------------------------------------------------- #
# Number formatting (deterministic, locale-independent)                       #
# --------------------------------------------------------------------------- #
def _fmt_score(score: float) -> str:
    """Format an evidence-strength score to a stable 4-decimal string.

    A fixed precision keeps output byte-identical across runs/platforms and
    reads as a score, not a probability. Trailing-zero trimming is deliberately
    avoided so ``0.8500`` and ``0.85`` never render two different ways for the
    same value.
    """
    return f"{float(score):.4f}"


def _fmt_count(count: int) -> str:
    """Format an integer count with thousands separators (e.g. ``18,950``)."""
    return f"{int(count):,}"


# --------------------------------------------------------------------------- #
# DossierRenderer                                                             #
# --------------------------------------------------------------------------- #
class DossierRenderer:
    """Deterministic, model-free Markdown renderer for Verified_Claims.

    Stateless: a single instance can render any number of claim sets. All inputs
    arrive through :meth:`render`; there is no hidden state, so the output is a
    pure function of the arguments (design "Dossier Renderer").
    """

    def render(
        self,
        verified_claims: Iterable[Claim],
        *,
        gene_symbol: str,
        disease_names: Optional[Mapping[str, str]] = None,
        include_safety_notice: bool = True,
    ) -> str:
        """Render the dossier Markdown for ``gene_symbol`` from verified claims.

        Parameters
        ----------
        verified_claims:
            The claims to render. Filtered defensively to ``VERIFIED`` — any
            non-Verified claim is excluded from the prose (Req 7.2, 8.1). Input
            order is irrelevant: claims are re-sorted by the fixed composite key
            (Req 8.2, 8.3).
        gene_symbol:
            The gene the dossier is about; the subject of the narrative.
        disease_names:
            Optional ``disease_id -> display name`` map. A disease id with no
            entry falls back to the id itself, so a missing name degrades
            gracefully (and deterministically) rather than raising.
        include_safety_notice:
            When True (default) the mandatory "Evidence summary, not medical
            advice" notice is placed in the header (Req 10.4); never contradicted.

        Returns
        -------
        str
            The dossier Markdown. Guaranteed to contain none of the forbidden
            terms as whole words (Req 10.2) — the render-time guard raises
            :class:`ForbiddenWordingError` instead of returning offending text.
        """
        names = dict(disease_names) if disease_names else {}

        # Defensive filter: Verified only. An Unsupported claim never reaches the
        # prose (Req 7.2, 8.1, steering rule 4).
        verified = [c for c in verified_claims if c.status is ClaimStatus.VERIFIED]

        disease_rank = self._disease_ranks(verified)
        ordered = sorted(verified, key=lambda c: self._sort_key(c, disease_rank))

        lines: list[str] = []
        lines.append(f"# Evidence dossier: {gene_symbol}")
        lines.append("")
        if include_safety_notice:
            lines.append(f"> {SAFETY_NOTICE}.")
            lines.append("")

        # Group the ordered claims by disease (gene-scoped group last), each
        # group already internally ordered by claim_type_rank then claim_id.
        grouped = self._group_in_order(ordered)

        if not grouped:
            lines.append(
                f"No verified claims are available for {gene_symbol}."
            )
        else:
            for disease_id, group in grouped:
                lines.extend(
                    self._render_group(gene_symbol, disease_id, group, names)
                )

        text = "\n".join(lines).rstrip("\n") + "\n"

        # Render-time wording guard (Req 10.2): refuse to emit forbidden prose.
        _assert_no_forbidden_wording(text)
        return text

    # -- ordering ----------------------------------------------------------- #
    def _disease_ranks(self, verified: Iterable[Claim]) -> dict[str, int]:
        """Map ``disease_id -> rank`` by descending ASSOCIATION Overall_Score.

        Derived purely from the ASSOCIATION claims among ``verified``: the
        highest Overall_Score gets rank 0, ties broken by ``disease_id``
        ascending. A disease that appears only in non-association claims (no
        ASSOCIATION claim present) is given a score of ``-inf`` so it still
        receives a deterministic rank after the scored diseases. Gene-scoped
        claims are not in this map (handled with ``+inf`` in :meth:`_sort_key`).
        """
        scores: dict[str, float] = {}
        for claim in verified:
            if _is_gene_scoped(claim):
                continue
            disease_id = _disease_id_of(claim)
            if claim.claim_type is ClaimType.ASSOCIATION and isinstance(
                claim.value, (int, float)
            ) and not isinstance(claim.value, bool):
                # Keep the association score (there is one per disease).
                scores[disease_id] = float(claim.value)
            else:
                scores.setdefault(disease_id, float("-inf"))

        # Sort disease ids by (-score, disease_id) for a stable descending rank.
        ordered_ids = sorted(scores, key=lambda d: (-scores[d], d))
        return {disease_id: rank for rank, disease_id in enumerate(ordered_ids)}

    def _sort_key(
        self, claim: Claim, disease_rank: Mapping[str, int]
    ) -> tuple[float, int, str, str]:
        """The fixed composite ``(disease_rank, claim_type_rank, disease_id, claim_id)``.

        Gene-scoped claims get ``disease_rank = +inf`` so the gene-level section
        sorts after every per-disease section, and ``disease_id = ""`` as the
        tie-breaker. ``claim_type_rank`` is the fixed ordinal from
        :data:`_CLAIM_TYPE_RANK`.
        """
        if _is_gene_scoped(claim):
            rank: float = math.inf
            disease_id = ""
        else:
            disease_id = _disease_id_of(claim)
            rank = float(disease_rank.get(disease_id, len(disease_rank)))
        type_rank = _CLAIM_TYPE_RANK.get(claim.claim_type, len(_CLAIM_TYPE_RANK))
        return (rank, type_rank, disease_id, claim.claim_id)

    def _group_in_order(
        self, ordered: list[Claim]
    ) -> list[tuple[str, list[Claim]]]:
        """Group an already-sorted claim list into ``(disease_id, claims)`` runs.

        The input is sorted so all claims for one disease (and the gene-scoped
        group, disease_id ``""``) are contiguous; this preserves that order,
        producing one group per disease in rank order with the gene-scoped group
        last.
        """
        groups: list[tuple[str, list[Claim]]] = []
        current_id: Optional[str] = None
        current: list[Claim] = []
        for claim in ordered:
            disease_id = "" if _is_gene_scoped(claim) else _disease_id_of(claim)
            if disease_id != current_id:
                if current:
                    groups.append((current_id or "", current))
                current_id = disease_id
                current = [claim]
            else:
                current.append(claim)
        if current:
            groups.append((current_id or "", current))
        return groups

    # -- rendering ---------------------------------------------------------- #
    def _render_group(
        self,
        gene_symbol: str,
        disease_id: str,
        group: list[Claim],
        names: Mapping[str, str],
    ) -> list[str]:
        """Render one disease group (or the gene-scoped group when ``disease_id`` is empty)."""
        if disease_id == "":
            return self._render_gene_scoped(gene_symbol, group)
        return self._render_disease(gene_symbol, disease_id, group, names)

    def _render_disease(
        self,
        gene_symbol: str,
        disease_id: str,
        group: list[Claim],
        names: Mapping[str, str],
    ) -> list[str]:
        """Render a per-disease section: headline + datatype breakdown + papers."""
        disease_name = names.get(disease_id, disease_id)
        lines: list[str] = [f"## {disease_name}", ""]

        by_type = self._index_by_type(group)

        # Headline sentence: associated-with + evidence-strength score, optionally
        # the co-mention count as "reported in N co-mentions".
        assoc = by_type.get(ClaimType.ASSOCIATION)
        co_mention = by_type.get(ClaimType.CO_MENTION_COUNT)
        if assoc is not None:
            sentence = (
                f"{gene_symbol} is associated with {disease_name} "
                f"(evidence-strength score {_fmt_score(assoc.value)}"
            )
            if co_mention is not None and isinstance(co_mention.value, int):
                sentence += (
                    f", reported in {_fmt_count(co_mention.value)} co-mentions"
                )
            sentence += ")."
            lines.append(sentence)
            lines.append("")
        elif co_mention is not None and isinstance(co_mention.value, int):
            # No association score, but a co-mention count — still approved wording.
            lines.append(
                f"{gene_symbol} and {disease_name} are reported together in "
                f"{_fmt_count(co_mention.value)} co-mentions."
            )
            lines.append("")

        # Datatype breakdown, sorted by id (Req 8.3; never dict/tuple order).
        breakdown = by_type.get(ClaimType.DATATYPE_BREAKDOWN)
        if breakdown is not None and isinstance(breakdown.value, tuple):
            lines.append("Evidence-strength score by datatype:")
            lines.append("")
            for ds in self._sorted_datatype_scores(breakdown.value):
                lines.append(f"- {ds.id}: {_fmt_score(ds.score)}")
            lines.append("")

        # Per-disease recent papers (RECENT_PAPER whose subject is this pair).
        papers = [c for c in group if c.claim_type is ClaimType.RECENT_PAPER]
        lines.extend(self._render_recent_papers(papers))
        return lines

    def _render_gene_scoped(
        self, gene_symbol: str, group: list[Claim]
    ) -> list[str]:
        """Render the gene-level section: literature count + gene-scoped papers."""
        lines: list[str] = [f"## {gene_symbol} literature", ""]
        by_type = self._index_by_type(group)

        lit = by_type.get(ClaimType.LITERATURE_COUNT)
        if lit is not None and isinstance(lit.value, int):
            lines.append(
                f"{gene_symbol} is reported in {_fmt_count(lit.value)} "
                "PubMed records."
            )
            lines.append("")

        papers = [c for c in group if c.claim_type is ClaimType.RECENT_PAPER]
        lines.extend(self._render_recent_papers(papers))
        return lines

    def _render_recent_papers(self, papers: list[Claim]) -> list[str]:
        """Render a stable, id-sorted list of recent-paper PMIDs."""
        if not papers:
            return []
        # Sort by claim_id so the list is deterministic regardless of input order.
        ordered = sorted(papers, key=lambda c: c.claim_id)
        lines = ["Recent papers (PMIDs):", ""]
        for claim in ordered:
            lines.append(f"- {claim.value}")
        lines.append("")
        return lines

    # -- small helpers ------------------------------------------------------ #
    @staticmethod
    def _index_by_type(group: list[Claim]) -> dict[ClaimType, Claim]:
        """Map the single-per-type claims of a group by type.

        ASSOCIATION / DATATYPE_BREAKDOWN / CO_MENTION_COUNT / LITERATURE_COUNT
        appear at most once per disease (or per gene); if a type appears more
        than once, the lexicographically-smallest ``claim_id`` wins so the choice
        is deterministic. RECENT_PAPER is handled separately (many per group).
        """
        indexed: dict[ClaimType, Claim] = {}
        for claim in sorted(group, key=lambda c: c.claim_id):
            if claim.claim_type is ClaimType.RECENT_PAPER:
                continue
            indexed.setdefault(claim.claim_type, claim)
        return indexed

    @staticmethod
    def _sorted_datatype_scores(
        scores: Iterable[DatatypeScore],
    ) -> list[DatatypeScore]:
        """Return the datatype scores sorted by ``id`` (Req 8.3)."""
        return sorted(scores, key=lambda ds: ds.id)


__all__ = [
    "DossierRenderer",
    "ForbiddenWordingError",
    "FORBIDDEN_TERMS",
    "SAFETY_NOTICE",
]
