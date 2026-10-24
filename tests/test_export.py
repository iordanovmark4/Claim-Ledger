"""Unit tests for Exporter (Req 14.5).

These build genuine Verified_Claims end-to-end from the committed real EGFR
cache envelopes (steering rule 8) via :class:`ClaimBuilder` + :func:`verify_all`,
plus one *tampered* claim so there is a real Unsupported claim (carrying its
rejection reason) to export alongside them.

Coverage:

* ``export_claims_json`` produces parseable JSON with ``"verified"`` /
  ``"unsupported"`` arrays and the optional ``"gene"`` key;
* every serialized claim round-trips: ``Claim.from_dict(dict) == original``
  (the invariant Property 7 / task 14.2 protects);
* an Unsupported claim carries its ``reason`` through the export (Req 7.1);
* the JSON is deterministic — identical inputs, and any permutation of them,
  produce a byte-identical string;
* ``export_dossier_markdown`` equals ``DossierRenderer().render(...)`` for the
  same verified claims and contains only verified content (Req 7.2, 8.1).
"""

from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from claimledger.claims import ClaimBuilder
from claimledger.clients.opentargets import parse_associations
from claimledger.evidence import EvidenceStore, record_from_envelope
from claimledger.export import Exporter
from claimledger.models import Claim, ClaimStatus, ClaimType, Source
from claimledger.render import DossierRenderer
from claimledger.verify import Verifier, verify_all

GENE_SYMBOL = "EGFR"
OT_ASSOC_ID = "ot_associations:testassoc000001"
GENE_ID = "pubmed_gene:testgene00000001"
PAIR_ID = "pubmed_pair:testpair00000001"

NSCLC_DISEASE_ID = "MONDO_0005233"


# --------------------------------------------------------------------------- #
# Fixtures: real EGFR evidence, genuine verified + one tampered unsupported   #
# --------------------------------------------------------------------------- #
def _with_id(record, evidence_id: str):
    return replace(record, evidence_id=evidence_id)


@pytest.fixture
def full_store(
    ot_associations_envelope,
    pubmed_gene_envelope,
    pubmed_gene_and_disease_envelope,
) -> EvidenceStore:
    store = EvidenceStore()
    store.add(
        _with_id(
            record_from_envelope(
                Source.OT_ASSOCIATIONS, OT_ASSOC_ID, ot_associations_envelope
            ),
            OT_ASSOC_ID,
        )
    )
    store.add(
        _with_id(
            record_from_envelope(Source.PUBMED_GENE, GENE_ID, pubmed_gene_envelope),
            GENE_ID,
        )
    )
    store.add(
        _with_id(
            record_from_envelope(
                Source.PUBMED_PAIR, PAIR_ID, pubmed_gene_and_disease_envelope
            ),
            PAIR_ID,
        )
    )
    return store


@pytest.fixture
def associations(ot_associations_envelope):
    return parse_associations(
        ot_associations_envelope["payload"], 5, OT_ASSOC_ID
    ).associations


@pytest.fixture
def disease_names(associations):
    return {a.disease_id: a.disease_name for a in associations}


@pytest.fixture
def built_claims(full_store, associations) -> list[Claim]:
    return ClaimBuilder(full_store).build(
        GENE_SYMBOL,
        associations,
        ot_assoc_evidence_id=OT_ASSOC_ID,
        gene_evidence_id=GENE_ID,
        pair_evidence_ids_by_disease={NSCLC_DISEASE_ID: PAIR_ID},
    )


@pytest.fixture
def verdicts(full_store, built_claims):
    """Genuine verified claims plus one tampered -> Unsupported claim.

    Tampering the NSCLC ASSOCIATION value yields a real Unsupported claim whose
    reason reports both the claim and the recomputed value (Req 6.3), so the
    export's Unsupported branch is exercised with authentic data.
    """
    nsclc_assoc = next(
        c
        for c in built_claims
        if c.claim_type is ClaimType.ASSOCIATION
        and c.subject == f"{GENE_SYMBOL}::{NSCLC_DISEASE_ID}"
    )
    tampered = replace(nsclc_assoc, value=0.12345)
    claims = [c for c in built_claims if c is not nsclc_assoc] + [tampered]

    result = verify_all(Verifier(), claims, full_store)
    assert result.verified, "expected real EGFR evidence to produce verified claims"
    assert result.unsupported, "expected the tampered claim to be unsupported"
    return result


