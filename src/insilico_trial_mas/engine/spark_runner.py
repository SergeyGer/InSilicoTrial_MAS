"""PySpark engine - horizontal scale on Databricks/AWS.

Two partitioning strategies, both driven by configuration:

``cohort``   ``df.groupBy("partition_key").applyInPandas(...)`` exactly as the
             specification prescribes, with ``cohorts_per_partition`` letting one
             task own several cohorts to amortise the per-task runtime setup.
``balanced`` ``df.repartition(n, "patient_id").mapInPandas(...)`` over Arrow
             batches sized by ``spark.sql.execution.arrow.maxRecordsPerBatch``.
             This removes the large-cohort straggler that punishes a naive
             ``groupBy`` on skewed cohort sizes.

Worker-Python pitfall handled here: a Spark worker uses ``PYSPARK_PYTHON``, not
the driver interpreter. If it points at a Python without pandas the job dies with
``ModuleNotFoundError: No module named 'pandas'`` deep inside a task. The engine
pins ``PYSPARK_PYTHON``/``PYSPARK_DRIVER_PYTHON`` to ``sys.executable`` (or the
Databricks-provided value) and *verifies* it on an existing session.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import pandas as pd

from ..errors import SimulationError
from ..logging_utils import get_logger
from ..schemas import SILVER_COLUMNS
from .base import BaseEngine, EngineResult
from .checklist import java_available
from .context import context_to_json
from .partition import make_cohort_batch_fn, make_map_batch_fn

logger = get_logger("engine.spark")

DTYPE_MAP = {
    "string": "StringType",
    "double": "DoubleType",
    "int": "IntegerType",
    "long": "LongType",
    "bool": "BooleanType",
}


def silver_struct_type():
    """Build the Spark ``StructType`` of the Silver table from the shared contract."""
    from pyspark.sql import types as T

    fields = [(name, getattr(T, DTYPE_MAP[dtype])()) for name, dtype in SILVER_COLUMNS]
    return T.StructType([T.StructField(name, dtype, nullable=True) for name, dtype in fields])


def cohort_struct_type():
    """Spark schema of the input cohort table."""
    from pyspark.sql import types as T

    from ..cohort.serialization import COHORT_COLUMNS

    fields = [T.StructField(name, getattr(T, DTYPE_MAP[dtype])(), nullable=True) for name, dtype in COHORT_COLUMNS]
    fields.append(T.StructField("partition_key", T.StringType(), nullable=True))
    return T.StructType(fields)


def ensure_worker_python(spark) -> dict[str, Any]:
    """Verify that executors use an interpreter able to import pandas."""
    info: dict[str, Any] = {"driver_python": sys.executable}
    try:
        configured = spark.conf.get("spark.pyspark.python", "")
    except Exception:
        configured = ""
    info["configured_python"] = configured
    if configured and os.path.realpath(configured) != os.path.realpath(sys.executable):
        logger.warning(
            f"Spark executors will use PYSPARK_PYTHON={configured!r} while the driver runs {sys.executable!r}; "
            "ensure pandas/pyarrow/insilico-trial-mas are installed in that interpreter "
            "(pip install -e . or %pip install in the notebook)"
        )
        info["mismatch"] = True
    else:
        info["mismatch"] = False
    return info


def detect_databricks_connect_shadowing() -> str:
    """Return a diagnosis when ``databricks-connect`` replaced local PySpark.

    ``pip install databricks-connect`` installs a patched PySpark that only talks
    to a remote cluster. Local runs then fail with "Only remote Spark sessions
    using Databricks Connect are supported", which says nothing about the cause.
    Detecting it turns a confusing failure into an actionable message.
    """
    try:
        import databricks.connect  # noqa: F401 - availability probe
    except ImportError:
        return ""
    remote_configured = bool(
        os.environ.get("DATABRICKS_HOST")
        and (os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_CLIENT_ID"))
    )
    if remote_configured:
        return ""
    return (
        "the 'databricks-connect' package is installed and replaces local PySpark with a remote-only "
        "client. Either uninstall it (pip uninstall databricks-connect && pip install pyspark==3.5.3) "
        "or configure a remote session (DATABRICKS_HOST + DATABRICKS_TOKEN) and point the Spark engine "
        "at that cluster."
    )


def get_spark_session(config=None, *, app_name: str = "InSilicoTrialMAS"):
    """Return an active session or create a local one (driver as executor)."""
    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover - guarded by the factory
        raise SimulationError(
            "pyspark is not installed; install it with: pip install 'insilico-trial-mas[spark]'"
        ) from exc

    shadowing = detect_databricks_connect_shadowing()
    if shadowing and not os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        logger.error(shadowing)
        raise SimulationError(shadowing)

    java_ok, java_path = java_available()
    if java_ok and not os.environ.get("JAVA_HOME"):
        os.environ["JAVA_HOME"] = os.path.dirname(os.path.dirname(java_path))
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    active = SparkSession.getActiveSession()
    if active is not None:
        ensure_worker_python(active)
        return active

    builder = SparkSession.builder.appName(app_name)
    if not os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        # Local / CI: use the driver node as the executor (Databricks CE pattern).
        builder = builder.master("local[*]")
    builder = (
        builder.config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .config("spark.sql.execution.arrow.pyspark.fallback.enabled", "true")
        .config("spark.pyspark.python", sys.executable)
        .config("spark.pyspark.driver.python", sys.executable)
    )
    if config is not None:
        engine = config.engine
        if engine.spark_shuffle_partitions:
            builder = builder.config("spark.sql.shuffle.partitions", str(engine.spark_shuffle_partitions))
        if engine.spark_max_records_per_batch:
            builder = builder.config(
                "spark.sql.execution.arrow.maxRecordsPerBatch", str(engine.spark_max_records_per_batch)
            )
        if engine.spark_local_dir:
            builder = builder.config("spark.local.dir", engine.spark_local_dir)
        if config.storage.backend == "delta":
            builder = _configure_delta(builder, config)

    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel(os.environ.get("INSILICO_SPARK_LOG_LEVEL", "WARN"))
    ensure_worker_python(spark)
    return spark


def _configure_delta(builder, config):
    """Attach the Delta Lake extensions when delta-spark is installed."""
    try:
        from delta import configure_spark_with_delta_pip

        logger.info("Delta Lake extensions enabled")
        return configure_spark_with_delta_pip(builder)
    except ImportError:
        logger.warning(
            "storage.backend=delta but delta-spark is not installed; "
            "the Spark session will start without Delta extensions"
        )
        return builder


class SparkEngine(BaseEngine):
    """Distributed engine built on ``applyInPandas`` / ``mapInPandas``."""

    backend = "spark"

    def __init__(
        self,
        context,
        *,
        spark=None,
        force_offline: bool = False,
    ) -> None:
        super().__init__(context)
        self._spark = spark
        self.force_offline = force_offline

    @property
    def spark(self):
        if self._spark is None:
            self._spark = get_spark_session(self.context.config)
        return self._spark

    # -- cohort frame -> Spark --------------------------------------------
    def _to_spark(self, cohort_frame: pd.DataFrame):
        from ..cohort.serialization import COHORT_COLUMN_NAMES

        frame = cohort_frame.copy()
        frame["partition_key"] = self._partition_keys(frame)
        for column in COHORT_COLUMN_NAMES:
            if column not in frame.columns:
                frame[column] = None
        pdf = frame[[*COHORT_COLUMN_NAMES, "partition_key"]]
        return self.spark.createDataFrame(pdf, schema=cohort_struct_type())

    def _partition_keys(self, frame: pd.DataFrame) -> list[str]:
        """Group cohorts into ``cohorts_per_partition`` buckets per Spark task."""
        group_size = max(1, self.context.config.engine.cohorts_per_partition)
        cohort_ids = sorted(frame["cohort_id"].astype(str).unique().tolist())
        bucket_of = {cohort_id: index // group_size for index, cohort_id in enumerate(cohort_ids)}
        return [f"P{bucket_of.get(str(cohort_id), 0):05d}" for cohort_id in frame["cohort_id"].astype(str)]

    # -- main entry point --------------------------------------------------
    def run(self, cohort_frame: pd.DataFrame, *, run_id: str | None = None) -> EngineResult:
        started = time.perf_counter()
        context = self.context
        config = context.config
        spark = self.spark
        logger.info(f"Spark {spark.version} master={spark.sparkContext.master} (app id {spark.sparkContext.applicationId})")

        context_json = context_to_json(context)
        spark_frame = self._to_spark(cohort_frame)
        schema = silver_struct_type()

        if config.engine.partition_mode == "cohort":
            n_partitions = max(1, spark_frame.select("partition_key").distinct().count())
            # A local/CI Spark session defaults to 200 shuffle partitions, which
            # would schedule 200 mostly-empty tasks for a handful of cohorts.
            if not config.engine.spark_shuffle_partitions:
                spark.conf.set("spark.sql.shuffle.partitions", str(max(4, min(200, n_partitions * 4))))
            logger.info(f"applyInPandas over {n_partitions} cohort partitions")
            result = spark_frame.groupBy("partition_key").applyInPandas(
                make_cohort_batch_fn(context_json, force_offline=self.force_offline), schema=schema
            )
        else:
            n_partitions = self._balanced_partitions(len(cohort_frame), config)
            logger.info(f"mapInPandas over {n_partitions} balanced partitions")
            result = spark_frame.repartition(n_partitions, "patient_id").mapInPandas(
                make_map_batch_fn(context_json, force_offline=self.force_offline), schema=schema
            )

        # Cache: the Silver write, the row count and the Gold aggregations all
        # reuse the same lineage; without this the agents would run three times.
        result = result.cache()
        duration = time.perf_counter() - started
        return EngineResult(
            backend=self.backend,
            n_patients=len(cohort_frame),
            n_rows=len(cohort_frame) * max(1, context.total_epochs),
            rows=None,
            dataframe=result,
            stats={
                "master": spark.sparkContext.master,
                "spark_version": spark.version,
                "partitions": n_partitions,
                "partition_mode": config.engine.partition_mode,
                "planning_seconds": round(duration, 3),
            },
            runtime_info={"spark_version": spark.version, "master": spark.sparkContext.master},
            duration_seconds=duration,
        )

    def _balanced_partitions(self, n_patients: int, config) -> int:
        if config.engine.spark_shuffle_partitions:
            return max(1, config.engine.spark_shuffle_partitions)
        default_parallelism = 0
        try:
            default_parallelism = int(self.spark.sparkContext.defaultParallelism)
        except Exception:
            default_parallelism = os.cpu_count() or 1
        # Aim for ~4 batches per core so that a slow batch cannot stall the stage.
        target = max(1, n_patients // max(1, config.engine.batch_size))
        return max(1, min(max(default_parallelism * 4, 1), max(target, 1)))
