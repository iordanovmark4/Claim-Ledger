"""Property 6: Score recomputation fidelity (pytest + Hypothesis).

**Validates: Requirements 2.4, 2.5, 6.5**

The recompute_* functions are the single source of truth for every claim value
(design "single source of truth", Req 4.5/6.1). This property pins their
*fidelity*: over OT associations payloads with variable-length and
arbitrarily-ordered datatype arrays, the recompute reproduces each row's
Overall_Score and its Datatype_Scores exactly — by keying on datatype ``id``,
never on position (Req 2.5) — within the 1e-9 score tolerance the verifier uses
(Req 6.5).

Concretely, for a synthetic payload built from generated rows (each with a
unique disease id so lookups are unambiguous):

* :func:`recompute_association` reproduces the row's ``score`` within abs_tol
  1e-9 (Req 2.4).
* :func:`recompute_datatype_breakdown` reproduces, keyed by id, exactly the
  row's generated ``{id: score}`` mapping within 1e-9 — independent of the order
  the datatypeScores were generated/stored in, and returned sorted by id so the
  value is canonical (Req 2.4, 2.5).
* Shuffling the datatypeScores array in the payload does not change the
  recomputed breakdown mapping (order-independence, Req 2.5).
"""

from __future__ import annotations

import math
import random

from hypothesis import given, settings
from hypothesis import strategies as st

from claimledger.evidence import record_from_envelope
from claimledger.models import DatatypeScore, EvidenceRecord, Source
from claimledger.recompute import (
    recompute_association,
    recompute_datatype_breakdown,
)
from tests.strategies import ot_association_rows, ot_associations_payload


def _record_from_rows(rows: list[dict]) -> EvidenceRecord:
    """Build an ot_associations EvidenceRecord wrapping the generated rows.

    Uses the same ``record_from_envelope`` the clients use, so the property
    exercises the real payload path ``data.target.associatedDiseases.rows[]``.
    """
    envelope = {
        "source_url": "https://synthetic.test/graphql",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "payload": ot_associations_payload(rows),
    }
    return record_from_envelope(Source.OT_ASSOCIATIONS, "synthetic-key", envelope)


@settings(max_examples=200)
@given(rows=ot_association_rows())
def test_recompute_reproduces_score_and_datatypes_by_id(rows: list[dict]) -> None:
    """Each row's score and id-keyed datatype mapping recompute within 1e-9.

    For every generated row, ``recompute_association`` reproduces its score and
    ``recompute_datatype_breakdown`` reproduces its datatype scores *keyed by id*
    (a {id: score} mapping), regardless of the datatypeScores order, and returns
    them sorted by id (canonical ordering). Validates Requirements 2.4, 2.5, 6.5.
    """
    record = _record_from_rows(rows)

    for row in rows:
        disease_id = row["disease"]["id"]

        # Req 2.4 / 6.5 — Overall_Score reproduced within the verifier tolerance.
        recomputed_score = recompute_association(record, disease_id)
        assert math.isclose(recomputed_score, row["score"], abs_tol=1e-9)

        # Req 2.5 — Datatype_Scores reproduced keyed by id, not position.
        breakdown = recompute_datatype_breakdown(record, disease_id)
        assert isinstance(breakdown, tuple)
        assert all(isinstance(ds, DatatypeScore) for ds in breakdown)

        expected = {e["id"]: e["score"] for e in row["datatypeScores"]}
        recomputed = {ds.id: ds.score for ds in breakdown}

        # Same set of ids, each with no duplicates in the output.
        assert set(recomputed) == set(expected)
        ids = [ds.id for ds in breakdown]
        assert len(ids) == len(set(ids))
        # Each datatype score matches the generated value keyed by id.
        for dt_id, exp_score in expected.items():
            assert math.isclose(recomputed[dt_id], exp_score, abs_tol=1e-9)

        # Canonical / deterministic ordering: ascending by datatype id.
        assert ids == sorted(ids)


@settings(max_examples=200)
@given(rows=ot_association_rows(), seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_datatype_breakdown_is_order_independent(rows: list[dict], seed: int) -> None:
    """Shuffling the datatypeScores array leaves the recomputed mapping intact.

    The recomputed {id: score} mapping (and its canonical by-id ordering) must be
    identical whether the datatypeScores array is in its generated order or a
    reshuffled one — the heart of "keyed by id, never by position" (Req 2.5).
    """
    record = _record_from_rows(rows)

    # Build a payload whose every row's datatypeScores array is independently
    # shuffled, then recompute from both and compare the id-keyed mappings.
    rng = random.Random(seed)
    shuffled_rows: list[dict] = []
    for row in rows:
        dts = list(row["datatypeScores"])
        rng.shuffle(dts)
        shuffled_rows.append({**row, "datatypeScores": dts})
    shuffled_record = _record_from_rows(shuffled_rows)

    for row in rows:
        disease_id = row["disease"]["id"]

        original = recompute_datatype_breakdown(record, disease_id)
        reshuffled = recompute_datatype_breakdown(shuffled_record, disease_id)

        # The canonical by-id tuple is identical regardless of payload order.
        assert original == reshuffled
        # And it matches the row's generated mapping.
        expected = {e["id"]: e["score"] for e in row["datatypeScores"]}
        assert {ds.id: ds.score for ds in reshuffled} == expected
