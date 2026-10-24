"""Assert the observed real shapes of the committed cache envelopes.

STEERING RULE 8 (Req 4.6, 12.8): parsers must be written against saved real
responses, never assumed shapes. These tests pin the shapes actually observed
in ``data/cache`` so the parsers in tasks 6 and 7 are built against reality:

* Open Targets ``associatedDiseases.count`` is an ``int``.
* PubMed ``esearchresult.count``/``retmax``/``retstart`` are ``str``.
* Open Targets rows carry *variable-length* ``datatypeScores`` (one row has 6,
  another has 3) and the datatype ids appear in a *different order* across
  rows, so parsers must key by id, never by position.
* ``querytranslation`` is present on the gene+disease PubMed response.
"""

from __future__ import annotations

from typing import Any


def test_envelopes_have_documented_three_key_shape(
    real_cache_envelopes: dict[str, dict[str, Any]],
) -> None:
    """Every committed cache file is a {source_url, retrieved_at, payload} envelope."""
    assert set(real_cache_envelopes) == {
        "ot_search",
        "ot_associations",
        "pubmed_gene",
        "pubmed_gene_and_disease",
    }
    for name, envelope in real_cache_envelopes.items():
        assert "source_url" in envelope, name
        assert "retrieved_at" in envelope, name
        assert "payload" in envelope, name
        assert isinstance(envelope["source_url"], str), name
        assert isinstance(envelope["retrieved_at"], str), name


def test_ot_search_resolves_single_egfr_target(
    ot_search_envelope: dict[str, Any],
) -> None:
    """The real OT search payload is a single EGFR target hit (Req 4.6)."""
    hits = ot_search_envelope["payload"]["data"]["search"]["hits"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["id"] == "ENSG00000146648"
    assert hit["name"] == "EGFR"
    assert hit["entity"] == "target"


def test_ot_associated_diseases_count_is_int(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """OT ``associatedDiseases.count`` is a real ``int`` (Req 4.6, 12.8)."""
    target = ot_associations_envelope["payload"]["data"]["target"]
    count = target["associatedDiseases"]["count"]
    # The real API returns a JSON number here; pin it as int (not bool).
    assert isinstance(count, int) and not isinstance(count, bool)
    assert count == 5860


def test_ot_rows_have_variable_length_datatype_scores(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """OT datatypeScores vary in length across rows (6 vs 3) (Req 2.5, 12.8)."""
    rows = ot_associations_envelope["payload"]["data"]["target"][
        "associatedDiseases"
    ]["rows"]
    lengths = [len(row["datatypeScores"]) for row in rows]
    # At least one row with 6 datatype scores and another with 3.
    assert 6 in lengths
    assert 3 in lengths


def test_ot_datatype_ids_differ_in_order_across_rows(
    ot_associations_envelope: dict[str, Any],
) -> None:
    """Datatype ids appear in a different order across rows (Req 2.5, 12.8).

    This is the reason parsers must key datatype scores by id, never by index.
    """
    rows = ot_associations_envelope["payload"]["data"]["target"][
        "associatedDiseases"
    ]["rows"]
    # The 6-length row and the 3-length row.
    six_row = next(r for r in rows if len(r["datatypeScores"]) == 6)
    three_row = next(r for r in rows if len(r["datatypeScores"]) == 3)

    six_ids = [ds["id"] for ds in six_row["datatypeScores"]]
    three_ids = [ds["id"] for ds in three_row["datatypeScores"]]

    # Every datatype id is a non-empty string and unique within its row.
    assert all(isinstance(i, str) and i for i in six_ids)
    assert len(set(six_ids)) == len(six_ids)
    assert len(set(three_ids)) == len(three_ids)

    # The shared ids appear in a different relative order between the two rows,
    # proving position is not a stable key.
    shared = [i for i in six_ids if i in three_ids]
    order_in_six = [i for i in six_ids if i in shared]
    order_in_three = [i for i in three_ids if i in shared]
    assert order_in_six != order_in_three


def test_pubmed_gene_counts_are_strings(
    pubmed_gene_envelope: dict[str, Any],
) -> None:
    """PubMed count/retmax/retstart are strings on the real response (Req 3.2, 12.8)."""
    result = pubmed_gene_envelope["payload"]["esearchresult"]
    assert isinstance(result["count"], str)
    assert isinstance(result["retmax"], str)
    assert isinstance(result["retstart"], str)
    # The string really does parse to a non-negative int (parser's job later).
    assert result["count"] == "108097"
    assert int(result["count"]) >= 0
    assert isinstance(result["idlist"], list)


def test_pubmed_gene_and_disease_counts_are_strings_with_querytranslation(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """Gene+disease response: string counts and a present querytranslation (Req 3.5, 3.8)."""
    result = pubmed_gene_and_disease_envelope["payload"]["esearchresult"]
    assert isinstance(result["count"], str)
    assert isinstance(result["retmax"], str)
    assert isinstance(result["retstart"], str)

    # querytranslation is present and non-empty on the gene+disease response.
    assert "querytranslation" in result
    assert isinstance(result["querytranslation"], str)
    assert result["querytranslation"].strip()


def test_malformed_ot_fixture_has_envelope_and_mixed_rows(
    malformed_ot_associations_envelope: dict[str, Any],
) -> None:
    """The crafted malformed OT fixture is valid JSON with the real envelope
    shape and carries both a complete row and at least one incomplete row,
    so task 6.2 can exercise the Req 2.6 incomplete-row path (Req 2.6, 12.8).
    """
    envelope = malformed_ot_associations_envelope

    # Same three-key envelope as the real captures.
    assert set(envelope) >= {"source_url", "retrieved_at", "payload"}
    assert isinstance(envelope["source_url"], str)
    assert isinstance(envelope["retrieved_at"], str)

    # Same data.target.associatedDiseases.rows[] structure as the real capture.
    target = envelope["payload"]["data"]["target"]
    rows = target["associatedDiseases"]["rows"]
    assert isinstance(rows, list) and len(rows) >= 2

    def _is_complete(row: dict[str, Any]) -> bool:
        disease = row.get("disease")
        return (
            "score" in row
            and "datatypeScores" in row
            and isinstance(disease, dict)
            and "id" in disease
            and "name" in disease
        )

    complete_rows = [r for r in rows if _is_complete(r)]
    incomplete_rows = [r for r in rows if not _is_complete(r)]

    # At least one well-formed row and at least one row missing an expected field.
    assert complete_rows, "fixture must contain at least one complete row"
    assert incomplete_rows, "fixture must contain at least one incomplete row"

    # Each incomplete row is missing a *different* expected field so the parser's
    # per-field incomplete handling is covered: missing score, missing
    # disease.name, and missing disease entirely are all represented.
    assert any("score" not in r for r in rows)
    assert any(
        isinstance(r.get("disease"), dict) and "name" not in r["disease"]
        for r in rows
    )
    assert any("disease" not in r for r in rows)


def test_pubmed_querytranslation_differs_from_submitted_term(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """The returned querytranslation differs from the submitted term (Req 3.8)."""
    result = pubmed_gene_and_disease_envelope["payload"]["esearchresult"]
    # The submitted term is visible in the source_url; the translation PubMed
    # returned is the canonicalized MeSH form, which is not identical.
    source_url = pubmed_gene_and_disease_envelope["source_url"]
    assert "term=" in source_url
    assert result["querytranslation"] == (
        '"EGFR"[Title/Abstract] AND "lung neoplasms"[MeSH Terms]'
    )
