"""Storage factory plus an in-memory backend used by tests."""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from ..errors import SimulationError
from ..logging_utils import get_logger
from .base import BaseStore, WriteResult, as_pandas
from .local_store import LocalVersionedStore

logger = get_logger("storage.factory")


class MemoryStore(BaseStore):
    """In-memory versioned store (unit tests, notebooks, dry runs)."""

    backend = "memory"

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)
        self._tables: dict[str, list[dict[str, Any]]] = {}

    def table_location(self, table: str) -> str:
        return f"memory://{table}"

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
        pdf = as_pandas(frame)
        versions = self._tables.setdefault(table, [])
        version = versions[-1]["version"] + 1 if versions else 1
        if mode == "overwrite":
            versions.clear()
        versions.append(
            {
                "version": version,
                "run_id": run_id,
                "mode": mode,
                "data": pdf.copy(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "partition_by": list(partition_by or []),
            }
        )
        return WriteResult(
            table=table,
            location=self.table_location(table),
            rows=len(pdf),
            version=version,
            backend=self.backend,
            duration_seconds=time.perf_counter() - started,
        )

    def read(self, table: str, *, version: int | None = None, run_id: str | None = None) -> pd.DataFrame:
        versions = self._tables.get(table, [])
        if version is not None:
            versions = [v for v in versions if v["version"] == version]
        if run_id:
            versions = [v for v in versions if v["run_id"] == run_id]
        if not versions:
            return pd.DataFrame()
        return pd.concat([v["data"] for v in versions], ignore_index=True)

    def history(self, table: str, *, limit: int = 50) -> list[dict[str, Any]]:
        entries = [
            {
                "version": v["version"],
                "timestamp": v["timestamp"],
                "run_id": v["run_id"],
                "rows": len(v["data"]),
                "operation": "WRITE" if v["mode"] == "overwrite" else "APPEND",
                "path": self.table_location(table),
            }
            for v in self._tables.get(table, [])
        ]
        return list(reversed(entries))[:limit]

    def list_tables(self) -> list[str]:
        return sorted(self._tables)


def create_store(config, *, spark=None) -> BaseStore:
    """Instantiate the store described by ``config.storage``."""
    backend = config.storage.backend
    if backend == "memory":
        return MemoryStore(config.storage)
    if backend == "local":
        return LocalVersionedStore(config.storage)
    if backend == "delta":
        from .delta_store import DeltaStore

        return DeltaStore(config.storage, spark=spark)
    raise SimulationError(f"unknown storage backend {backend!r}; expected 'local', 'delta' or 'memory'")


__all__ = ["BaseStore", "LocalVersionedStore", "MemoryStore", "WriteResult", "create_store"]
