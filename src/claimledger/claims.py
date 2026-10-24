"""ClaimBuilder: build the five claim types from evidence (Req 5).

``ClaimBuilder`` turns an :class:`~claimledger.evidence.EvidenceStore` full of
real cached responses into structured :class:`~claimledger.models.Claim`
objects of the five fixed types (Req 5.1). It is the only place claims are
*constructed*, but it is **not** a numeric source of truth: every value is
produced by calling the matching ``recompute_*`` function in
:mod:`claimledger.recompute` (the single source of truth, Req 5.8, steering
rule 3). Because the builder and the :class:`~claimledger.verify.Verifier` both
compute values through the same ``recompute_*`` functions, a freshly built
claim verifies by construction — they can never disagree by accident
(design "Claim Building", Req 4.5).

Evidence attachment (Req 5.2, steering rule 1)
----------------------------------------------
Every claim carries exactly the Evidence_ID(s) of the record its value was
recomputed from:

* ``ASSOCIATION`` / ``DATATYPE_BREAKDOWN`` -> the OT associations record id
  (Req 5.3, 5.4);
* ``LITERATURE_COUNT`` -> the gene-only PubMed record id (Req 5.5);
* ``CO_MENTION_COUNT`` -> the gene+disease PubMed record id (Req 5.6);
* ``RECENT_PAPER`` -> the PubMed record the PMID was drawn from (Req 5.7).

No claim is ever emitted with zero evidence ids.

Availability handling (steering rule 6, Req 3.8, design "Claim Building")
-------------------------------------------------------------------------
A value is only ever produced by ``recompute_*`` reading a real record — it is
never fabricated. Two kinds of unavailability are handled by **omission**, the
coherent choice taken here:

1. **The evidence record is absent from the store.** When PubMed was
   unavailable and no gene/pair record was stored, or an OT id was never minted,
   there is simply nothing to attach a claim to — so that claim is omitted
   rather than invented. Emitting an evidence-less claim would violate Req 5.2;
   emitting one that references a missing id would just produce a guaranteed
   ``UNSUPPORTED`` with no informational value and no evidence to show.
2. **The record is present but ``recompute_*`` cannot produce a value**
   (e.g. ``esearchresult.count`` absent/unparseable, or a claimed ``disease_id``
   not in the rows). ``recompute_*`` raises a precise error in that case; the
   builder catches it and omits the claim, because a record that cannot yield a
   supported value must not be turned into a fabricated claim (steering rule 3).

In both cases the claim is *omitted* — never silently kept with a made-up
value. (The design permits either "omit" or "emit-for-rejection"; this builder
consistently omits when the underlying value cannot be genuinely recomputed, so
no claim ever carries a number the evidence does not support.)

Determinism
-----------
Each claim gets a stable, deterministic ``claim_id`` derived from its type,
subject, and a discriminator (disease id or PMID). The build order is
deterministic too (associations in their selected order, PMIDs in idlist
order), though the renderer re-sorts by its own composite key later
(design "Deterministic ordering").
"""

from __future__ import annotations

from typing import Iterable, Optional

from .evidence import EvidenceStore
from .models import Association, Claim, ClaimType, Source
from .recompute import (
    recompute_association,
    recompute_co_mention_count,
    recompute_datatype_breakdown,
    recompute_literature_count,
    recompute_recent_paper,
)

# Separator used to build a "GENE::DISEASE" subject so a pair claim carries both
# sides of its context in one stable string (models.Claim.subject intent).
SUBJECT_SEP = "::"


def _pair_subject(gene_symbol: str, disease_id: str) -> str:
    """Build the ``GENE::DISEASE`` subject for a gene-disease pair claim."""
    return f"{gene_symbol}{SUBJECT_SEP}{disease_id}"


