"""Property 10: Evidence_ID stability (pytest + Hypothesis).

**Validates: Requirements 4.2, 11.7**

The Evidence_ID is a deterministic function of the *logical request*, never of
wall-clock time or object identity, so a cached fetch and a live fetch of the
same request mint the **same** id. These property tests assert that invariant
across the input space the normalization is meant to collapse:

1. Determinism + shape of :func:`mint_evidence_id` for any source and key.
2. OT_SEARCH parity: surrounding whitespace / letter case on a gene symbol does
   not change the minted id.
3. PubMed parity: ``tool`` / ``email`` values and query-param ordering do not
   change the minted id.
4. Envelope parity (Req 11.7): a record built from an envelope mints
   ``mint_evidence_id(source, key)`` regardless of ``from_cache``, and preserves
   ``source_url`` -> url / ``retrieved_at`` / ``payload``.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.evidence import (
    mint_evidence_id,
    ot_search_request_key,
    pubmed_request_key,
    record_from_envelope,
)
from claimledger.models import Source
from tests.strategies import (
    normalized_request_keys,
    pubmed_param_sets,
    sources,
    whitespace_padded_symbols,
)

# `source.value + ":" + 16 lowercase hex chars`
_ID_SHAPE = re.compile(r"^[a-z_]+:[0-9a-f]{16}$")

_PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


# --------------------------------------------------------------------------- #
# Property 10.1 — determinism and shape                                       #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(source=sources, key=normalized_request_keys)
def test_mint_is_deterministic_and_shaped(source: Source, key: str) -> None:
    """Repeated minting yields the same id, shaped ``source.value:16 hex``."""
    first = mint_evidence_id(source, key)
    second = mint_evidence_id(source, key)

    assert first == second

    prefix, _, digest = first.partition(":")
    assert prefix == source.value
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert _ID_SHAPE.match(first)


# --------------------------------------------------------------------------- #
# Property 10.2 — OT_SEARCH cache/live parity                                 #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(pair=whitespace_padded_symbols())
def test_ot_search_symbol_normalization_parity(pair: tuple[str, str]) -> None:
    """A whitespace/case-perturbed symbol mints the same id as its clean form.

    The "cached" side is the already-normalized (trimmed, upper-cased) symbol;
    the "live" side is the same symbol with arbitrary surrounding whitespace and
    arbitrary letter case. Normalization in ``ot_search_request_key`` must make
    both map to one Evidence_ID.
    """
    logical, perturbed = pair

    clean_key = ot_search_request_key(logical)
    perturbed_key = ot_search_request_key(perturbed)

    assert clean_key == perturbed_key
    assert mint_evidence_id(Source.OT_SEARCH, clean_key) == mint_evidence_id(
        Source.OT_SEARCH, perturbed_key
    )


def test_ot_search_dotted_capital_i_regression() -> None:
    """Regression for the U+0130 counterexample (shrunk by Hypothesis).

    ``İ`` (U+0130, LATIN CAPITAL LETTER I WITH DOT ABOVE) and its lower-cased
    form ``i̇`` (``'i\\u0307'``, i + COMBINING DOT ABOVE) are the same logical
    letter but do not round-trip through ``.upper()``/``.lower()``. The original
    ``.upper()``-based normalization minted two different Evidence_IDs for them;
    NFKC + casefold collapses them to one.
    """
    logical = "\u0130"       # İ
    perturbed = "i\u0307"    # i + combining dot above (İ lower-cased)

    assert ot_search_request_key(logical) == ot_search_request_key(perturbed)
    assert mint_evidence_id(
        Source.OT_SEARCH, ot_search_request_key(logical)
    ) == mint_evidence_id(Source.OT_SEARCH, ot_search_request_key(perturbed))


# --------------------------------------------------------------------------- #
# Property 10.3 — PubMed cache/live parity                                    #
# --------------------------------------------------------------------------- #
def _build_url(params: list[tuple[str, str]]) -> str:
    return f"{_PUBMED_BASE}?{urlencode(params)}"


@settings(max_examples=200)
@given(
    base_params=pubmed_param_sets,
    tool_a=st.text(min_size=0, max_size=12),
    tool_b=st.text(min_size=0, max_size=12),
    email_a=st.emails(),
    email_b=st.emails(),
    data=st.data(),
)
def test_pubmed_key_invariant_to_identity_and_order(
    base_params: dict[str, str],
    tool_a: str,
    tool_b: str,
    email_a: str,
    email_b: str,
    data: st.DataObject,
) -> None:
    """Minting over a PubMed URL is invariant to tool/email and param ordering.

    Two URLs for the same logical esearch — one with ``tool``/``email`` in one
    order, the other with different caller identity and a shuffled param order —
    must produce the same Evidence_ID (design cache/live parity, Req 11.7).
    """
    items = list(base_params.items())

    # URL A: base params in one order, carrying caller identity A.
    params_a = items + [("tool", tool_a), ("email", email_a)]

    # URL B: a permutation of the base params, carrying caller identity B, with
    # the identity params interleaved in a different position.
    permuted = data.draw(st.permutations(items))
    params_b = [("email", email_b)] + permuted + [("tool", tool_b)]

    key_a = pubmed_request_key(_build_url(params_a))
    key_b = pubmed_request_key(_build_url(params_b))

    assert key_a == key_b
    assert mint_evidence_id(Source.PUBMED_GENE, key_a) == mint_evidence_id(
        Source.PUBMED_GENE, key_b
    )


# --------------------------------------------------------------------------- #
# Property 10.4 — envelope parity (Req 11.7)                                   #
# --------------------------------------------------------------------------- #
@settings(max_examples=200)
@given(
    source=sources,
    key=normalized_request_keys,
    retrieved_at=st.text(min_size=1, max_size=30),
    payload=st.dictionaries(
        st.text(max_size=8),
        st.one_of(st.integers(), st.text(max_size=8), st.booleans()),
        max_size=4,
    ),
    from_cache=st.booleans(),
)
def test_envelope_record_parity_and_provenance(
    source: Source,
    key: str,
    retrieved_at: str,
    payload: dict,
    from_cache: bool,
) -> None:
    """A record from an envelope mints ``mint_evidence_id(source, key)`` and
    preserves provenance, independent of ``from_cache``.

    A cached fetch (``from_cache=True``) and a live fetch (``from_cache=False``)
    of the same logical request produce the identical Evidence_ID, and the
    record maps ``source_url`` -> url, ``retrieved_at`` -> retrieved_at,
    ``payload`` -> payload unchanged (Req 11.7).
    """
    source_url = f"{_PUBMED_BASE}?db=pubmed&term=x"
    envelope = {
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "payload": payload,
    }

    record = record_from_envelope(source, key, envelope, from_cache=from_cache)

    expected_id = mint_evidence_id(source, key)
    assert record.evidence_id == expected_id

    # from_cache must not change the id: build the mirror form and compare.
    mirror = record_from_envelope(source, key, envelope, from_cache=not from_cache)
    assert mirror.evidence_id == record.evidence_id == expected_id

    # Provenance preserved verbatim (Req 11.7).
    assert record.url == source_url
    assert record.retrieved_at == retrieved_at
    assert record.payload == payload
    assert record.source is source
    assert record.from_cache is from_cache
