"""HttpFetcher + RateLimiter (<=3 req/s), timeouts, cache-first glue (Req 3.9, 11.4-11.6).

This module holds the two pieces that sit between a client (``OTClient`` /
``PubMedClient``) and the network: a :class:`RateLimiter` that paces outbound
calls, and an :class:`HttpFetcher` that applies the cache-first / cache-fallback
policy from the design ("Cache-first / cache-fallback", Req 11.4-11.7, 4.4).

Neither class performs real HTTP itself. The actual request is injected per call
as ``do_request`` — a zero-argument callable that returns the three envelope
fields ``(source_url, retrieved_at, payload)`` or raises/times out. This keeps
the network at the edge: the clients build ``do_request`` with ``requests`` and
the configured ``http_timeout_s``; the tests (task 4.3) inject a fake that
returns canned data or raises, so no socket is ever opened under test.

The fetch outcome is a :class:`FetchResult` so a caller can tell the three
terminal states apart without inspecting a payload:

* a **live** success  — ``available=True``, ``from_cache=False``;
* a **cache** hit/fallback — ``available=True``, ``from_cache=True`` (the UI
  surfaces "came from cache", Req 11.5), carrying the envelope's *original*
  ``source_url`` / ``retrieved_at`` (Req 11.7);
* **unavailable** — ``available=False`` with no payload, when the live request
  failed/timed out and no cache exists (Req 11.6). The fetcher never fabricates
  a payload in this state (steering rule 3); the caller continues with the
  evidence it already gathered.

The evidence layer (task 5) wraps a successful/cached ``FetchResult`` into an
``EvidenceRecord``; this module deliberately stops at the envelope + flags.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .cache import ResponseCache
from .config import Config

# A per-call request function: it performs the actual HTTP and returns the three
# envelope fields, or raises (including on timeout). Injected so no real network
# is needed under test.
DoRequest = Callable[[], "tuple[str, str, Any]"]


class RateLimiter:
    """Pace outbound calls to at most ``max_requests_per_second`` (Req 3.9).

    Enforces a minimum spacing of ``1 / max_requests_per_second`` seconds between
    successive :meth:`acquire` calls (default 3.0 req/s -> 1/3 s). The timing
    source and the sleep are both injected so tests can drive virtual time and
    assert the spacing without sleeping for real:

    Parameters
    ----------
    max_requests_per_second:
        Upper bound on call rate; the min spacing is its reciprocal. Must be > 0.
    clock:
        Monotonic time source returning seconds as a float. Defaults to
        :func:`time.monotonic` (never goes backwards, unlike wall-clock time).
    sleep:
        Blocking sleep taking a duration in seconds. Defaults to
        :func:`time.sleep`. A test injects a fake that advances its own clock.
    """

    def __init__(
        self,
        max_requests_per_second: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be > 0")
        self._min_spacing_s = 1.0 / max_requests_per_second
        self._clock = clock
        self._sleep = sleep
        # Monotonic timestamp of the last acquire; None until the first call so
        # the first acquire never waits.
        self._last_acquire: Optional[float] = None

    @property
    def min_spacing_s(self) -> float:
        """The enforced minimum seconds between successive acquires (1/rate)."""
        return self._min_spacing_s

    def acquire(self) -> None:
        """Block until at least ``min_spacing_s`` has elapsed since the last call.

        The first call returns immediately. Thereafter, if less than the minimum
        spacing has elapsed since the previous acquire, it sleeps for exactly the
        remaining deficit. The "last acquire" marker is advanced by the full
        spacing from the previous marker (not merely to "now"), so a burst of
        back-to-back calls is paced to a steady ``min_spacing_s`` cadence rather
        than drifting.
        """
        now = self._clock()
        if self._last_acquire is None:
            self._last_acquire = now
            return

        earliest_next = self._last_acquire + self._min_spacing_s
        wait = earliest_next - now
        if wait > 0:
            self._sleep(wait)
            # Next slot is one spacing after the slot we just honored, keeping a
            # steady cadence under a burst.
            self._last_acquire = earliest_next
        else:
            # Enough real time already elapsed; the current moment is the slot.
            self._last_acquire = now


@dataclass(frozen=True)
class FetchResult:
    """The outcome of an :meth:`HttpFetcher.fetch`, with flags the caller reads.

    ``available`` distinguishes "we have an envelope" (live or cached) from
    "interface unavailable, no cache" (Req 11.6); ``from_cache`` distinguishes a
    cache hit/fallback from a live response (Req 11.5). When ``available`` is
    ``False`` the envelope fields are ``None`` — the fetcher never invents a
    payload (steering rule 3).

    For a cache result the ``source_url`` and ``retrieved_at`` are the *original*
    values stored in the envelope, not the moment of re-read (Req 11.7), so an
    ``EvidenceRecord`` built from this result carries the real response's
    provenance.
    """

    available: bool
    from_cache: bool
    source_url: Optional[str] = None
    retrieved_at: Optional[str] = None
    payload: Any = None

    @classmethod
    def unavailable(cls) -> "FetchResult":
        """A terminal 'interface unavailable, no cache' result (Req 11.6)."""
        return cls(available=False, from_cache=False)


class HttpFetcher:
    """Apply the cache-first / cache-fallback policy around an injected request.

    Collaborators are injected so the policy is testable with no real network or
    clock:

    Parameters
    ----------
    cache:
        The :class:`~claimledger.cache.ResponseCache` used to read a prior
        envelope and to persist a fresh one (Req 4.4). Defaults to a cache over
        the repository ``data/cache`` directory.
    rate_limiter:
        A :class:`RateLimiter` whose :meth:`~RateLimiter.acquire` is called
        immediately before each *live* request (Req 3.9). Defaults to one at the
        configured ``max_requests_per_second``.
    config:
        Supplies ``http_timeout_s`` and ``max_requests_per_second`` defaults.
    external_available:
        Predicate answering "is external access currently available?". In
        offline / Example_Gene mode this returns ``False`` so a present cache is
        served directly (Req 11.4). Defaults to always-available; a caller flips
        it for offline mode. Equivalent to an "offline flag" inverted.
    """

    def __init__(
        self,
        cache: Optional[ResponseCache] = None,
        rate_limiter: Optional[RateLimiter] = None,
        config: Optional[Config] = None,
        external_available: Callable[[], bool] = lambda: True,
    ) -> None:
        self.config = config if config is not None else Config()
        self.cache = cache if cache is not None else ResponseCache()
        self.rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else RateLimiter(max_requests_per_second=self.config.max_requests_per_second)
        )
        self._external_available = external_available

    @property
    def http_timeout_s(self) -> float:
        """The timeout (seconds) a ``do_request`` should apply to its HTTP call."""
        return self.config.http_timeout_s

    def fetch(self, request_key: str, url: str, do_request: DoRequest) -> FetchResult:
        """Fetch the response for ``request_key`` under the cache-first policy.

        The flow matches the design "Cache-first / cache-fallback" exactly:

        1. Compute the cache path from ``request_key`` (via :class:`ResponseCache`).
        2. **Cache-first (offline / Example_Gene):** if a cache file exists *and*
           external access is unavailable, return the cached envelope with
           ``from_cache=True`` using its original ``source_url`` / ``retrieved_at``
           (Req 11.4, 11.7).
        3. Otherwise attempt the live request (``do_request``) with rate limiting;
           on success persist the envelope to the cache (Req 4.4) and return it
           with ``from_cache=False``.
        4. **Fallback:** if the live request fails/times out and a cache file
           exists, return the cached envelope with ``from_cache=True`` (Req 11.5).
        5. If it fails/times out and no cache exists, return
           :meth:`FetchResult.unavailable` so the caller surfaces "interface
           unavailable" and continues with evidence already gathered (Req 11.6).

        ``do_request`` is expected to apply :attr:`http_timeout_s` to its HTTP
        call and to raise on failure/timeout; any exception it raises is caught
        here and routed to the fallback path.
        """
        cache_present = self.cache.exists(request_key)

        # Step 2 — cache-first for offline / Example_Gene mode.
        if cache_present and not self._external_available():
            cached = self._load_cache(request_key)
            if cached is not None:
                return cached

        # Step 3 — attempt the live request (rate-limited, timeout inside do_request).
        try:
            self.rate_limiter.acquire()
            source_url, retrieved_at, payload = do_request()
        except Exception:
            # Steps 4/5 — live failure/timeout: fall back to cache if present,
            # else signal unavailability. We never re-raise: the pipeline must be
            # able to continue with the evidence it already has (Req 11.6).
            if cache_present:
                cached = self._load_cache(request_key)
                if cached is not None:
                    return cached
            return FetchResult.unavailable()

        # Live success — persist the envelope (Req 4.4) and return it live.
        self.cache.write(
            request_key,
            source_url=source_url,
            retrieved_at=retrieved_at,
            payload=payload,
        )
        return FetchResult(
            available=True,
            from_cache=False,
            source_url=source_url,
            retrieved_at=retrieved_at,
            payload=payload,
        )

    def _load_cache(self, request_key: str) -> Optional[FetchResult]:
        """Read the cached envelope for ``request_key`` into a cache FetchResult.

        Returns ``None`` when the envelope is absent or malformed (missing a
        required key), so the caller can proceed as if no usable cache existed
        rather than fabricating fields (steering rule 3). A cache result carries
        the envelope's original ``source_url`` / ``retrieved_at`` (Req 11.7).
        """
        envelope = self.cache.read(request_key)
        if not envelope:
            return None
        try:
            source_url = envelope["source_url"]
            retrieved_at = envelope["retrieved_at"]
            payload = envelope["payload"]
        except (KeyError, TypeError):
            return None
        return FetchResult(
            available=True,
            from_cache=True,
            source_url=source_url,
            retrieved_at=retrieved_at,
            payload=payload,
        )
