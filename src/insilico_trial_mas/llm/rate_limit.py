"""Client-side rate limiting and retry policy for LLM calls.

The specification's diagram promised "Async LLM API Calls (Claude/Bedrock with
Rate Limiting)" but the blueprint contained no limiter at all - every partition
would hammer Bedrock until it started returning ``ThrottlingException``. This
module supplies:

* :class:`AsyncTokenBucket` - a refill-based requests-per-second limiter,
* :class:`RetryPolicy` - exponential backoff with full jitter and a retry budget,
* :func:`is_retryable` - a provider-agnostic classification of transient errors.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass


class AsyncTokenBucket:
    """Asyncio-friendly token bucket (requests per second with burst capacity)."""

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self.rate = float(rate_per_second)
        self.capacity = float(max(1, burst))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available; returns the waited seconds."""
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
            await asyncio.sleep(delay)
            waited += delay

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking variant used by adaptive controllers."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    multiplier: float = 2.0
    jitter: bool = True

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before retry ``attempt`` (1-based: 1 -> base delay)."""
        raw = self.base_delay_seconds * (self.multiplier ** max(0, attempt - 1))
        raw = min(raw, self.max_delay_seconds)
        if self.jitter:
            generator = rng or random
            return float(generator.uniform(0.0, raw))
        return float(raw)


RETRYABLE_MARKERS = (
    "throttl",
    "too many requests",
    "rate exceeded",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "serviceunavailable",
    "service unavailable",
    "internalservererror",
    "internal server error",
    "502",
    "503",
    "504",
    "429",
)

NON_RETRYABLE_MARKERS = (
    "validationexception",
    "accessdenied",
    "unauthorized",
    "invalid api key",
    "model not found",
    "does not exist",
    "throttlingexception: model",  # e.g. model-level hard denial
)


def is_retryable(exc: BaseException) -> bool:
    """Classify an exception as transient (retry) or permanent (fail fast)."""
    name = type(exc).__name__.lower()
    text = f"{name}: {exc}".lower()
    if any(marker in text for marker in NON_RETRYABLE_MARKERS):
        return False
    if any(marker in text for marker in RETRYABLE_MARKERS):
        return True
    # Unknown errors: one retry is cheap insurance, more is a waste of budget.
    return isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError))
