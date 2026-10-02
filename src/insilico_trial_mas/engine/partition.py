"""Partition-level simulation entry points shared by every engine.

This module is the *only* place where a Spark executor, a multiprocessing worker
and the sequential engine differ in how they enter the agent code: all three call
:func:`simulate_batch`. Keeping that boundary thin is what makes the
cross-engine determinism test meaningful.

Spark contract notes
--------------------
* ``mapInPandas`` functions must be importable and must not close over live
  objects - hence the ``context_json`` string parameter (a JSON literal, not a
  pickled driver object).
* The output frame is built in the exact ``SILVER_COLUMNS`` order with the exact
  declared dtypes, otherwise Spark fails at schema-merge time on the very first
  large run.
* Empty partitions must still return an empty frame *with the right columns*:
  Spark raises ``RuntimeError: Result vector from pandas_udf was not the
  required length`` otherwise.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..agents.base import AgentContext
from ..agents.patient_agent import PatientPersonaAgent, PatientRunOutcome
from ..async_utils import gather_bounded, run_sync
from ..cohort.serialization import arm_map_from_frame, frame_to_profiles
from ..errors import SimulationError
from ..logging_utils import get_logger
from ..schemas import SILVER_COLUMN_NAMES, SILVER_COLUMNS
from .context import context_from_json
from .runtime import WorkerRuntime, get_runtime

logger = get_logger("engine.partition")

#: dtypes used when the partition function builds its output frame.
PANDAS_DTYPES: dict[str, str] = {
    "string": "object",
    "double": "float64",
    "int": "int64",
    "long": "int64",
    "bool": "bool",
}


#: Silver columns that carry wall-clock *measurements* rather than simulated
#: state (when the run happened, how long a provider call took). Two runs of the
#: same seed differ only in these, so cross-engine and reproducibility
#: comparisons must exclude them - everything else must match exactly.
NON_DETERMINISTIC_COLUMNS: tuple[str, ...] = ("observation_ts", "llm_latency_ms")


def comparable_rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Order-independent, metadata-free projection of Silver rows.

    Used by the cross-engine determinism tests (and available to users who want to
    prove that a re-run reproduced a previous trial).
    """
    ignored = set(NON_DETERMINISTIC_COLUMNS)
    return sorted(
        tuple(sorted((key, value) for key, value in row.items() if key not in ignored)) for row in rows
    )


@dataclass(slots=True)
class BatchOutcome:
    """Result of simulating one batch of patients."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    patients: int = 0
    adverse_events: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    measurements: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0

    def as_frame(self) -> pd.DataFrame:
        return rows_to_frame(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "patients": self.patients,
            "rows": len(self.rows),
            "adverse_events": self.adverse_events,
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
            "failures": len(self.failures),
            "duration_seconds": round(self.duration_seconds, 3),
        }


def empty_frame() -> pd.DataFrame:
    """Empty Silver frame with the canonical schema (Spark-safe)."""
    return pd.DataFrame({name: pd.Series(dtype=PANDAS_DTYPES[dtype]) for name, dtype in SILVER_COLUMNS})


def rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a Silver frame from observation rows, enforcing column order/types."""
    if not rows:
        return empty_frame()
    frame = pd.DataFrame(rows, columns=list(SILVER_COLUMN_NAMES))
    for name, dtype in SILVER_COLUMNS:
        target = PANDAS_DTYPES[dtype]
        if target == "object":
            frame[name] = frame[name].astype(object).where(frame[name].notna(), "")
        else:
            frame[name] = frame[name].astype(target)
    return frame


async def simulate_batch_async(
    profiles: list[Any],
    context: AgentContext,
    runtime: WorkerRuntime,
    *,
    concurrency: int | None = None,
    fail_fast: bool | None = None,
) -> BatchOutcome:
    """Simulate a list of patients concurrently inside one worker process."""
    started = time.perf_counter()
    outcome = BatchOutcome(patients=len(profiles))
    fail_fast = context.config.engine.fail_fast if fail_fast is None else fail_fast
    limit = concurrency or max(1, context.config.llm.max_concurrency)

    async def _run_one(profile: Any) -> PatientRunOutcome:
        agent = PatientPersonaAgent(profile, context, runtime)
        return await agent.run()

    results = await gather_bounded(
        (_run_one(profile) for profile in profiles),
        limit=max(1, limit),
        return_exceptions=not fail_fast,
    )
    for profile, result in zip(profiles, results, strict=True):
        if isinstance(result, BaseException):
            if fail_fast:
                raise SimulationError(f"patient {profile.patient_id} failed: {result}") from result
            outcome.failures.append({"patient_id": profile.patient_id, "error": f"{type(result).__name__}: {result}"})
            continue
        outcome.rows.extend(result.rows())
        outcome.adverse_events += len(result.adverse_events)
        outcome.llm_calls += result.llm_calls
        outcome.llm_errors += result.llm_errors
    outcome.duration_seconds = time.perf_counter() - started
    return outcome


def simulate_batch(
    profiles: list[Any],
    context: AgentContext,
    runtime: WorkerRuntime | None = None,
    *,
    concurrency: int | None = None,
) -> BatchOutcome:
    """Synchronous wrapper around :func:`simulate_batch_async`."""
    runtime = runtime or get_runtime(context)
    return run_sync(simulate_batch_async(profiles, context, runtime, concurrency=concurrency))


# ---------------------------------------------------------------------------
# Spark entry points
# ---------------------------------------------------------------------------


def simulate_frame(
    pdf: pd.DataFrame,
    context_json: str,
    *,
    force_offline: bool = False,
    concurrency: int | None = None,
) -> pd.DataFrame:
    """Simulate every patient contained in ``pdf`` - the Spark partition body.

    Called inside ``mapInPandas`` / ``applyInPandas``. Must never raise for a
    recoverable per-patient problem, and must always return the Silver schema.
    """
    if pdf is None or pdf.empty:
        return empty_frame()
    context = context_from_json(context_json)
    runtime = get_runtime(context, force_offline=force_offline)
    # The allocation travels with the cohort frame; merge it into the context so
    # that a worker can serve a cohort whose arm map it has never seen.
    for patient_id, arm_id in arm_map_from_frame(pdf).items():
        context.arm_by_patient.setdefault(patient_id, arm_id)

    profiles = frame_to_profiles(pdf)
    if not profiles:
        return empty_frame()
    outcome = run_sync(simulate_batch_async(profiles, context, runtime, concurrency=concurrency))
    return rows_to_frame(outcome.rows)


def make_map_batch_fn(context_json: str, *, force_offline: bool = False):
    """Build a picklable ``mapInPandas`` function (balanced batch mode)."""

    def _map_batch(iterator: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
        for pdf in iterator:
            yield simulate_frame(pdf, context_json, force_offline=force_offline)

    return _map_batch


def make_cohort_batch_fn(context_json: str, *, force_offline: bool = False):
    """Build a picklable ``applyInPandas`` function (one or more cohorts per group)."""

    def _run_cohort(key: Any, pdf: pd.DataFrame) -> pd.DataFrame:
        del key  # the group key is already a column of the frame
        return simulate_frame(pdf, context_json, force_offline=force_offline)

    return _run_cohort


def batch_stats_json(outcome: BatchOutcome) -> str:
    """Compact JSON summary - used to surface worker-side counters on the driver."""
    return json.dumps(outcome.as_dict(), sort_keys=True)
