"""Per-worker runtime: model + LLM client initialised once per executor process.

The specification's diagram says "LangChain Initialization (Per Worker Thread)".
Initialising a provider client *per row* (as in the blueprint) would create and
destroy thousands of HTTP clients per partition; initialising it per *process*
and reusing it across batches is the correct granularity. :func:`get_runtime`
implements that cache with a lock, and :func:`reset_runtimes` exists for tests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..agents.base import AgentContext
from ..agents.protocol_agent import ProtocolAgent
from ..config import SimulationConfig
from ..llm.base import BaseLLMClient
from ..llm.cache import LLMResponseCache
from ..llm.factory import create_llm_client
from ..llm.resilient import LLMCallStats
from ..logging_utils import get_logger
from ..ml.registry import LoadedModel, load_physiology_model
from ..reproducibility import stable_hash

logger = get_logger("engine.runtime")


@dataclass
class WorkerRuntime:
    """Everything a Patient Persona agent needs inside one worker process."""

    context: AgentContext
    model: Any
    llm: BaseLLMClient
    stats: LLMCallStats = field(default_factory=LLMCallStats)
    protocol_agent: ProtocolAgent | None = None
    model_info: dict[str, Any] = field(default_factory=dict)
    llm_available: bool = True
    trace: Any | None = None

    @classmethod
    def create(
        cls,
        context: AgentContext,
        *,
        force_offline: bool = False,
        llm_client: BaseLLMClient | None = None,
    ) -> WorkerRuntime:
        """Build a runtime for ``context`` (model + LLM + protocol agent + tracer)."""
        loaded: LoadedModel = load_physiology_model(context.protocol, context.config.ml)
        stats = LLMCallStats()
        if llm_client is None:
            llm_client = create_llm_client(
                context.config.llm,
                stats=stats,
                cache=LLMResponseCache(
                    context.config.llm.cache_path,
                    enabled=context.config.llm.cache_enabled and context.config.llm_mode != "off",
                ),
                force_offline=force_offline,
            )
        runtime = cls(
            context=context,
            model=loaded.model,
            llm=llm_client,
            stats=stats,
            protocol_agent=ProtocolAgent(context),
            model_info=loaded.as_dict(),
            trace=_build_trace_recorder(context),
        )
        logger.info(
            "worker runtime initialised",
            extra={
                "extra_fields": {
                    "run_id": context.run_id,
                    "model_backend": loaded.backend,
                    "model_source": loaded.source,
                    "llm_provider": getattr(llm_client, "provider", "unknown"),
                    "llm_model": getattr(llm_client, "model", ""),
                }
            },
        )
        return runtime

    async def aclose(self) -> None:
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()

    @property
    def cache_key(self) -> str:
        """Digest identifying this runtime configuration (used by the cache)."""
        return stable_hash(
            self.context.run_id,
            self.model_info.get("version", ""),
            getattr(self.llm, "provider", ""),
            getattr(self.llm, "model", ""),
            length=16,
        )


_RUNTIME_CACHE: dict[str, WorkerRuntime] = {}
_RUNTIME_LOCK = threading.Lock()


def runtime_cache_key(context: AgentContext, force_offline: bool = False) -> str:
    """Stable cache key for a runtime (same run + model + provider configuration)."""
    return stable_hash(
        context.run_id,
        context.model_path,
        context.model_version,
        context.config.llm.provider,
        context.config.llm.model,
        context.config.llm.temperature,
        force_offline,
        length=20,
    )


def get_runtime(context: AgentContext, *, force_offline: bool = False) -> WorkerRuntime:
    """Return the cached runtime for ``context``, creating it on first use."""
    key = runtime_cache_key(context, force_offline)
    runtime = _RUNTIME_CACHE.get(key)
    if runtime is not None:
        return runtime
    with _RUNTIME_LOCK:
        runtime = _RUNTIME_CACHE.get(key)
        if runtime is None:
            runtime = WorkerRuntime.create(context, force_offline=force_offline)
            _RUNTIME_CACHE[key] = runtime
    return runtime


def reset_runtimes() -> None:
    """Drop cached runtimes (tests, and long-lived notebook sessions)."""
    with _RUNTIME_LOCK:
        _RUNTIME_CACHE.clear()


def runtime_stats() -> dict[str, Any]:
    """Aggregate telemetry across every cached runtime in this process."""
    with _RUNTIME_LOCK:
        runtimes = list(_RUNTIME_CACHE.values())
    merged = LLMCallStats()
    for runtime in runtimes:
        merged.merge(runtime.stats)
    return {"runtimes": len(runtimes), **merged.as_dict()}


def all_runtimes() -> list[WorkerRuntime]:
    with _RUNTIME_LOCK:
        return list(_RUNTIME_CACHE.values())


def simulation_config_of(context: AgentContext) -> SimulationConfig:
    return context.config


def _build_trace_recorder(context: AgentContext) -> Any | None:
    """Create the LLM trace recorder when tracking is enabled.

    On a cluster the path must be shared (a DBFS path or Unity Catalog volume);
    otherwise each executor writes to its own local disk and only the driver's
    spans end up in the report. That limitation is documented in
    ``docs/RUNBOOK_DATABRICKS_AWS.md``.
    """
    config = context.config.tracking
    if not config.enabled or not config.trace_path:
        return None
    try:
        from ..mlflow_tracking.tracing import TraceRecorder

        return TraceRecorder(config.trace_path, run_id=context.run_id, enabled=True)
    except Exception as exc:
        logger.warning(f"LLM tracing disabled: {exc}")
        return None
