"""Property 3: Rendering determinism (pytest + Hypothesis).

**Validates: Requirements 8.2, 8.3**

The :class:`~claimledger.render.DossierRenderer` is a pure, model-free function
of its inputs (design "Dossier Renderer", Req 8.4). Requirement 8.2 demands
identical output for the same set of Verified_Claims on every invocation, and
Requirement 8.3 demands a fixed ordering that does not depend on retrieval order
or dictionary iteration order. These properties pin both:

* **Idempotence** — ``render(claims) == render(claims)`` for any generated set.
* **Permutation invariance** — ``render(permutation(claims)) ==
  render(claims)``: the output is byte-identical no matter what order the claims
  arrive in (Req 8.2, 8.3).
* **Insertion-order independence** — the ``disease_names`` map and the claim
  list are built in randomized insertion orders (Hypothesis varies them), and
  the output is still identical: determinism must not leak through dict
  iteration order (Req 8.3).
* **Datatype ordering** — the datatype breakdown lines render in id-sorted
  (ascending) order for the top disease, independent of the order the
  ``DatatypeScore`` tuple was built in (Req 8.3).

Strategy domain / narrowing (documented)
----------------------------------------
A generated input is a *set of Verified_Claims* describing a small set of
synthetic diseases, mirroring what the ClaimBuilder produces:

* **Diseases**: 1..4 synthetic diseases with UNIQUE ``disease_id`` tokens and a
  display name each; a ``disease_names`` map carries ``disease_id -> name``. Ids
  are restricted to an uppercase/digit/underscore token alphabet (the genuine
  MONDO/EFO-style id domain) so a per-disease lookup is unambiguous; names draw
  from a small printable alphabet.
* **Per disease**: an ASSOCIATION (float score in [0, 1] — the real
  Overall_Score domain, never NaN/inf), a DATATYPE_BREAKDOWN (tuple of
  :class:`DatatypeScore` with UNIQUE ids drawn from the fixed OT datatype set;
  the renderer's canonical form is id-sorted, matching builder/recompute output,
  so the tuple is deliberately SHUFFLED on the way in), and a CO_MENTION_COUNT
  (non-negative int).
* **Gene-level**: a LITERATURE_COUNT (non-negative int) whose subject is the
  bare gene, and 0..4 RECENT_PAPER claims whose values are digit-string PMIDs
  (the genuine ``esearchresult.idlist`` domain).

Every claim is ``status=VERIFIED`` and carries >= 1 Evidence_ID. The Evidence_ID
values are synthetic: the renderer reads only claim fields (never the store), so
they need not resolve against any EvidenceStore here — this test targets
rendering determinism, not verification (covered by Property 1). Scores use
``st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)``;
counts are non-negative ints; PMIDs are non-empty digit strings.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.claims import SUBJECT_SEP
from claimledger.models import Claim, ClaimStatus, ClaimType, DatatypeScore
from claimledger.render import FORBIDDEN_TERMS, DossierRenderer, _fmt_count

# The fixed OT evidence-datatype id set (models.DatatypeScore docstring /
# tests.strategies._DATATYPE_IDS). A breakdown draws a UNIQUE subset so the
# id-keyed canonical (sorted) ordering is unambiguous.
_DATATYPE_IDS = (
    "clinical",
    "genetic_literature",
    "affected_pathway",
    "literature",
    "genetic_association",
    "somatic_mutation",
)

# Scores: the genuine OT Overall_Score / Datatype_Score domain — finite float in
# [0, 1], never NaN/inf (see data/cache/preflight_ot_associations.json).
_scores = st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)

# Counts: non-negative ints (Literature_Count / Co_Mention_Count domain).
_counts = st.integers(min_value=0, max_value=10_000_000)

# PMIDs: non-empty digit strings (esearchresult.idlist domain).
_pmids = st.text(alphabet="0123456789", min_size=1, max_size=9)

# A gene symbol token: HGNC-style ASCII letters/digits/hyphen, non-empty.
_gene_symbols = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-",
    min_size=1,
    max_size=10,
)

# Disease id token: uppercase/digit/underscore (MONDO/EFO-style) — never
# containing the "::" subject separator, so pair subjects parse unambiguously.
_disease_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=16,
)

# Disease display names: a small printable alphabet, deliberately EXCLUDING any
# token that would introduce a forbidden standalone word or the "::" separator.
# The alphabet alone does not prevent a drawn name from *spelling* a forbidden
# standalone word (e.g. "proves"), which would legitimately trip the renderer's
# wording guard — a name is not application prose, so such a draw is outside this
# determinism/provenance property's domain. We therefore filter out any name that
# contains a forbidden term as a whole word (the same whole-word rule the guard
# uses), keeping the generated names inside the renderable domain.
_FORBIDDEN_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in FORBIDDEN_TERMS) + r")\b",
    re.IGNORECASE,
)
_disease_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ",
    min_size=0,
    max_size=20,
).filter(lambda s: not _FORBIDDEN_NAME_RE.search(s))


def _pair_subject(gene: str, disease_id: str) -> str:
    """The ``GENE::DISEASE`` subject the ClaimBuilder uses for pair claims."""
    return f"{gene}{SUBJECT_SEP}{disease_id}"


@st.composite
def datatype_breakdown(draw: st.DrawFn) -> tuple[DatatypeScore, ...]:
    """Draw a DATATYPE_BREAKDOWN value: 0..6 DatatypeScore, unique ids, SHUFFLED.

    The tuple is returned in an arbitrary (permuted) order so the test can assert
    the renderer still emits the lines id-sorted (Req 8.3), independent of build
    order.
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
    entries = [DatatypeScore(id=dt_id, score=draw(_scores)) for dt_id in chosen_ids]
    permuted = draw(st.permutations(entries))
    return tuple(permuted)


