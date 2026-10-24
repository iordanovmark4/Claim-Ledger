"""Property 8: No-repair invariant (pytest + Hypothesis).

**Validates: Requirements 6.7, 13.4**

Verification is a *read-only gate*: recompute-and-compare, report a verdict, and
**never** mutate, repair, or substitute a claim's value — not on the Verified
path, not on the Unsupported path, and not even on the display-annotation path
(design "Verifier": pure, side-effect-free, no repair; steering rule 3). This
module pins that invariant directly.

For a mixed generator of claims across all five claim types — matching
(``VERIFIED``) and tampered/mismatching and missing-evidence (``UNSUPPORTED``) —
the test:

* snapshots ``claim.value`` (and ``claim.original_value``) *before*
  ``Verifier().verify(claim, store)`` and asserts the live claim object is
  byte-for-byte unchanged afterwards (``Claim`` is a frozen dataclass, so the
  very same object must still equal the pre-call snapshot and be ``is`` the same
  object);
* asserts :func:`~claimledger.verify.annotate` returns a claim whose ``value``
  (and ``original_value``) equals the input's — only ``status``/``reason`` may
  differ — and that when status/reason change the annotated claim is a *distinct*
  object while the input claim is left unmutated;
* asserts :func:`~claimledger.verify.verify_all` mutates no input claim's value
  and that every annotated output preserves its input's value; and
* deep-checks the ``DATATYPE_BREAKDOWN`` tuple — same ``DatatypeScore`` ids and
  scores, in the same order — is identical after both verify and annotate, for
  both verdict branches.

The invariant holds on *both* verdict branches, which is the whole point: a
mismatch is *reported*, never silently corrected (Req 6.7), and the tamper
control relies on the value being left exactly as handed in so a reset can
restore it (Req 13.4).
"""

from __future__ import annotations

import copy
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    DatatypeScore,
    Source,
)
from claimledger.recompute import (
    recompute_association,
    recompute_co_mention_count,
    recompute_datatype_breakdown,
    recompute_literature_count,
)
from claimledger.verify import Verifier, annotate, verify_all
from tests.strategies import (
    ot_association_rows,
    ot_associations_payload,
    pmids,
    pubmed_esearch_payload,
)

# Test-controlled Evidence_IDs. The PRESENT ids are added to the store; the
# ABSENT id is never added so a claim wired to it exercises the UNSUPPORTED
# missing-evidence branch while the no-repair invariant must still hold.
OT_PRESENT_ID = "ot_associations:present00000001"
GENE_PRESENT_ID = "pubmed_gene:present0000000001"
PAIR_PRESENT_ID = "pubmed_pair:present0000000001"
ABSENT_ID = "ot_associations:absent000000001"

GENE_SYMBOL = "SYNTH"
SUBJECT_SEP = "::"


