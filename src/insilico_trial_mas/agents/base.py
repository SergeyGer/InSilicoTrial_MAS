"""Agent base classes and the shared per-run context."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import SimulationConfig
from ..logging_utils import get_logger
from ..schemas import SimulationProvenance, TrialProtocol


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class AgentContext:
    """Immutable state shared by every agent of one simulation run.

    It travels to Spark executors as JSON (see
    :mod:`insilico_trial_mas.engine.context`) so that worker processes rebuild an
    identical view of the trial without shipping live objects across the wire.
    """

    run_id: str
    protocol: TrialProtocol
    config: SimulationConfig
    arm_by_patient: dict[str, str] = field(default_factory=dict)
    seed: int = 0
    model_path: str = ""
    model_digest: str = ""
    model_version: str = "mechanistic-v1"
    total_epochs: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def arm_of(self, patient_id: str) -> str:
        """Return the randomised arm id, defaulting to the first arm."""
        return self.arm_by_patient.get(patient_id, self.protocol.arms[0].arm_id)

    def provenance_template(self) -> SimulationProvenance:
        from ..version import __version__, git_revision

        return SimulationProvenance(
            sim_run_id=self.run_id,
            protocol_id=self.protocol.protocol_id,
            protocol_digest=self.protocol.digest(),
            protocol_version=self.protocol.version,
            package_version=__version__,
            git_revision=git_revision(),
            engine_backend=self.config.engine.backend,
            seed=self.seed,
            n_epochs=self.total_epochs,
            physiology_model_version=self.model_version,
            physiology_model_digest=self.model_digest,
            storage_backend=self.config.storage.backend,
            llm_provider=self.config.llm.provider,
            llm_model=self.config.llm.model,
            llm_temperature=self.config.llm.temperature,
            llm_mode=self.config.llm_mode,
            started_at=self.created_at,
        )


class BaseAgent:
    """Common behaviour for all agents (structured logging + role identity)."""

    role: str = "agent"

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self.log = get_logger(f"agents.{self.role}")

    @property
    def run_id(self) -> str:
        return self.context.run_id

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} run={self.run_id} role={self.role}>"
