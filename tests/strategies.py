"""Hypothesis strategies for Association, DatatypeScore, Claim, and Evidence.

Populated as the property-based tests are implemented. These strategies are
shared across property-based test modules so the generated input space is
defined once and constrained intelligently to each type's real domain.
"""

from __future__ import annotations

from hypothesis import strategies as st

from claimledger.models import Source

# --------------------------------------------------------------------------- #
# Evidence_ID minting inputs (Property 10)                                    #
# --------------------------------------------------------------------------- #

# Any Source member — mint_evidence_id prefixes the id with source.value, so the
# property must hold for every interface that produces evidence.
sources = st.sampled_from(list(Source))

# Arbitrary normalized request keys. Evidence_ID minting is a pure function of
# an opaque key string, so the determinism/shape property holds for *any* text,
# including empty, unicode, and whitespace-bearing strings.
normalized_request_keys = st.text()


# A gene symbol as a *normalized* token: non-empty, no surrounding or internal
# whitespace, so that wrapping it in arbitrary whitespace below is the only
# whitespace present. Upper/lower mix is produced by the case-perturbation
# helper, not here, to keep the "logical symbol" well-defined.
def _is_clean_symbol(s: str) -> bool:
    return bool(s) and s == s.strip() and not any(ch.isspace() for ch in s)


# ASCII letters + digits + hyphen: the genuine gene-symbol domain. HGNC gene
# symbols are ASCII (uppercase Latin letters, digits, hyphens) — e.g. EGFR,
# TP53, BRCA1, HLA-A. The alphabet is deliberately restricted to this domain
# rather than all Unicode letters because non-ASCII letters are NOT gene symbols
# and carry Turkish-i style case-mapping pathologies (U+0131 "ı" vs ASCII "I",
# U+0130 "İ"): a per-character case perturbation of such a letter does not yield
# a case-variant of the *same* logical token, so it would be an invalid input to
# this parity property, not a real counterexample. (Documented domain narrowing;
# the U+0130 non-round-trip that a broader alphabet surfaced is still pinned by
# test_ot_search_dotted_capital_i_regression and fixed in the implementation.)
_SYMBOL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"

gene_symbols = st.text(
    alphabet=_SYMBOL_ALPHABET,
    min_size=1,
    max_size=12,
).filter(_is_clean_symbol)

# Whitespace to wrap a symbol with, exercising the strip() normalization. Empty
# string is allowed so "no surrounding whitespace" is in the space too.
_whitespace = st.text(alphabet=" \t\n\r", max_size=4)


@st.composite
def whitespace_padded_symbols(draw: st.DrawFn) -> tuple[str, str]:
    """Draw ``(logical_symbol, perturbed)`` for OT_SEARCH parity.

    ``logical_symbol`` is the clean trimmed/upper-cased token; ``perturbed`` is
    the same symbol with arbitrary surrounding whitespace and arbitrary letter
    case — the shape a "live" caller might pass. ``ot_search_request_key`` must
    normalize both to the same key.
    """
    symbol = draw(gene_symbols)
    logical = symbol.upper()
    # Perturb case character-by-character, then pad with whitespace.
    cased = "".join(
        draw(st.sampled_from([ch.upper(), ch.lower()])) for ch in symbol
    )
    lead = draw(_whitespace)
    trail = draw(_whitespace)
    perturbed = f"{lead}{cased}{trail}"
    return logical, perturbed


# --------------------------------------------------------------------------- #
# PubMed esearch URL params (Property 10, cache/live parity)                  #
# --------------------------------------------------------------------------- #

# Param names that are *not* caller-identity (tool/email are added separately in
# the test). Values are url-safe-ish text; parse_qsl/urlencode round-trip them.
_param_names = st.sampled_from(
    ["db", "term", "retmax", "retmode", "retstart", "sort", "field"]
)
_param_values = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" "),
    min_size=0,
    max_size=10,
)

# A dict of non-identity query params. Dict keys are unique, which keeps the
# canonicalization unambiguous (urlencode of sorted (name, value) pairs).
pubmed_param_sets = st.dictionaries(_param_names, _param_values, min_size=1, max_size=6)


# --------------------------------------------------------------------------- #
# Synthetic OT associations payloads (Property 6: score recompute fidelity)   #
# --------------------------------------------------------------------------- #

# OT association scores are real floats in [0, 1] — never NaN/inf. This mirrors
# the real OT score domain (see data/cache/preflight_ot_associations.json, where
# every row/datatype score is a finite float in [0, 1]); generating NaN/inf here
# would be outside the genuine input domain, not a real counterexample.
ot_scores = st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)