# --------------------------------------------------------------------------- #
# JSON structure (Req 14.5)                                                    #
# --------------------------------------------------------------------------- #
def test_export_json_parses_and_has_expected_arrays(verdicts) -> None:
    out = Exporter().export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    doc = json.loads(out)
    assert doc["gene"] == GENE_SYMBOL
    assert isinstance(doc["verified"], list) and doc["verified"]
    assert isinstance(doc["unsupported"], list) and doc["unsupported"]
    assert len(doc["verified"]) == len(verdicts.verified)
    assert len(doc["unsupported"]) == len(verdicts.unsupported)


def test_export_json_omits_gene_when_not_provided(verdicts) -> None:
    out = Exporter().export_claims_json(verdicts.verified, verdicts.unsupported)
    doc = json.loads(out)
    assert "gene" not in doc
    assert set(doc) == {"verified", "unsupported"}


# --------------------------------------------------------------------------- #
# Round-trip: each serialized claim reconstructs an equal Claim (Property 7)   #
# --------------------------------------------------------------------------- #
def test_each_serialized_claim_round_trips(verdicts) -> None:
    out = Exporter().export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    doc = json.loads(out)

    originals_by_id = {
        c.claim_id: c for c in list(verdicts.verified) + list(verdicts.unsupported)
    }
    for serialized in doc["verified"] + doc["unsupported"]:
        reconstructed = Claim.from_dict(serialized)
        assert reconstructed == originals_by_id[reconstructed.claim_id]


# --------------------------------------------------------------------------- #
# Unsupported claims carry their reason (Req 7.1)                              #
# --------------------------------------------------------------------------- #
def test_unsupported_claims_carry_reason(verdicts) -> None:
    out = Exporter().export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    doc = json.loads(out)
    for serialized in doc["unsupported"]:
        assert serialized["status"] == ClaimStatus.UNSUPPORTED.value
        assert serialized["reason"], "an Unsupported claim must carry a reason"
    # The tampered value is reported in the reason it travels with.
    assert any("0.12345" in s["reason"] for s in doc["unsupported"])


# --------------------------------------------------------------------------- #
# Determinism: same + permuted inputs -> byte-identical string                 #
# --------------------------------------------------------------------------- #
def test_export_json_is_deterministic_same_input(verdicts) -> None:
    exporter = Exporter()
    a = exporter.export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    b = exporter.export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    assert a == b


def test_export_json_is_deterministic_under_permutation(verdicts) -> None:
    exporter = Exporter()
    baseline = exporter.export_claims_json(
        verdicts.verified, verdicts.unsupported, gene_symbol=GENE_SYMBOL
    )
    rng = random.Random(4242)
    for _ in range(10):
        v = list(verdicts.verified)
        u = list(verdicts.unsupported)
        rng.shuffle(v)
        rng.shuffle(u)
        out = exporter.export_claims_json(v, u, gene_symbol=GENE_SYMBOL)
        assert out == baseline


# --------------------------------------------------------------------------- #
# Markdown export delegates to the one renderer (Req 7.2, 8.1, 14.5)           #
# --------------------------------------------------------------------------- #
def test_export_markdown_equals_renderer_output(verdicts, disease_names) -> None:
    out = Exporter().export_dossier_markdown(
        verdicts.verified, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    expected = DossierRenderer().render(
        verdicts.verified, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    assert out == expected


def test_export_markdown_excludes_unsupported_content(verdicts, disease_names) -> None:
    """Feeding unsupported claims into the Markdown export changes nothing.

    The renderer filters to Verified only, so the dossier for the verified
    claims is identical whether or not unsupported claims are also passed in —
    no Unsupported content ever reaches the prose (Req 7.2, 8.1).
    """
    verified_only = Exporter().export_dossier_markdown(
        verdicts.verified, gene_symbol=GENE_SYMBOL, disease_names=disease_names
    )
    with_unsupported = Exporter().export_dossier_markdown(
        list(verdicts.verified) + list(verdicts.unsupported),
        gene_symbol=GENE_SYMBOL,
        disease_names=disease_names,
    )
    assert with_unsupported == verified_only
