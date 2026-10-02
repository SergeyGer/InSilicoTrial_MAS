"""Physiology-model resolution: artifact -> MLflow registry -> mechanistic fallback.

The platform never fails a simulation because a model artifact is missing: it
degrades to the mechanistic backbone and records that decision in the run
provenance (``physiology_backend``). That behaviour is deliberate - a silent
upgrade would be worse than a documented downgrade in a regulated workflow.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import MLConfig
from ..errors import ModelRegistryError
from ..logging_utils import get_logger
from ..schemas import TrialProtocol
from .physiology import HybridPhysiologyModel, MechanisticPhysiology, RidgeResidualHead

logger = get_logger("ml.registry")

#: Extension point: ``model_type`` string -> loader ``(payload|path, metadata) -> model``.
BACKEND_LOADERS: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register_backend(model_type: str, loader: Callable[[dict[str, Any]], Any]) -> None:
    """Register a custom physiology backend (e.g. an XGBoost or PyTorch head)."""
    BACKEND_LOADERS[model_type] = loader


@dataclass(slots=True)
class LoadedModel:
    """A physiology model plus everything needed to audit it."""

    model: Any
    version: str
    backend: str
    digest: str = ""
    path: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    source: str = "mechanistic"
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend": self.backend,
            "digest": self.digest,
            "path": self.path,
            "source": self.source,
            "metrics": self.metrics,
            "notes": self.notes,
        }


def _load_artifact(path: Path) -> LoadedModel:
    """Load a JSON physiology artifact (currently the ridge residual head)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelRegistryError(f"cannot read physiology model artifact {path}: {exc}") from exc

    model_type = str(payload.get("model_type", "ridge_residual"))
    if model_type in BACKEND_LOADERS:
        model = BACKEND_LOADERS[model_type](payload)
        return LoadedModel(
            model=model,
            version=getattr(model, "version", model_type),
            backend=getattr(model, "backend", model_type),
            digest=getattr(model, "digest", lambda: "")(),
            path=str(path),
            metrics={k: float(v) for k, v in (payload.get("metrics") or {}).items()},
            source="artifact",
        )
    if model_type == "ridge_residual":
        head = RidgeResidualHead.from_json(payload)
        model = HybridPhysiologyModel(head)
        return LoadedModel(
            model=model,
            version=model.version,
            backend=model.backend,
            digest=head.digest(),
            path=str(path),
            metrics={k: float(v) for k, v in head.metrics.items()},
            source="artifact",
        )
    raise ModelRegistryError(
        f"unsupported physiology model_type {model_type!r} in {path}; "
        f"register it with insilico_trial_mas.ml.registry.register_backend()"
    )


def _try_mlflow_registry(config: MLConfig) -> LoadedModel | None:
    """Download the registered model artifact from the MLflow Model Registry."""
    try:
        import mlflow
    except ImportError:
        logger.warning("use_mlflow_registry is enabled but mlflow is not installed; using the local artifact")
        return None
    if config.registry_uri:
        mlflow.set_registry_uri(config.registry_uri)
    uri = f"models:/{config.mlflow_model_name}/{config.mlflow_stage}"
    try:
        download = mlflow.artifacts.download_artifacts
        downloaded = Path(download(artifact_uri=uri))
    except Exception as exc:
        logger.warning(f"could not download model from MLflow registry ({uri}): {exc}")
        return None
    candidate = downloaded / "physiology_model.json" if downloaded.is_dir() else downloaded
    if not candidate.exists():
        matches = sorted(downloaded.rglob("physiology_model.json")) if downloaded.is_dir() else []
        if not matches:
            logger.warning(f"registered model at {uri} contains no physiology_model.json")
            return None
        candidate = matches[0]
    loaded = _load_artifact(candidate)
    loaded.source = "mlflow-registry"
    loaded.notes = uri
    return loaded


def load_physiology_model(
    protocol: TrialProtocol,
    config: MLConfig,
    *,
    train_if_missing: bool | None = None,
) -> LoadedModel:
    """Resolve the physiology model according to the ML configuration."""
    backend = config.backend
    if backend == "mechanistic":
        mechanistic: Any = MechanisticPhysiology()
        model: Any = mechanistic
        return LoadedModel(model=model, version=model.version, backend=model.backend, source="mechanistic")

    if config.use_mlflow_registry:
        loaded = _try_mlflow_registry(config)
        if loaded is not None:
            logger.info(f"loaded physiology model from MLflow registry: {loaded.version}")
            return loaded

    path = Path(config.model_path)
    if path.exists():
        loaded = _load_artifact(path)
        logger.info(f"loaded physiology model artifact {path} (version {loaded.version})")
        if backend in {"ridge", "xgboost"} and loaded.backend != backend:
            logger.warning(
                f"configured backend {backend!r} but artifact reports backend {loaded.backend!r}; using the artifact"
            )
        return loaded

    should_train = config.auto_train_if_missing if train_if_missing is None else train_if_missing
    if not should_train:
        logger.warning(f"physiology model artifact {path} not found; falling back to the mechanistic model")
        model = MechanisticPhysiology()
        return LoadedModel(
            model=model,
            version=model.version,
            backend=model.backend,
            source="mechanistic-fallback",
            notes=f"artifact missing: {path}",
        )

    # Train on demand with a reduced row budget so that a first run stays interactive.
    import dataclasses

    from ..config import SimulationConfig
    from .training import train_and_register

    n_rows = min(4000, config.training_rows)
    bootstrap = dataclasses.replace(
        SimulationConfig(),
        ml=config,
        n_patients=n_rows,
        n_cohorts=max(1, n_rows // 250),
        epochs=protocol.epochs,
    )
    logger.info("no physiology artifact found - training one on synthetic historical data")
    result = train_and_register(protocol, bootstrap, n_rows=n_rows, register=False)
    model = HybridPhysiologyModel(result.head)
    return LoadedModel(
        model=model,
        version=result.head.version,
        backend=model.backend,
        digest=result.head.digest(),
        path=result.artifact_path,
        metrics=result.metrics,
        source="trained-on-demand",
    )
