"""Example-based tests for the PubMed parsers against the real fixtures.

Task 7.3. Per STEERING RULE 8 (Req 4.6, 12.8) the PubMed term formatter, the
esearch URL/param builders, and the response parser are exercised against the
*committed real responses* loaded by the conftest envelope fixtures
(``pubmed_gene_envelope``, ``pubmed_gene_and_disease_envelope``), never against
assumed shapes. These are example-based (not Hypothesis) assertions that pin the
exact behaviour tasks 7.1/7.2 must exhibit:

Term formatting (Req 3.4)
  * :func:`gene_term` wraps the symbol and appends ``[Title/Abstract]``.
  * :func:`pair_term` joins the gene and disease clauses with `` AND ``.

URL / params always carry tool + email (Req 3.10)
  * :func:`build_esearch_params` always emits ``db=pubmed``, ``retmode=json``,
    the default ``retmax`` of ``"3"``, the ``term``, and a non-empty ``tool`` /
    ``email`` — both when defaulted and when passed explicitly.
  * :func:`build_esearch_url` percent-encodes those into the query string so
    ``tool=`` and ``email=`` always ride on the wire.

Parsing the real fixtures (Req 3.2, 3.3, 3.5, 3.7, 3.8)
  * The gene fixture: string ``count`` -> int ``108097`` (count available),
    PMID ``idlist`` preserved in payload order, ``retmax`` / ``retstart`` -> ints.
  * The gene+disease fixture: ``count`` -> ``18950`` and the stored
    ``querytranslation`` is PubMed's MeSH rewrite, which *differs* from the
    submitted :func:`pair_term` (the Req 3.5 / 4.3 provenance point).
  * Count-unavailable handling: a missing / negative / non-integer / float-like
    ``count``, and a payload with no ``esearchresult`` at all, each degrade to
    ``count is None`` / ``count_available is False`` with no exception and never
    a fabricated number (Req 3.8, steering rule 3).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from claimledger.clients.pubmed import (
    PubMedResult,
    build_esearch_params,
    build_esearch_url,
    gene_term,
    pair_term,
    parse_esearch,
)

# Pinned identities from the real EGFR captures (data/cache/preflight_pubmed_*.json).
EGFR_SYMBOL = "EGFR"
DISEASE_NAME = "lung neoplasms"
GENE_COUNT = 108097
PAIR_COUNT = 18950
# PubMed rewrote the submitted free-text disease clause into a [MeSH Terms] form;
# this is the exact querytranslation the real gene+disease capture returned.
PAIR_QUERY_TRANSLATION = '"EGFR"[Title/Abstract] AND "lung neoplasms"[MeSH Terms]'
# The PMIDs exactly as the committed captures list them (order matters, Req 3.7).
GENE_IDLIST = ("42920723", "42920699", "42920570")
PAIR_IDLIST = ("42919317", "42917888", "42911709")


# --------------------------------------------------------------------------- #
# Term formatting (Req 3.4)
# --------------------------------------------------------------------------- #


def test_gene_term_wraps_symbol_with_title_abstract_tag() -> None:
    """``gene_term`` quotes the symbol and appends ``[Title/Abstract]`` (Req 3.4)."""
    assert gene_term("EGFR") == '"EGFR"[Title/Abstract]'


def test_pair_term_joins_gene_and_disease_with_and() -> None:
    """``pair_term`` joins two tagged clauses with `` AND `` (Req 3.4)."""
    assert (
        pair_term("EGFR", "lung neoplasms")
        == '"EGFR"[Title/Abstract] AND "lung neoplasms"[Title/Abstract]'
    )


# --------------------------------------------------------------------------- #
# URL / params always carry tool + email (Req 3.10)
# --------------------------------------------------------------------------- #


def test_build_esearch_params_carries_fixed_fields_and_default_retmax() -> None:
    """Defaulted params include db/retmode/retmax/term plus non-empty tool+email (Req 3.1, 3.6, 3.10)."""
    term = gene_term("EGFR")
    params = build_esearch_params(term)

    assert params["db"] == "pubmed"
    assert params["retmode"] == "json"
    assert params["retmax"] == "3"  # default from Config.thresholds.retmax (Req 3.6)
    assert params["term"] == term

    # tool + email always present and non-empty (Req 3.10).
    assert "tool" in params and params["tool"]
    assert "email" in params and params["email"]


def test_build_esearch_params_keeps_explicit_tool_and_email() -> None:
    """Explicitly passed tool/email remain present and non-empty (Req 3.10)."""
    params = build_esearch_params(
        gene_term("EGFR"), tool="mytool", email="me@example.org"
    )

    assert params["tool"] == "mytool"
    assert params["email"] == "me@example.org"
    # Still non-empty on the explicit path (Req 3.10).
    assert params["tool"]
    assert params["email"]


def test_build_esearch_url_encodes_tool_and_email_into_query() -> None:
    """The built URL's query string carries tool= and email= (URL-encoded) (Req 3.10)."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = build_esearch_params(gene_term("EGFR"))
    url = build_esearch_url(base, params)

    # Raw substring check: the keys ride on the wire.
    assert "tool=" in url
    assert "email=" in url

    # Decoded check: the values survive percent-encoding intact.
    query = parse_qs(urlsplit(url).query)
    assert query["tool"] == [params["tool"]]
    assert query["email"] == [params["email"]]
    assert query["db"] == ["pubmed"]
    assert query["retmode"] == ["json"]
    assert query["retmax"] == ["3"]
    assert query["term"] == [params["term"]]


