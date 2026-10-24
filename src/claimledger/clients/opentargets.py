"""OTClient: GraphQL search + associatedDiseases queries, parsing (Req 1, 2).

This task (6.1) implements the *search + resolution* half of the Open Targets
client: turning a user-entered gene symbol into a resolved ``Ensembl_ID`` via the
GraphQL ``search`` query. The associations parser (``associated_diseases``,
task 6.2) is intentionally not implemented here.

Grounding in the real response (steering rule 8, Req 4.6)
---------------------------------------------------------
Every shape below was read from the committed real capture
``data/cache/preflight_ot_search.json``, never assumed. That envelope is the
documented three-key shape ``{source_url, retrieved_at, payload}`` and its
``payload.data.search.hits[]`` carries one hit per entry, each with exactly
``{id, name, entity}``. The EGFR example has a single hit
``{id: "ENSG00000146648", name: "EGFR", entity: "target"}``. No other hit field
is assumed.

Pure parse vs. injected fetch
-----------------------------
:func:`parse_search_hits` is a pure function over a payload ``dict`` so tests can
parse the real fixture directly with no network. :class:`OTClient` wires the
fetch (through the injected :class:`~claimledger.http.HttpFetcher` and a
``do_request`` the tests replace) and stores the response as an
:class:`~claimledger.models.EvidenceRecord` with the stable ``Evidence_ID`` minted
by the evidence layer — but it delegates all shape-reading to the pure parser,
and all selection to the pure :func:`resolve`.

Resolution (Req 1.3, 1.5, 1.6, 1.7)
-----------------------------------
:func:`resolve` filters hits to ``entity == "target"`` and then:

* **1.3** selects the target hit whose ``name`` matches the submitted symbol
  case-insensitively, using the *same* Unicode-aware normalization as
  :func:`claimledger.evidence._normalize_symbol` (NFKC + casefold) — not a plain
  ``.upper()`` — so the fix from task 5.2 is honored and ``" egfr "`` resolves to
  ``EGFR``;
* **1.5** returns a ``NOT_FOUND`` outcome (stop the flow) when no target hit
  exists;
* **1.6** returns a ``DISAMBIGUATE`` outcome carrying the target hits when target
  hits exist but none name-match — never auto-selecting the first;
* **1.7** :func:`resolve_chosen` turns a user's chosen hit into a ``RESOLVED``
  outcome carrying its ``id``/``name``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from ..config import Config
from ..evidence import (
    EvidenceStore,
    _normalize_symbol,
    mint_evidence_id,
    ot_associations_request_key,
    ot_search_request_key,
    record_from_envelope,
)
from ..http import HttpFetcher
from ..models import Association, DatatypeScore, EvidenceRecord, Source

# The GraphQL search query (design "Open Targets GraphQL"): target entities only,
# selecting exactly the hit fields the real payload carries (id, name, entity).
SEARCH_QUERY = (
    "query Search($q: String!) {\n"
    "  search(queryString: $q, entityNames: [\"target\"]) {\n"
    "    hits { id name entity }\n"
    "  }\n"
    "}"
)

# The one entity kind the resolver accepts as a target hit (Req 1.3, 1.5, 1.6).
_TARGET_ENTITY = "target"

# The associatedDiseases GraphQL query (design "Open Targets GraphQL",
# Req 2.1-2.5). ``$size`` is the top_n page size; the selection set mirrors
# exactly the fields the committed real capture
# ``data/cache/preflight_ot_associations.json`` carries: the target's id /
# approvedSymbol / approvedName, the association ``count``, and each row's
# ``score`` + ``datatypeScores { id score }`` + ``disease { id name }``.
ASSOCIATIONS_QUERY = (
    "query Assoc($ensemblId: String!, $size: Int!) {\n"
    "  target(ensemblId: $ensemblId) {\n"
    "    id\n"
    "    approvedSymbol\n"
    "    approvedName\n"
    "    associatedDiseases(page: { index: 0, size: $size }) {\n"
    "      count\n"
    "      rows {\n"
    "        score\n"
    "        datatypeScores { id score }\n"
    "        disease { id name }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)

# Sentinel Overall_Score for an incomplete row whose ``score`` is absent or
# unparseable (Req 2.6). ``float("nan")`` is deliberate: it is not a plausible
# fabricated value (steering rule 3 — "never fabricate"), it compares unequal to
# everything (so it can never masquerade as a real score in verification), and
# it is the sort key that pushes incomplete rows last in the top_n selection
# (Req 2.7) while the row itself is still retained and visible (Req 2.6).
_MISSING_SCORE = float("nan")


@dataclass(frozen=True)
class SearchHit:
    """One parsed hit from ``payload.data.search.hits[]`` (Req 1.3).

    Carries exactly the three fields the real OT search payload provides. A
    frozen dataclass so a hit is hashable and cheap to compare in tests.
    """

    id: str
    name: str
    entity: str


class ResolveStatus(str, Enum):
    """The three terminal outcomes of resolving a symbol against its hits.

    * ``RESOLVED`` — an exact case-insensitive target name match (Req 1.3) or a
      user-chosen hit (Req 1.7); ``ensembl_id`` / ``resolved_symbol`` are set.
    * ``NOT_FOUND`` — no ``entity == "target"`` hit at all; stop the flow
      (Req 1.5).
    * ``DISAMBIGUATE`` — target hits exist but none name-match; ``hits`` holds the
      target hits for the UI to choose from, and nothing is auto-selected
      (Req 1.6).
    """

    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    DISAMBIGUATE = "DISAMBIGUATE"


@dataclass(frozen=True)
class ResolveOutcome:
    """The result of :func:`resolve` / :func:`resolve_chosen` (Req 1.3, 1.5-1.7).

    ``status`` is the discriminant. For ``RESOLVED`` the ``ensembl_id`` and
    ``resolved_symbol`` are populated from the selected hit. For ``DISAMBIGUATE``
    the ``hits`` tuple holds the target hits the UI must present (Req 1.6); it is
    empty for the other states. Nothing here is auto-selected on disambiguation —
    the caller supplies the user's choice back through :func:`resolve_chosen`
    (Req 1.7).
    """

    status: ResolveStatus
    ensembl_id: Optional[str] = None
    resolved_symbol: Optional[str] = None
    hits: tuple[SearchHit, ...] = ()


def parse_search_hits(payload: dict[str, Any]) -> list[SearchHit]:
    """Parse ``payload.data.search.hits[]`` into :class:`SearchHit` objects.

    Pure and separately callable on a raw payload ``dict`` so tests parse the
    committed fixture directly (steering rule 8). Reads only the three documented
    hit fields (``id``, ``name``, ``entity``); a hit missing any of them is
    skipped rather than repaired or fabricated (steering rule 3). A payload whose
    ``data.search.hits`` path is absent yields an empty list (the resolver then
    reports ``NOT_FOUND``), never an exception.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    search = data.get("search") if isinstance(data, dict) else None
    hits = search.get("hits") if isinstance(search, dict) else None
    if not isinstance(hits, list):
        return []

    parsed: list[SearchHit] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        hit_id = hit.get("id")
        name = hit.get("name")
        entity = hit.get("entity")
        # Every documented field must be present; skip (do not repair) otherwise.
        if not (isinstance(hit_id, str) and isinstance(name, str) and isinstance(entity, str)):
            continue
        parsed.append(SearchHit(id=hit_id, name=name, entity=entity))
    return parsed


