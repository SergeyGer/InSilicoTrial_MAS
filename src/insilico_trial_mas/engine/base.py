"""Engine interface and shared result types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..agents.base import AgentContext


@dataclass(slots=True)
class EngineResult:
    """Outcome of one simulation run, whatever the backend."""

    backend: str
    n_patients: int
    n_rows: int
    #: In-memory Silver rows (sequential/local). ``None`` for lazily evaluated Spark runs.
    rows: list[dict[str, Any]] | None = None
    #: Backend-native handle (a Spark DataFrame) when rows are not materialised.
    dataframe: Any | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    runtime_info: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def is_materialised(self) -> bool:
        return self.rows is not None


class BaseEngine(abc.ABC):
    """Common interface for every execution backend."""

    backend: str = "base"

    def __init__(self, context: AgentContext) -> None:
        self.context = context

    @abc.abstractmethod
    def run(self, cohort_frame, *, run_id: str | None = None) -> EngineResult:
        """Simulate the cohort and return the Silver observations."""

    def close(self) -> None:
        """Release engine resources (Spark sessions are left to the caller)."""
        return None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "epochs": self.context.total_epochs}