# --------------------------------------------------------------------------- #
# Parsing the real gene fixture (Req 3.2, 3.3, 3.7)
# --------------------------------------------------------------------------- #


def test_parse_gene_fixture_converts_string_count_to_int(
    pubmed_gene_envelope: dict[str, Any],
) -> None:
    """The gene fixture's string ``count`` parses to int ``108097`` (Req 3.2)."""
    result = parse_esearch(pubmed_gene_envelope["payload"])

    assert result.count == GENE_COUNT
    assert isinstance(result.count, int)
    # A genuine non-negative int is available, never fabricated (Req 3.8).
    assert result.count_available is True


def test_parse_gene_fixture_preserves_idlist_order(
    pubmed_gene_envelope: dict[str, Any],
) -> None:
    """``idlist`` is a tuple of PMID strings in payload order (Req 3.3, 3.7)."""
    result = parse_esearch(pubmed_gene_envelope["payload"])

    assert result.idlist == GENE_IDLIST
    assert isinstance(result.idlist, tuple)
    assert all(isinstance(pmid, str) for pmid in result.idlist)


def test_parse_gene_fixture_parses_retmax_and_retstart_to_ints(
    pubmed_gene_envelope: dict[str, Any],
) -> None:
    """The string ``retmax`` / ``retstart`` window bounds parse to ints (Req 3.3)."""
    result = parse_esearch(pubmed_gene_envelope["payload"])

    assert result.retmax == 3
    assert result.retstart == 0
    assert isinstance(result.retmax, int)
    assert isinstance(result.retstart, int)


# --------------------------------------------------------------------------- #
# Parsing the real gene+disease fixture (Req 3.5, 4.3)
# --------------------------------------------------------------------------- #


def test_parse_pair_fixture_count_is_comention_total(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """The gene+disease fixture's ``count`` parses to int ``18950`` (Req 3.5)."""
    result = parse_esearch(pubmed_gene_and_disease_envelope["payload"])

    assert result.count == PAIR_COUNT
    assert isinstance(result.count, int)
    assert result.count_available is True


def test_parse_pair_fixture_stores_pubmed_query_translation(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """The stored ``querytranslation`` is PubMed's MeSH rewrite (Req 3.5)."""
    result = parse_esearch(pubmed_gene_and_disease_envelope["payload"])

    assert result.querytranslation == PAIR_QUERY_TRANSLATION


def test_pair_query_translation_differs_from_submitted_term(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """PubMed rewrote the disease clause to MeSH, so the stored translation
    differs from the submitted ``pair_term`` — the Req 3.5 / 4.3 provenance point.
    """
    submitted = pair_term(EGFR_SYMBOL, DISEASE_NAME)
    result = parse_esearch(pubmed_gene_and_disease_envelope["payload"])

    # Both are present and both are strings...
    assert submitted == '"EGFR"[Title/Abstract] AND "lung neoplasms"[Title/Abstract]'
    assert result.querytranslation == PAIR_QUERY_TRANSLATION
    # ...but they are NOT equal: the disease clause became [MeSH Terms] (Req 3.5).
    assert result.querytranslation != submitted


def test_parse_pair_fixture_preserves_idlist_order(
    pubmed_gene_and_disease_envelope: dict[str, Any],
) -> None:
    """The gene+disease ``idlist`` is preserved in payload order (Req 3.3, 3.7)."""
    result = parse_esearch(pubmed_gene_and_disease_envelope["payload"])

    assert result.idlist == PAIR_IDLIST


# --------------------------------------------------------------------------- #
# Count-unavailable handling (Req 3.8, steering rule 3)
# --------------------------------------------------------------------------- #


def test_parse_missing_count_is_unavailable_not_fabricated() -> None:
    """A payload whose ``esearchresult`` omits ``count`` yields count unavailable (Req 3.8)."""
    payload = {
        "esearchresult": {
            "idlist": ["111", "222"],
            "retmax": "3",
            "retstart": "0",
        }
    }
    result = parse_esearch(payload)

    assert result.count is None
    assert result.count_available is False
    # The rest of the payload still parses — a present idlist is kept, not dropped.
    assert result.idlist == ("111", "222")


def test_parse_negative_count_is_unavailable() -> None:
    """A negative ``count`` ("-5") is treated as unavailable, never fabricated (Req 3.8)."""
    result = parse_esearch({"esearchresult": {"count": "-5", "idlist": []}})

    assert result.count is None
    assert result.count_available is False
    assert result.idlist == ()


def test_parse_non_numeric_count_is_unavailable() -> None:
    """A non-numeric ``count`` ("abc") is treated as unavailable (Req 3.8)."""
    result = parse_esearch({"esearchresult": {"count": "abc", "idlist": []}})

    assert result.count is None
    assert result.count_available is False


def test_parse_float_like_count_is_unavailable() -> None:
    """A float-looking ``count`` ("1.5") is treated as unavailable (Req 3.8)."""
    result = parse_esearch({"esearchresult": {"count": "1.5", "idlist": []}})

    assert result.count is None
    assert result.count_available is False


def test_parse_payload_without_esearchresult_degrades_gracefully() -> None:
    """A payload with no ``esearchresult`` key yields count None, empty idlist, no exception (Req 3.8)."""
    result = parse_esearch({"header": {"type": "esearch"}})

    assert isinstance(result, PubMedResult)
    assert result.count is None
    assert result.count_available is False
    assert result.idlist == ()