def resolve(symbol: str, hits: list[SearchHit]) -> ResolveOutcome:
    """Apply the Req 1.3/1.5/1.6 selection logic to parsed ``hits``.

    Pure: no I/O. The steps mirror the design "Resolution logic":

    1. Filter to ``entity == "target"``.
    2. **Req 1.5** — no target hit -> ``NOT_FOUND`` (stop the flow).
    3. **Req 1.3** — a target hit whose ``name`` matches ``symbol``
       case-insensitively (via the shared Unicode-aware normalization) ->
       ``RESOLVED`` carrying that hit's id/name.
    4. **Req 1.6** — target hits exist but none name-match -> ``DISAMBIGUATE``
       carrying the target hits; the first is **not** auto-selected.
    """
    target_hits = tuple(h for h in hits if h.entity == _TARGET_ENTITY)

    # Req 1.5 — nothing resolvable; caller stops the retrieval flow.
    if not target_hits:
        return ResolveOutcome(status=ResolveStatus.NOT_FOUND)

    # Req 1.3 — exact case-insensitive name match. Reuse the same NFKC+casefold
    # normalization the Evidence_ID keys use (task 5.2 fix): a plain .upper()
    # would mis-handle characters like U+0130 and is explicitly not used here.
    normalized_symbol = _normalize_symbol(symbol)
    for hit in target_hits:
        if _normalize_symbol(hit.name) == normalized_symbol:
            return ResolveOutcome(
                status=ResolveStatus.RESOLVED,
                ensembl_id=hit.id,
                resolved_symbol=hit.name,
            )

    # Req 1.6 — ambiguous: hand the target hits to the UI, auto-select nothing.
    return ResolveOutcome(status=ResolveStatus.DISAMBIGUATE, hits=target_hits)


