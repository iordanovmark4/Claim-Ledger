"""ResponseCache: request-key -> cache file, read/write envelope (Req 4.4, 11.7).

The on-disk cache is the project's single durable copy of every raw API
response. Each cache file holds the documented three-key envelope::

    {"source_url": <str>, "retrieved_at": <ISO-8601 str>, "payload": <json>}

which is exactly the shape of the committed ``data/cache/preflight_*.json``
captures and of every live response the ``HttpFetcher`` (task 4.2) writes.

``ResponseCache`` is deliberately narrow: it maps a *normalized request key*
(the same stable string used to mint an ``Evidence_ID`` — see
design "Evidence IDs and Determinism") to a deterministic, filesystem-safe
cache file, and reads/writes that envelope. It performs no HTTP, no rate
limiting, and no ``EvidenceRecord`` construction — those live in ``http.py``
(task 4.2) and ``evidence.py`` (task 5.1) respectively.

Why a hashed filename? The normalized key can be an arbitrary string (a GraphQL
URL plus a symbol, or a full canonicalized esearch URL with query params) that
is not safe or portable as a filename. Hashing the key yields a fixed-length,
filesystem-safe name that is *deterministic*: the same logical request — served
live or from cache — normalizes to the same key and therefore the same file, so
a cached fetch and a live fetch of the same request land in the same place
(design "Cache-first / cache-fallback", Req 11.7).

On read the original ``source_url`` and ``retrieved_at`` from the stored
envelope are returned untouched, so a downstream ``EvidenceRecord`` built from a
cache hit carries the *original* provenance of the real response rather than the
moment it was re-read (Req 11.7).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Repository root: src/claimledger/cache.py -> src/claimledger -> src -> repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Default on-disk cache directory (Req 4.4). Injectable via the constructor so
# tests can point the cache at a tmp_path and never touch the committed files.
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "cache"

# The three keys every cache file is expected to carry.
_ENVELOPE_KEYS = ("source_url", "retrieved_at", "payload")


class ResponseCache:
    """Maps a normalized request key to a cache file holding the envelope.

    Parameters
    ----------
    cache_dir:
        Directory under which cache files live. Defaults to ``data/cache`` at
        the repository root (Req 4.4); pass a ``tmp_path`` in tests so writes
        are isolated from the committed captures.
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR

    def path_for(self, key: str) -> Path:
        """Return the deterministic cache file path for a normalized request key.

        The key is hashed with SHA-256 to produce a fixed-length,
        filesystem-safe filename. The mapping is a pure function of the key, so
        the same logical request (live or cached) always maps to the same file
        (design "Cache-first / cache-fallback", Req 11.7). Distinct keys map to
        distinct files except for a cryptographic hash collision.
        """
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def exists(self, key: str) -> bool:
        """Return True iff a cache file for ``key`` is present on disk."""
        return self.path_for(key).is_file()

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the stored ``{source_url, retrieved_at, payload}`` envelope.

        Returns ``None`` when no cache file exists for ``key``. The envelope is
        returned as parsed from disk, preserving the original ``source_url`` and
        ``retrieved_at`` so a downstream ``EvidenceRecord`` carries the real
        response's provenance (Req 11.7).
        """
        path = self.path_for(key)
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def write(
        self,
        key: str,
        source_url: str,
        retrieved_at: str,
        payload: Any,
    ) -> Path:
        """Persist the ``{source_url, retrieved_at, payload}`` envelope (Req 4.4).

        Creates the cache directory if needed, writes the three-key envelope as
        UTF-8 JSON to ``key``'s cache file, and returns the path written. The
        envelope shape matches the committed ``preflight_*.json`` captures so a
        file written here is indistinguishable from a real capture on later
        reads.
        """
        envelope: dict[str, Any] = {
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "payload": payload,
        }
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, indent=2)
        return path
