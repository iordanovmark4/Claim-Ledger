"""PubMedClient: esearch URLs/params, parsing, rate limit (Req 3).

This task (7.1) implements only the *request-shaping* half of the PubMed
E-utilities client: the pure term-formatting functions (Req 3.4) and the pure
esearch URL/param builders that guarantee ``tool`` and ``email`` ride on every
request (Req 3.1, 3.6, 3.10). The response parser — ``esearchresult.count`` /
``idlist`` / ``retmax`` / ``retstart`` / ``querytranslation`` — is deliberately
left to task 7.2, and rate limiting to a later task.

Grounding in the real request shape (steering rule 8)
-----------------------------------------------------
The param set and term format below were read from the two committed real
captures, never assumed:

* ``data/cache/preflight_pubmed_gene.json``
* ``data/cache/preflight_pubmed_gene_and_disease.json``

Each envelope's ``source_url`` is the esearch URL exactly as issued — e.g.
``.../esearch.fcgi?db=pubmed&term=...&retmax=3&retmode=json&tool=claimledger&email=hackathon%40example.com``
— confirming the five + two params this module builds: ``db=pubmed``,
``retmode=json``, ``retmax``, ``term``, plus the caller-identity ``tool`` and
``email`` on *every* request (Req 3.10). The gene+disease capture's
``esearchresult.querytranslation`` shows PubMed rewrote the submitted disease
clause into a ``[MeSH Terms]`` form; that is why we submit and store our own
term verbatim and read PubMed's translation separately (task 7.2 / Req 3.5).

Pure shaping vs. injected fetch
-------------------------------
:func:`gene_term`, :func:`pair_term`, :func:`build_esearch_params` and
:func:`build_esearch_url` are pure functions so the task-7.3 tests can assert the
term format and the always-present ``tool``/``email`` with no network.
:func:`build_esearch_do_request` mirrors the :class:`~claimledger.clients.opentargets.OTClient`
pattern — ``requests`` is imported lazily *inside* the returned callable, so this
module (and every pure function above) imports cleanly whether or not
``requests`` is installed and the pure tests never touch a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from ..config import Config
from ..evidence import (
    EvidenceStore,
    pubmed_request_key,
    record_from_envelope,
)
from ..http import HttpFetcher
from ..models import Source

# The PubMed Title/Abstract field tag appended to each term clause (Req 3.4).
# A module constant so the gene-only and pair term share one source of truth.
_TITLE_ABSTRACT_TAG = "[Title/Abstract]"

# The AND operator joining the gene and disease clauses of a pair term (Req 3.4).
# Surrounded by single spaces per the format the design and real captures show.
_AND_JOINER = " AND "


# --------------------------------------------------------------------------- #
# Term formatting (pure, Req 3.4)                                             #
# --------------------------------------------------------------------------- #
def gene_term(symbol: str) -> str:
    '''Format the gene-only esearch term ``"GENE"[Title/Abstract]`` (Req 3.1, 3.4).

    The ``symbol`` is wrapped in double quotes and followed by the
    ``[Title/Abstract]`` field tag. The text is preserved *verbatim* inside the
    quotes — not upper-cased — because the symbol handed in is already the
    user-entered / resolved approved symbol, and the submitted term is what gets
    stored as provenance (PubMed may still rewrite it; its ``querytranslation``
    is captured separately in task 7.2, Req 3.5).
    '''
    return f'"{symbol}"{_TITLE_ABSTRACT_TAG}'


def pair_term(symbol: str, disease_name: str) -> str:
    '''Format the gene+disease term for a co-mention query (Req 3.4).

    Produces ``"GENE"[Title/Abstract] AND "DISEASE NAME"[Title/Abstract]``: each
    clause double-quoted and tagged ``[Title/Abstract]`` via :func:`gene_term`'s
    same shaping, the two joined by `` AND ``. Both ``symbol`` and
    ``disease_name`` are kept verbatim inside their quotes (not upper-cased); the
    exact submitted string is the one stored as the pair's provenance (Req 4.3),
    while PubMed's possible MeSH rewrite is read back as ``querytranslation``
    (task 7.2, Req 3.5).
    '''
    gene_clause = f'"{symbol}"{_TITLE_ABSTRACT_TAG}'
    disease_clause = f'"{disease_name}"{_TITLE_ABSTRACT_TAG}'
    return f"{gene_clause}{_AND_JOINER}{disease_clause}"


# --------------------------------------------------------------------------- #
# esearch URL / param building (pure, Req 3.1, 3.6, 3.10)                     #
# --------------------------------------------------------------------------- #
def build_esearch_params(
    term: str,
    retmax: Optional[int] = None,
    tool: Optional[str] = None,
    email: Optional[str] = None,
    config: Optional[Config] = None,
) -> dict[str, str]:
    """Build the esearch query params for ``term`` (Req 3.1, 3.6, 3.10).

    Returns an ordered ``dict`` carrying exactly the params the two committed
    captures show: a fixed ``db=pubmed`` and ``retmode=json`` (Req 3.1 — JSON
    output), the ``retmax`` recent-PMID cap, the search ``term``, and the
    caller-identity ``tool`` / ``email``. ``tool`` and ``email`` are **always**
    present (Req 3.10): they default from :class:`~claimledger.config.Config`
    (``claimledger`` / ``hackathon@example.com``, matching the cached URLs so
    cache/live request keys line up) and are included even if passed explicitly.

    ``retmax`` defaults to ``Config.thresholds.retmax`` (3, Req 3.6). All values
    are stringified so the dict maps straight onto a query string; insertion
    order mirrors the ``db, term, retmax, retmode, tool, email`` shape of the
    real ``source_url`` captures.
    """
    cfg = config if config is not None else Config()
    effective_retmax = retmax if retmax is not None else cfg.thresholds.retmax
    effective_tool = tool if tool is not None else cfg.tool
    effective_email = email if email is not None else cfg.email

    return {
        "db": "pubmed",
        "term": term,
        "retmax": str(effective_retmax),
        "retmode": "json",
        "tool": effective_tool,
        "email": effective_email,
    }


def build_esearch_url(base_url: str, params: dict[str, str]) -> str:
    """Join ``base_url`` and ``params`` into a full esearch URL (Req 3.1).

    Percent-encodes the params via :func:`urllib.parse.urlencode` (so a ``term``
    like ``"EGFR"[Title/Abstract]`` and an ``email`` with ``@`` encode exactly as
    the cached ``source_url`` shows) and appends them to ``base_url`` with a
    single ``?``. Pure and order-preserving: the query string follows the
    ``params`` dict's insertion order, which the Evidence_ID key normalization
    (sorted, caller-identity-stripped) renders irrelevant for id stability.
    """
    return f"{base_url}?{urlencode(params)}"


def build_esearch_do_request(url: str, http_timeout_s: float):
    """Build the zero-arg ``do_request`` the fetcher calls for a live esearch.

    Mirrors :meth:`claimledger.clients.opentargets.OTClient._build_do_request`:
    ``requests`` is imported lazily *inside* the returned callable so this module
    imports cleanly without it and the pure shaping functions never touch the
    network. The GET applies ``http_timeout_s`` and returns the three envelope
    fields ``(source_url, retrieved_at, payload)`` — ``source_url`` being the
    full esearch ``url`` exactly as the cached envelopes store it. (Response
    parsing is task 7.2; this only performs the request.)
    """

    def do_request() -> "tuple[str, str, Any]":
        import datetime
        import requests

        response = requests.get(url, timeout=http_timeout_s)
        response.raise_for_status()
        retrieved_at = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return url, retrieved_at, response.json()

    return do_request


# --------------------------------------------------------------------------- #
# Response parsing (pure, Req 3.2, 3.3, 3.5, 3.7, 3.8)                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PubMedResult:
    """The parsed fields of a PubMed esearch ``esearchresult`` (Req 3.2-3.8).

    "Count unavailable" is represented **explicitly** and without fabrication
    (steering rule 3, Req 3.8): ``count`` is ``None`` and ``count_available`` is
    ``False`` whenever ``esearchresult.count`` is absent, non-numeric, or a
    negative number. A caller must therefore check ``count_available`` (or test
    ``count is not None``) rather than defaulting a missing count to ``0`` — a
    zero is a real "no papers" answer, never a stand-in for "we don't know".

    Fields
    ------
    count:
        The gene / co-mention paper count as a non-negative ``int`` (Req 3.2),
        or ``None`` when unavailable (Req 3.8).
    count_available:
        ``True`` iff ``count`` was parsed as a genuine non-negative integer. The
        explicit companion flag for ``count`` so "unavailable" is unambiguous
        even though ``None`` already signals it.
    idlist:
        The PMIDs as a ``tuple[str, ...]`` **in returned order** (Req 3.3), which
        is the order recent papers are displayed in (Req 3.7). Kept as strings.
    retmax / retstart:
        The esearch window bounds as ``int`` (the real payload sends them as
        strings), or ``None`` when absent/unparseable. Non-critical, so a bad
        value degrades to ``None`` rather than raising.
    querytranslation:
        PubMed's interpretation of the submitted term (Req 3.5), or ``None`` when
        absent. Stored so the gene+disease provenance can show how PubMed rewrote
        the query (Req 4.3).
    """

    count: Optional[int]
    count_available: bool
    idlist: tuple[str, ...]
    retmax: Optional[int] = None
    retstart: Optional[int] = None
    querytranslation: Optional[str] = None


def _parse_non_negative_int(raw: Any) -> Optional[int]:
    """Parse ``raw`` into a non-negative ``int``, or ``None`` if it cannot be.

    PubMed sends ``count`` / ``retmax`` / ``retstart`` as **strings** (e.g.
    ``"108097"``, ``"3"``, ``"0"``). This converts such a string via ``int``
    *only* when it is a valid non-negative integer literal, and returns ``None``
    for anything else — absent (``None``), non-numeric text, a float-looking
    string, or a negative value — so an unparseable field degrades to "unknown"
    rather than being guessed (steering rule 3, Req 3.8).

    A genuine ``int`` is accepted too (``bool`` rejected, since it subclasses
    ``int``) for robustness, but the real payloads are always strings.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        # str.isdigit is True only for a run of decimal digits: no sign, no
        # decimal point, no whitespace survives — exactly "non-negative integer".
        if text.isdigit():
            return int(text)
    return None


