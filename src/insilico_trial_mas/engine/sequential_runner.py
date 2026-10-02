"""Sequential engine - the reference implementation.

Used for tests, debugging and tiny cohorts. It processes patients one batch at a
time in the calling process, so a debugger breakpoint inside a Patient agent
behaves as expected. Because every stochastic draw is seeded from
``(seed, run, patient, epoch)``, its output is bit-identical to the local and
Spark engines.
"""

from __future__ import annotations

import time

import pandas as pd

from ..cohort.serialization import arm_map_from_frame, frame_to_profiles
from ..logging_utils import get_logger
from .base import BaseEngine, EngineResult
from .partition import simulate_batch
from .runtime import get_runtime

logger = get_logger("engine.sequential")


class SequentialEngine(BaseEngine):
    """Single-process, single-threaded execution."""

    backend = "sequential"

    def run(self, cohort_frame: pd.DataFrame, *, run_id: str | None = None) -> EngineResult:
        started = time.perf_counter()
        context = self.context
        for patient_id, arm_id in arm_map_from_frame(cohort_frame).items():
            context.arm_by_patient.setdefault(patient_id, arm_id)

        runtime = get_runtime(context)
        batch_size = max(1, context.config.engine.batch_size)
        profiles = frame_to_profiles(cohort_frame)
        rows: list[dict] = []
        llm_calls = 0
        llm_errors = 0
        adverse_events = 0
        failures: list[dict] = []

        for start in range(0, len(profiles), batch_size):
            batch = profiles[start : start + batch_size]
            outcome = simulate_batch(batch, context, runtime, concurrency=1)
            rows.extend(outcome.rows)
            llm_calls += outcome.llm_calls
            llm_errors += outcome.llm_errors
            adverse_events += outcome.adverse_events
            failures.extend(outcome.failures)

        rows.sort(key=_row_sort_key)
        duration = time.perf_counter() - started
        logger.info(
            f"sequential run finished: {len(profiles)} patients, {len(rows)} rows in {duration:.1f}s"
        )
        return EngineResult(
            backend=self.backend,
            n_patients=len(profiles),
            n_rows=len(rows),
            rows=rows,
            stats={
                "llm_calls": llm_calls,
                "llm_errors": llm_errors,
                "adverse_events": adverse_events,
                "failures": failures,
                "batches": (len(profiles) + batch_size - 1) // batch_size,
                "runtime": runtime.model_info,
            },
            runtime_info=runtime.model_info,
            duration_seconds=duration,
        )


def _row_sort_key(row: dict) -> tuple[str, str, int]:
    """Deterministic ordering shared by every engine (cohort, patient, epoch)."""
    return (str(row.get("cohort_id", "")), str(row.get("patient_id", "")), int(row.get("epoch", 0)))
