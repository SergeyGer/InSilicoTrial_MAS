"""Local versioned Parquet store - Delta-style time travel without a JVM.

Layout::

    <root>/<schema>/<table>/_version=000001/part-00000.parquet
    <root>/<schema>/<table>/_version=000002/<partition>=<value>/part-00000.parquet
    <root>/<schema>/<table>/_manifest.json

The manifest is the equivalent of the Delta log: it records one entry per write
(version, timestamp, run id, row count, columns, partition columns, path) and is
what makes ``read(version=n)`` and ``history()`` work.

Every write creates a new immutable version directory and appends one entry to
the manifest, so ``read(version=n)`` is a genuine point-in-time read and
``history()`` mirrors ``DESCRIBE HISTORY``. This is what makes the notebook's
time-travel cell reproducible on Databricks Community Edition or a laptop.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..errors import StorageError
from ..logging_utils import get_logger
from .base import BaseStore, WriteResult, as_pandas, sanitise_for_parquet

logger = get_logger("storage.local")

MANIFEST_NAME = "_manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalVersionedStore(BaseStore):
    """Append-only versioned Parquet store."""

    backend = "local"

    def __init__(self, config, *, root: str | Path | None = None) -> None:
        super().__init__(config)
        self.root = Path(root or config.local_root or "artifacts/lake")

    # -- paths -------------------------------------------------------------
    def table_path(self, table: str) -> Path:
        schema, _, name = table.partition("/")
        return self.root / (schema or "default") / (name or schema)

    def table_location(self, table: str) -> str:
        return str(self.table_path(table))

    def exists(self, table: str) -> bool:
        return (self.table_path(table) / MANIFEST_NAME).exists()

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
        pdf = sanitise_for_parquet(as_pandas(frame))
        path = self.table_path(table)
        path.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest(path)
        version = int(manifest[-1]["version"]) + 1 if manifest else 1
        version_dir = path / f"_version={version:06d}"
        version_dir.mkdir(parents=True, exist_ok=True)

        try:
            if mode == "overwrite" and manifest:
                # Overwrite semantics: the new version shadows all previous data,
                # but older versions stay readable (that is the point of history).
                logger.info(f"overwrite write to {table}: previous data retained as history")
            if partition_by:
                # Hive-style partitioning keeps large Silver tables prunable.
                pdf.to_parquet(version_dir, index=False, partition_cols=[c for c in partition_by if c in pdf.columns])
            else:
                pdf.to_parquet(version_dir / "part-00000.parquet", index=False)
        except (OSError, ValueError, ImportError) as exc:
            raise StorageError(f"failed to write {table} to {version_dir}: {exc}") from exc

        entry = {
            "version": version,
            "timestamp": _now(),
            "run_id": run_id,
            "mode": mode,
            "rows": len(pdf),
            "columns": list(pdf.columns),
            "partition_by": list(partition_by or []),
            "path": str(version_dir),
            "operation": "WRITE" if mode == "overwrite" else "APPEND",
        }
        if mode == "overwrite":
            entry["replaces_versions"] = [int(m["version"]) for m in manifest]
        manifest.append(entry)
        self._save_manifest(path, manifest, keep=self.config.time_travel_versions_kept)
        duration = time.perf_counter() - started
        logger.info(f"wrote {len(pdf)} rows to {table} version {version} in {duration:.2f}s")
        return WriteResult(
            table=table,
            location=str(version_dir),
            rows=len(pdf),
            version=version,
            backend=self.backend,
            duration_seconds=duration,
        )

    # -- reads -------------------------------------------------------------
    def read(self, table: str, *, version: int | None = None, run_id: str | None = None) -> pd.DataFrame:
        path = self.table_path(table)
        manifest = self._load_manifest(path)
        if not manifest:
            return pd.DataFrame()
        if version is not None:
            versions = [v for v in manifest if int(v["version"]) == int(version)]
            if not versions:
                raise StorageError(f"version {version} not found for table {table} (latest is {manifest[-1]['version']})")
            return self._read_versions(path, versions)
        if run_id:
            versions = [v for v in manifest if v.get("run_id") == run_id]
            if not versions:
                raise StorageError(f"no versions of {table} were written by run {run_id}")
            return self._read_versions(path, versions)
        return self._read_versions(path, self._effective_versions(manifest))

    def read_as_of(self, table: str, version: int) -> pd.DataFrame:
        """Alias used by the CLI and notebooks (``VERSION AS OF n``)."""
        return self.read(table, version=version)

    def history(self, table: str, *, limit: int = 50) -> list[dict[str, Any]]:
        manifest = self._load_manifest(self.table_path(table))
        return list(reversed(manifest))[:limit]

    def list_tables(self) -> list[str]:
        tables: list[str] = []
        if not self.root.exists():
            return tables
        for manifest in self.root.glob(f"*/*/{MANIFEST_NAME}"):
            tables.append(f"{manifest.parent.parent.name}/{manifest.parent.name}")
        return sorted(tables)

    def vacuum(self, table: str, *, keep: int = 2) -> int:
        """Delete older version directories, keeping the newest ``keep`` versions."""
        path = self.table_path(table)
        manifest = self._load_manifest(path)
        if len(manifest) <= keep:
            return 0
        removed = 0
        for entry in manifest[:-keep]:
            version_dir = Path(entry["path"])
            if version_dir.exists():
                shutil.rmtree(version_dir, ignore_errors=True)
                removed += 1
        self._save_manifest(path, manifest[-keep:], keep=self.config.time_travel_versions_kept)
        return removed

    # -- internals ---------------------------------------------------------
    def _read_versions(self, path: Path, versions: list[dict[str, Any]]) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for entry in versions:
            version_dir = Path(entry["path"])
            if not version_dir.exists():
                continue
            files = sorted(version_dir.rglob("*.parquet"))
            if not files:
                continue
            # Hive-style partitioning stores `arm_id` (and friends) in the directory
            # names, not inside the files. Reading the version directory as one
            # dataset restores those columns; a per-file read silently loses them.
            try:
                frames.append(pd.read_parquet(version_dir))
                continue
            except (OSError, ValueError, ImportError):  # pragma: no cover - non-partitioned layout
                pass
            parts = []
            for file in files:
                part = pd.read_parquet(file)
                for key in entry.get("partition_by", []):
                    if key not in part.columns:
                        for token in file.parts:
                            if token.startswith(f"{key}="):
                                part[key] = token.split("=", 1)[1]
                parts.append(part)
            frames.append(pd.concat(parts, ignore_index=True))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @staticmethod
    def _effective_versions(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply overwrite semantics: keep only versions after the last overwrite."""
        last_overwrite = 0
        for index, entry in enumerate(manifest):
            if entry.get("mode") == "overwrite":
                last_overwrite = index
        return manifest[last_overwrite:]

    def _load_manifest(self, path: Path) -> list[dict[str, Any]]:
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.exists():
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"corrupt manifest at {manifest_path}: {exc}") from exc
        if not isinstance(payload, list):
            raise StorageError(f"manifest at {manifest_path} must be a JSON list")
        return payload

    def _save_manifest(self, path: Path, manifest: list[dict[str, Any]], *, keep: int) -> None:
        # Retention: keep the newest `keep` versions (0 disables trimming).
        if keep and len(manifest) > keep:
            manifest = manifest[-keep:]
        (path / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
