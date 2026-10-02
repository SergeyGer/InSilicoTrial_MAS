"""Execution engines: sequential, local multiprocessing and PySpark."""

from .base import BaseEngine, EngineResult
from .checklist import EnvironmentReport, inspect_environment
from .factory import available_backends, create_engine, engine_plan, resolve_backend
from .local_runner import LocalEngine
from .sequential_runner import SequentialEngine

__all__ = [
    "BaseEngine",
    "EngineResult",
    "EnvironmentReport",
    "LocalEngine",
    "SequentialEngine",
    "available_backends",
    "create_engine",
    "engine_plan",
    "inspect_environment",
    "resolve_backend",
]


def __getattr__(name: str):  # pragma: no cover - keeps pyspark an optional dependency
    if name == "SparkEngine":
        from .spark_runner import SparkEngine

        return SparkEngine
    raise AttributeError(name)