def resolve_chosen(hit: SearchHit) -> ResolveOutcome:
    """Turn a user's chosen disambiguation hit into a ``RESOLVED`` outcome (Req 1.7).

    When the UI presents the :attr:`ResolveOutcome.hits` of a ``DISAMBIGUATE``
    outcome and the user picks one, this extracts the chosen hit's ``id`` as the
    ``Ensembl_ID`` and its ``name`` as the resolved symbol.
    """
    return ResolveOutcome(
        status=ResolveStatus.RESOLVED,
        ensembl_id=hit.id,
        resolved_symbol=hit.name,
    )


@dataclass(frozen=True)
class TargetAssociations:
    """The parsed result of an OT associatedDiseases response (Req 2.2, 2.3, 2.7).

    Carries the target identity (``data.target.{id, approvedSymbol,
    approvedName}``, Req 2.2), the total association ``count`` as an **int**
    (``associatedDiseases.count``, Req 2.3), and the selected top-``top_n``
    :class:`~claimledger.models.Association` rows ordered by descending
    Overall_Score with a ``disease_id`` tie-break (Req 2.7).

    ``associations`` holds only the selected top-N. Incomplete rows (Req 2.6) are
    retained in the full parse and remain visible via :attr:`incomplete_count`
    and by being eligible for selection — but, lacking a usable score, they sort
    last and so only appear in ``associations`` when fewer than ``top_n`` complete
    rows exist. The frozen dataclass gives value semantics for tests.
    """

    target_id: str | None
    approved_symbol: str | None
    approved_name: str | None
    count: int | None
    associations: tuple[Association, ...]
    incomplete_count: int = 0


def parse_datatype_scores(raw: object) -> tuple[tuple[DatatypeScore, ...], bool]:
    """Parse a row's ``datatypeScores[]`` by element content, keyed on ``id``.

    Returns the parsed scores (in source order, which is captured but never
    relied upon) and a ``saw_incomplete`` flag. Critically, each entry is read by
    its own ``{id, score}`` content — **never by position** — so a row with 6
    datatype scores and a row with 3 in a different order both parse correctly
    (Req 2.5): the parser assumes no fixed length, order, or set of datatype ids.

    Every datatype entry present is preserved (Req 2.5). An entry missing/ mistyped
    ``id`` or ``score`` is skipped (not fabricated, steering rule 3) and sets the
    incomplete flag; a non-list ``datatypeScores`` also sets it. An empty but
    valid list is complete-as-parsed.
    """
    if not isinstance(raw, list):
        return (), True

    scores: list[DatatypeScore] = []
    saw_incomplete = False
    for entry in raw:
        if not isinstance(entry, dict):
            saw_incomplete = True
            continue
        dt_id = entry.get("id")
        dt_score = entry.get("score")
        # Key strictly on content: id must be a string, score a real number
        # (bool is an int subclass in Python, so reject it explicitly).
        if not isinstance(dt_id, str) or isinstance(dt_score, bool) or not isinstance(
            dt_score, (int, float)
        ):
            saw_incomplete = True
            continue
        scores.append(DatatypeScore(id=dt_id, score=float(dt_score)))
    return tuple(scores), saw_incomplete