@st.composite
def render_input(draw: st.DrawFn) -> tuple[str, list[Claim], dict[str, str]]:
    """Draw ``(gene_symbol, claims, disease_names)`` for a Verified_Claims set.

    Builds 1..4 synthetic diseases (unique ids + a name each) and, per disease,
    an ASSOCIATION, DATATYPE_BREAKDOWN, and CO_MENTION_COUNT claim; plus one
    gene-level LITERATURE_COUNT and 0..4 RECENT_PAPER claims. Every claim is
    VERIFIED with a synthetic Evidence_ID. The claim list and the disease_names
    map are both built, then the list is SHUFFLED and the map is rebuilt in a
    randomized insertion order so no determinism can leak through input order or
    dict iteration order (Req 8.3).
    """
    gene = draw(_gene_symbols)

    disease_ids = draw(
        st.lists(_disease_ids, min_size=1, max_size=4, unique=True)
    )

    claims: list[Claim] = []
    names_pairs: list[tuple[str, str]] = []

    for disease_id in disease_ids:
        subject = _pair_subject(gene, disease_id)
        names_pairs.append((disease_id, draw(_disease_names)))

        # ASSOCIATION (value = Overall_Score float).
        claims.append(
            Claim(
                claim_id=f"ASSOCIATION:{subject}",
                claim_type=ClaimType.ASSOCIATION,
                subject=subject,
                value=draw(_scores),
                evidence_ids=(f"ot_associations:{disease_id}",),
                status=ClaimStatus.VERIFIED,
            )
        )
        # DATATYPE_BREAKDOWN (value = shuffled tuple of DatatypeScore).
        claims.append(
            Claim(
                claim_id=f"DATATYPE_BREAKDOWN:{subject}",
                claim_type=ClaimType.DATATYPE_BREAKDOWN,
                subject=subject,
                value=draw(datatype_breakdown()),
                evidence_ids=(f"ot_associations:{disease_id}",),
                status=ClaimStatus.VERIFIED,
            )
        )
        # CO_MENTION_COUNT (value = int).
        claims.append(
            Claim(
                claim_id=f"CO_MENTION_COUNT:{subject}",
                claim_type=ClaimType.CO_MENTION_COUNT,
                subject=subject,
                value=draw(_counts),
                evidence_ids=(f"pubmed_pair:{disease_id}",),
                status=ClaimStatus.VERIFIED,
            )
        )

    # Gene-level LITERATURE_COUNT (subject = bare gene).
    claims.append(
        Claim(
            claim_id=f"LITERATURE_COUNT:{gene}",
            claim_type=ClaimType.LITERATURE_COUNT,
            subject=gene,
            value=draw(_counts),
            evidence_ids=(f"pubmed_gene:{gene}",),
            status=ClaimStatus.VERIFIED,
        )
    )

    # Gene-level RECENT_PAPER claims (unique digit-string PMIDs).
    pmids = draw(st.lists(_pmids, min_size=0, max_size=4, unique=True))
    for pmid in pmids:
        claims.append(
            Claim(
                claim_id=f"RECENT_PAPER:{gene}:{pmid}",
                claim_type=ClaimType.RECENT_PAPER,
                subject=gene,
                value=pmid,
                evidence_ids=(f"pubmed_gene:{gene}",),
                status=ClaimStatus.VERIFIED,
            )
        )

    # Randomize insertion order of the claim list (Req 8.3: no reliance on input
    # order) and of the disease_names map (no reliance on dict iteration order).
    shuffled_claims = draw(st.permutations(claims))
    shuffled_name_pairs = draw(st.permutations(names_pairs))
    names = dict(shuffled_name_pairs)

    return gene, list(shuffled_claims), names


