"""LLM provider abstraction.

Every Synthetic Patient Persona agent talks to this interface only, so the
platform can run with a real Bedrock/OpenAI model in production and with a
deterministic offline provider in CI, on a laptop, or inside a Databricks
Community Edition notebook without credentials.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMRequest:
    """One chat completion request."""

    prompt: str
    system: str = ""
    #: Structured context (never sent to the provider by the offline client only;
    #: providers that support tool/JSON mode may use it).
    context: dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.2
    max_tokens: int = 512
    #: Stable key for the response cache; defaults to a hash of prompt+system.
    cache_key: str = ""
    #: ``None`` means "use the client's configured timeout".
    timeout_seconds: float | None = None


@dataclass(slots=True)
class LLMResponse:
    """One chat completion response plus telemetry."""

    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    cache_hit: bool = False
    attempts: int = 1
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BaseLLMClient(abc.ABC):
    """Interface implemented by all providers."""

    provider: str = "base"
    model: str = "unknown"
    #: Providers that are deterministic and free (used to pick defaults in tests).
    offline: bool = False

    @abc.abstractmethod
    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        """Execute one completion. Must raise on unrecoverable failure."""

    async def aclose(self) -> None:
        """Release provider resources (HTTP sessions, clients)."""
        return None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Provider-agnostic token estimate (~4 characters per token)."""
        return max(1, len(text) // 4)

    def _timed(self, start: float) -> float:
        return (time.perf_counter() - start) * 1000.0

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} provider={self.provider} model={self.model}>"
