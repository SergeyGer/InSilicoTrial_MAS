"""MLflow experiment tracking, model registry access and LLM trace persistence.

Tracking is *optional by design*: every call is wrapped so that an unreachable
tracking server degrades to a warning instead of failing a multi-hour simulation.
The trace recorder always writes a local JSONL file, which keeps the
"MLflow Tracing" notebook cell working even when the MLflow UI is unavailable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger

logger = get_logger("tracking.mlflow")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_tracking_uri(config) -> str:
    """Resolve the tracking URI: explicit config > Databricks workspace > local ``mlruns``."""
    if config.tracking.tracking_uri:
        return config.tracking.tracking_uri
    host = os.environ.get("DATABRICKS_HOST", "")
    token = os.environ.get("DATABRICKS_TOKEN", "")
    if host and token:
        return "databricks"
    return f"file://{(Path(config.output_dir) / 'mlruns').resolve()}"


class MlflowTracker:
    """Thin, failure-tolerant wrapper around the MLflow client API."""

    def __init__(self, config, tracking_uri: str | None = None) -> None:
        self.config = config
        self.tracking_uri = tracking_uri or config.tracking_uri or ""
        self._mlflow: Any | None = None
        self._run_active = False
        self._degraded_reason = ""
        self._logged_warning = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def available(self) -> bool:
        if not self.config.enabled:
            return False
        return self._import_mlflow() is not None

    def _import_mlflow(self) -> Any | None:
        if self._mlflow is not None:
            return self._mlflow
        try:
            import mlflow

            if self.tracking_uri:
                mlflow.set_tracking_uri(self.tracking_uri)
            mlflow.set_experiment(self.config.experiment_name)
            self._mlflow = mlflow
        except Exception as exc:
            self._degraded_reason = f"{type(exc).__name__}: {exc}"
            self._warn_once(f"MLflow unavailable ({exc}); tracking disabled")
            self._mlflow = None
        return self._mlflow

    def _warn_once(self, message: str) -> None:
        if self._logged_warning:
            return
        logger.warning(message)
        self._logged_warning = True

    @contextmanager
    def start_run(self, run_name: str | None = None, tags: dict[str, str] | None = None) -> Iterator[MlflowTracker]:
        """Start (or degrade from) an MLflow run."""
        if not self.config.enabled:
            yield self
            return
        mlflow = self._import_mlflow()
        if mlflow is None:
            yield self
            return
        try:
            with mlflow.start_run(run_name=run_name or self.config.run_name or None, tags=tags or {}):
                self._run_active = True
                try:
                    yield self
                finally:
                    self._run_active = False
        except Exception as exc:
            self._run_active = False
            self._degraded_reason = f"{type(exc).__name__}: {exc}"
            if self.config.strict:
                raise
            self._warn_once(f"MLflow run failed ({exc}); continuing without tracking")
            yield self

    # -- logging -----------------------------------------------------------
    def log_params(self, params: dict[str, Any]) -> None:
        if not self.config.enabled:
            return
        mlflow = self._import_mlflow()
        if mlflow is None:
            return
        try:
            mlflow.log_params({k: _flatten(v) for k, v in params.items() if v is not None})
        except Exception as exc:
            self._handle(exc)

    def log_metrics(self, metrics: dict[str, Any], *, step: int | None = None) -> None:
        if not self.config.enabled:
            return
        mlflow = self._import_mlflow()
        if mlflow is None:
            return
        numeric = {}
        for key, value in metrics.items():
            try:
                numeric[key] = float(value)
            except (TypeError, ValueError):
                continue
        try:
            mlflow.log_metrics(numeric, step=step)
        except Exception as exc:
            self._handle(exc)

    def log_artifact(self, path: str | Path) -> None:
        if not self.config.enabled:
            return
        mlflow = self._import_mlflow()
        if mlflow is None or not self.config.log_artifacts:
            return
        candidate = Path(path)
        if not candidate.exists():
            return
        try:
            if candidate.is_dir():
                mlflow.log_artifacts(str(candidate))
            else:
                mlflow.log_artifact(str(candidate))
        except Exception as exc:
            self._handle(exc)

    def log_dict(self, payload: dict[str, Any], artifact_file: str) -> None:
        if not self.config.enabled:
            return
        mlflow = self._import_mlflow()
        if mlflow is None or not self.config.log_artifacts:
            return
        try:
            mlflow.log_dict(payload, artifact_file)
        except Exception as exc:
            self._handle(exc)

    def set_tags(self, tags: dict[str, str]) -> None:
        if not self.config.enabled:
            return
        mlflow = self._import_mlflow()
        if mlflow is None:
            return
        try:
            mlflow.set_tags(tags)
        except Exception as exc:
            self._handle(exc)

    def register_model(self, artifact_path: str | Path, model_name: str) -> str:
        """Register a physiology artifact in the MLflow Model Registry."""
        if not self.config.enabled:
            return ""
        mlflow = self._import_mlflow()
        if mlflow is None:
            return ""
        candidate = Path(artifact_path)
        if not candidate.exists():
            self._warn_once(f"model artifact {candidate} does not exist; skipping registration")
            return ""
        try:
            # The artifact is a JSON file, so it is logged and then registered by
            # run-relative URI; a pyfunc flavour can be added without touching the
            # simulation code (see docs/ARCHITECTURE.md).
            mlflow.log_artifact(str(candidate))
            if not self._run_active:
                return ""
            run_id = mlflow.active_run().info.run_id
            model_uri = f"runs:/{run_id}/{candidate.name}"
            result = mlflow.register_model(model_uri, model_name)
            logger.info(f"registered model {model_name} version {getattr(result, 'version', '?')}")
            return str(getattr(result, "version", ""))
        except Exception as exc:
            self._handle(exc)
            return ""

    # -- diagnostics -------------------------------------------------------
    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "available": self.available,
            "tracking_uri": self.tracking_uri,
            "experiment": self.config.experiment_name,
            "degraded_reason": self._degraded_reason,
        }

    def _handle(self, exc: BaseException) -> None:
        if self.config.strict:
            raise exc
        self._degraded_reason = f"{type(exc).__name__}: {exc}"
        self._warn_once(f"MLflow call failed ({exc}); continuing")


def _flatten(value: Any) -> Any:
    """MLflow params must be scalars; stringify everything else deterministically."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


@dataclass(slots=True)
class NullTracker:
    """No-op tracker used in tests (keeps call sites identical)."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __enter__(self) -> NullTracker:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    @contextmanager
    def start_run(self, run_name: str | None = None, tags: dict[str, str] | None = None) -> Iterator[NullTracker]:
        self.calls.append(("start_run", {"run_name": run_name, "tags": tags or {}}))
        yield self

    def log_params(self, params: dict[str, Any]) -> None:
        self.calls.append(("log_params", dict(params)))

    def log_metrics(self, metrics: dict[str, Any], *, step: int | None = None) -> None:
        self.calls.append(("log_metrics", {**metrics, "step": step}))

    def log_artifact(self, path: str | Path) -> None:
        self.calls.append(("log_artifact", {"path": str(path)}))

    def log_dict(self, payload: dict[str, Any], artifact_file: str) -> None:
        self.calls.append(("log_dict", {"artifact_file": artifact_file}))

    def set_tags(self, tags: dict[str, str]) -> None:
        self.calls.append(("set_tags", dict(tags)))

    def register_model(self, artifact_path: str | Path, model_name: str) -> str:
        self.calls.append(("register_model", {"path": str(artifact_path), "name": model_name}))
        return ""
