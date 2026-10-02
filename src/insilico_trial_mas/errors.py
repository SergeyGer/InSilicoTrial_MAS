"""Exception hierarchy for InSilicoTrial MAS.

Keeping a dedicated hierarchy lets the agents degrade gracefully: a failure in an
optional subsystem (LLM provider, MLflow, Delta Lake) is caught at the boundary
where it is raised and converted into a structured, auditable record instead of
aborting a 10,000-agent simulation.
"""

from __future__ import annotations

from typing import Any


class InSilicoTrialError(Exception):
    """Base class for every error raised by the platform."""


class ConfigurationError(InSilicoTrialError):
    """Raised when configuration files or environment overrides are invalid."""


class ProtocolValidationError(InSilicoTrialError):
    """Raised when a trial protocol violates the design invariants."""


class CohortGenerationError(InSilicoTrialError):
    """Raised when a synthetic cohort cannot be generated."""


class SimulationError(InSilicoTrialError):
    """Raised when the simulation engine fails irrecoverably."""


class StorageError(InSilicoTrialError):
    """Raised when a storage backend (local versioned store, Delta Lake, UC) fails."""


class LLMProviderError(InSilicoTrialError):
    """Raised when an LLM provider call fails after all retries."""


class LLMResponseError(LLMProviderError):
    """Raised when an LLM answer cannot be parsed into the expected schema."""


class ModelRegistryError(InSilicoTrialError):
    """Raised when a physiology model artifact cannot be loaded or registered."""


class GovernanceError(InSilicoTrialError):
    """Raised on Unity Catalog namespace or privilege violations."""


class AgentError(InSilicoTrialError):
    """Raised by an agent when it cannot complete its responsibility.

    The payload is expected to be JSON-serialisable so that the error can be
    persisted next to the simulation logs for post-mortem analysis.
    """

    def __init__(self, message: str, *, agent: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.agent = agent
        self.context = context or {}

    def to_record(self) -> dict[str, Any]:
        return {"agent": self.agent, "error_type": type(self).__name__, "message": str(self), **self.context}