# --------------------------------------------------------------------------- #
# Property 3.1 — idempotence: same input renders byte-identically.             #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_render_is_idempotent(data: tuple[str, list[Claim], dict[str, str]]) -> None:
    """``render(claims) == render(claims)`` for any generated Verified_Claims set.

    Validates Requirement 8.2.
    """
    gene, claims, names = data
    renderer = DossierRenderer()
    first = renderer.render(claims, gene_symbol=gene, disease_names=names)
    second = renderer.render(claims, gene_symbol=gene, disease_names=names)
    assert first == second


# --------------------------------------------------------------------------- #
# Property 3.2 — permutation invariance: input order never changes output.     #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input(), perm_data=st.data())
def test_render_is_permutation_invariant(
    data: tuple[str, list[Claim], dict[str, str]],
    perm_data: st.DataObject,
) -> None:
    """``render(permutation(claims)) == render(claims)`` for arbitrary permutations.

    The output is byte-identical regardless of the order the claims are supplied
    in (and regardless of disease_names insertion order, already randomized by
    the input strategy). Validates Requirements 8.2, 8.3.
    """
    gene, claims, names = data
    renderer = DossierRenderer()
    baseline = renderer.render(claims, gene_symbol=gene, disease_names=names)

    permuted = perm_data.draw(st.permutations(claims))
    out = renderer.render(list(permuted), gene_symbol=gene, disease_names=names)
    assert out == baseline


# --------------------------------------------------------------------------- #
# Property 3.3 — datatype breakdown renders id-sorted, independent of build    #
# order (Req 8.3).                                                             #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=render_input())
def test_datatype_breakdown_is_id_sorted_for_top_disease(
    data: tuple[str, list[Claim], dict[str, str]],
) -> None:
    """The top disease's datatype lines appear in ascending id order.

    The DATATYPE_BREAKDOWN tuple is shuffled on the way in; the renderer must
    still emit the bullet lines sorted by id. Asserted for at least the top
    disease (the one whose section renders first), proving the order is derived
    from the id, not the tuple's build order. Validates Requirement 8.3.
    """
    gene, claims, names = data
    out = DossierRenderer().render(claims, gene_symbol=gene, disease_names=names)

    # The top disease is the ASSOCIATION with the highest Overall_Score, ties
    # broken by disease_id ascending — exactly the renderer's disease_rank.
    assoc_claims = [c for c in claims if c.claim_type is ClaimType.ASSOCIATION]
    assert assoc_claims  # every generated input has >= 1 disease
    top = min(assoc_claims, key=lambda c: (-float(c.value), _disease_id_of(c)))
    top_disease_id = _disease_id_of(top)

    # Find the matching DATATYPE_BREAKDOWN for the top disease.
    breakdown = next(
        c
        for c in claims
        if c.claim_type is ClaimType.DATATYPE_BREAKDOWN
        and _disease_id_of(c) == top_disease_id
    )
    ids = [ds.id for ds in breakdown.value]
    if len(ids) < 2:
        # Fewer than two datatypes -> nothing meaningful to order; skip.
        return

    # Each datatype id appears as a bullet "- <id>: <score>"; the positions of
    # the ids in id-sorted order must be strictly increasing in the output.
    sorted_ids = sorted(ids)
    positions = [out.index(f"- {ds_id}: ") for ds_id in sorted_ids]
    assert positions == sorted(positions)


def _disease_id_of(claim: Claim) -> str:
    """Disease id from a ``GENE::DISEASE`` subject (``""`` if gene-scoped)."""
    _, sep, disease_id = claim.subject.partition(SUBJECT_SEP)
    return disease_id if sep else ""


