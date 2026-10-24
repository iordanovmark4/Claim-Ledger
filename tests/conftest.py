"""Shared pytest fixtures (STEERING RULE 8, Req 4.6, 12.8).

Per steering rule 8, parsers must be written against *saved real responses*,
never assumed shapes. These fixtures load the committed real cache envelopes in
``data/cache`` and expose their ``{source_url, retrieved_at, payload}`` shape so
every later parser (tasks 6 and 7) is tested against the actual API output.

A lazily-importing ``evidence_store_factory`` builds an ``EvidenceStore`` from
these envelopes for downstream tests. ``EvidenceStore`` does not exist until
task 5.1, so the factory imports ``claimledger.evidence`` inside its body and
skips gracefully when the class is absent. This keeps the suite collecting and
passing today while the helper becomes usable once task 5 lands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

# Repository root (two levels up from this file: tests/ -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "data" / "cache"

# Crafted (non-real) fixtures committed under tests/fixtures/cache. These are
# deliberately malformed to exercise error paths the real captures do not, such
# as the Requirement 2.6 incomplete-row path (task 6.2).
FIXTURE_CACHE_DIR = REPO_ROOT / "tests" / "fixtures" / "cache"

# The committed real responses, by logical name. Each is a
# ``{source_url, retrieved_at, payload}`` envelope captured from a live call.
REAL_CACHE_FILES: dict[str, str] = {
    "ot_search": "preflight_ot_search.json",
    "ot_associations": "preflight_ot_associations.json",
    "pubmed_gene": "preflight_pubmed_gene.json",
    "pubmed_gene_and_disease": "preflight_pubmed_gene_and_disease.json",
}


def _load_envelope(filename: str) -> dict[str, Any]:
    """Read one committed cache file and return its parsed envelope."""
    path = CACHE_DIR / filename
    with path.open(encoding="utf-8") as fh:
        envelope = json.load(fh)
    # Every committed cache file is the documented three-key envelope.
    assert set(envelope) >= {"source_url", "retrieved_at", "payload"}, (
        f"{filename} is not a {{source_url, retrieved_at, payload}} envelope"
    )
    return envelope


@pytest.fixture(scope="session")
def real_cache_envelopes() -> dict[str, dict[str, Any]]:
    """Map logical name -> committed real cache envelope (Req 4.6, 12.8)."""
    return {name: _load_envelope(fn) for name, fn in REAL_CACHE_FILES.items()}


@pytest.fixture(scope="session")
def ot_search_envelope(
    real_cache_envelopes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The real Open Targets target-search envelope."""
    return real_cache_envelopes["ot_search"]


@pytest.fixture(scope="session")
def ot_associations_envelope(
    real_cache_envelopes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The real Open Targets associated-diseases envelope."""
    return real_cache_envelopes["ot_associations"]


@pytest.fixture(scope="session")
def pubmed_gene_envelope(
    real_cache_envelopes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The real PubMed gene-only esearch envelope."""
    return real_cache_envelopes["pubmed_gene"]


@pytest.fixture(scope="session")
def pubmed_gene_and_disease_envelope(
    real_cache_envelopes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The real PubMed gene+disease esearch envelope."""
    return real_cache_envelopes["pubmed_gene_and_disease"]


@pytest.fixture(scope="session")
def malformed_ot_associations_envelope() -> dict[str, Any]:
    """A crafted malformed OT associations envelope for the incomplete-row path.

    Based on the real shape of ``preflight_ot_associations.json`` (same
    ``{source_url, retrieved_at, payload}`` envelope and
    ``data.target.associatedDiseases.rows[]`` structure) but deliberately
    small and broken: it holds one well-formed row and several rows each
    missing an expected field (``score``, ``disease.name``, or ``disease``
    entirely). It drives the Requirement 2.6 "mark Association.incomplete=True
    and continue" path that task 6.2 implements.
    """
    path = FIXTURE_CACHE_DIR / "malformed_ot_associations.json"
    with path.open(encoding="utf-8") as fh:
        envelope = json.load(fh)
    assert set(envelope) >= {"source_url", "retrieved_at", "payload"}, (
        "malformed_ot_associations.json is not a "
        "{source_url, retrieved_at, payload} envelope"
    )
    return envelope


@pytest.fixture
def evidence_store_factory() -> Callable[..., Any]:
    """Return a factory that builds an ``EvidenceStore`` from the envelopes.

    ``EvidenceStore`` and the envelope->``EvidenceRecord`` mapping are built in
    task 5.1. Until then this factory lazily imports ``claimledger.evidence``
    and skips the calling test when the class (or a documented construction
    entry point) is unavailable, so the suite still collects and passes today.

    Once task 5 lands, the factory prefers, in order:
      * ``EvidenceStore.from_envelopes(mapping)`` if provided, else
      * ``EvidenceStore.from_envelope`` / ``add_envelope`` per envelope, else
      * building ``EvidenceRecord`` objects from each envelope and ``add``-ing
        them (mapping ``source_url`` -> url, ``retrieved_at``, ``payload``).
    """

    def _factory(envelopes: dict[str, dict[str, Any]] | None = None) -> Any:
        try:
            import claimledger.evidence as evidence_mod
        except Exception as exc:  # pragma: no cover - defensive
            pytest.skip(f"claimledger.evidence not importable yet: {exc}")

        store_cls = getattr(evidence_mod, "EvidenceStore", None)
        if store_cls is None:
            pytest.skip("EvidenceStore not implemented yet (task 5.1)")

        envs = (
            envelopes
            if envelopes is not None
            else {name: _load_envelope(fn) for name, fn in REAL_CACHE_FILES.items()}
        )

        # Preferred bulk constructor, if the real implementation exposes one.
        if hasattr(store_cls, "from_envelopes"):
            return store_cls.from_envelopes(envs)

        store = store_cls()
        for name, envelope in envs.items():
            if hasattr(store, "add_envelope"):
                store.add_envelope(name, envelope)
            elif hasattr(store_cls, "from_envelope") and hasattr(store, "add"):
                store.add(store_cls.from_envelope(name, envelope))
            else:  # pragma: no cover - exercised only once task 5 lands
                pytest.skip(
                    "EvidenceStore lacks an envelope construction entry point"
                )
        return store

    return _factory