class ClaimBuilder:
    """Construct the five claim types from an :class:`EvidenceStore`.

    The builder is given the per-run store (so it can look records up by id and
    decide whether evidence is present) and, at build time, the Evidence_IDs the
    pipeline minted for each retrieved record plus the parsed associations. It
    reads values *only* through ``recompute_*`` (Req 5.8) and attaches the
    matching Evidence_ID to every claim (Req 5.2).
    """

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    # -- individual builders ------------------------------------------------ #
    def _build_association_claims(
        self,
        gene_symbol: str,
        associations: Iterable[Association],
        ot_assoc_evidence_id: Optional[str],
    ) -> list[Claim]:
        """ASSOCIATION + DATATYPE_BREAKDOWN per pair (Req 5.3, 5.4).

        Both claim types read the same OT associations record. If that record is
        not in the store (OT unavailable / no id minted) every association claim
        is omitted — there is nothing to attach (steering rule 6). For each
        association, the Overall_Score and the datatype breakdown are recomputed
        by ``disease_id``; a row that cannot be recomputed (absent id / broken
        row) is skipped rather than fabricated.
        """
        claims: list[Claim] = []
        if ot_assoc_evidence_id is None or not self._store.contains(
            ot_assoc_evidence_id
        ):
            return claims
        record = self._store.get(ot_assoc_evidence_id)
        if record is None:  # pragma: no cover - contains() already guarded
            return claims

        for assoc in associations:
            disease_id = assoc.disease_id
            subject = _pair_subject(gene_symbol, disease_id)

            # ASSOCIATION (value = Overall_Score recomputed by id).
            try:
                score = recompute_association(record, disease_id)
            except (LookupError, ValueError):
                # Evidence cannot genuinely supply this score — omit, never fake.
                pass
            else:
                claims.append(
                    Claim(
                        claim_id=f"ASSOCIATION:{subject}",
                        claim_type=ClaimType.ASSOCIATION,
                        subject=subject,
                        value=score,
                        evidence_ids=(ot_assoc_evidence_id,),
                    )
                )

            # DATATYPE_BREAKDOWN (value = ordered-by-id tuple of DatatypeScore).
            try:
                breakdown = recompute_datatype_breakdown(record, disease_id)
            except (LookupError, ValueError):
                pass
            else:
                claims.append(
                    Claim(
                        claim_id=f"DATATYPE_BREAKDOWN:{subject}",
                        claim_type=ClaimType.DATATYPE_BREAKDOWN,
                        subject=subject,
                        value=breakdown,
                        evidence_ids=(ot_assoc_evidence_id,),
                    )
                )
        return claims

    def _build_literature_count_claim(
        self, gene_symbol: str, gene_evidence_id: Optional[str]
    ) -> list[Claim]:
        """LITERATURE_COUNT for the gene (Req 5.5).

        Omitted when the gene-only PubMed record is absent (PubMed unavailable)
        or when its count cannot be recomputed (count absent/unparseable) — never
        fabricated as zero.
        """
        if gene_evidence_id is None or not self._store.contains(gene_evidence_id):
            return []
        record = self._store.get(gene_evidence_id)
        if record is None:  # pragma: no cover - contains() already guarded
            return []
        try:
            count = recompute_literature_count(record)
        except (LookupError, ValueError):
            return []
        return [
            Claim(
                claim_id=f"LITERATURE_COUNT:{gene_symbol}",
                claim_type=ClaimType.LITERATURE_COUNT,
                subject=gene_symbol,
                value=count,
                evidence_ids=(gene_evidence_id,),
            )
        ]

    def _build_co_mention_claims(
        self,
        gene_symbol: str,
        pair_evidence_ids_by_disease: dict[str, str],
    ) -> list[Claim]:
        """CO_MENTION_COUNT per pair (Req 5.6).

        For each ``disease_id -> pair Evidence_ID`` wiring, recompute the
        co-mention count from the gene+disease PubMed record. A disease whose
        pair record is absent from the store (PubMed pair unavailable) or whose
        count cannot be recomputed is omitted (steering rule 6).
        """
        claims: list[Claim] = []
        # Deterministic iteration regardless of mapping insertion order.
        for disease_id in sorted(pair_evidence_ids_by_disease):
            pair_evidence_id = pair_evidence_ids_by_disease[disease_id]
            if not self._store.contains(pair_evidence_id):
                continue
            record = self._store.get(pair_evidence_id)
            if record is None:  # pragma: no cover - contains() already guarded
                continue
            try:
                count = recompute_co_mention_count(record)
            except (LookupError, ValueError):
                continue
            subject = _pair_subject(gene_symbol, disease_id)
            claims.append(
                Claim(
                    claim_id=f"CO_MENTION_COUNT:{subject}",
                    claim_type=ClaimType.CO_MENTION_COUNT,
                    subject=subject,
                    value=count,
                    evidence_ids=(pair_evidence_id,),
                )
            )
        return claims

    def _build_recent_paper_claims(
        self,
        gene_symbol: str,
        recent_from: Iterable[tuple[str, Optional[str]]],
    ) -> list[Claim]:
        """RECENT_PAPER: one claim per PMID in a record's idlist (Req 5.7).

        ``recent_from`` is an ordered iterable of ``(evidence_id, subject)``
        pairs naming each PubMed record to draw recent papers from (at minimum
        the gene record; optionally per-pair records). For each record present in
        the store, one claim is emitted per PMID in its ``esearchresult.idlist``,
        with the PMID confirmed against the idlist via ``recompute_recent_paper``
        (a PMID that is not genuinely in the list is skipped, never fabricated).
        ``subject`` defaults to the gene symbol when ``None``.
        """
        claims: list[Claim] = []
        for evidence_id, subject in recent_from:
            if evidence_id is None or not self._store.contains(evidence_id):
                continue
            record = self._store.get(evidence_id)
            if record is None:  # pragma: no cover - contains() already guarded
                continue
            claim_subject = subject if subject is not None else gene_symbol
            idlist = self._idlist(record.payload)
            for pmid in idlist:
                try:
                    confirmed = recompute_recent_paper(record, pmid)
                except (LookupError, ValueError):
                    continue
                claims.append(
                    Claim(
                        claim_id=f"RECENT_PAPER:{claim_subject}:{confirmed}",
                        claim_type=ClaimType.RECENT_PAPER,
                        subject=claim_subject,
                        value=confirmed,
                        evidence_ids=(evidence_id,),
                    )
                )
        return claims

    @staticmethod
    def _idlist(payload: object) -> list[str]:
        """Read ``payload.esearchresult.idlist`` as a list (order preserved).

        Returns ``[]`` when the path is absent or not a list, so a record with no
        recent papers simply yields no RECENT_PAPER claims (never fabricated).
        """
        esearchresult = payload.get("esearchresult") if isinstance(payload, dict) else None
        idlist = esearchresult.get("idlist") if isinstance(esearchresult, dict) else None
        if not isinstance(idlist, list):
            return []
        return [pmid for pmid in idlist if isinstance(pmid, str)]

    # -- public API --------------------------------------------------------- #
    def build(
        self,
        gene_symbol: str,
        associations: Iterable[Association],
        *,
        ot_assoc_evidence_id: Optional[str] = None,
        gene_evidence_id: Optional[str] = None,
        pair_evidence_ids_by_disease: Optional[dict[str, str]] = None,
        recent_from: Optional[Iterable[tuple[str, Optional[str]]]] = None,
    ) -> list[Claim]:
        """Build all five claim types for one gene run (Req 5.1-5.7).

        Parameters
        ----------
        gene_symbol:
            The resolved gene symbol; used as the subject of gene-scoped claims
            and the ``GENE`` half of each ``GENE::DISEASE`` pair subject.
        associations:
            The parsed top :class:`Association` rows
            (``OTClient.associated_diseases(...).associations``). ASSOCIATION and
            DATATYPE_BREAKDOWN claims are built per association.
        ot_assoc_evidence_id:
            Evidence_ID of the OT associations record. ``None`` or absent-from-
            store ⇒ no association/datatype claims (nothing to attach).
        gene_evidence_id:
            Evidence_ID of the gene-only PubMed record (LITERATURE_COUNT).
        pair_evidence_ids_by_disease:
            ``disease_id -> pair Evidence_ID`` for the gene+disease PubMed
            records (CO_MENTION_COUNT per pair). Defaults to empty.
        recent_from:
            Ordered ``(evidence_id, subject)`` pairs to draw RECENT_PAPER claims
            from. When omitted, recent papers are drawn from the gene record
            (``gene_evidence_id``) with the gene symbol as subject, per the
            design's "at minimum from the gene record's idlist".

        Returns
        -------
        list[Claim]
            All built claims in a deterministic order. Every claim carries >= 1
            Evidence_ID (Req 5.2); claims whose value cannot be genuinely
            recomputed (missing record / unavailable value) are omitted, never
            fabricated (steering rule 6).
        """
        pair_ids = pair_evidence_ids_by_disease or {}
        if recent_from is None:
            # Default: recent papers from the gene record only (design minimum).
            recent_from = (
                [(gene_evidence_id, gene_symbol)]
                if gene_evidence_id is not None
                else []
            )

        claims: list[Claim] = []
        claims.extend(
            self._build_association_claims(
                gene_symbol, associations, ot_assoc_evidence_id
            )
        )
        claims.extend(
            self._build_literature_count_claim(gene_symbol, gene_evidence_id)
        )
        claims.extend(self._build_co_mention_claims(gene_symbol, pair_ids))
        claims.extend(self._build_recent_paper_claims(gene_symbol, recent_from))
        return claims


__all__ = ["ClaimBuilder", "SUBJECT_SEP"]
