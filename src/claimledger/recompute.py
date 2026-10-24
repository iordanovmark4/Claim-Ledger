"""recompute_<type>() pure functions: SINGLE SOURCE OF TRUTH (Req 4.5, 6.1).

These five pure functions are the *only* place a claim value is computed. Both
the :class:`~claimledger.claims.ClaimBuilder` (task 9) and the
:class:`~claimledger.verify.Verifier` (task 10) call the same function for a
given claim type, so the builder and verifier can never disagree by
construction (design "Claim Building" / "Verifier", Req 4.5, 6.1; steering
rule 3 "single source of truth").

Every function reads strictly from an :class:`~claimledger.models.EvidenceRecord`
and its raw ``.payload`` (and ``.source`` where a source distinction matters) —
never from a separately-stored parsed field. The value is recomputed from the
cached real response on every call, so there is no cached-parse to drift from
the raw bytes the verifier checks against (steering rule 8 "parse against saved
real responses", steering rule 3 "never repair/fabricate").

Failure is a first-class, *precise* outcome, not a repaired default. When a
requested disease id, count, or PMID is not genuinely present in the payload,
the function raises a specific exception with a message naming what was missing
rather than returning a fabricated or sentinel value. This is deliberate: the
verifier treats a recompute failure as a mismatch/rejection (an
``UNSUPPORTED`` verdict), so a claim that references evidence which does not
actually contain its value is correctly rejected rather than silently
"verified" against a guessed number (Req 6.3, 6.7; steering rule 3).

Shape grounding (steering rule 8)
---------------------------------
Shapes were read from the committed real captures, never assumed:

* ``data/cache/preflight_ot_associations.json`` —
  ``payload.data.target.associatedDiseases.rows[]`` each with a float ``score``,
  a ``disease.{id, name}``, and a variable-length ``datatypeScores[].{id, score}``
  (6 entries in the NSCLC row, 3 in another, differing order). The datatype
  parse therefore keys on ``id`` and is returned sorted by ``id`` ascending so
  the value is canonical/deterministic (design "Deterministic ordering").
* ``data/cache/preflight_pubmed_gene.json`` /
  ``preflight_pubmed_gene_and_disease.json`` — ``payload.esearchresult`` with a
  **string** ``count`` ("108097" / "18950") and an ordered ``idlist`` of PMID
  strings. The string→int conversion reuses the same non-negative-int parse the
  PubMed client uses, so literature/co-mention recompute and the client parser
  never diverge on what "a valid count" means.

Shared parsing (no subtly-different duplicate)
----------------------------------------------
To stay the single source of truth, this module reuses the existing parse
helpers rather than re-deriving them:

* :func:`claimledger.clients.opentargets.parse_datatype_scores` for the datatype
  breakdown (so OT client parsing and recompute agree on how a datatypeScores
  array becomes :class:`DatatypeScore` objects);
* :func:`claimledger.clients.pubmed._parse_non_negative_int` for the PubMed
  count (so the string→int rule is identical to the client's).
"""

from __future__ import annotations

from typing import Any

from .clients.opentargets import parse_datatype_scores
from .clients.pubmed import _parse_non_negative_int
from .models import DatatypeScore, EvidenceRecord


def _association_rows(record: EvidenceRecord) -> list:
    """Return ``payload.data.target.associatedDiseases.rows`` as a list.

    Reads the rows straight from the raw payload (never a cached parse). Raises
    :class:`LookupError` with a precise message when the rows path is absent or
    not a list, so a record that cannot supply any association recompute fails
    loudly rather than appearing to hold zero rows (steering rule 3).
    """
    payload: Any = record.payload
    data = payload.get("data") if isinstance(payload, dict) else None
    target = data.get("target") if isinstance(data, dict) else None
    associated = target.get("associatedDiseases") if isinstance(target, dict) else None
    rows = associated.get("rows") if isinstance(associated, dict) else None
    if not isinstance(rows, list):
        raise LookupError(
            "cannot recompute association: payload has no "
            "data.target.associatedDiseases.rows list"
        )
    return rows


def _find_row(record: EvidenceRecord, disease_id: str) -> dict:
    """Find the association row whose ``disease.id == disease_id``.

    Raises :class:`LookupError` naming the missing ``disease_id`` when no row
    matches — the verifier reads that as a mismatch and rejects the claim, which
    is exactly right for a claim that points at a disease the evidence does not
    actually contain (Req 6.3; steering rule 3). A claimed id that is not present
    must never be repaired into some "closest" row.
    """
    for row in _association_rows(record):
        if not isinstance(row, dict):
            continue
        disease = row.get("disease")
        if isinstance(disease, dict) and disease.get("id") == disease_id:
            return row
    raise LookupError(
        f"cannot recompute association: disease_id {disease_id!r} not present "
        f"in payload.data.target.associatedDiseases.rows"
    )


def recompute_association(record: EvidenceRecord, disease_id: str) -> float:
    """Recompute the Overall_Score for ``disease_id`` from an OT assoc record.

    Finds the row in ``payload.data.target.associatedDiseases.rows`` whose
    ``disease.id == disease_id`` and returns its ``score`` as a ``float``
    (Req 5.3, value table). Pure: reads only ``record.payload``.

    Raises
    ------
    LookupError
        If ``disease_id`` is not present in the rows (a claimed disease the
        evidence does not contain). The verifier treats this recompute failure
        as a mismatch and rejects the claim (Req 6.3; steering rule 3), so the
        precise exception is the correct behavior — never a fabricated score.
    ValueError
        If the matched row has no numeric ``score`` (a structurally broken row
        cannot yield a real number; fabricating one is forbidden).
    """
    row = _find_row(record, disease_id)
    raw_score = row.get("score")
    # bool is an int subclass in Python; reject it explicitly so True/False can
    # never masquerade as a 1.0/0.0 score.
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise ValueError(
            f"cannot recompute association: row for disease_id {disease_id!r} "
            f"has no numeric score (got {raw_score!r})"
        )
    return float(raw_score)


