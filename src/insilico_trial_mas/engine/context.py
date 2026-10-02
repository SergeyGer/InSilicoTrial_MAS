"""Run context codec: how one simulation describes itself to a worker process.

A Spark executor must be able to rebuild the entire trial view from a plain,
JSON-serialisable payload. This module owns that contract; it is the reason the
same agent code runs unchanged on the driver, in a multiprocessing pool and on a
remote worker node.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any

from ..agents.base import AgentContext
from ..config import SimulationConfig, config_from_dict
from ..schemas import TrialProtocol

CONTEXT_SCHEMA_VERSION = "1.0.0"


def config_to_dict(config: SimulationConfig) -> dict[str, Any]:
    """Serialise the simulation configuration."""
    return dataclasses.asdict(config)


def context_to_payload(context: AgentContext) -> dict[str, Any]:
    """Serialise an :class:`AgentContext` into a JSON-safe mapping."""
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "run_id": context.run_id,
        "protocol": context.protocol.model_dump(mode="json"),
        "config": config_to_dict(context.config),
        "arm_by_patient": dict(context.arm_by_patient),
        "seed": context.seed,
        "model_path": context.model_path,
        "model_digest": context.model_digest,
        "model_version": context.model_version,
        "total_epochs": context.total_epochs,
        "created_at": context.created_at,
        "extra": context.extra,
    }


def context_to_json(context: AgentContext) -> str:
    """Compact JSON string handed to Spark executors (broadcast as a literal)."""
    return json.dumps(context_to_payload(context), separators=(",", ":"), default=str)


def context_from_json(payload: str | dict[str, Any]) -> AgentContext:
    """Rebuild an :class:`AgentContext` on a worker."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    version = data.get("schema_version", CONTEXT_SCHEMA_VERSION)
    if version != CONTEXT_SCHEMA_VERSION:
        raise ValueError(f"unsupported run-context schema version {version!r}")
    protocol = TrialProtocol.model_validate(data["protocol"])
    config = config_from_dict(data["config"])
    return AgentContext(
        run_id=data["run_id"],
        protocol=protocol,
        config=config,
        arm_by_patient={str(k): str(v) for k, v in (data.get("arm_by_patient") or {}).items()},
        seed=int(data.get("seed", config.seed)),
        model_path=str(data.get("model_path", "")),
        model_digest=str(data.get("model_digest", "")),
        model_version=str(data.get("model_version", "mechanistic-v1")),
        total_epochs=int(data.get("total_epochs", protocol.epochs)),
        created_at=str(data.get("created_at", "")),
        extra=dict(data.get("extra") or {}),
    )


@dataclass(slots=True)
class RunSpec:
    """Driver-side description of a run, before an :class:`AgentContext` exists."""

    run_id: str
    protocol: TrialProtocol
    config: SimulationConfig
    arm_by_patient: dict[str, str]
    seed: int
    total_epochs: int

    def to_context(self, *, model_version: str = "mechanistic-v1", model_path: str = "", model_digest: str = "") -> AgentContext:
        return AgentContext(
            run_id=self.run_id,
            protocol=self.protocol,
            config=self.config,
            arm_by_patient=dict(self.arm_by_patient),
            seed=self.seed,
            model_path=model_path,
            model_digest=model_digest,
            model_version=model_version,
            total_epochs=self.total_epochs,
        )
