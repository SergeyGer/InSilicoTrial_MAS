"""Provider factory: turn an :class:`~insilico_trial_mas.config.LLMConfig` into a client.

The factory always returns a :class:`ResilientLLMClient` wrapper (cache, rate
limit, retry, telemetry) and attaches the deterministic offline provider as a
fallback whenever a real provider is configured. That guarantees a long
simulation cannot be destroyed by a provider outage - degraded rows are flagged
with ``llm_error`` instead of aborting the run.
"""

from __future__ import annotations

from ..config import LLMConfig
from ..logging_utils import get_logger
from .base import BaseLLMClient
from .cache import LLMResponseCache
from .offline import EchoLLMClient, OfflineLLMClient
from .rate_limit import RetryPolicy
from .resilient import LLMCallStats, ResilientLLMClient

logger = get_logger("llm.factory")

OFFLINE_PROVIDERS = {"offline", "mock", "none"}


def create_llm_client(
    config: LLMConfig,
    *,
    stats: LLMCallStats | None = None,
    cache: LLMResponseCache | None = None,
    force_offline: bool = False,
) -> ResilientLLMClient:
    """Build the LLM client described by ``config``.

    Parameters
    ----------
    force_offline:
        Override the configured provider with the deterministic offline provider
        (used by tests and by the ``--offline`` CLI flag).
    """
    stats = stats if stats is not None else LLMCallStats()
    cache = cache if cache is not None else LLMResponseCache(config.cache_path, enabled=config.cache_enabled)

    inner: BaseLLMClient
    fallback: BaseLLMClient | None = None
    provider_name = "offline" if force_offline else config.provider

    if provider_name in OFFLINE_PROVIDERS:
        inner = OfflineLLMClient()
    elif provider_name == "echo":
        inner = EchoLLMClient()
    else:
        from .langchain_provider import LangChainChatClient

        inner = LangChainChatClient.from_config(config)
        # Offline fallback keeps a 10,000-agent run alive if Bedrock throttles hard.
        fallback = OfflineLLMClient()
        logger.info(f"LLM provider {config.provider!r} initialised with model {inner.model!r}")

    return ResilientLLMClient(
        inner,
        cache=cache,
        rate_per_second=config.requests_per_second,
        burst=config.burst,
        retry_policy=RetryPolicy(max_attempts=max(1, config.max_retries)),
        timeout_seconds=config.timeout_seconds,
        fallback=fallback,
        stats=stats,
        temperature=config.temperature,
        limiter_enabled=not inner.offline,
    )


def provider_is_offline(config: LLMConfig) -> bool:
    return config.provider in OFFLINE_PROVIDERS