# =========================================================================== #
# Property 4: Rendering provenance (pytest + Hypothesis).                      #
#                                                                             #
# **Validates: Requirements 7.2, 8.1**                                        #
#                                                                             #
# Req 8.1: the Dossier_Renderer renders prose using ONLY Verified_Claims.     #
# Req 7.2: no Unsupported_Claim contributes any content to the rendered       #
# dossier prose. Together they mean an UNSUPPORTED claim must make NO          #
# difference to the output: rendering the full mixed set must be byte-        #
# identical to rendering only the Verified subset, and no distinctive token    #
# carried by an UNSUPPORTED claim may appear anywhere in the text.             #
#                                                                             #
# Strategy / sentinel design (documented narrowing)                           #
# -------------------------------------------------                           #
# We reuse :func:`render_input` for the VERIFIED backbone (its claims are all  #
# VERIFIED and already exercise every claim type and the deterministic order).  #
# On top of it we synthesize UNSUPPORTED claims whose every rendered-relevant   #
# field is a DISTINCTIVE SENTINEL — a value in the genuine domain shape but     #
# chosen so it cannot collide with any verified content generated by           #
# render_input:                                                                #
#                                                                             #
# * Sentinel PMIDs are long digit strings prefixed with a fixed 12-digit       #
#   banner (``9999990000NN``). render_input draws PMIDs of max_size 9, so a    #
#   >= 12-digit sentinel can never be produced as a verified PMID.             #
# * Sentinel disease ids / names embed the literal uppercase token             #
#   ``ZZSENTINELZZ`` plus a unique counter. render_input's disease names draw  #
#   from a lowercase+space alphabet and ids from an uppercase/digit/underscore  #
#   alphabet of max 16 chars, so the banner token (with its fixed length and   #
#   its ``SENTINEL`` run) cannot appear in verified content.                   #
# * Sentinel counts use a fixed astronomically distinctive value               #
#   (``987654321987``) above render_input's count ceiling (1e7), so it can      #
#   never equal a verified count.                                              #
# * Sentinel datatype scores embed the sentinel id text; since the renderer    #
#   only renders a DATATYPE_BREAKDOWN for a VERIFIED claim, the id leaking      #
#   would be the signal of a bug.                                             #
#                                                                             #
# Each sentinel is distinct per UNSUPPORTED claim (a per-example counter), so   #
# a leak of ANY one unsupported claim is detectable. The sentinel tokens are    #
# collected and asserted absent from the rendered text; separately we assert    #
# render(mixed) == render(verified-only) so even a collision-free but content-  #
# shifting leak (e.g. an unsupported ASSOCIATION reordering a disease section)  #
# is caught.                                                                   #
# =========================================================================== #

# A banner token that cannot appear in any render_input-generated field: it is
# uppercase + contains the literal run "SENTINEL" and "ZZ" bookends. Verified
# disease names are lowercase; verified ids are <= 16 chars; this token is used
# only in unsupported-claim fields.
_SENTINEL_BANNER = "ZZSENTINELZZ"

# Sentinel count: a fixed value far above render_input's count ceiling (1e7),
# so it can never equal a VERIFIED Literature_/Co_Mention_Count.
_SENTINEL_COUNT = 987654321987

# Verified PMIDs from render_input are <= 9 digits; sentinel PMIDs are >= 12.
_SENTINEL_PMID_PREFIX = "999999000000"


def _sentinel_tokens_of(claim: Claim) -> list[str]:
    """The genuinely DISTINCTIVE sentinel strings an UNSUPPORTED ``claim`` carries.

    Only tokens that CANNOT, by construction, appear in any VERIFIED content from
    :func:`render_input` are returned, so their presence in the output is an
    unambiguous Req 7.2 violation:

    * the ``_SENTINEL_BANNER``-bearing disease id and its display name (for a
      fresh sentinel disease), and any sentinel datatype id;
    * the sentinel count value (>= ``_SENTINEL_COUNT``, above render_input's
      count ceiling); and
    * the sentinel PMID (>= 12 digits, longer than any verified PMID).

    The claim's bare ``subject`` and shared ``disease_id`` are deliberately NOT
    returned: when an unsupported claim shares a disease with a verified one,
    that disease id/gene legitimately renders from the VERIFIED claim. The
    ``render(mixed) == render(verified-only)`` equality assertion is what proves
    the unsupported claim adds nothing in that shared-disease case; this helper
    only pins the tokens that could never be verified content.
    """
    tokens: list[str] = []

    # A sentinel disease id (and its display name) only leaks if the unsupported
    # claim's own disease is a fresh sentinel disease, never a shared verified one.
    disease_id = _disease_id_of(claim)
    if disease_id and _SENTINEL_BANNER in disease_id:
        tokens.append(disease_id)

    if claim.claim_type is ClaimType.DATATYPE_BREAKDOWN and isinstance(
        claim.value, tuple
    ):
        for ds in claim.value:
            if _SENTINEL_BANNER in ds.id:
                tokens.append(ds.id)
    elif claim.claim_type in (
        ClaimType.CO_MENTION_COUNT,
        ClaimType.LITERATURE_COUNT,
    ) and isinstance(claim.value, int) and not isinstance(claim.value, bool):
        if claim.value >= _SENTINEL_COUNT:
            tokens.append(str(claim.value))
            tokens.append(_fmt_count(claim.value))
    elif claim.claim_type is ClaimType.RECENT_PAPER and isinstance(claim.value, str):
        if claim.value.startswith(_SENTINEL_PMID_PREFIX):
            tokens.append(claim.value)

    return [t for t in tokens if t]


