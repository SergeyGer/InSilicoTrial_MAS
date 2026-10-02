"""Local multiprocessing engine - the Databricks Community Edition path.

The specification requires the project to stay fully reproducible for an
open-source visitor without a paid cluster. This engine splits the cohort into
batches, fans them out to a process pool and, inside every worker process, runs
the Patient agents on a single asyncio event loop with bounded LLM concurrency.

Two safeguards matter in shared environments (notebooks, CI containers):

* the pool uses the ``spawn`` start method, so forking a process that already
  holds threads (Spark, boto3, MLflow) cannot deadlock the child;
* if a process pool cannot be created at all, the engine degrades to sequential
  execution with an explicit warning instead of crashing the run.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..cohort.serialization import arm_map_from_frame, frame_to_profiles
from ..logging_utils import get_logger
from .base import BaseEngine, EngineResult
from .partition import simulate_batch
from .runtime import get_runtime

logger = get_logger("engine.local")


@dataclass(slots=True)
class _BatchTask:
    """Picklable unit of work handed to a worker process."""

    context_json: str
    frame: pd.DataFrame
    force_offline: bool = False
    concurrency: int = 32


def run_batch_task(task: _BatchTask) -> dict[str, Any]:
    """Worker entry point: simulate one batch and return rows plus counters.

    Must stay module-level: ``ProcessPoolExecutor`` pickles the callable by
    qualified name.
    """
    from .context import context_from_json

    started = time.perf_counter()
    context = context_from_json(task.context_json)
    runtime = get_runtime(context, force_offline=task.force_offline)
    for patient_id, arm_id in arm_map_from_frame(task.frame).items():
        context.arm_by_patient.setdefault(patient_id, arm_id)
    profiles = frame_to_profiles(task.frame)
    outcome = simulate_batch(profiles, context, runtime, concurrency=task.concurrency)
    return {
        "rows": outcome.rows,
        "patients": outcome.patients,
        "llm_calls": outcome.llm_calls,
        "llm_errors": outcome.llm_errors,
        "adverse_events": outcome.adverse_events,
        "failures": outcome.failures,
        "duration_seconds": time.perf_counter() - started,
    }


def split_batches(
    frame: pd.DataFrame,
    *,
    batch_size: int,
    partition_mode: str,
    cohorts_per_partition: int,
) -> list[pd.DataFrame]:
    """Split the cohort frame into worker batches.

    ``cohort`` mode keeps whole cohorts together (patients inside a cohort share
    site and enrolment wave, so this is the natural Spark partition key);
    ``balanced`` mode ignores cohort boundaries to avoid the classic
    one-giant-cohort straggler problem.
    """
    if frame.empty:
        return []
    if partition_mode == "balanced":
        return [frame.iloc[start : start + batch_size] for start in range(0, len(frame), batch_size)]

    batches: list[pd.DataFrame] = []
    cohort_ids = sorted(frame["cohort_id"].unique().tolist()) if "cohort_id" in frame.columns else ["COHORT-001"]
    group_size = max(1, cohorts_per_partition)
    for start in range(0, len(cohort_ids), group_size):
        group = set(cohort_ids[start : start + group_size])
        subset = frame[frame["cohort_id"].isin(group)]
        for chunk in range(0, len(subset), batch_size):
            batches.append(subset.iloc[chunk : chunk + batch_size])
    return batches


class LocalEngine(BaseEngine):
    """Process-pool engine with an asyncio event loop inside each worker."""

    backend = "local"

    def __init__(self, context, *, force_offline: bool = False) -> None:
        super().__init__(context)
        self.force_offline = force_offline
        self.fallback_reason = ""

    def run(self, cohort_frame: pd.DataFrame, *, run_id: str | None = None) -> EngineResult:
        from .context import context_to_json

        started = time.perf_counter()
        context = self.context
        for patient_id, arm_id in arm_map_from_frame(cohort_frame).items():
            context.arm_by_patient.setdefault(patient_id, arm_id)

        config = context.config
        batches = split_batches(
            cohort_frame,
            batch_size=max(1, config.engine.batch_size),
            partition_mode=config.engine.partition_mode,
            cohorts_per_partition=max(1, config.engine.cohorts_per_partition),
        )
        workers = config.engine.max_workers or max(1, (os.cpu_count() or 1))
        workers = max(1, min(workers, len(batches) or 1))
        tasks = [
            _BatchTask(
                context_json=context_to_json(context),
                frame=batch,
                force_offline=self.force_offline,
                concurrency=max(1, config.llm.max_concurrency),
            )
            for batch in batches
        ]

        rows: list[dict] = []
        llm_calls = 0
        llm_errors = 0
        adverse_events = 0
        failures: list[dict] = []

        if workers == 1 or len(tasks) <= 1:
            for task in tasks:
                payload = run_batch_task(task)
                rows.extend(payload["rows"])
                llm_calls += payload["llm_calls"]
                llm_errors += payload["llm_errors"]
                adverse_events += payload["adverse_events"]
                failures.extend(payload["failures"])
        else:
            try:
                import multiprocessing as mp

                ctx = mp.get_context("spawn")
                with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                    for payload in pool.map(run_batch_task, tasks):
                        rows.extend(payload["rows"])
                        llm_calls += payload["llm_calls"]
                        llm_errors += payload["llm_errors"]
                        adverse_events += payload["adverse_events"]
                        failures.extend(payload["failures"])
            except Exception as exc:
                self.fallback_reason = f"{type(exc).__name__}: {exc}"
                logger.warning(f"process pool unavailable ({exc}); falling back to in-process execution")
                for task in tasks:
                    payload = run_batch_task(task)
                    rows.extend(payload["rows"])
                    llm_calls += payload["llm_calls"]
                    llm_errors += payload["llm_errors"]
                    adverse_events += payload["adverse_events"]
                    failures.extend(payload["failures"])

        rows.sort(key=lambda row: (str(row.get("cohort_id", "")), str(row.get("patient_id", "")), int(row.get("epoch", 0))))
        duration = time.perf_counter() - started
        n_patients = len(cohort_frame)
        logger.info(
            f"local run finished: {n_patients} patients, {len(rows)} rows, {workers} workers, {duration:.1f}s"
        )
        return EngineResult(
            backend=self.backend,
            n_patients=n_patients,
            n_rows=len(rows),
            rows=rows,
            stats={
                "workers": workers,
                "batches": len(tasks),
                "llm_calls": llm_calls,
                "llm_errors": llm_errors,
                "adverse_events": adverse_events,
                "failures": failures,
                "fallback_reason": self.fallback_reason,
            },
            duration_seconds=duration,
        )


# Exposed for tests: the reference implementation used inside workers.
__all__ = ["LocalEngine", "run_batch_task", "split_batches"]