def _parse_row(row: object, evidence_id: str) -> Association | None:
    """Parse one ``associatedDiseases.rows[]`` entry into an :class:`Association`.

    Returns ``None`` only when the row is not even a dict (nothing to represent).
    Otherwise always returns an Association, marking ``incomplete=True`` when any
    expected field is absent/mistyped so the row is retained rather than dropped
    (Req 2.6). Missing-value representation (documented, never fabricated):

    * missing/invalid ``score`` -> ``overall_score = float('nan')`` + incomplete;
    * missing ``disease`` / ``disease.id`` / ``disease.name`` -> empty string for
      the absent field + incomplete;
    * malformed ``datatypeScores`` -> preserved valid entries + incomplete.
    """
    if not isinstance(row, dict):
        return None

    incomplete = False

    # Overall_Score (Req 2.4). Reject bool (an int subclass) as a score.
    raw_score = row.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        overall_score = _MISSING_SCORE
        incomplete = True
    else:
        overall_score = float(raw_score)

    # disease.{id, name} (Req 2.4). Absent fields become "" and mark incomplete,
    # rather than being fabricated (steering rule 3).
    disease = row.get("disease")
    if isinstance(disease, dict):
        disease_id = disease.get("id")
        disease_name = disease.get("name")
        if not isinstance(disease_id, str):
            disease_id = ""
            incomplete = True
        if not isinstance(disease_name, str):
            disease_name = ""
            incomplete = True
    else:
        disease_id = ""
        disease_name = ""
        incomplete = True

    # datatypeScores[] parsed by content/id (Req 2.5).
    datatype_scores, dt_incomplete = parse_datatype_scores(row.get("datatypeScores"))
    if dt_incomplete:
        incomplete = True

    return Association(
        disease_id=disease_id,
        disease_name=disease_name,
        overall_score=overall_score,
        datatype_scores=datatype_scores,
        evidence_id=evidence_id,
        incomplete=incomplete,
    )


def _selection_sort_key(assoc: Association) -> tuple:
    """Sort key for top_n selection (Req 2.7): descending score, then disease_id.

    Incomplete rows without a usable score (``overall_score`` is NaN) sort *last*
    regardless of order supplied: a leading ``is_nan`` flag (0 for a usable score,
    1 for NaN) groups them after every scored row. Within usable scores, the
    negated score gives descending order; the ``disease_id`` ascending breaks
    ties deterministically.
    """
    score = assoc.overall_score
    is_nan = score != score  # True only for NaN
    primary = 0.0 if is_nan else -score
    return (1 if is_nan else 0, primary, assoc.disease_id)


