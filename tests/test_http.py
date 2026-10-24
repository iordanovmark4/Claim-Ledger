"""Example-based tests for RateLimiter and HttpFetcher (Req 3.9, 11.4-11.7, 12.8).

These are deterministic example tests (no Hypothesis). Timing is driven by a
fake monotonic clock plus a fake sleep that advances that clock, so the pacing
assertions hold without any real waiting. The cache side uses a *real*
``ResponseCache`` over ``tmp_path`` so no committed capture under ``data/cache``
is ever read or written.

Coverage:

RateLimiter (Req 3.9)
* ``min_spacing_s`` is the reciprocal of the rate (1/3 s at 3.0 req/s).
* the first ``acquire`` never sleeps;
* a back-to-back burst is paced to >= 1/3 s between successive acquires;
* no sleep is requested when enough virtual time has already elapsed.

HttpFetcher (Req 11.4-11.7)
* live success -> available, ``from_cache=False``, envelope written to cache;
* live failure with a cache present -> available, ``from_cache=True``, carrying
  the *original* ``source_url`` / ``retrieved_at`` from the cached envelope;
* live failure with no cache -> ``available=False`` and no fabricated payload;
* cache-first/offline -> cached envelope served with ``from_cache=True`` without
  ever calling ``do_request``.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from claimledger.cache import ResponseCache
from claimledger.config import Config
from claimledger.http import FetchResult, HttpFetcher, RateLimiter


class FakeClock:
    """A controllable monotonic clock whose time only ever advances.

    ``__call__`` returns the current virtual time; a paired :class:`FakeSleep`
    advances it. Starts at an arbitrary non-zero offset so no test accidentally
    relies on t0 == 0.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    """A fake ``sleep`` that advances a :class:`FakeClock` and records durations.

    Records every requested duration in ``durations`` so a test can assert both
    *whether* a sleep happened and *how long* it was, without blocking.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.durations: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        self._clock.now += seconds


# --- RateLimiter (Req 3.9) ---------------------------------------------------


def test_min_spacing_is_reciprocal_of_rate() -> None:
    """At 3.0 req/s the enforced min spacing is exactly 1/3 s."""
    limiter = RateLimiter(max_requests_per_second=3.0)
    assert limiter.min_spacing_s == pytest.approx(1.0 / 3.0)


def test_first_acquire_does_not_sleep() -> None:
    """The very first acquire returns immediately with no sleep requested."""
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(max_requests_per_second=3.0, clock=clock, sleep=sleep)

    limiter.acquire()

    assert sleep.durations == []


def test_burst_is_paced_to_at_least_one_third_second_spacing() -> None:
    """A back-to-back burst yields >= 1/3 s between successive acquires (Req 3.9).

    With the fake sleep advancing the fake clock, each acquire's observed time
    is recorded; consecutive differences must be at least the min spacing.
    """
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(max_requests_per_second=3.0, clock=clock, sleep=sleep)
    min_spacing = limiter.min_spacing_s

    observed_times: List[float] = []
    for _ in range(5):
        limiter.acquire()  # no virtual time elapses between calls on its own
        observed_times.append(clock.now)

    # First acquire does not sleep; the remaining four each wait a full spacing.
    assert sleep.durations == pytest.approx([min_spacing] * 4)

    spacings = [b - a for a, b in zip(observed_times, observed_times[1:])]
    for spacing in spacings:
        assert spacing >= min_spacing - 1e-12
    assert spacings == pytest.approx([min_spacing] * 4)


def test_no_sleep_when_enough_time_already_elapsed() -> None:
    """When the virtual clock already passed the min spacing, no sleep occurs."""
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(max_requests_per_second=3.0, clock=clock, sleep=sleep)

    limiter.acquire()  # first — no sleep
    # Advance well past the min spacing before the next acquire.
    clock.now += limiter.min_spacing_s * 2
    limiter.acquire()  # enough elapsed — must not sleep

    assert sleep.durations == []


def test_rate_limiter_rejects_non_positive_rate() -> None:
    """A non-positive rate is a configuration error."""
    with pytest.raises(ValueError):
        RateLimiter(max_requests_per_second=0)


# --- HttpFetcher helpers -----------------------------------------------------


def _fetcher(
    tmp_path: Path,
    external_available: bool = True,
) -> HttpFetcher:
    """Build an HttpFetcher over a real tmp cache with a fake-clock limiter."""
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(max_requests_per_second=3.0, clock=clock, sleep=sleep)
    cache = ResponseCache(cache_dir=tmp_path)
    return HttpFetcher(
        cache=cache,
        rate_limiter=limiter,
        config=Config(),
        external_available=lambda: external_available,
    )


# --- HttpFetcher: live success (Req 11.x write-through) ----------------------


def test_live_success_is_available_not_from_cache_and_writes_cache(
    tmp_path: Path,
) -> None:
    """A live success returns the live envelope and persists it to cache."""
    fetcher = _fetcher(tmp_path)
    key = "ot|associations|ENSG00000146648|5"
    source_url = "https://api.platform.opentargets.org/api/v4/graphql"
    retrieved_at = "2026-10-24T15:21:34.685346Z"
    payload = {"data": {"target": {"id": "ENSG00000146648"}}}

    def do_request() -> "tuple[str, str, dict]":
        return source_url, retrieved_at, payload

    result = fetcher.fetch(key, source_url, do_request)

    assert result.available is True
    assert result.from_cache is False
    assert result.source_url == source_url
    assert result.retrieved_at == retrieved_at
    assert result.payload == payload

    # The envelope must have been written to the cache verbatim.
    stored = fetcher.cache.read(key)
    assert stored == {
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "payload": payload,
    }


# --- HttpFetcher: cache fallback on failure (Req 11.5, 11.7) -----------------


def test_failure_with_cache_present_falls_back_from_cache_with_original_provenance(
    tmp_path: Path,
) -> None:
    """A live failure with a cache present returns the cached envelope.

    The returned result carries ``from_cache=True`` and the *original*
    ``source_url`` / ``retrieved_at`` stored in the cache, not any live value
    (Req 11.5, 11.7). We populate the cache via one live success first, then a
    second fetch whose request raises.
    """
    fetcher = _fetcher(tmp_path)
    key = "pubmed|gene|EGFR"
    orig_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?x=1"
    orig_retrieved_at = "2026-10-24T15:00:00.000000Z"
    orig_payload = {"esearchresult": {"count": "42"}}

    def live_ok() -> "tuple[str, str, dict]":
        return orig_url, orig_retrieved_at, orig_payload

    first = fetcher.fetch(key, orig_url, live_ok)
    assert first.from_cache is False and first.available is True

    def live_fails() -> "tuple[str, str, dict]":
        raise RuntimeError("simulated network failure / timeout")

    fallback = fetcher.fetch(key, orig_url, live_fails)

    assert fallback.available is True
    assert fallback.from_cache is True
    # Original provenance preserved, not the moment of re-read (Req 11.7).
    assert fallback.source_url == orig_url
    assert fallback.retrieved_at == orig_retrieved_at
    assert fallback.payload == orig_payload


def test_pre_populated_cache_is_used_on_failure(tmp_path: Path) -> None:
    """Pre-seeding the cache directly also drives the fallback path (Req 11.5)."""
    cache = ResponseCache(cache_dir=tmp_path)
    key = "pubmed|comention|EGFR|lung-cancer"
    cache.write(
        key,
        source_url="https://eutils.ncbi.nlm.nih.gov/x?term=seeded",
        retrieved_at="2026-01-01T00:00:00.000000Z",
        payload={"esearchresult": {"count": "7"}},
    )
    fetcher = _fetcher(tmp_path)

    def live_fails() -> "tuple[str, str, dict]":
        raise TimeoutError("simulated timeout")

    result = fetcher.fetch(key, "https://eutils.ncbi.nlm.nih.gov/x", live_fails)

    assert result.available is True
    assert result.from_cache is True
    assert result.source_url == "https://eutils.ncbi.nlm.nih.gov/x?term=seeded"
    assert result.retrieved_at == "2026-01-01T00:00:00.000000Z"
    assert result.payload == {"esearchresult": {"count": "7"}}


# --- HttpFetcher: unavailable path (Req 11.6) --------------------------------


def test_failure_with_no_cache_is_unavailable_with_no_payload(
    tmp_path: Path,
) -> None:
    """A live failure with no cache yields unavailable and fabricates nothing."""
    fetcher = _fetcher(tmp_path)
    key = "pubmed|gene|NEVER_CACHED"

    def live_fails() -> "tuple[str, str, dict]":
        raise RuntimeError("simulated failure, nothing cached")

    result = fetcher.fetch(key, "https://eutils.ncbi.nlm.nih.gov/x", live_fails)

    assert result.available is False
    assert result.from_cache is False
    assert result.source_url is None
    assert result.retrieved_at is None
    assert result.payload is None
    assert result == FetchResult.unavailable()


# --- HttpFetcher: cache-first / offline (Req 11.4) ---------------------------


def test_cache_first_when_external_unavailable_does_not_call_do_request(
    tmp_path: Path,
) -> None:
    """With external access off and a cache present, the cache is served directly.

    ``do_request`` must never run in this path; we pass one that fails the test
    if it is called (Req 11.4). The result carries ``from_cache=True`` and the
    cached envelope's original provenance.
    """
    cache = ResponseCache(cache_dir=tmp_path)
    key = "ot|search|EGFR"
    cache.write(
        key,
        source_url="https://api.platform.opentargets.org/api/v4/graphql",
        retrieved_at="2026-02-02T02:02:02.000000Z",
        payload={"data": {"search": {"hits": [{"id": "ENSG00000146648"}]}}},
    )
    fetcher = _fetcher(tmp_path, external_available=False)

    def do_request_must_not_be_called() -> "tuple[str, str, dict]":
        raise AssertionError("do_request was called in cache-first/offline mode")

    result = fetcher.fetch(
        key,
        "https://api.platform.opentargets.org/api/v4/graphql",
        do_request_must_not_be_called,
    )

    assert result.available is True
    assert result.from_cache is True
    assert result.source_url == "https://api.platform.opentargets.org/api/v4/graphql"
    assert result.retrieved_at == "2026-02-02T02:02:02.000000Z"
    assert result.payload == {"data": {"search": {"hits": [{"id": "ENSG00000146648"}]}}}
