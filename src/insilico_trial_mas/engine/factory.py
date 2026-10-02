"""Engine selection.

``auto`` is the default and resolves in this order: Spark (if PySpark and a JVM
are present *and* either Databricks or a live Spark session is detected) ->
local multiprocessing -> sequential. When ``strict_engines`` is true, an
explicitly requested backend that cannot be honoured raises instead of silently
degrading, because a silent downgrade on a 50-million-row run is a very
expensive surprise.
"""

from __future__ import annotations

from ..agents.base import AgentContext
from ..errors import ConfigurationError, SimulationError
from ..logging_utils import get_logger
from .base import BaseEngine
from .checklist import inspect_environment
from .local_runner import LocalEngine
from .sequential_runner import SequentialEngine

logger = get_logger("engine.factory")

BACKENDS = ("auto", "sequential", "local", "spark")


def available_backends() -> dict[str, bool]:
    """Which backends can actually run in this interpreter."""
    report = inspect_environment()
    return {
        "sequential": True,
        "local": True,
        "spark": bool(report.pyspark_available and report.java_available),
    }


def resolve_backend(context: AgentContext) -> str:
    """Resolve ``auto`` into a concrete backend."""
    requested = context.config.engine.backend
    if requested not in BACKENDS:
        raise ConfigurationError(f"unknown engine backend {requested!r}; expected one of {BACKENDS}")
    if requested != "auto":
        return requested
    report = inspect_environment(context.config)
    return report.recommended_backend


def create_engine(context: AgentContext, *, force_offline: bool = False) -> BaseEngine:
    """Instantiate the engine selected by the configuration."""
    backend = resolve_backend(context)
    availability = available_backends()
    if backend == "spark" and not availability["spark"]:
        message = (
            "engine backend 'spark' was requested but PySpark or a JVM is unavailable "
            "(set JAVA_HOME or install the [spark] extra)"
        )
        if context.config.strict_engines:
            raise SimulationError(message)
        logger.warning(f"{message}; falling back to the local engine")
        backend = "local"

    if backend == "spark":
        from .spark_runner import SparkEngine

        return SparkEngine(context, force_offline=force_offline)
    if backend == "local":
        return LocalEngine(context, force_offline=force_offline)
    return SequentialEngine(context)


def engine_plan(context: AgentContext) -> dict[str, object]:
    """Explain the engine decision (surfaced in the run manifest and the CLI)."""
    report = inspect_environment(context.config)
    return {
        "requested": context.config.engine.backend,
        "resolved": resolve_backend(context),
        "available": available_backends(),
        "recommended": report.recommended_backend,
        "notes": report.notes,
    }