def recompute_datatype_breakdown(
    record: EvidenceRecord, disease_id: str
) -> tuple[DatatypeScore, ...]:
    """Recompute the datatype breakdown for ``disease_id``, ordered by id.

    Finds the association row for ``disease_id`` and parses its
    ``datatypeScores[]`` **by content keyed on id** — reusing
    :func:`claimledger.clients.opentargets.parse_datatype_scores` so OT-client
    parsing and recompute can never diverge — then returns the parsed
    :class:`DatatypeScore` objects **sorted by ``id`` ascending** (Req 5.4,
    design "Deterministic ordering": datatype scores sorted by id). Sorting makes
    the value canonical/deterministic so the builder and verifier compare the
    same ordered tuple regardless of the array's order in the raw payload.

    Returns a ``tuple[DatatypeScore, ...]`` (immutable, hashable).

    Raises
    ------
    LookupError
        If ``disease_id`` is not present in the rows (see
        :func:`recompute_association`).
    """
    row = _find_row(record, disease_id)
    scores, _saw_incomplete = parse_datatype_scores(row.get("datatypeScores"))
    # Canonical order: ascending by datatype id (design "Deterministic
    # ordering"). The incomplete flag is intentionally not consulted here — any
    # entry parse_datatype_scores could read is a genuine (id, score) from the
    # payload; a malformed entry it skipped is simply absent, never fabricated.
    return tuple(sorted(scores, key=lambda ds: ds.id))


def _recompute_count(record: EvidenceRecord, what: str) -> int:
    """Recompute a non-negative integer count from ``payload.esearchresult.count``.

    Shared body of :func:`recompute_literature_count` and
    :func:`recompute_co_mention_count`. Reads the **string** ``count`` straight
    from the raw payload and converts it with the PubMed client's own
    :func:`claimledger.clients.pubmed._parse_non_negative_int`, so recompute and
    the client agree exactly on what counts as a valid count.

    Raises :class:`ValueError` with a message naming ``what`` when ``count`` is
    absent, non-numeric, or negative — never fabricating a number for an
    unavailable/unparseable count (Req 3.8; steering rule 3). ``what`` is the
    human label ("literature count" / "co-mention count") for the message.
    """
    payload: Any = record.payload
    esearchresult = payload.get("esearchresult") if isinstance(payload, dict) else None
    if not isinstance(esearchresult, dict):
        raise ValueError(
            f"cannot recompute {what}: payload has no esearchresult object"
        )
    raw_count = esearchresult.get("count")
    count = _parse_non_negative_int(raw_count)
    if count is None:
        raise ValueError(
            f"cannot recompute {what}: esearchresult.count is absent, "
            f"non-numeric, or negative (got {raw_count!r})"
        )
    return count


def recompute_literature_count(record: EvidenceRecord) -> int:
    """Recompute the gene-only literature count = ``int(esearchresult.count)``.

    For the gene-only PubMed record (``Source.PUBMED_GENE``): returns the paper
    count as a non-negative ``int`` (Req 5.5, value table). Pure: reads only
    ``record.payload``.

    Raises :class:`ValueError` if the count is absent/unparseable/negative — a
    recompute failure the verifier rejects, never a fabricated zero (Req 3.8,
    6.3; steering rule 3).
    """
    return _recompute_count(record, "literature count")


def recompute_co_mention_count(record: EvidenceRecord) -> int:
    """Recompute the gene+disease co-mention count = ``int(esearchresult.count)``.

    Identical computation to :func:`recompute_literature_count` but for the
    gene+disease record (``Source.PUBMED_PAIR``): returns the co-mention paper
    count as a non-negative ``int`` (Req 5.6, value table). Pure: reads only
    ``record.payload``.

    Raises :class:`ValueError` if the count is absent/unparseable/negative — a
    recompute failure the verifier rejects, never a fabricated zero (Req 3.8,
    6.3; steering rule 3).
    """
    return _recompute_count(record, "co-mention count")


def recompute_recent_paper(record: EvidenceRecord, pmid: str) -> str:
    """Recompute (confirm) a recent-paper PMID against ``esearchresult.idlist``.

    Returns ``pmid`` **iff** it is genuinely present in
    ``payload.esearchresult.idlist`` (Req 5.7, value table). Pure: reads only
    ``record.payload``.

    Raises :class:`LookupError` naming the ``pmid`` when it is not in the idlist.
    This is the point of the function: a claimed "recent paper" that is not
    actually in the saved evidence is a recompute failure, which the verifier
    turns into an ``UNSUPPORTED`` rejection (Req 6.3; steering rule 3). The PMID
    is never fabricated or "corrected" to a nearby one in the list.
    """
    payload: Any = record.payload
    esearchresult = payload.get("esearchresult") if isinstance(payload, dict) else None
    idlist = esearchresult.get("idlist") if isinstance(esearchresult, dict) else None
    if not isinstance(idlist, list):
        raise LookupError(
            "cannot recompute recent paper: payload has no "
            "esearchresult.idlist list"
        )
    if pmid not in idlist:
        raise LookupError(
            f"cannot recompute recent paper: pmid {pmid!r} not present in "
            f"esearchresult.idlist"
        )
    return pmid


__all__ = [
    "recompute_association",
    "recompute_datatype_breakdown",
    "recompute_literature_count",
    "recompute_co_mention_count",
    "recompute_recent_paper",
]
