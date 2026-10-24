"""Exporter: claims -> JSON, dossier -> Markdown (Req 14.5).

The :class:`Exporter` turns a run's verdicts into the two artifacts the UI's
download control offers (Req 14.5, design "Presentation"):

* **JSON** of the Verified_Claims *and* the Unsupported_Claims, serialized
  through each claim's own :meth:`~claimledger.models.Claim.to_dict` so the
  export round-trips: ``Claim.from_dict(json-loaded dict) == original`` (the
  invariant Property 7 / task 14.2 protects). Because ``to_dict`` carries the
  ``status`` and ``reason`` fields, an exported Unsupported claim keeps the
  reason it was rejected for (Req 7.1).
* **Markdown** of the dossier, delegated verbatim to
  :class:`~claimledger.render.DossierRenderer` so there is exactly one renderer
  and the export can never drift from what the UI shows. Only Verified_Claims
  reach the dossier (the renderer filters defensively — Req 7.2, 8.1).

Purity and determinism
-----------------------
Both methods are **pure string producers**: they take claims in and return a
``str``, touching no files, no network, and no global state (writing files is
the UI's job, task 18.4; steering rules 5/10). Determinism is deliberate so
exports diff cleanly and tests can assert string equality:

* claims are serialized in a **sorted order** — by ``(status, claim_id)`` within
  each array — so the input order never changes the bytes (the pipeline/UI may
  hand claims over in any order);
* :func:`json.dumps` is called with ``indent=2``, ``sort_keys=True`` and
  ``ensure_ascii=False`` so key order and spacing are fixed and non-ASCII
  disease names are preserved as themselves rather than ``\\uXXXX`` escapes.

Together these mean identical inputs — and any permutation of them — produce a
byte-identical JSON string.
"""

from __future__ import annotations

import json
from typing import Iterable, Mapping, Optional

from .models import Claim
from .render import DossierRenderer

__all__ = ["Exporter"]


def _claim_sort_key(claim: Claim) -> tuple[str, str]:
    """Deterministic per-claim ordering key: ``(status, claim_id)``.

    ``claim_id`` is already a stable, unique string minted by the builder
    (``TYPE:subject[:discriminator]``), so sorting by it alone is deterministic;
    ``status`` is prepended only to keep the ordering intuitive within an array
    that (in principle) could mix statuses. Both components are plain strings so
    the sort is total and locale-independent.
    """
    return (claim.status.value, claim.claim_id)


def _serialize_claims(claims: Iterable[Claim]) -> list[dict]:
    """Serialize claims to a list of dicts in deterministic ``claim_id`` order.

    Each claim is serialized via its own :meth:`Claim.to_dict`, so the output
    round-trips through :meth:`Claim.from_dict`. The list is sorted by
    :func:`_claim_sort_key` so input order does not affect the result.
    """
    return [claim.to_dict() for claim in sorted(claims, key=_claim_sort_key)]


class Exporter:
    """Serialize a run's claims to JSON and its dossier to Markdown (Req 14.5).

    Stateless: a single instance can export any number of runs. Holds a private
    :class:`DossierRenderer` purely as the one place Markdown is produced; it has
    no per-run state, so every method is a pure function of its arguments.
    """

    def __init__(self) -> None:
        self._renderer = DossierRenderer()

    def export_claims_json(
        self,
        verified_claims: Iterable[Claim],
        unsupported_claims: Iterable[Claim],
        *,
        gene_symbol: Optional[str] = None,
    ) -> str:
        """Serialize verified + unsupported claims to a deterministic JSON string.

        Parameters
        ----------
        verified_claims, unsupported_claims:
            The two claim lists from :func:`~claimledger.verify.verify_all`. Each
            claim is serialized through its own :meth:`Claim.to_dict`, which
            carries ``status`` and ``reason`` — so an Unsupported claim's reason
            travels with it (Req 7.1) and every serialized claim round-trips via
            :meth:`Claim.from_dict`.
        gene_symbol:
            Optional gene the run was about; included as the top-level ``"gene"``
            key only when provided, so the artifact is self-describing without
            forcing a value when the caller has none.

        Returns
        -------
        str
            A pretty-printed JSON object with the shape::

                {"gene": <symbol?>, "verified": [...], "unsupported": [...]}

            Claims within each array are ordered by ``(status, claim_id)`` and
            the object is dumped with ``indent=2``, ``sort_keys=True`` and
            ``ensure_ascii=False`` — so the same claims (in any order) always
            produce the identical string.
        """
        document: dict[str, object] = {
            "verified": _serialize_claims(verified_claims),
            "unsupported": _serialize_claims(unsupported_claims),
        }
        if gene_symbol is not None:
            document["gene"] = gene_symbol

        return json.dumps(
            document,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )

    def export_dossier_markdown(
        self,
        verified_claims: Iterable[Claim],
        *,
        gene_symbol: str,
        disease_names: Optional[Mapping[str, str]] = None,
    ) -> str:
        """Render the dossier Markdown for ``gene_symbol`` from verified claims.

        Delegates verbatim to :meth:`DossierRenderer.render` so the exported
        Markdown is byte-identical to what the UI renders — there is a single
        renderer and the export can never diverge from it. Only Verified_Claims
        contribute (the renderer filters defensively, Req 7.2, 8.1).
        """
        return self._renderer.render(
            verified_claims,
            gene_symbol=gene_symbol,
            disease_names=disease_names,
        )