def parse_associations(
    payload: dict[str, Any], top_n: int, evidence_id: str
) -> TargetAssociations:
    """Parse an OT associatedDiseases ``payload`` into a :class:`TargetAssociations`.

    Pure and separately callable on a raw payload ``dict`` so the fixture tests
    (task 6.3) can parse the committed real capture directly with no network
    (steering rule 8). ``evidence_id`` is passed in (not minted here) so parsing
    stays pure; the caller supplies the ot_associations EvidenceRecord id that
    every produced :class:`~claimledger.models.Association` carries.

    Steps:

    1. read ``data.target.{id, approvedSymbol, approvedName}`` (Req 2.2) and
       ``associatedDiseases.count`` as an int (Req 2.3);
    2. parse every ``rows[]`` entry via :func:`_parse_row`, keying datatype
       scores on ``id`` (Req 2.4, 2.5) and flagging incomplete rows while
       continuing (Req 2.6);
    3. select the top ``top_n`` by descending Overall_Score, tie-broken by
       ``disease_id`` ascending, with incomplete (NaN-score) rows sorted last
       (Req 2.7).

    A payload missing any path degrades to ``None``/empty rather than raising, so
    an unavailable/odd response never crashes the pipeline (steering rule 3).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    target = data.get("target") if isinstance(data, dict) else None
    if not isinstance(target, dict):
        return TargetAssociations(
            target_id=None,
            approved_symbol=None,
            approved_name=None,
            count=None,
            associations=(),
            incomplete_count=0,
        )

    target_id = target.get("id")
    approved_symbol = target.get("approvedSymbol")
    approved_name = target.get("approvedName")

    associated = target.get("associatedDiseases")
    associated = associated if isinstance(associated, dict) else {}

    # count is an int in the real capture (Req 2.3); keep only a genuine int
    # (reject bool), leaving it None when absent/unparseable rather than guessing.
    raw_count = associated.get("count")
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else None

    rows = associated.get("rows")
    rows = rows if isinstance(rows, list) else []

    parsed: list[Association] = []
    for row in rows:
        assoc = _parse_row(row, evidence_id)
        if assoc is not None:
            parsed.append(assoc)

    incomplete_count = sum(1 for a in parsed if a.incomplete)

    # Req 2.7 — stable sort by (nan-last, -score, disease_id); take top_n.
    selected = tuple(sorted(parsed, key=_selection_sort_key)[: max(top_n, 0)])

    return TargetAssociations(
        target_id=target_id if isinstance(target_id, str) else None,
        approved_symbol=approved_symbol if isinstance(approved_symbol, str) else None,
        approved_name=approved_name if isinstance(approved_name, str) else None,
        count=count,
        associations=selected,
        incomplete_count=incomplete_count,
    )


class OTClient:
    """Open Targets GraphQL client: target search + resolution (Req 1.2-1.7).

    Collaborators are injected so the client is testable with no real network:

    Parameters
    ----------
    fetcher:
        The :class:`~claimledger.http.HttpFetcher` applying the cache-first
        policy. The actual HTTP is supplied per call as the injected
        ``do_request`` the fetcher invokes, so tests never open a socket.
    store:
        The per-run :class:`~claimledger.evidence.EvidenceStore` the fetched
        search response is recorded into, keyed by its stable ``Evidence_ID``.
    config:
        Supplies the GraphQL endpoint used for both the request and the stable
        request key. Defaults to :class:`~claimledger.config.Config`.

    The search response is stored as an :class:`EvidenceRecord` whose
    ``Evidence_ID`` is minted from :func:`ot_search_request_key` +
    :func:`mint_evidence_id`, so a cached and a live fetch of the same symbol mint
    the same id (design "Stable Evidence_IDs").
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

    def _build_do_request(self, symbol: str):
        """Build the zero-arg ``do_request`` the fetcher calls for a live search.

        Imports ``requests`` lazily and inside the callable so the module (and
        every pure function above) imports cleanly whether or not ``requests`` is
        installed, and so no network dependency is touched under the pure tests.
        The POST applies the configured ``http_timeout_s`` and returns the three
        envelope fields ``(source_url, retrieved_at, payload)``.
        """

        def do_request() -> "tuple[str, str, Any]":
            import datetime
            import requests

            url = self.config.ot_graphql_url
            response = requests.post(
                url,
                json={"query": SEARCH_QUERY, "variables": {"q": symbol}},
                timeout=self.fetcher.http_timeout_s,
            )
            response.raise_for_status()
            retrieved_at = (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            return url, retrieved_at, response.json()

        return do_request

    def search(self, symbol: str) -> list[SearchHit]:
        """Fetch + store the OT search response for ``symbol`` and return its hits.

        Wires the fetch through the cache-first :class:`HttpFetcher`, records a
        successful/cached response as an :class:`EvidenceRecord` under its stable
        ``Evidence_ID``, and returns the parsed hits via :func:`parse_search_hits`.
        If the interface is unavailable and no cache exists, returns an empty list
        (the resolver then reports ``NOT_FOUND``) rather than fabricating hits
        (steering rule 3). Parsing itself stays in the pure
        :func:`parse_search_hits` so it is independently testable on the fixture.
        """
        request_key = ot_search_request_key(symbol, self.config.ot_graphql_url)
        result = self.fetcher.fetch(
            request_key,
            self.config.ot_graphql_url,
            self._build_do_request(symbol),
        )
        if not result.available:
            return []

        record = record_from_envelope(
            Source.OT_SEARCH,
            request_key,
            {
                "source_url": result.source_url,
                "retrieved_at": result.retrieved_at,
                "payload": result.payload,
            },
            from_cache=result.from_cache,
        )
        self.store.add(record)
        return parse_search_hits(result.payload)

    def resolve(self, symbol: str) -> ResolveOutcome:
        """Convenience: :meth:`search` the symbol then :func:`resolve` the hits.

        Keeps the pure :func:`resolve` separately callable for tests while giving
        the pipeline a single entry point from symbol to :class:`ResolveOutcome`.
        """
        return resolve(symbol, self.search(symbol))

    def _build_assoc_do_request(self, ensembl_id: str, size: int):
        """Build the zero-arg ``do_request`` the fetcher calls for a live assoc query.

        Mirrors :meth:`_build_do_request`: ``requests`` is imported lazily inside
        the callable so the module imports cleanly without it and the pure parse
        functions never touch the network. The POST applies the configured
        ``http_timeout_s`` and returns the three envelope fields
        ``(source_url, retrieved_at, payload)``.
        """

        def do_request() -> "tuple[str, str, Any]":
            import datetime
            import requests

            url = self.config.ot_graphql_url
            response = requests.post(
                url,
                json={
                    "query": ASSOCIATIONS_QUERY,
                    "variables": {"ensemblId": ensembl_id, "size": size},
                },
                timeout=self.fetcher.http_timeout_s,
            )
            response.raise_for_status()
            retrieved_at = (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            return url, retrieved_at, response.json()

        return do_request

    def associated_diseases(
        self, ensembl_id: str, top_n: Optional[int] = None
    ) -> TargetAssociations:
        """Fetch + store the OT associations response and return parsed top_n rows.

        Wires the fetch through the cache-first :class:`HttpFetcher` exactly like
        :meth:`search`: it mints the stable ot_associations ``Evidence_ID`` from
        :func:`ot_associations_request_key` + :func:`mint_evidence_id`, records a
        successful/cached response as an :class:`EvidenceRecord` under that id, and
        delegates all shape-reading to the pure :func:`parse_associations` so the
        parser stays independently testable on the fixture (steering rule 8).

        ``top_n`` defaults to ``Config.thresholds.top_n`` (5, Req 2.7). When the
        interface is unavailable and no cache exists, returns an empty
        :class:`TargetAssociations` rather than fabricating rows (steering rule 3).
        """
        size = top_n if top_n is not None else self.config.thresholds.top_n
        request_key = ot_associations_request_key(
            ensembl_id, size, self.config.ot_graphql_url
        )
        evidence_id = mint_evidence_id(Source.OT_ASSOCIATIONS, request_key)

        result = self.fetcher.fetch(
            request_key,
            self.config.ot_graphql_url,
            self._build_assoc_do_request(ensembl_id, size),
        )
        if not result.available:
            return TargetAssociations(
                target_id=None,
                approved_symbol=None,
                approved_name=None,
                count=None,
                associations=(),
                incomplete_count=0,
            )

        record = record_from_envelope(
            Source.OT_ASSOCIATIONS,
            request_key,
            {
                "source_url": result.source_url,
                "retrieved_at": result.retrieved_at,
                "payload": result.payload,
            },
            from_cache=result.from_cache,
        )
        self.store.add(record)
        return parse_associations(result.payload, size, evidence_id)


__all__ = [
    "SEARCH_QUERY",
    "ASSOCIATIONS_QUERY",
    "SearchHit",
    "ResolveStatus",
    "ResolveOutcome",
    "TargetAssociations",
    "parse_search_hits",
    "resolve",
    "resolve_chosen",
    "parse_datatype_scores",
    "parse_associations",
    "OTClient",
]
