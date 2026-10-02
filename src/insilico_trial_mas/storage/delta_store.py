"""Delta Lake store (Databricks / AWS).

Writes the Silver layer as a real Delta table so that the notebook's
``SELECT * FROM ... VERSION AS OF n`` cell works exactly as specified, including
``DESCRIBE HISTORY``, ``OPTIMIZE``/``ZORDER`` and Unity Catalog lineage.

Two naming modes are supported:

* **Unity Catalog** (default): ``catalog.schema.table`` three-part names, which is
  what the Terraform module provisions and what gives automatic lineage;
* **path/URI mode**: when ``storage.root_uri`` is set (``s3://bucket/...``,
  ``/Volumes/catalog/schema/volume/...``, ``dbfs:/...``), tables are written to
  that root - useful for external locations and for tests against local Delta.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from ..errors import SimulationError, StorageError
from ..logging_utils import get_logger
from .base import BaseStore, WriteResult, as_pandas, sanitise_for_parquet

logger = get_logger("storage.delta")


class DeltaStore(BaseStore):
    """Delta Lake backend driven by a Spark session."""

    backend = "delta"

    def __init__(self, config, *, spark=None) -> None:
        super().__init__(config)
        self._spark = spark

    @property
    def spark(self):
        if self._spark is None:
            try:
                from pyspark.sql import SparkSession
            except ImportError as exc:
                raise SimulationError(
                    "storage.backend=delta requires pyspark; install 'insilico-trial-mas[spark]' "
                    "or use storage.backend=local"
                ) from exc
            self._spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
        return self._spark

    # -- naming ------------------------------------------------------------
    def table_location(self, table: str) -> str:
        schema, _, name = table.partition("/")
        name = name or schema
        if self.config.root_uri:
            return f"{self.config.root_uri.rstrip('/')}/{self.config.schema_for(schema)}/{name}"
        return f"{self.config.catalog}.{self.config.schema_for(schema)}.{name}"

    def _is_path(self) -> bool:
        return bool(self.config.root_uri)

    def exists(self, table: str) -> bool:
        try:
            if self._is_path():
                return self.spark._jsparkSession.catalog().tableExists(self.table_location(table))
            return self.spark.catalog.tableExists(self.table_location(table))
        except Exception as exc:
            logger.debug(f"tableExists({table}) raised {type(exc).__name__}: {exc}")
            return False

    # -- writes ------------------------------------------------------------
    def write(
        self,
        table: str,
        frame: Any,
        *,
        run_id: str = "",
        mode: str = "append",
        partition_by: list[str] | None = None,
    ) -> WriteResult:
        started = time.perf_counter()
        location = self.table_location(table)
        spark_frame = self._to_spark(frame)
        partition_columns = [c for c in (partition_by or self.config.delta_partition_by) if c in spark_frame.columns]
        writer = spark_frame.write.format("delta").mode(mode)
        if partition_columns:
            writer = writer.partitionBy(*partition_columns)
        try:
            if self._is_path():
                writer = writer.option("path", location)
                if self.spark.catalog.tableExists(location):
                    writer = writer.insertInto(location)
                else:
                    writer.saveAsTable(location)
            else:
                writer.saveAsTable(location)
        except Exception as exc:
            raise StorageError(f"Delta write to {location} failed: {exc}") from exc

        rows = int(spark_frame.count())
        duration = time.perf_counter() - started
        logger.info(f"wrote {rows} rows to Delta table {location} in {duration:.2f}s")
        return WriteResult(
            table=table,
            location=location,
            rows=rows,
            version=self.latest_version(table),
            backend=self.backend,
            duration_seconds=duration,
            extra={"partition_by": partition_columns, "run_id": run_id},
        )

    # -- reads -------------------------------------------------------------
    def read(self, table: str, *, version: int | None = None, run_id: str | None = None) -> pd.DataFrame:
        reader = self.spark.read.format("delta")
        if version is not None:
            reader = reader.option("versionAsOf", int(version))
        location = self.table_location(table)
        try:
            frame = reader.table(location) if not self._is_path() else reader.load(location)
        except Exception as exc:
            raise StorageError(f"Delta read from {location} failed: {exc}") from exc
        if run_id and "sim_run_id" in frame.columns:
            frame = frame.filter(frame["sim_run_id"] == run_id)
        return frame.toPandas()

    def read_spark(self, table: str, *, version: int | None = None):
        """Return the Spark DataFrame (avoids collecting 10M rows onto the driver)."""
        reader = self.spark.read.format("delta")
        if version is not None:
            reader = reader.option("versionAsOf", int(version))
        location = self.table_location(table)
        return reader.table(location) if not self._is_path() else reader.load(location)

    def history(self, table: str, *, limit: int = 50) -> list[dict[str, Any]]:
        location = self.table_location(table)
        try:
            rows = self.spark.sql(f"DESCRIBE HISTORY {location} LIMIT {int(limit)}").collect()
        except Exception as exc:
            raise StorageError(f"DESCRIBE HISTORY failed for {location}: {exc}") from exc
        history: list[dict[str, Any]] = []
        for row in rows:
            payload = row.asDict(recursive=True)
            history.append(
                {
                    "version": int(payload.get("version", -1)),
                    "timestamp": str(payload.get("timestamp", "")),
                    "operation": str(payload.get("operation", "")),
                    "operationParameters": payload.get("operationParameters", {}),
                    "userName": str(payload.get("userName", "")),
                    "run_id": str((payload.get("operationParameters") or {}).get("run_id", "")),
                }
            )
        return history

    def optimize(self, table: str, *, zorder_by: list[str] | None = None) -> None:
        """Compact small files (and optionally Z-ORDER) after a large simulation."""
        location = self.table_location(table)
        statement = f"OPTIMIZE {location}"
        if zorder_by:
            statement += f" ZORDER BY ({', '.join(zorder_by)})"
        try:
            self.spark.sql(statement)
        except Exception as exc:
            logger.warning(f"OPTIMIZE {location} failed: {exc}")

    # -- internals ---------------------------------------------------------
    def _to_spark(self, frame: Any):
        if hasattr(frame, "toDF") and not isinstance(frame, pd.DataFrame):  # already a Spark DataFrame
            return frame
        pdf = sanitise_for_parquet(as_pandas(frame))
        return self.spark.createDataFrame(pdf)