def parse_esearch(payload: Any) -> PubMedResult:
    """Parse a PubMed esearch ``payload`` into a :class:`PubMedResult` (Req 3.2-3.8).

    Pure and independently callable on a raw payload ``dict`` so the task-7.3
    tests parse the committed real fixtures directly with no network (steering
    rule 8). Reads ``payload.esearchresult`` and:

    * ``count`` — converts the **string** count to a non-negative ``int``
      (Req 3.2); when absent or not a non-negative integer, records it as
      **unavailable** (``count=None``, ``count_available=False``) and continues
      rather than fabricating a number (Req 3.8, steering rule 3);
    * ``idlist`` — the PMIDs as a ``tuple[str, ...]`` **in payload order**
      (Req 3.3), which is the order recent papers use (Req 3.7); only string
      entries are kept (a malformed entry is skipped, not repaired);
    * ``retmax`` / ``retstart`` — parsed from their string form to ``int`` when
      valid, else ``None`` (Req 3.3, non-critical);
    * ``querytranslation`` — PubMed's query interpretation string, or ``None``
      (Req 3.5).

    A payload missing ``esearchresult`` (or not even a dict) degrades to a result
    with count unavailable and an empty ``idlist`` rather than raising, so an
    unavailable/odd response never crashes the pipeline (steering rule 3).
    """
    esearchresult = payload.get("esearchresult") if isinstance(payload, dict) else None
    if not isinstance(esearchresult, dict):
        return PubMedResult(count=None, count_available=False, idlist=())

    count = _parse_non_negative_int(esearchresult.get("count"))

    raw_idlist = esearchresult.get("idlist")
    if isinstance(raw_idlist, list):
        # Preserve order (Req 3.7); keep only genuine PMID strings, skipping any
        # malformed entry rather than coercing it (steering rule 3).
        idlist = tuple(pmid for pmid in raw_idlist if isinstance(pmid, str))
    else:
        idlist = ()

    retmax = _parse_non_negative_int(esearchresult.get("retmax"))
    retstart = _parse_non_negative_int(esearchresult.get("retstart"))

    raw_qt = esearchresult.get("querytranslation")
    querytranslation = raw_qt if isinstance(raw_qt, str) else None

    return PubMedResult(
        count=count,
        count_available=count is not None,
        idlist=idlist,
        retmax=retmax,
        retstart=retstart,
        querytranslation=querytranslation,
    )


