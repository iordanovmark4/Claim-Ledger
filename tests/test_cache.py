"""Unit tests for ResponseCache (Req 4.4, 11.7).

These tests write exclusively to ``tmp_path`` so the committed real captures in
``data/cache`` are never touched. They cover:

* a write -> read round-trip returning the same envelope;
* ``exists`` reflecting on-disk presence;
* deterministic key -> path mapping (same key -> same path, different keys ->
  different paths);
* ``read`` of an absent key returning ``None``;
* reading a committed real envelope's ``{source_url, retrieved_at, payload}``
  through the cache after copying it into the cache dir under its key's path,
  confirming the original provenance is preserved (Req 11.7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimledger.cache import DEFAULT_CACHE_DIR, ResponseCache

# The committed real OT target-search capture, used for the real-envelope test.
_REAL_OT_SEARCH = DEFAULT_CACHE_DIR / "preflight_ot_search.json"


def test_write_then_read_round_trips_the_envelope(tmp_path: Path) -> None:
    """write(key, ...) then read(key) returns the same three-key envelope."""
    cache = ResponseCache(cache_dir=tmp_path)
    key = "https://api.platform.opentargets.org/api/v4/graphql|search|EGFR"
    source_url = "https://api.platform.opentargets.org/api/v4/graphql"
    retrieved_at = "2026-10-24T15:21:34.685346Z"
    payload = {"data": {"search": {"hits": [{"id": "ENSG00000146648"}]}}}

    cache.write(key, source_url, retrieved_at, payload)
    envelope = cache.read(key)

    assert envelope == {
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "payload": payload,
    }


def test_write_persists_the_file_under_the_cache_dir(tmp_path: Path) -> None:
    """write returns a path inside the cache dir and actually creates it."""
    cache = ResponseCache(cache_dir=tmp_path)
    key = "k"

    path = cache.write(key, "u", "t", {"a": 1})

    assert path == cache.path_for(key)
    assert path.is_file()
    assert path.parent == tmp_path


def test_exists_reflects_presence(tmp_path: Path) -> None:
    """exists is False before a write and True after it."""
    cache = ResponseCache(cache_dir=tmp_path)
    key = "some|request|key"

    assert cache.exists(key) is False
    cache.write(key, "u", "t", {"p": True})
    assert cache.exists(key) is True


def test_read_of_absent_key_returns_none(tmp_path: Path) -> None:
    """read returns None when no cache file exists for the key."""
    cache = ResponseCache(cache_dir=tmp_path)
    assert cache.read("never-written") is None


def test_same_key_maps_to_same_path(tmp_path: Path) -> None:
    """The key -> path mapping is deterministic across calls and instances."""
    key = "https://eutils.ncbi.nlm.nih.gov/esearch?db=pubmed&term=X"

    cache_a = ResponseCache(cache_dir=tmp_path)
    cache_b = ResponseCache(cache_dir=tmp_path)

    assert cache_a.path_for(key) == cache_a.path_for(key)
    assert cache_a.path_for(key) == cache_b.path_for(key)


def test_different_keys_map_to_different_paths(tmp_path: Path) -> None:
    """Distinct keys map to distinct cache files."""
    cache = ResponseCache(cache_dir=tmp_path)

    p1 = cache.path_for("key-one")
    p2 = cache.path_for("key-two")

    assert p1 != p2


def test_distinct_keys_do_not_collide_on_write_read(tmp_path: Path) -> None:
    """Two keys written to the same cache keep independent envelopes."""
    cache = ResponseCache(cache_dir=tmp_path)

    cache.write("gene:EGFR", "url-a", "t-a", {"count": 1})
    cache.write("gene:TP53", "url-b", "t-b", {"count": 2})

    assert cache.read("gene:EGFR") == {
        "source_url": "url-a",
        "retrieved_at": "t-a",
        "payload": {"count": 1},
    }
    assert cache.read("gene:TP53") == {
        "source_url": "url-b",
        "retrieved_at": "t-b",
        "payload": {"count": 2},
    }


def test_reading_a_committed_real_envelope_preserves_provenance(
    tmp_path: Path,
) -> None:
    """A copied real capture reads back with its original source_url/retrieved_at.

    Reproduces the cache-hit path's provenance guarantee (Req 11.7): copy the
    committed real OT search envelope to the cache file for a chosen key, then
    read it through ResponseCache and confirm the original ``source_url`` and
    ``retrieved_at`` come back unchanged along with the payload.
    """
    real = json.loads(_REAL_OT_SEARCH.read_text(encoding="utf-8"))
    assert set(real) >= {"source_url", "retrieved_at", "payload"}

    cache = ResponseCache(cache_dir=tmp_path)
    key = "https://api.platform.opentargets.org/api/v4/graphql|search|EGFR"
    # Place the real capture exactly where this key maps.
    target = cache.path_for(key)
    target.write_text(
        json.dumps(real, ensure_ascii=False), encoding="utf-8"
    )

    envelope = cache.read(key)

    assert envelope is not None
    assert envelope["source_url"] == real["source_url"]
    assert envelope["retrieved_at"] == real["retrieved_at"]
    assert envelope["payload"] == real["payload"]
