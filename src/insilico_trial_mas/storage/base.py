"""Storage abstraction: Delta Lake in production, a versioned Parquet store locally.

The platform has to demonstrate Delta time travel (specification section 5, cell 4)
while remaining runnable without a JVM. Both backends therefore implement the
same interface and expose the same *version* concept:

* :class:`~insilico_trial_mas.storage.delta_store.DeltaStore` - real Delta tables
  with ``DESCRIBE HISTORY`` / ``VERSION AS OF``;
* :class:`~insilico_trial_mas.storage.local_store.LocalVersionedStore` - an
  append-only ``_version=NNNN`` Parquet layout with a JSON manifest that offers
  identical point-in-time reads.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(slots=True)
class WriteResult:
    """Outcome of one table write."""

    table: str
    location: str
    rows: int
    version: int = 0
    backend: str = ""
    duration_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "location": self.location,
            "rows": self.rows,
            "version": self.version,
            "backend": self.backend,
            "duration_seconds": round(self.duration_seconds, 3),
            **self.extra,
        }


class BaseStore(abc.ABC):
    """Interface implemented by every storage backend."""

    backend: str = "base"

    def __init__(self, config) -> None:
        self.config = config

    # -- paths -------------------------------------------------------------
    @abc.abstractmethod
    def table_location(self, table: str) -> str:
        """Physical location (path or ``catalog.schema.table``) of ``table``."""

    @abc.abstractmethod
    def write(
        self,
        table: str,
        frame: Any,
        *,
        run_id: str = "",
        mode: str = "append",
        partition_by: list[str] | None = None,
    ) -> WriteResult:
        """Persist ``frame`` (pandas DataFrame, list of rows or Spark DataFrame)."""

    @abc.abstractmethod
    def read(self, table: str, *, version: int | None = None, run_id: str | None = None) -> pd.DataFrame:
        """Read a table, optionally as of a version or limited to one run."""

    @abc.abstractmethod
    def history(self, table: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the version history (Delta ``DESCRIBE HISTORY`` equivalent)."""

    def exists(self, table: str) -> bool:  # pragma: no cover - backend specific
        try:
            return not self.read(table).empty
        except Exception:
            return False

    def latest_version(self, table: str) -> int:
        entries = self.history(table, limit=1)
        return int(entries[0]["version"]) if entries else -1


def sanitise_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    """Encode nested values as JSON strings so Parquet/Delta writers never fail.

    Analysis rows legitimately contain dicts (grade distributions) and lists
    (confidence intervals). Columnar formats require scalars, so anything nested
    is serialised deterministically - the value survives a round trip through
    ``json.loads``.
    """
    import json

    out = frame.copy()
    for column in out.columns:
        if out[column].dtype != object:
            continue
        sample = out[column].dropna()
        if sample.empty:
            continue
        if sample.map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
            out[column] = out[column].map(
                lambda value: json.dumps(value, sort_keys=True, default=str)
                if isinstance(value, (dict, list, tuple, set))
                else value
            )
    return out


def as_pandas(frame: Any) -> pd.DataFrame:
    """Normalise the accepted write payloads into a pandas DataFrame."""
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.DataFrame):
        return frame
    if isinstance(frame, list):
        return pd.DataFrame(frame)
    to_pandas = getattr(frame, "toPandas", None)
    if callable(to_pandas):  # Spark DataFrame
        return to_pandas()
    raise TypeError(f"unsupported frame type for storage: {type(frame).__name__}")