@st.composite
def unsupported_claim(
    draw: st.DrawFn, gene: str, disease_id: str, nonce: int
) -> Claim:
    """Draw one UNSUPPORTED claim whose rendered fields are distinctive sentinels.

    ``disease_id`` is the (already sentinel-banner-bearing or verified-shared)
    disease the claim is about; ``nonce`` makes every sentinel unique per claim
    so any single leak is detectable. The claim type is drawn across all five so
    every rendering path is covered.
    """
    subject = _pair_subject(gene, disease_id)
    claim_type = draw(
        st.sampled_from(
            [
                ClaimType.ASSOCIATION,
                ClaimType.DATATYPE_BREAKDOWN,
                ClaimType.CO_MENTION_COUNT,
                ClaimType.LITERATURE_COUNT,
                ClaimType.RECENT_PAPER,
            ]
        )
    )

    if claim_type is ClaimType.ASSOCIATION:
        # Score is not itself a sentinel string, but an unsupported ASSOCIATION
        # must still contribute nothing (it must not create/rerank a section).
        value: object = draw(_scores)
        claim_subject = subject
    elif claim_type is ClaimType.DATATYPE_BREAKDOWN:
        # Sentinel datatype ids: the banner guarantees they cannot equal any of
        # the six real OT datatype ids the renderer would legitimately print.
        value = (
            DatatypeScore(id=f"{_SENTINEL_BANNER}_DT_{nonce}", score=draw(_scores)),
        )
        claim_subject = subject
    elif claim_type is ClaimType.CO_MENTION_COUNT:
        value = _SENTINEL_COUNT + nonce
        claim_subject = subject
    elif claim_type is ClaimType.LITERATURE_COUNT:
        value = _SENTINEL_COUNT + nonce
        # LITERATURE_COUNT is gene-scoped; use the bare gene subject.
        claim_subject = gene
    else:  # RECENT_PAPER
        value = f"{_SENTINEL_PMID_PREFIX}{nonce:04d}"
        # Draw gene-scoped vs pair-scoped so both recent-paper paths are covered.
        claim_subject = draw(st.sampled_from([gene, subject]))

    return Claim(
        claim_id=f"UNSUP:{claim_type.value}:{claim_subject}:{nonce}",
        claim_type=claim_type,
        subject=claim_subject,
        value=value,
        evidence_ids=(f"missing_or_tampered:{nonce}",),
        status=ClaimStatus.UNSUPPORTED,
        reason="sentinel unsupported claim (must never reach prose)",
    )


