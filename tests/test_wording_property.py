"""Property 11: Wording and score-label safety (pytest + Hypothesis).

**Validates: Requirements 8.5, 10.1, 10.2, 10.5**

The :class:`~claimledger.render.DossierRenderer` is the only component that turns
Verified_Claims into prose, and the steering/wording constraints (Req 10) are
therefore enforced here. This module pins the wording and score-label safety
guarantees of the rendered dossier:

* **No forbidden terms (Req 10.2).** The rendered text contains NONE of
  :data:`~claimledger.render.FORBIDDEN_TERMS` ("causes", "proves", "validated",
  "promising") as standalone words (case-insensitive, whole-word). A forbidden
  term appearing as a *substring* of a longer legitimate token is allowed; as a
  standalone word it must never appear. The same whole-word regex the renderer
  uses is applied to the output.
* **Approved wording present when there is content (Req 10.1).** Whenever the
  dossier carries at least one per-disease ASSOCIATION or CO_MENTION section,
  one of the approved phrases "associated with" / "reported in" appears. (An
  empty verified set requires neither.)
* **Score-label safety (Req 8.5, 10.5).** The words "probability" and
  "likelihood" never appear; wherever an Overall_Score is shown it is labeled an
  *evidence-strength score* (the literal "evidence-strength score" appears
  whenever at least one ASSOCIATION is rendered), and no score is rendered
  followed by a ``%`` probability phrasing.
* **Guard soundness (Req 10.2).** ``render`` either returns guard-clean text or
  raises :class:`~claimledger.render.ForbiddenWordingError`; it never returns
  text containing a forbidden standalone word. For :func:`render_input` (whose
  names are filtered free of forbidden standalone words) ``render`` returns and
  is clean.

Separately, a **guard positive test** generates a disease name that genuinely
contains a forbidden standalone word (``"<prefix> proves <suffix>"`` with the
forbidden word as a whole token) and asserts ``render`` RAISES
``ForbiddenWordingError`` — proving the render-time guard actually fires on
genuinely forbidden generated prose, rather than being vacuously satisfied.

Strategy domain / narrowing (documented)
----------------------------------------
The main properties reuse :func:`tests.test_render_property.render_input`
unchanged: it already generates a realistic set of Verified_Claims (1..4
diseases, each with an ASSOCIATION / DATATYPE_BREAKDOWN / CO_MENTION_COUNT, plus
a gene-level LITERATURE_COUNT and 0..4 RECENT_PAPERs), and its disease display
names are already constrained to not spell a forbidden standalone word — so
``render`` is expected to return clean for every draw. No additional narrowing
is applied to the main properties.

The guard positive test uses its own generator: it composes a disease name
``"<prefix> <forbidden> <suffix>"`` placing a forbidden term from
``FORBIDDEN_TERMS`` as a standalone whole word, with prefix/suffix drawn from a
lowercase+space alphabet that cannot itself introduce a forbidden word or the
``"::"`` subject separator. A single VERIFIED ASSOCIATION claim for that disease
is enough to force the forbidden name into the rendered prose, so the guard must
fire. This generator is deliberately kept separate so it never weakens the main
properties' clean-input domain.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.claims import SUBJECT_SEP
from claimledger.models import Claim, ClaimStatus, ClaimType
from claimledger.render import (
    FORBIDDEN_TERMS,
    DossierRenderer,
    ForbiddenWordingError,
)

# Reuse the shared Verified_Claims generator and the pair-subject helper from the
# determinism/provenance module so the wording properties run over exactly the
# same realistic input space (its disease names are already filtered free of
# forbidden standalone words, keeping draws inside the renderable domain).
from tests.test_render_property import _pair_subject, render_input

# The same whole-word, case-insensitive matcher the renderer uses for the
# forbidden terms (render._FORBIDDEN_RE). Rebuilt here from the public
# FORBIDDEN_TERMS so the test asserts against the documented rule, not a private
# symbol: a forbidden term embedded in a longer token is allowed; standing alone
# as a word it is forbidden.
_FORBIDDEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in FORBIDDEN_TERMS) + r")\b",
    re.IGNORECASE,
)

# The approved gene-disease phrasings (Req 10.1). At least one must appear
# whenever the dossier carries any ASSOCIATION or CO_MENTION content.
_APPROVED_PHRASES = ("associated with", "reported in")

# The mandatory score label (Req 8.5, 10.5): an Overall_Score is an
# evidence-strength score, never a probability/likelihood. The renderer's
# headline emits the literal lowercase phrase.
_SCORE_LABEL = "evidence-strength score"

# Words that must never appear anywhere in the prose (Req 8.5, 10.5): a score is
# never dressed as a probability or likelihood.
_PROBABILITY_WORDS = ("probability", "likelihood")


def _disease_id_of(claim: Claim) -> str:
    """Disease id from a ``GENE::DISEASE`` subject (``""`` if gene-scoped)."""
    _, sep, disease_id = claim.subject.partition(SUBJECT_SEP)
    return disease_id if sep else ""


def _has_pair_content(claims: list[Claim]) -> bool:
    """True iff some VERIFIED claim would render a per-disease section.

    A per-disease ASSOCIATION or CO_MENTION_COUNT claim drives the approved
    "associated with" / "reported in" wording. Only VERIFIED, pair-scoped claims
    of those two types count (the renderer ignores non-Verified and gene-scoped
    ones for that wording).
    """
    for c in claims:
        if c.status is not ClaimStatus.VERIFIED:
            continue
        if c.claim_type in (ClaimType.ASSOCIATION, ClaimType.CO_MENTION_COUNT) and (
            _disease_id_of(c) != ""
        ):
            return True
    return False


def _renders_association(claims: list[Claim]) -> bool:
    """True iff some VERIFIED, pair-scoped ASSOCIATION claim is present.

    The renderer emits the "evidence-strength score" label exactly for such a
    claim's headline, so this is the precondition for asserting the label.
    """
    return any(
        c.status is ClaimStatus.VERIFIED
        and c.claim_type is ClaimType.ASSOCIATION
        and _disease_id_of(c) != ""
        for c in claims
    )


# --------------------------------------------------------------------------- #
# Property 11.1 — no forbidden standalone words, and the guard is sound:       #
# render returns clean text (never forbidden) for the filtered-name domain.    #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_no_forbidden_terms_and_guard_sound(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """Rendered text holds no forbidden standalone word; render stays guard-clean.

    render_input's names are filtered free of forbidden standalone words, so
    render must RETURN (not raise) and the returned text must contain none of
    FORBIDDEN_TERMS as whole words (Req 10.2). This also exercises guard
    soundness: render either returns guard-clean text or raises
    ForbiddenWordingError — here it returns, and is clean.

    Validates Requirement 10.2.
    """
    gene, claims, names = data
    renderer = DossierRenderer()

    # For this (filtered-name) domain render must not raise; if the guard ever
    # fired here it would signal our own templates emitting a forbidden word.
    try:
        out = renderer.render(claims, gene_symbol=gene, disease_names=names)
    except ForbiddenWordingError as exc:  # pragma: no cover - would be a real bug
        pytest.fail(
            "render raised ForbiddenWordingError on clean (filtered-name) input; "
            f"our own prose emitted a forbidden term: {exc.found!r}"
        )

    found = sorted({m.group(0).lower() for m in _FORBIDDEN_RE.finditer(out)})
    assert not found, f"forbidden standalone word(s) in rendered prose: {found!r}"


# --------------------------------------------------------------------------- #
# Property 11.2 — approved wording present whenever there is per-disease        #
# content (Req 10.1).                                                          #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_approved_wording_present_when_content(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """Whenever a per-disease ASSOCIATION/CO_MENTION renders, an approved phrase appears.

    The renderer describes gene-disease relationships only as "associated with"
    / "reported in"; at least one such phrase must be present whenever the
    dossier carries any per-disease content. (render_input always has >= 1
    disease with an ASSOCIATION, so content is always present here.)

    Validates Requirement 10.1.
    """
    gene, claims, names = data
    out = DossierRenderer().render(claims, gene_symbol=gene, disease_names=names)

    if _has_pair_content(claims):
        assert any(phrase in out for phrase in _APPROVED_PHRASES), (
            "per-disease content rendered without an approved phrasing "
            f"{_APPROVED_PHRASES!r}"
        )


# --------------------------------------------------------------------------- #
# Property 11.3 — score-label safety: no "probability"/"likelihood"; the        #
# evidence-strength label is present when a score renders; no "N%" score        #
# phrasing (Req 8.5, 10.5).                                                     #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_score_label_safety(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """Scores are evidence-strength labeled, never probabilities.

    Asserts (a) the words "probability" and "likelihood" never appear, (b) the
    literal "evidence-strength score" appears whenever at least one ASSOCIATION
    is rendered, and (c) no rendered score is immediately followed by a ``%``
    probability phrasing.

    Validates Requirements 8.5 and 10.5.
    """
    gene, claims, names = data
    out = DossierRenderer().render(claims, gene_symbol=gene, disease_names=names)
    lowered = out.lower()

    # (a) No probability/likelihood vocabulary anywhere.
    for word in _PROBABILITY_WORDS:
        assert word not in lowered, (
            f"score-label-unsafe vocabulary {word!r} appeared in rendered prose"
        )

    # (b) The evidence-strength label is present whenever a score renders.
    if _renders_association(claims):
        assert _SCORE_LABEL in lowered, (
            "an ASSOCIATION rendered but the 'evidence-strength score' label is "
            "absent"
        )

    # (c) No score is presented as a percentage probability: a number directly
    # followed by '%' would read as a probability, which the renderer must never
    # produce. The renderer formats scores as plain decimals (e.g. 0.8500).
    assert not re.search(r"\d\s*%", out), (
        "a numeric value is rendered with a '%' sign, reading as a probability"
    )


# --------------------------------------------------------------------------- #
# Property 11.4 — guard soundness: render returns guard-clean text or raises     #
# ForbiddenWordingError, never returning forbidden text (Req 10.2).             #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_render_is_guard_clean_or_raises(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """render never returns text with a forbidden standalone word.

    Either it returns guard-clean text or it raises ForbiddenWordingError. Since
    render_input's names are filtered, render should NOT raise here; it must
    return, and the returned text must be clean.

    Validates Requirement 10.2.
    """
    gene, claims, names = data
    renderer = DossierRenderer()
    try:
        out = renderer.render(claims, gene_symbol=gene, disease_names=names)
    except ForbiddenWordingError:  # pragma: no cover - would be a real bug here
        pytest.fail("render raised ForbiddenWordingError on filtered-name input")
    # Returned => must be clean.
    assert not _FORBIDDEN_RE.search(out)


# =========================================================================== #
# Guard positive test — a disease NAME carrying a forbidden standalone word     #
# forces forbidden prose, and the render-time guard MUST fire (Req 10.2).       #
# =========================================================================== #

# Prefix/suffix alphabet for the forbidden-name generator: lowercase letters and
# a space only. It cannot introduce another forbidden standalone word (all four
# forbidden terms contain letters only and are whole tokens) nor the "::" subject
# separator, so the ONLY forbidden word in the composed name is the one we inject.
_PLAIN_ALPHA = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=8)

# A gene symbol token, same HGNC-style domain render_input uses.
_gene_symbols = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-",
    min_size=1,
    max_size=10,
)


@st.composite
def forbidden_name_input(draw: st.DrawFn) -> tuple[str, list[Claim], dict[str, str]]:
    """Draw ``(gene, [one VERIFIED ASSOCIATION], names)`` whose disease NAME is forbidden.

    The disease display name is ``"<prefix> <forbidden> <suffix>"`` with a term
    from FORBIDDEN_TERMS placed as a standalone whole word, so when the renderer
    emits the per-disease headline ("GENE is associated with <name> ...") the
    forbidden word lands in the prose as its own word and the guard must fire.
    A single VERIFIED ASSOCIATION claim is enough to render that headline.
    """
    gene = draw(_gene_symbols)
    forbidden = draw(st.sampled_from(FORBIDDEN_TERMS))
    prefix = draw(_PLAIN_ALPHA)
    suffix = draw(_PLAIN_ALPHA)
    # Compose a name with the forbidden term as a standalone, space-delimited word.
    disease_name = f"{prefix} {forbidden} {suffix}".strip()
    # A disease id in the usual MONDO/EFO-style token domain.
    disease_id = draw(
        st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
            min_size=1,
            max_size=16,
        )
    )
    subject = _pair_subject(gene, disease_id)
    claim = Claim(
        claim_id=f"ASSOCIATION:{subject}",
        claim_type=ClaimType.ASSOCIATION,
        subject=subject,
        value=0.5,
        evidence_ids=(f"ot_associations:{disease_id}",),
        status=ClaimStatus.VERIFIED,
    )
    names = {disease_id: disease_name}
    return gene, [claim], names


@settings(max_examples=100)
@given(data=forbidden_name_input())
def test_guard_fires_on_genuinely_forbidden_prose(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """render RAISES ForbiddenWordingError when the prose genuinely holds a forbidden word.

    This proves the render-time guard is not vacuous: when a disease name spells
    a forbidden standalone word and that name is rendered into the headline, the
    guard refuses to emit the text. The raised error names the offending term.

    Validates Requirement 10.2.
    """
    gene, claims, names = data
    with pytest.raises(ForbiddenWordingError) as excinfo:
        DossierRenderer().render(claims, gene_symbol=gene, disease_names=names)

    # The guard reports the specific forbidden term(s) it found.
    assert excinfo.value.found, "guard raised but reported no offending terms"
    assert all(term in FORBIDDEN_TERMS for term in excinfo.value.found)