# The fixed set of OT evidence-datatype ids (models.DatatypeScore docstring /
# design). A row's datatypeScores draw a *unique* subset of these, so a
# breakdown keyed by id is unambiguous (no two entries share an id within a row).
_DATATYPE_IDS = (
    "clinical",
    "genetic_literature",
    "affected_pathway",
    "literature",
    "genetic_association",
    "somatic_mutation",
)


@st.composite
def ot_datatype_scores(draw: st.DrawFn) -> list[dict]:
    """Draw a row's ``datatypeScores[]``: 0..6 entries, unique ids, shuffled.

    Each entry is a ``{"id": <datatype id>, "score": <float in [0,1]>}`` with a
    distinct ``id`` drawn from the fixed OT datatype set, and the array is
    returned in an arbitrary (shuffled) order so the order-independence of the
    id-keyed recompute (Req 2.5) is exercised. Variable length (including the
    empty list) matches the real captures, where one row carries 6 datatypes and
    another carries 3 in a different order.
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
    entries = [{"id": dt_id, "score": draw(ot_scores)} for dt_id in chosen_ids]
    # Shuffle so the payload order is independent of any canonical ordering.
    return draw(st.permutations(entries))


@st.composite
def ot_association_rows(draw: st.DrawFn) -> list[dict]:
    """Draw a list of association rows with *distinct* disease ids.

    Each row is ``{"score": <float in [0,1]>, "datatypeScores": [...],
    "disease": {"id": <unique>, "name": <text>}}``. Disease ids are unique across
    the list so a ``disease_id`` lookup in :func:`recompute_association` /
    :func:`recompute_datatype_breakdown` is unambiguous. At least one row is drawn
    so there is always something to recompute.
    """
    disease_ids = draw(
        st.lists(
            st.text(
                alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
                min_size=1,
                max_size=16,
            ),
            min_size=1,
            max_size=8,
            unique=True,
        )
    )
    rows: list[dict] = []
    for disease_id in disease_ids:
        rows.append(
            {
                "score": draw(ot_scores),
                "datatypeScores": draw(ot_datatype_scores()),
                "disease": {
                    "id": disease_id,
                    "name": draw(st.text(min_size=0, max_size=20)),
                },
            }
        )
    return rows


def ot_associations_payload(rows: list[dict]) -> dict:
    """Wrap generated ``rows`` in the real OT associations payload shape.

    Mirrors ``payload.data.target.associatedDiseases.rows[]`` from the committed
    real capture (``data/cache/preflight_ot_associations.json``) so the recompute
    functions read exactly the path they read in production.
    """
    return {
        "data": {
            "target": {
                "id": "ENSG00000000000",
                "approvedSymbol": "SYNTH",
                "approvedName": "synthetic target",
                "associatedDiseases": {
                    "count": len(rows),
                    "rows": rows,
                },
            }
        }
    }


# --------------------------------------------------------------------------- #
# Synthetic PubMed esearch payloads (Property 2: no claim without evidence)   #
# --------------------------------------------------------------------------- #

# PMID-like tokens: non-empty digit strings, as seen in the real captures'
# ``esearchresult.idlist`` (e.g. "42920723"). PMIDs are numeric strings; the
# recompute/builder treat them as opaque strings, so a digit-string generator
# mirrors the genuine domain without inventing non-PMID shapes.
pmids = st.text(alphabet="0123456789", min_size=1, max_size=9)


@st.composite
def pubmed_esearch_payload(draw: st.DrawFn) -> dict:
    """Draw a real-shaped PubMed esearch payload: ``{"esearchresult": {...}}``.

    Mirrors ``payload.esearchresult`` from the committed real captures
    (``data/cache/preflight_pubmed_gene.json`` /
    ``preflight_pubmed_gene_and_disease.json``), where:

    * ``count`` is a **numeric string** (e.g. "108097") — PubMed returns the
      count as a string, which the recompute converts via the client's
      non-negative-int parse; and
    * ``idlist`` is an ordered list of PMID-like strings.

    ``count`` is drawn as the string form of a non-negative int so the payload
    always yields a genuinely recomputable literature/co-mention count — the
    "record present and recomputable" case this property needs. ``idlist`` is a
    possibly-empty list of distinct PMID strings (distinct so a RECENT_PAPER
    claim per PMID is unambiguous); an empty idlist simply yields no
    RECENT_PAPER claims, never a fabricated one.
    """
    count = draw(st.integers(min_value=0, max_value=10_000_000))
    idlist = draw(st.lists(pmids, min_size=0, max_size=5, unique=True))
    return {
        "esearchresult": {
            "count": str(count),
            "retmax": str(len(idlist)),
            "retstart": "0",
            "idlist": idlist,
        }
    }