@st.composite
def mixed_render_input(
    draw: st.DrawFn,
) -> tuple[str, list[Claim], list[Claim], dict[str, str]]:
    """Draw ``(gene, verified, unsupported, names)`` mixing VERIFIED + sentinels.

    ``verified`` comes from :func:`render_input` (all VERIFIED). ``unsupported``
    is a list of sentinel-bearing UNSUPPORTED claims: some for fresh sentinel
    diseases, and — importantly — at least one whose disease is SHARED with a
    verified disease, so the test proves an unsupported claim contributes nothing
    even inside a disease that does render. ``names`` includes display names for
    the sentinel diseases too (so if a sentinel leaked, its name could surface).
    """
    gene, verified_claims, names = draw(render_input())
    names = dict(names)

    verified_disease_ids = sorted(
        {
            _disease_id_of(c)
            for c in verified_claims
            if _disease_id_of(c)
        }
    )

    unsupported: list[Claim] = []
    nonce = 0

    # (a) A fresh sentinel disease whose id AND name are distinctive sentinels.
    n_fresh = draw(st.integers(min_value=1, max_value=3))
    for _ in range(n_fresh):
        sentinel_disease_id = f"{_SENTINEL_BANNER}_DIS_{nonce}"
        names[sentinel_disease_id] = f"{_SENTINEL_BANNER} disease {nonce}"
        unsupported.append(
            draw(unsupported_claim(gene, sentinel_disease_id, nonce))
        )
        nonce += 1

    # (b) At least one UNSUPPORTED claim sharing a disease with a VERIFIED claim,
    # so the shared disease's section must still show none of its sentinels.
    if verified_disease_ids:
        shared = draw(st.sampled_from(verified_disease_ids))
        unsupported.append(draw(unsupported_claim(gene, shared, nonce)))
        nonce += 1

    # (c) A few more unsupported claims across fresh + shared diseases.
    extra = draw(st.integers(min_value=0, max_value=3))
    candidate_diseases = verified_disease_ids + [
        did for did in names if _SENTINEL_BANNER in did
    ]
    for _ in range(extra):
        if not candidate_diseases:
            break
        target = draw(st.sampled_from(candidate_diseases))
        unsupported.append(draw(unsupported_claim(gene, target, nonce)))
        nonce += 1

    return gene, list(verified_claims), unsupported, names


# --------------------------------------------------------------------------- #
# Property 4.1 — unsupported claims make NO difference to the output, and none  #
# of their sentinels appear in the rendered text (Req 7.2, 8.1).               #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=mixed_render_input(), perm_data=st.data())
def test_unsupported_claims_contribute_nothing_to_prose(
    data: tuple[str, list[Claim], list[Claim], dict[str, str]],
    perm_data: st.DataObject,
) -> None:
    """render(verified + unsupported) == render(verified), and no sentinel leaks.

    Validates Requirements 7.2 and 8.1.
    """
    gene, verified, unsupported, names = data
    renderer = DossierRenderer()

    verified_only = renderer.render(verified, gene_symbol=gene, disease_names=names)

    # The mixed set, shuffled so no positional coincidence masks a leak.
    mixed = perm_data.draw(st.permutations(list(verified) + list(unsupported)))
    mixed_out = renderer.render(list(mixed), gene_symbol=gene, disease_names=names)

    # (1) Unsupported claims make no difference whatsoever (Req 7.2, 8.1).
    assert mixed_out == verified_only

    # (2) No distinctive sentinel from any unsupported claim appears in the text.
    for claim in unsupported:
        for token in _sentinel_tokens_of(claim):
            assert token not in mixed_out, (
                f"unsupported {claim.claim_type.value} sentinel {token!r} leaked "
                "into rendered prose"
            )


# --------------------------------------------------------------------------- #
# Property 4.2 — degenerate case: ALL claims unsupported renders the same as an #
# empty verified set, and contains none of their sentinels (Req 7.2, 8.1).     #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(data=mixed_render_input())
def test_all_unsupported_renders_like_empty_verified_set(
    data: tuple[str, list[Claim], list[Claim], dict[str, str]],
) -> None:
    """An all-unsupported set renders identically to an empty verified set.

    Validates Requirements 7.2 and 8.1. The unsupported claims are the mixed
    set's unsupported half plus the verified half forcibly marked UNSUPPORTED,
    so EVERY claim is unsupported; the output must equal rendering no claims at
    all, and contain none of the unsupported sentinels.
    """
    gene, verified, unsupported, names = data
    renderer = DossierRenderer()

    # Force the verified backbone to UNSUPPORTED too, so nothing is verified.
    all_unsupported = [
        Claim(
            claim_id=c.claim_id,
            claim_type=c.claim_type,
            subject=c.subject,
            value=c.value,
            evidence_ids=c.evidence_ids,
            status=ClaimStatus.UNSUPPORTED,
            reason="forced unsupported for degenerate-case test",
        )
        for c in verified
    ] + list(unsupported)

    empty_out = renderer.render([], gene_symbol=gene, disease_names=names)
    all_unsupported_out = renderer.render(
        all_unsupported, gene_symbol=gene, disease_names=names
    )

    assert all_unsupported_out == empty_out

    # None of the unsupported sentinels appear (checked for the synthesized ones).
    for claim in unsupported:
        for token in _sentinel_tokens_of(claim):
            assert token not in all_unsupported_out
