"""Example-based tests for the Open Targets parsers against the real fixtures.

Task 6.3. Per STEERING RULE 8 (Req 4.6, 12.8) the OT parsers are exercised
against the *committed real responses* loaded by the conftest envelope
fixtures, never against assumed shapes. These are example-based (not Hypothesis)
assertions that pin the exact behaviour task 6.1/6.2 must exhibit:

Search + resolution (Req 1.3)
  * :func:`parse_search_hits` on the real OT search payload yields the single
    EGFR target hit.
  * :func:`resolve` matches the symbol case- and whitespace-insensitively,
    returns ``NOT_FOUND`` when no target hit exists, and ``DISAMBIGUATE``
    (auto-selecting nothing) when target hits exist but none name-match.
  * :func:`resolve_chosen` turns a chosen hit into a ``RESOLVED`` outcome.

Associations (Req 2.3, 2.5, 2.6)
  * :func:`parse_associations` on the real payload: integer ``count == 5860``
    (not a bool), five selected rows sorted by descending score, the top row
    carrying six datatype scores and the "cancer" row exactly three, with
    datatype scores matched **by id, never by position**.
  * The crafted malformed fixture flags incomplete rows and keeps parsing: all
    rows are represented, the single complete row sorts ahead of the incomplete
    ones, ``incomplete_count`` equals the number of broken rows, and no
    exception is raised.
  * :func:`parse_datatype_scores` on an out-of-order / variable-length list
    preserves every valid entry keyed by id and does not rely on order.
"""

from __future__ import annotations

import math
from typing import Any

from claimledger.clients.opentargets import (
    ResolveStatus,
    SearchHit,
    parse_associations,
    parse_datatype_scores,
    parse_search_hits,
    resolve,
    resolve_chosen,
)

# Pinned identities from the real EGFR captures (data/cache/*.json).
EGFR_ENSEMBL_ID = "ENSG00000146648"
NSCLC_NAME = "non-small cell lung carcinoma"
NSCLC_ID = "MONDO_0005233"
CANCER_NAME = "cancer"
CANCER_ID = "MONDO_0004992"
# An arbitrary non-empty Evidence_ID passed to the pure parser (not minted here).
EVIDENCE_ID = "ot_associations:deadbeefdeadbeef"


# --------------------------------------------------------------------------- #
# Search + resolution (Req 1.3, 1.5, 1.6, 1.7)
# --------------------------------------------------------------------------- #


def test_parse_search_hits_on_real_payload_is_single_egfr_target(
    ot_search_envelope: dict[str, Any],
) -> None:
    """parse_search_hits on the real OT search payload yields one EGFR hit (Req 1.3)."""
    hits = parse_search_hits(ot_search_envelope["payload"])

    assert hits == [
        SearchHit(id=EGFR_ENSEMBL_ID, name="EGFR", entity="target"),
    ]


def test_resolve_exact_symbol_resolves_to_egfr_ensembl_id(
    ot_search_envelope: dict[str, Any],
) -> None:
    """resolve('EGFR', hits) -> RESOLVED with the EGFR Ensembl_ID (Req 1.3)."""
    hits = parse_search_hits(ot_search_envelope["payload"])

    outcome = resolve("EGFR", hits)

    assert outcome.status is ResolveStatus.RESOLVED
    assert outcome.ensembl_id == EGFR_ENSEMBL_ID
    assert outcome.resolved_symbol == "EGFR"


def test_resolve_is_case_and_whitespace_insensitive(
    ot_search_envelope: dict[str, Any],
) -> None:
    """resolve('egfr') and resolve(' EGFR ') also RESOLVE to EGFR (Req 1.3)."""
    hits = parse_search_hits(ot_search_envelope["payload"])

    for symbol in ("egfr", " EGFR "):
        outcome = resolve(symbol, hits)
        assert outcome.status is ResolveStatus.RESOLVED, symbol
        assert outcome.ensembl_id == EGFR_ENSEMBL_ID, symbol
        assert outcome.resolved_symbol == "EGFR", symbol


def test_resolve_target_hit_with_no_name_match_disambiguates_without_autoselect() -> None:
    """A target hit whose name does not match -> DISAMBIGUATE, no auto-select (Req 1.6)."""
    hits = [
        SearchHit(id="ENSG00000000001", name="SOMETHINGELSE", entity="target"),
    ]

    outcome = resolve("EGFR", hits)

    assert outcome.status is ResolveStatus.DISAMBIGUATE
    # The target hits are handed back for the UI to choose from...
    assert outcome.hits == tuple(hits)
    # ...and nothing is auto-selected.
    assert outcome.ensembl_id is None
    assert outcome.resolved_symbol is None