# --------------------------------------------------------------------------- #
# Small local record builders (same record_from_envelope the prod path uses). #
# Kept local to avoid cross-test coupling.                                     #
# --------------------------------------------------------------------------- #
def _ot_record(rows: list[dict], evidence_id: str):
    """Build an ot_associations record with a forced id, wrapping ``rows``."""
    envelope = {
        "source_url": "https://synthetic.test/graphql",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": ot_associations_payload(rows),
    }
    record = record_from_envelope(Source.OT_ASSOCIATIONS, "synthetic-ot-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _pubmed_record(payload: dict, source: Source, evidence_id: str):
    """Build a PubMed record with a forced id, wrapping an esearch ``payload``."""
    envelope = {
        "source_url": "https://synthetic.test/esearch.fcgi",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": payload,
    }
    record = record_from_envelope(source, "synthetic-pubmed-key", envelope)
    return replace(record, evidence_id=evidence_id)


def _pair_subject(disease_id: str) -> str:
    return f"{GENE_SYMBOL}{SUBJECT_SEP}{disease_id}"


# --------------------------------------------------------------------------- #
# Deep structural equality for a claim value (DATATYPE_BREAKDOWN included).    #
# --------------------------------------------------------------------------- #
def _assert_value_identical(before, after, context: str) -> None:
    """Assert a claim value is unchanged — deep-checking a breakdown tuple.

    For a ``DATATYPE_BREAKDOWN`` value the tuple and each :class:`DatatypeScore`
    inside it must match on id *and* score, **in the same order** (the tuple is
    canonical/ordered); every other value type is compared with ``==`` (and type
    equality so an ``int`` count is never silently treated as equal to a ``bool``
    or ``float`` substitute).
    """
    # Both breakdown tuples?  Deep-check element-by-element, order included.
    if _is_breakdown(before) or _is_breakdown(after):
        assert _is_breakdown(before) and _is_breakdown(after), (
            f"{context}: breakdown shape changed {before!r} -> {after!r}"
        )
        assert len(before) == len(after), (
            f"{context}: breakdown length changed "
            f"{len(before)} -> {len(after)}"
        )
        for i, (b, a) in enumerate(zip(before, after)):
            assert isinstance(a, DatatypeScore), (
                f"{context}: breakdown[{i}] is not a DatatypeScore: {a!r}"
            )
            assert b.id == a.id, (
                f"{context}: breakdown[{i}] id changed {b.id!r} -> {a.id!r} "
                f"(order or id mutated)"
            )
            assert b.score == a.score, (
                f"{context}: breakdown[{i}] score changed "
                f"{b.score!r} -> {a.score!r}"
            )
        return
    # Non-breakdown values: exact type + value equality.
    assert type(before) is type(after), (
        f"{context}: value type changed {type(before)} -> {type(after)}"
    )
    assert before == after, f"{context}: value changed {before!r} -> {after!r}"


def _is_breakdown(value) -> bool:
    """True iff ``value`` is a tuple of :class:`DatatypeScore` (a breakdown)."""
    return isinstance(value, tuple) and all(
        isinstance(item, DatatypeScore) for item in value
    )


# --------------------------------------------------------------------------- #
# A store with one OT assoc record + gene/pair PubMed records.                #
# --------------------------------------------------------------------------- #
@st.composite
def _store_and_rows(draw):
    """Draw a populated store plus the OT rows / PubMed payloads behind it."""
    rows = draw(ot_association_rows())
    gene_payload = draw(pubmed_esearch_payload())
    pair_payload = draw(pubmed_esearch_payload())
    store = EvidenceStore()
    store.add(_ot_record(rows, OT_PRESENT_ID))
    store.add(_pubmed_record(gene_payload, Source.PUBMED_GENE, GENE_PRESENT_ID))
    store.add(_pubmed_record(pair_payload, Source.PUBMED_PAIR, PAIR_PRESENT_ID))
    return store, rows, gene_payload, pair_payload


# --------------------------------------------------------------------------- #
# A mixed generator: matching (VERIFIED) + tampered/missing (UNSUPPORTED).    #
# --------------------------------------------------------------------------- #
@st.composite
def _claim_and_store(draw):
    """Draw a ``(claim, store)`` spanning all five types and both verdicts.

    A claim type is chosen, a well-formed MATCHING claim is built from the store
    (value = recompute, ids present), then one of several mutations is optionally
    applied to push it into the UNSUPPORTED region:

    * value tampered beyond tolerance (score), off-by->=1 (count), not-in-idlist
      (PMID), a datatype score bumped or a datatype id dropped; or
    * the evidence id swapped for an ABSENT id (missing-evidence case).

    Both verdict branches are therefore exercised by the single no-repair
    assertion that consumes this generator.
    """
    store, rows, gene_payload, _pair_payload = draw(_store_and_rows())
    disease_ids = [row["disease"]["id"] for row in rows]
    disease_id = draw(st.sampled_from(disease_ids))
    record = store.get(OT_PRESENT_ID)

    claim_type = draw(
        st.sampled_from(
            [
                ClaimType.ASSOCIATION,
                ClaimType.DATATYPE_BREAKDOWN,
                ClaimType.LITERATURE_COUNT,
                ClaimType.CO_MENTION_COUNT,
                ClaimType.RECENT_PAPER,
            ]
        )
    )

    if claim_type is ClaimType.ASSOCIATION:
        subject = _pair_subject(disease_id)
        value = recompute_association(record, disease_id)
        eid = OT_PRESENT_ID
    elif claim_type is ClaimType.DATATYPE_BREAKDOWN:
        subject = _pair_subject(disease_id)
        value = recompute_datatype_breakdown(record, disease_id)
        eid = OT_PRESENT_ID
    elif claim_type is ClaimType.LITERATURE_COUNT:
        subject = GENE_SYMBOL
        value = recompute_literature_count(store.get(GENE_PRESENT_ID))
        eid = GENE_PRESENT_ID
    elif claim_type is ClaimType.CO_MENTION_COUNT:
        subject = _pair_subject(disease_id)
        value = recompute_co_mention_count(store.get(PAIR_PRESENT_ID))
        eid = PAIR_PRESENT_ID
    else:  # RECENT_PAPER
        subject = GENE_SYMBOL
        idlist = gene_payload["esearchresult"]["idlist"]
        if not idlist:
            # No PMIDs to build a matching RECENT_PAPER from: fall back to a
            # well-defined LITERATURE_COUNT matching base.
            subject = GENE_SYMBOL
            value = recompute_literature_count(store.get(GENE_PRESENT_ID))
            eid = GENE_PRESENT_ID
            claim_type = ClaimType.LITERATURE_COUNT
        else:
            value = draw(st.sampled_from(idlist))
            eid = GENE_PRESENT_ID

    claim = Claim(
        claim_id=f"{claim_type.value}:{subject}",
        claim_type=claim_type,
        subject=subject,
        value=value,
        evidence_ids=(eid,),
    )

    mutation = draw(st.sampled_from(["none", "value", "missing_evidence"]))
    if mutation == "missing_evidence":
        return replace(claim, evidence_ids=(ABSENT_ID,)), store
    if mutation == "value":
        claim = _perturb_value(draw, claim)
    return claim, store


def _perturb_value(draw, claim: Claim) -> Claim:
    """Push a matching claim's value into the UNSUPPORTED region, per type."""
    ct = claim.claim_type
    if ct is ClaimType.ASSOCIATION:
        delta = draw(st.floats(min_value=1e-6, max_value=1.0, allow_nan=False))
        return replace(claim, value=float(claim.value) + delta)
    if ct is ClaimType.DATATYPE_BREAKDOWN:
        breakdown = tuple(claim.value)
        if not breakdown:
            bogus = (DatatypeScore(id="__bogus__", score=0.5),)
            return replace(claim, value=bogus)
        how = draw(st.sampled_from(["bump", "drop"]))
        if how == "bump":
            first = breakdown[0]
            bumped = DatatypeScore(id=first.id, score=first.score + 0.5)
            return replace(claim, value=(bumped,) + breakdown[1:])
        return replace(claim, value=breakdown[1:])
    if ct in (ClaimType.LITERATURE_COUNT, ClaimType.CO_MENTION_COUNT):
        delta = draw(st.integers(min_value=1, max_value=1000))
        sign = draw(st.sampled_from([1, -1]))
        new = int(claim.value) + sign * delta
        if new < 0:
            new = int(claim.value) + delta
        return replace(claim, value=new)
    # RECENT_PAPER: a PMID not in the idlist (non-digit prefix guarantees it).
    return replace(claim, value="X" + draw(pmids))


# --------------------------------------------------------------------------- #
# Property 8 — verify never mutates/repairs/substitutes the claim value.      #
# --------------------------------------------------------------------------- #
@settings(max_examples=300)
@given(data=_claim_and_store())
def test_verify_never_repairs_value(data) -> None:
    """``verify`` leaves ``claim.value``/``original_value`` identical (Req 6.7, 13.4).

    Holds on BOTH verdict branches: the snapshot taken before the call must equal
    the claim's value after, the frozen ``Claim`` object must be the very same
    object (``is``) and still equal its pre-call self, and a
    ``DATATYPE_BREAKDOWN`` tuple is deep-checked element-by-element.
    """
    claim, store = data

    # Deep snapshots taken BEFORE verify.
    value_before = copy.deepcopy(claim.value)
    original_before = copy.deepcopy(claim.original_value)
    claim_identity = claim  # same-object check
    claim_equal_snapshot = copy.deepcopy(claim)

    result = Verifier().verify(claim, store)

    # The verdict is genuinely one or the other — both branches are exercised.
    assert result.status in (ClaimStatus.VERIFIED, ClaimStatus.UNSUPPORTED)

    # value / original_value are byte-for-byte unchanged (deep-checked).
    _assert_value_identical(value_before, claim.value, "verify: value")
    _assert_value_identical(
        original_before, claim.original_value, "verify: original_value"
    )
    # Frozen dataclass: the live object is the same object and still equal to the
    # pre-call snapshot (no field — value, status, reason — was mutated in place).
    assert claim is claim_identity
    assert claim == claim_equal_snapshot

    # The result reports the claim value as-is (never a repaired substitute).
    _assert_value_identical(
        value_before, result.claim_value, "verify: result.claim_value"
    )


# --------------------------------------------------------------------------- #
# annotate preserves value; changes only status/reason; input left intact.    #
# --------------------------------------------------------------------------- #
@settings(max_examples=300)
@given(data=_claim_and_store())
def test_annotate_preserves_value_and_leaves_input_unmutated(data) -> None:
    """``annotate`` only sets status/reason; value/original_value are intact.

    Asserts the annotated claim's value (and original_value) equals the input's,
    that status/reason carry the verdict, that the INPUT claim is left unmutated
    (frozen), and that when status/reason actually change the annotated claim is
    a DIFFERENT object carrying an identical value (Req 6.7, 7.1).
    """
    claim, store = data

    value_before = copy.deepcopy(claim.value)
    original_before = copy.deepcopy(claim.original_value)
    input_snapshot = copy.deepcopy(claim)

    result = Verifier().verify(claim, store)
    annotated = annotate(claim, result)

    # Annotated value/original_value equal the input's (deep-checked breakdown).
    _assert_value_identical(value_before, annotated.value, "annotate: value")
    _assert_value_identical(
        original_before, annotated.original_value, "annotate: original_value"
    )
    # Only status/reason carry the verdict.
    assert annotated.status is result.status
    assert annotated.reason == result.reason
    # Every other identifying field is untouched.
    assert annotated.claim_id == claim.claim_id
    assert annotated.claim_type is claim.claim_type
    assert annotated.subject == claim.subject
    assert annotated.evidence_ids == claim.evidence_ids

    # The INPUT claim is left completely unmutated (frozen dataclass).
    assert claim == input_snapshot
    _assert_value_identical(value_before, claim.value, "annotate: input value")

    # When status/reason changed, the annotated claim is a DISTINCT object whose
    # value is nonetheless identical to the input's.
    if annotated.status is not claim.status or annotated.reason != claim.reason:
        assert annotated is not claim
        _assert_value_identical(
            claim.value, annotated.value, "annotate: distinct-object value"
        )


# --------------------------------------------------------------------------- #
# verify_all mutates no input value; annotated outputs preserve values.       #
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(claims_data=st.lists(_claim_and_store(), min_size=1, max_size=6))
def test_verify_all_does_not_mutate_input_values(claims_data) -> None:
    """``verify_all`` leaves every input claim's value intact (Req 6.7, 13.4).

    Snapshots a list of claims' values, runs ``verify_all`` against a single
    shared store, then asserts every original input claim is unchanged (same
    object, equal to its snapshot) and that each annotated output preserves its
    source claim's value, across both verdict branches.
    """
    # Build one shared store and a list of claims referencing it. Each draw
    # carries its own store, but they are structurally interchangeable: adding
    # every claim's backing records into one store keeps all present claims
    # verifiable while ABSENT-id claims stay UNSUPPORTED.
    store = EvidenceStore()
    claims: list[Claim] = []
    for claim, src_store in claims_data:
        # Copy each source store's records into the shared store (idempotent on
        # evidence_id); this keeps present-evidence claims verifiable.
        for eid in (OT_PRESENT_ID, GENE_PRESENT_ID, PAIR_PRESENT_ID):
            rec = src_store.get(eid)
            if rec is not None and not store.contains(eid):
                store.add(rec)
        claims.append(claim)

    # Deep snapshots + identity before verify_all.
    value_snapshots = [copy.deepcopy(c.value) for c in claims]
    claim_snapshots = [copy.deepcopy(c) for c in claims]
    identities = list(claims)

    verdicts = verify_all(Verifier(), claims, store)

    # Every input claim is unchanged: same object and equal to its snapshot.
    for i, claim in enumerate(claims):
        assert claim is identities[i]
        assert claim == claim_snapshots[i]
        _assert_value_identical(
            value_snapshots[i], claim.value, f"verify_all: input[{i}] value"
        )

    # Annotated outputs preserve their source claim's value. Pair each result's
    # annotated claim back to its source by claim_id (unique per generated claim
    # is not guaranteed, so match on claim_id + evidence_ids + original value via
    # the results list order, which mirrors the input order).
    assert len(verdicts.results) == len(claims)
    for (annotated, result), source, snap in zip(
        verdicts.results, claims, value_snapshots
    ):
        assert result.status in (ClaimStatus.VERIFIED, ClaimStatus.UNSUPPORTED)
        _assert_value_identical(
            snap, annotated.value, "verify_all: annotated value"
        )
        # annotate only touched status/reason.
        assert annotated.claim_id == source.claim_id
        assert annotated.subject == source.subject
        assert annotated.evidence_ids == source.evidence_ids

    # The verified/unsupported split also preserves values.
    for annotated in list(verdicts.verified) + list(verdicts.unsupported):
        if _is_breakdown(annotated.value):
            for ds in annotated.value:
                assert isinstance(ds, DatatypeScore)


# --------------------------------------------------------------------------- #
# Deep DATATYPE_BREAKDOWN check across BOTH verdict branches explicitly.       #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(st_rows=_store_and_rows(), which=st.data())
def test_breakdown_tuple_unchanged_after_verify_and_annotate(st_rows, which) -> None:
    """A DATATYPE_BREAKDOWN tuple is identical after verify + annotate.

    Builds both a MATCHING breakdown (-> VERIFIED) and a TAMPERED breakdown
    (a bumped score, -> UNSUPPORTED) and asserts the tuple and every
    :class:`DatatypeScore` (id, score, order) are unchanged through verify and
    through annotate on both branches (Req 6.7).
    """
    store, rows, _gene_payload, _pair_payload = st_rows
    record = store.get(OT_PRESENT_ID)
    disease_id = which.draw(st.sampled_from([row["disease"]["id"] for row in rows]))
    subject = _pair_subject(disease_id)
    breakdown = recompute_datatype_breakdown(record, disease_id)

    verifier = Verifier()

    # --- Matching breakdown -> VERIFIED, value untouched. ---
    matching = Claim(
        claim_id=f"DATATYPE_BREAKDOWN:{subject}",
        claim_type=ClaimType.DATATYPE_BREAKDOWN,
        subject=subject,
        value=breakdown,
        evidence_ids=(OT_PRESENT_ID,),
    )
    before = copy.deepcopy(matching.value)
    result = verifier.verify(matching, store)
    assert result.status is ClaimStatus.VERIFIED
    _assert_value_identical(before, matching.value, "breakdown verify (match)")
    annotated = annotate(matching, result)
    _assert_value_identical(before, annotated.value, "breakdown annotate (match)")
    _assert_value_identical(before, matching.value, "breakdown input after annotate")

    # --- Tampered breakdown -> UNSUPPORTED, value STILL untouched. ---
    if breakdown:
        first = breakdown[0]
        tampered_value = (
            DatatypeScore(id=first.id, score=first.score + 0.5),
        ) + breakdown[1:]
    else:
        tampered_value = (DatatypeScore(id="__bogus__", score=0.5),)
    tampered = Claim(
        claim_id=f"DATATYPE_BREAKDOWN:{subject}:tampered",
        claim_type=ClaimType.DATATYPE_BREAKDOWN,
        subject=subject,
        value=tampered_value,
        evidence_ids=(OT_PRESENT_ID,),
    )
    t_before = copy.deepcopy(tampered.value)
    t_result = verifier.verify(tampered, store)
    assert t_result.status is ClaimStatus.UNSUPPORTED
    _assert_value_identical(t_before, tampered.value, "breakdown verify (tamper)")
    t_annotated = annotate(tampered, t_result)
    _assert_value_identical(
        t_before, t_annotated.value, "breakdown annotate (tamper)"
    )
    _assert_value_identical(
        t_before, tampered.value, "breakdown tampered input after annotate"
    )
    # The verifier reports the (tampered) claim value as-is, never repaired.
    _assert_value_identical(
        t_before, t_result.claim_value, "breakdown result.claim_value (tamper)"
    )