# --------------------------------------------------------------------------- #
# PubMedClient: fetch + store + parse (Req 3, 4.3)                            #
# --------------------------------------------------------------------------- #
class PubMedClient:
    """PubMed esearch client: gene + gene-disease fetches, parsing (Req 3, 4.3).

    Collaborators are injected so the client is testable with no real network,
    mirroring :class:`~claimledger.clients.opentargets.OTClient`:

    Parameters
    ----------
    fetcher:
        The :class:`~claimledger.http.HttpFetcher` applying the cache-first
        policy. The actual HTTP is supplied per call as the injected
        ``do_request`` the fetcher invokes (built by
        :func:`build_esearch_do_request`), so tests never open a socket. The
        fetcher calls ``rate_limiter.acquire()`` before each live request, so
        wiring the fetcher is enough to honor the <=3 req/s limit (Req 3.9).
    store:
        The per-run :class:`~claimledger.evidence.EvidenceStore` each fetched
        esearch response is recorded into, keyed by its stable ``Evidence_ID``.
    config:
        Supplies the esearch endpoint, ``tool`` / ``email`` identity and
        ``thresholds.retmax`` used for both the request and the stable request
        key. Defaults to :class:`~claimledger.config.Config`.

    Each response is stored as an :class:`EvidenceRecord` whose ``Evidence_ID``
    is minted from :func:`~claimledger.evidence.pubmed_request_key` +
    :func:`~claimledger.evidence.mint_evidence_id`, so a cached and a live fetch
    of the same logical esearch mint the same id (design "Stable Evidence_IDs").
    Shape-reading is delegated to the pure :func:`parse_esearch` so the parser
    stays independently testable on the committed fixtures (steering rule 8).
    """

    def __init__(
        self,
        fetcher: HttpFetcher,
        store: EvidenceStore,
        config: Optional[Config] = None,
    ) -> None:
        self.fetcher = fetcher
        self.store = store
        self.config = config if config is not None else Config()

    def _esearch_url(self, term: str) -> str:
        """Build the full esearch URL for ``term`` (Req 3.1, 3.6, 3.10).

        Uses the pure :func:`build_esearch_params` (so ``tool`` / ``email`` ride
        on every request, Req 3.10, and ``retmax`` defaults from config, Req 3.6)
        and :func:`build_esearch_url` against the configured esearch endpoint.
        """
        params = build_esearch_params(term, config=self.config)
        return build_esearch_url(self.config.pubmed_esearch_url, params)

    def _fetch(self, term: str):
        """Fetch ``term`` through the cache-first fetcher, returning its result.

        The request key is the canonicalized esearch URL (``tool`` / ``email``
        stripped, params sorted) so live and cached forms of the same logical
        request share one ``Evidence_ID``. The fetcher applies the rate limiter
        and timeout before each live call; the live request is built lazily by
        :func:`build_esearch_do_request` so ``requests`` is only touched on a
        real fetch.
        """
        url = self._esearch_url(term)
        request_key = pubmed_request_key(url)
        do_request = build_esearch_do_request(url, self.fetcher.http_timeout_s)
        result = self.fetcher.fetch(request_key, url, do_request)
        return request_key, result

    def esearch_gene(self, symbol: str) -> PubMedResult:
        """Fetch + store the gene-only esearch response and return its parsed result.

        Formats the gene term ``"GENE"[Title/Abstract]`` (Req 3.1, 3.4), fetches
        it cache-first, records a successful/cached response as a
        :class:`Source.PUBMED_GENE` :class:`EvidenceRecord` under its stable
        ``Evidence_ID``, and returns :func:`parse_esearch` of the payload. When
        the interface is unavailable and no cache exists, returns a result with
        the count **unavailable** and an empty ``idlist`` and stores **no**
        fabricated record (Req 3.8, 11.6, steering rule 3).
        """
        term = gene_term(symbol)
        request_key, result = self._fetch(term)
        if not result.available:
            return PubMedResult(count=None, count_available=False, idlist=())

        record = record_from_envelope(
            Source.PUBMED_GENE,
            request_key,
            {
                "source_url": result.source_url,
                "retrieved_at": result.retrieved_at,
                "payload": result.payload,
            },
            from_cache=result.from_cache,
        )
        self.store.add(record)
        return parse_esearch(result.payload)

    def esearch_pair(self, symbol: str, disease_name: str) -> PubMedResult:
        """Fetch + store the gene+disease esearch response and return its parsed result.

        Formats the pair term ``"GENE"[Title/Abstract] AND "DISEASE"[Title/Abstract]``
        (Req 3.4), fetches it cache-first, and records a successful/cached
        response as a :class:`Source.PUBMED_PAIR` :class:`EvidenceRecord`. Because
        the exact submitted query string is *ours* (it is not echoed in the
        payload), it is carried onto the record as ``submitted_query`` alongside
        the parsed ``query_translation`` from ``esearchresult.querytranslation``,
        giving the co-mention provenance view (Req 4.3, 3.5). Returns the parsed
        :class:`PubMedResult` (its ``count`` is the Co_Mention_Count, Req 3.5).

        When the interface is unavailable and no cache exists, returns a result
        with the count **unavailable** and an empty ``idlist`` and stores **no**
        fabricated record (Req 3.8, 11.6, steering rule 3).
        """
        term = pair_term(symbol, disease_name)
        request_key, result = self._fetch(term)
        if not result.available:
            return PubMedResult(count=None, count_available=False, idlist=())

        parsed = parse_esearch(result.payload)
        record = record_from_envelope(
            Source.PUBMED_PAIR,
            request_key,
            {
                "source_url": result.source_url,
                "retrieved_at": result.retrieved_at,
                "payload": result.payload,
            },
            from_cache=result.from_cache,
            submitted_query=term,
            query_translation=parsed.querytranslation,
        )
        self.store.add(record)
        return parsed


__all__ = [
    "gene_term",
    "pair_term",
    "build_esearch_params",
    "build_esearch_url",
    "build_esearch_do_request",
    "PubMedResult",
    "parse_esearch",
    "PubMedClient",
]