def test_resolve_no_target_entity_is_not_found() -> None:
    """Hits with no target entity -> NOT_FOUND (Req 1.5)."""
    hits = [
        SearchHit(id="EFO_0000001", name="EGFR", entity="disease"),
        SearchHit(id="CHEMBL1", name="gefitinib", entity="drug"),
    ]

    outcome = resolve("EGFR", hits)

    assert outcome.status is ResolveStatus.NOT_FOUND
    assert outcome.ensembl_id is None
    assert outcome.hits == ()


def test_resolve_chosen_returns_resolved_with_hit_identity() -> None:
    """resolve_chosen(hit) -> RESOLVED carrying that hit's id/name (Req 1.7)."""
    hit = SearchHit(id=EGFR_ENSEMBL_ID, name="EGFR", entity="target")

    outcome = resolve_chosen(hit)

    assert outcome.status is ResolveStatus.RESOLVED
    assert outcome.ensembl_id == EGFR_ENSEMBL_ID
    assert outcome.resolved_symbol == "EGFR"


# --------------------------------------------------------------------------- #
# Associations on the real payload (Req 2.3, 2.5, 2.7)
# --------------------------------------------------------------------------- #


def test_parse_associations_count_is_int_not_bool(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """associatedDiseases.count parses to the integer 5860, not a bool (Req 2.3)."""
    parsed = parse_associations(ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID)

    assert parsed.count == 5860
    assert isinstance(parsed.count, int)
    assert not isinstance(parsed.count, bool)


def test_parse_associations_target_identity(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """The target identity is read from data.target.{id, approvedSymbol, approvedName}."""
    parsed = parse_associations(ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID)

    assert parsed.target_id == EGFR_ENSEMBL_ID
    assert parsed.approved_symbol == "EGFR"
    assert parsed.approved_name == "epidermal growth factor receptor"


def test_parse_associations_selects_five_rows_sorted_by_descending_score(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """Exactly five associations are selected, ordered by descending score (Req 2.7)."""
    parsed = parse_associations(ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID)

    assert len(parsed.associations) == 5

    scores = [a.overall_score for a in parsed.associations]
    assert scores == sorted(scores, reverse=True)

    # Every selected row carries the passed-in Evidence_ID and is complete.
    assert all(a.evidence_id == EVIDENCE_ID for a in parsed.associations)
    assert all(not a.incomplete for a in parsed.associations)
    assert parsed.incomplete_count == 0


def test_parse_associations_top_row_is_nsclc_with_six_datatype_scores(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """Top association is NSCLC (~0.8503) with six datatype scores matched by id (Req 2.4, 2.5)."""
    parsed = parse_associations(ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID)

    top = parsed.associations[0]
    assert top.disease_name == NSCLC_NAME
    assert top.disease_id == NSCLC_ID
    assert math.isclose(top.overall_score, 0.8503239816603523, abs_tol=1e-9)

    # Six datatype scores, each uniquely keyed by id.
    assert len(top.datatype_scores) == 6
    by_id = {ds.id: ds.score for ds in top.datatype_scores}
    assert len(by_id) == 6
    # Assert scores are matched BY ID, not by position.
    assert math.isclose(by_id["literature"], 0.9996584498625893, abs_tol=1e-9)
    assert math.isclose(by_id["clinical"], 0.9959475193895682, abs_tol=1e-9)
    assert math.isclose(by_id["somatic_mutation"], 0.8315878016229145, abs_tol=1e-9)
    assert math.isclose(by_id["affected_pathway"], 0.8209966976180988, abs_tol=1e-9)
    assert math.isclose(by_id["genetic_association"], 0.6991204172533643, abs_tol=1e-9)
    assert math.isclose(by_id["genetic_literature"], 0.607930797611621, abs_tol=1e-9)


def test_parse_associations_cancer_row_has_exactly_three_datatype_scores_by_id(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """The "cancer" row carries exactly three datatype scores matched by id (Req 2.5).

    In the real payload this row's datatypeScores are ordered
    literature, affected_pathway, clinical -- a different order from the
    six-score top row -- so matching must be by id, never by position.
    """
    parsed = parse_associations(ot_associations_envelope["payload"], top_n=5, evidence_id=EVIDENCE_ID)

    cancer = next(a for a in parsed.associations if a.disease_id == CANCER_ID)
    assert cancer.disease_name == CANCER_NAME

    assert len(cancer.datatype_scores) == 3
    by_id = {ds.id: ds.score for ds in cancer.datatype_scores}
    assert set(by_id) == {"literature", "affected_pathway", "clinical"}
    assert math.isclose(by_id["literature"], 0.9941497202576182, abs_tol=1e-9)
    assert math.isclose(by_id["affected_pathway"], 0.9919541334408821, abs_tol=1e-9)
    assert math.isclose(by_id["clinical"], 0.792415739687779, abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# Associations on the crafted malformed fixture (Req 2.6)
# --------------------------------------------------------------------------- #


def test_parse_associations_flags_incomplete_rows_and_continues(
    malformed_ot_associations_envelope: dict[str, Any],
) -> None:
    """Malformed rows are flagged incomplete=True and parsing continues (Req 2.6).

    The crafted fixture has one complete row (NSCLC) and three broken rows
    (one missing ``score``, one missing ``disease.name``, one missing
    ``disease`` entirely). Parsing must not raise, every row must be
    represented, the complete row must sort ahead of the incomplete ones, and
    ``incomplete_count`` must equal the number of broken rows.
    """
    payload = malformed_ot_associations_envelope["payload"]
    raw_rows = payload["data"]["target"]["associatedDiseases"]["rows"]
    broken = sum(
        1
        for r in raw_rows
        if "score" not in r
        or not isinstance(r.get("disease"), dict)
        or "name" not in r.get("disease", {})
    )

    # No exception is raised, and top_n is large enough to keep every row.
    parsed = parse_associations(payload, top_n=10, evidence_id=EVIDENCE_ID)

    # All four rows are represented (none dropped).
    assert len(parsed.associations) == len(raw_rows) == 4
    assert parsed.incomplete_count == broken == 3

    # Exactly the complete NSCLC row is marked complete; the rest are incomplete.
    complete = [a for a in parsed.associations if not a.incomplete]
    assert len(complete) == 1
    assert complete[0].disease_id == NSCLC_ID

    # The complete row sorts ahead of every incomplete row.
    assert parsed.associations[0].disease_id == NSCLC_ID
    assert not parsed.associations[0].incomplete
    assert all(a.incomplete for a in parsed.associations[1:])


def test_parse_associations_on_malformed_fixture_preserves_partial_datatypes(
    malformed_ot_associations_envelope: dict[str, Any],
) -> None:
    """Incomplete rows still retain their valid datatype scores, keyed by id (Req 2.5, 2.6)."""
    payload = malformed_ot_associations_envelope["payload"]
    parsed = parse_associations(payload, top_n=10, evidence_id=EVIDENCE_ID)

    # The row missing disease.name (MONDO_0004992 in the malformed fixture) keeps
    # its two valid datatype scores matched by id.
    row = next(a for a in parsed.associations if a.disease_id == "MONDO_0004992")
    assert row.incomplete
    by_id = {ds.id: ds.score for ds in row.datatype_scores}
    assert math.isclose(by_id["literature"], 0.9941497202576182, abs_tol=1e-9)
    assert math.isclose(by_id["clinical"], 0.792415739687779, abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# parse_datatype_scores: by-id, order-independent, variable-length (Req 2.5)
# --------------------------------------------------------------------------- #


def test_parse_datatype_scores_is_order_independent_and_keyed_by_id() -> None:
    """An out-of-order, variable-length list preserves all entries by id (Req 2.5)."""
    raw = [
        {"id": "literature", "score": 0.99},
        {"id": "clinical", "score": 0.80},
        {"id": "somatic_mutation", "score": 0.42},
    ]
    shuffled = [raw[1], raw[2], raw[0]]

    scores_a, incomplete_a = parse_datatype_scores(raw)
    scores_b, incomplete_b = parse_datatype_scores(shuffled)

    assert incomplete_a is False
    assert incomplete_b is False

    # Keying by id makes both orderings equivalent as a mapping.
    by_id_a = {ds.id: ds.score for ds in scores_a}
    by_id_b = {ds.id: ds.score for ds in scores_b}
    assert by_id_a == by_id_b
    assert by_id_a == {
        "literature": 0.99,
        "clinical": 0.80,
        "somatic_mutation": 0.42,
    }


def test_parse_datatype_scores_skips_invalid_entries_and_flags_incomplete() -> None:
    """A mistyped/missing entry is skipped (not fabricated) and sets incomplete (Req 2.5, 2.6)."""
    raw = [
        {"id": "clinical", "score": 0.8},
        {"id": "literature"},            # missing score
        {"score": 0.5},                  # missing id
        {"id": "genetic_association", "score": True},  # bool is not a real score
        {"id": "somatic_mutation", "score": 0.3},
    ]

    scores, incomplete = parse_datatype_scores(raw)

    assert incomplete is True
    by_id = {ds.id: ds.score for ds in scores}
    # Only the two well-formed entries survive, keyed by id.
    assert by_id == {"clinical": 0.8, "somatic_mutation": 0.3}
