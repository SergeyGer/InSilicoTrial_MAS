"""Cross-engine determinism: sequential, local multiprocessing and Spark.

This is the test that justifies the platform's core architectural claim - that
the *same* agent code produces the *same* trial on a laptop, in a process pool and
on a cluster. Every stochastic draw is seeded from
``(seed, run, patient, epoch)``, so partition layout must not change a single
number.
"""

from __future__ import annotations

import pytest

from insilico_trial_mas.engine.checklist import inspect_environment, java_available
from insilico_trial_mas.engine.factory import available_backends, create_engine, engine_plan, resolve_backend
from insilico_trial_mas.engine.local_runner import split_batches
from insilico_trial_mas.engine.partition import comparable_rows, rows_to_frame, simulate_frame
from insilico_trial_mas.engine.sequential_runner import SequentialEngine


def _rows_as_tuples(rows: list[dict]) -> list[tuple]:
    """Comparable projection: wall-clock metadata is intentionally excluded."""
    return comparable_rows(rows)


def test_sequential_matches_local_multiprocessing(context, planned) -> None:
    _, _, frame = planned
    sequential = SequentialEngine(context).run(frame)

    local_config = context.config
    local_config.engine.backend = "local"
    local_config.engine.max_workers = 2
    local_config.engine.batch_size = 32
    local = create_engine(context).run(frame)

    assert local.backend == "local"
    assert local.n_rows == sequential.n_rows
    assert _rows_as_tuples(local.rows) == _rows_as_tuples(sequential.rows), (
        "the local engine must reproduce the sequential engine bit-for-bit"
    )


def test_partition_function_matches_engine(context, planned) -> None:
    """The Spark entry point must produce the same rows as the driver engine."""
    from insilico_trial_mas.engine.context import context_to_json

    _, _, frame = planned
    sequential = SequentialEngine(context).run(frame)
    context_json = context_to_json(context)
    partitioned = simulate_frame(frame, context_json)
    assert len(partitioned) == len(sequential.rows)
    produced = sorted(
        (row["patient_id"], row["epoch"], round(float(row["sbp"]), 6))
        for row in partitioned.to_dict(orient="records")
    )
    expected = sorted((row["patient_id"], row["epoch"], round(float(row["sbp"]), 6)) for row in sequential.rows)
    assert produced == expected


def test_empty_partition_returns_the_silver_schema(context) -> None:
    from insilico_trial_mas.engine.context import context_to_json
    from insilico_trial_mas.schemas import SILVER_COLUMN_NAMES

    empty = simulate_frame(__import__("pandas").DataFrame(), context_to_json(context))
    assert list(empty.columns) == list(SILVER_COLUMN_NAMES)
    assert empty.empty


def test_rows_to_frame_enforces_types(context, planned) -> None:
    _, _, frame = planned
    result = SequentialEngine(context).run(frame)
    pdf = rows_to_frame(result.rows)
    assert str(pdf["epoch"].dtype) == "int64"
    assert str(pdf["sbp"].dtype) == "float64"
    assert str(pdf["responder"].dtype) == "bool"


def test_batch_splitting_modes(context, planned) -> None:
    _, _, frame = planned
    by_cohort = split_batches(frame, batch_size=20, partition_mode="cohort", cohorts_per_partition=1)
    balanced = split_batches(frame, batch_size=20, partition_mode="balanced", cohorts_per_partition=1)
    assert sum(len(batch) for batch in by_cohort) == len(frame)
    assert sum(len(batch) for batch in balanced) == len(frame)
    assert len(by_cohort) >= len(balanced)
    # Cohort mode never splits a cohort across batches when it fits in one.
    for batch in by_cohort:
        assert batch["cohort_id"].nunique() <= 1 or len(batch) == 20


def test_engine_factory_and_plan(context) -> None:
    context.config.engine.backend = "auto"
    plan = engine_plan(context)
    assert plan["requested"] == "auto"
    assert plan["resolved"] in {"sequential", "local", "spark"}
    assert set(available_backends()) == {"sequential", "local", "spark"}
    context.config.engine.backend = "sequential"
    assert resolve_backend(context) == "sequential"
    assert isinstance(create_engine(context), SequentialEngine)


def test_unknown_backend_is_rejected(context) -> None:
    context.config.engine.backend = "kubernetes"
    with pytest.raises(Exception, match="unknown engine backend"):
        resolve_backend(context)


def test_environment_report_is_serialisable() -> None:
    report = inspect_environment()
    payload = report.as_dict()
    assert payload["cpu_count"] >= 1
    assert isinstance(payload["recommended_backend"], str)
    assert "python_version" in payload
    assert report.render().startswith("InSilicoTrial MAS")


@pytest.mark.spark
@pytest.mark.slow
def test_spark_engine_matches_sequential(context, planned) -> None:
    """Distributed execution must agree with the reference implementation."""
    java_ok, _ = java_available()
    if not java_ok or not available_backends()["spark"]:
        pytest.skip("PySpark or a JVM is unavailable in this environment")

    _, _, frame = planned
    sequential = SequentialEngine(context).run(frame)

    context.config.engine.backend = "spark"
    context.config.engine.partition_mode = "cohort"
    spark_result = create_engine(context).run(frame)
    collected = spark_result.dataframe.toPandas()
    assert len(collected) == len(sequential.rows)

    expected = sorted((row["patient_id"], row["epoch"], round(float(row["sbp"]), 6)) for row in sequential.rows)
    produced = sorted(
        (row["patient_id"], row["epoch"], round(float(row["sbp"]), 6)) for row in collected.to_dict(orient="records")
    )
    assert produced == expected, "Spark must reproduce the sequential engine exactly"


@pytest.mark.spark
@pytest.mark.slow
def test_spark_balanced_partitioning_matches_cohort_partitioning(context, planned) -> None:
    java_ok, _ = java_available()
    if not java_ok or not available_backends()["spark"]:
        pytest.skip("PySpark or a JVM is unavailable in this environment")

    _, _, frame = planned
    from insilico_trial_mas.engine.spark_runner import SparkEngine

    context.config.engine.backend = "spark"
    context.config.engine.partition_mode = "cohort"
    cohort_rows = SparkEngine(context).run(frame).dataframe.toPandas()
    context.config.engine.partition_mode = "balanced"
    context.config.engine.spark_shuffle_partitions = 3
    balanced_rows = SparkEngine(context).run(frame).dataframe.toPandas()

    key = ["patient_id", "epoch"]
    left = cohort_rows.sort_values(key).reset_index(drop=True)
    right = balanced_rows.sort_values(key).reset_index(drop=True)
    assert list(left["patient_id"]) == list(right["patient_id"])
    assert left["sbp"].round(6).tolist() == right["sbp"].round(6).tolist()
