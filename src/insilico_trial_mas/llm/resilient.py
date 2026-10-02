"""Resilience decorator around any :class:`BaseLLMClient`.

Composes, in order: response cache -> token-bucket rate limit -> timeout ->
exponential-backoff retry -> optional deterministic fallback. It also maintains
the telemetry that ends up in MLflow Tracing and in the run manifest.

None of these concerns existed in the specification's blueprint, which called the
provider inline inside the row loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..errors import LLMProviderError
from ..logging_utils import get_logger
from .base import BaseLLMClient, LLMRequest, LLMResponse
from .cache import CacheEntry, LLMResponseCache, cache_key
from .rate_limit import AsyncTokenBucket, RetryPolicy, is_retryable

logger = get_logger("llm.resilient")


@dataclass
class LLMCallStats:
    """Thread-safe accumulator of LLM telemetry for one worker process."""

    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    retries: int = 0
    fallbacks: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_total: float = 0.0
    latency_ms_max: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, response: LLMResponse) -> None:
        with self._lock:
            self.calls += 1
            if response.cache_hit:
                self.cache_hits += 1
            self.tokens_in += response.tokens_in
            self.tokens_out += response.tokens_out
            self.latency_ms_total += response.latency_ms
            self.latency_ms_max = max(self.latency_ms_max, response.latency_ms)
            self.retries += max(0, response.attempts - 1)

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks += 1

    def merge(self, other: LLMCallStats) -> None:
        """Fold another worker's stats into this one (driver-side aggregation)."""
        with self._lock:
            self.calls += other.calls
            self.cache_hits += other.cache_hits
            self.failures += other.failures
            self.retries += other.retries
            self.fallbacks += other.fallbacks
            self.tokens_in += other.tokens_in
            self.tokens_out += other.tokens_out
            self.latency_ms_total += other.latency_ms_total
            self.latency_ms_max = max(self.latency_ms_max, other.latency_ms_max)

    @property
    def mean_latency_ms(self) -> float:
        return self.latency_ms_total / self.calls if self.calls else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "llm_calls": float(self.calls),
            "llm_cache_hits": float(self.cache_hits),
            "llm_failures": float(self.failures),
            "llm_retries": float(self.retries),
            "llm_fallbacks": float(self.fallbacks),
            "llm_tokens_in": float(self.tokens_in),
            "llm_tokens_out": float(self.tokens_out),
            "llm_mean_latency_ms": round(self.mean_latency_ms, 3),
            "llm_max_latency_ms": round(self.latency_ms_max, 3),
        }


class ResilientLLMClient(BaseLLMClient):
    """Cache + rate limit + timeout + retry + fallback around a provider."""

    def __init__(
        self,
        inner: BaseLLMClient,
        *,
        cache: LLMResponseCache | None = None,
        rate_per_second: float = 8.0,
        burst: int = 16,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        fallback: BaseLLMClient | None = None,
        stats: LLMCallStats | None = None,
        temperature: float = 0.2,
        limiter_enabled: bool = True,
    ) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.offline = inner.offline
        self.cache = cache
        self.bucket = AsyncTokenBucket(rate_per_second, burst)
        self.policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds
        self.fallback = fallback
        self.stats = stats or LLMCallStats()
        #: Local/offline providers have no API quota, so throttling them only adds
        #: artificial latency to a 10,000-agent run.
        self.limiter_enabled = bool(limiter_enabled and not inner.offline)
        #: Temperature participates in the cache key so that changing it invalidates
        #: cached answers instead of silently mixing generations.
        self.temperature = temperature

    # -- public API --------------------------------------------------------
    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        key = request.cache_key or cache_key(
            provider=self.provider,
            model=self.model,
            temperature=request.temperature if request.temperature is not None else self.temperature,
            prompt=request.prompt,
            system=request.system,
        )
        if self.cache is not None:
            entry = self.cache.get(key)
            if entry is not None:
                response = self._from_cache(entry)
                self.stats.record(response)
                return response

        last_error: BaseException | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.limiter_enabled:
                await self.bucket.acquire()
            try:
                response = await asyncio.wait_for(
                    self.inner.acomplete(request), timeout=request.timeout_seconds or self.timeout_seconds
                )
                response.attempts = attempt
                self._store(key, response)
                self.stats.record(response)
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= self.policy.max_attempts or not is_retryable(exc):
                    break
                delay = self.policy.delay_for(attempt)
                logger.warning(
                    f"LLM attempt {attempt}/{self.policy.max_attempts} failed ({type(exc).__name__}: {exc}); "
                    f"retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

        self.stats.record_failure()
        if self.fallback is not None:
            logger.warning(f"falling back to provider {self.fallback.provider!r}: {last_error}")
            self.stats.record_fallback()
            response = await self.fallback.acomplete(request)
            response.error = f"fallback after {type(last_error).__name__}: {last_error}"
            response.attempts = self.policy.max_attempts
            self.stats.record(response)
            return response
        raise LLMProviderError(f"LLM request failed via provider {self.provider!r}: {last_error}") from last_error

    async def aclose(self) -> None:
        await self.inner.aclose()
        if self.fallback is not None:
            await self.fallback.aclose()

    # -- internals ---------------------------------------------------------
    def _from_cache(self, entry: CacheEntry) -> LLMResponse:
        return LLMResponse(
            text=entry.text,
            provider=entry.provider or self.provider,
            model=entry.model or self.model,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            latency_ms=0.0,
            cache_hit=True,
            attempts=1,
        )

    def _store(self, key: str, response: LLMResponse) -> None:
        if self.cache is None or response.error:
            return
        self.cache.put(
            CacheEntry(
                key=key,
                text=response.text,
                provider=response.provider,
                model=response.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                metadata={"prompt_hash": response.raw.get("prompt_hash", "")},
            )
        )

    @property
    def bucket_stats(self) -> dict[str, float]:
        return {"rate_per_second": self.bucket.rate, "burst": self.bucket.capacity}


def build_stats() -> LLMCallStats:
    return LLMCallStats()


def timed_call(func):
    """Measure wall-clock latency of a synchronous call (test helper)."""

    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            wrapper.last_duration_ms = (time.perf_counter() - start) * 1000.0

    wrapper.last_duration_ms = 0.0
    return wrapper
