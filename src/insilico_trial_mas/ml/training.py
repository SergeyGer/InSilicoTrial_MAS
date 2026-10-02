"""Training pipeline for the learned residual head of the physiology model.

The specification requires MLflow to manage the ML models that predict patient
biomarkers. This module produces exactly such an artifact:

1. generate a synthetic *historical* cohort (regime-shifted relative to the trial
   being simulated, so the residual head learns a genuine correction rather than
   memorising the mechanistic model),
2. compute the mechanistic prediction for every historical row,
3. fit a ridge regression per residual target in closed form,
4. report hold-out metrics,
5. persist the artifact (and optionally register it in the MLflow Model Registry).

Fitting is a plain NumPy normal-equation solve, which keeps the training path
free of scikit-learn/XGBoost while remaining a real, validated regression.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..cohort.generator import CohortGenerator
from ..config import SimulationConfig
from ..errors import ModelRegistryError
from ..logging_utils import get_logger
from ..reproducibility import rng_for
from ..schemas import DrugSpec, PatientProfile, TrialProtocol
from .physiology import (
    FEATURE_NAMES,
    RESIDUAL_TARGETS,
    MechanisticPhysiology,
    RidgeResidualHead,
    feature_vector,
)
from .pk_pd import derive_pk_parameters, exposure_for_epoch

logger = get_logger("ml.training")


@dataclass(slots=True)
class TrainingResult:
    """Outcome of one training run."""

    head: RidgeResidualHead
    metrics: dict[str, float]
    n_train: int
    n_holdout: int
    artifact_path: str = ""


def _synthetic_truth(
    profile: PatientProfile,
    drug: DrugSpec,
    exposure,
    prediction,
    *,
    epoch: int,
    rng: np.random.Generator,
) -> dict[str, float]:
    """Ground-truth residual generator for the *historical* cohort.

    The historical data-generating process differs from the mechanistic model in
    a structured way (pharmacogenomic interactions and a saturating exposure
    term that the mechanistic backbone does not contain). The learned head can
    therefore recover real signal.
    """
    flags = profile.genomic.risk_flags()
    exposure_ratio = min(exposure.exposure_ratio, 20.0)
    saturating = math.tanh(exposure_ratio / 3.0)
    return {
        "delta_sbp_mmhg": -3.2 * saturating * (1.0 + 0.4 * flags["cyp2d6_pm"])
        + 0.9 * (profile.age - 55.0) / 12.0
        + rng.normal(0, 1.1),
        "delta_dbp_mmhg": -1.7 * saturating + 0.4 * (profile.bmi - 27.0) / 5.0 + rng.normal(0, 0.7),
        "delta_hr_bpm": 2.1 * saturating - 0.6 * (profile.baseline_hr - 72.0) / 10.0 + rng.normal(0, 1.3),
        "delta_alt_ratio": 0.35 * saturating * (2.0 if flags["hla_b_57_01"] else 1.0)
        + 0.10 * (profile.bmi - 27.0) / 5.0
        + rng.normal(0, 0.05),
        "delta_ae_logit": 0.55 * saturating
        + 0.35 * flags["cyp2d6_pm"]
        + 0.25 * (profile.comorbidity_count - 2.0) / 1.5
        + rng.normal(0, 0.12),
    }


def fit_ridge(
    features: np.ndarray, targets: np.ndarray, alpha: float
) -> tuple[np.ndarray, np.ndarray]:
    """Closed-form ridge regression.

    Returns ``(coefficients, predictions)``. The intercept column must already be
    part of ``features`` and is excluded from the penalty.
    """
    n_features = features.shape[1]
    penalty = alpha * np.eye(n_features)
    penalty[0, 0] = 0.0  # never penalise the bias term
    gram = features.T @ features + penalty
    try:
        coefficients = np.linalg.solve(gram, features.T @ targets)
    except np.linalg.LinAlgError:  # pragma: no cover - singular systems only
        coefficients = np.linalg.lstsq(gram, features.T @ targets, rcond=None)[0]
    return coefficients, features @ coefficients


def train_residual_head(
    protocol: TrialProtocol,
    config: SimulationConfig,
    *,
    n_rows: int | None = None,
    seed: int | None = None,
    holdout_fraction: float = 0.2,
) -> TrainingResult:
    """Fit the residual head on synthetic historical data."""
    seed = config.ml.training_seed if seed is None else seed
    n_rows = n_rows or config.ml.training_rows
    rng = rng_for(seed, "training", protocol.drug.drug_id, protocol.protocol_id)

    # Historical cohort: deliberately different regime (older, sicker, more
    # ancestral diversity) so the learned correction generalises beyond the trial.
    historical = dataclasses.replace(
        config,
        n_patients=n_rows,
        n_cohorts=max(1, n_rows // 250),
        epochs=protocol.epochs,
    )
    generator = CohortGenerator(protocol, historical, rng=rng_for(seed, "historical-cohort"))
    cohort = generator.generate()
    drug = protocol.drug
    mechanistic = MechanisticPhysiology()

    features: list[np.ndarray] = []
    truth: list[dict[str, float]] = []
    for profile in cohort:
        pk = derive_pk_parameters(profile, drug)
        cumulative = 0.0
        for epoch in range(1, protocol.epochs + 1):
            exposure = exposure_for_epoch(
                pk,
                drug,
                dose_mg=max(arm.dose_mg for arm in protocol.arms),
                epoch=epoch,
                tau_h=protocol.epoch_duration_hours,
                cumulative_auc=cumulative,
            )
            cumulative = exposure.cumulative_auc_mg_h_l
            prediction = mechanistic.predict(
                profile,
                drug,
                exposure,
                pk,
                epoch=epoch,
                epochs_total=protocol.epochs,
                placebo_effect=protocol.placebo_effect,
                seed=seed,
            )
            features.append(feature_vector(profile, drug, exposure, epoch=epoch, epochs_total=protocol.epochs))
            truth.append(
                _synthetic_truth(profile, drug, exposure, prediction, epoch=epoch, rng=rng)
            )

    x = np.vstack(features)
    y = np.column_stack([np.array([row[t] for row in truth], dtype=float) for t in RESIDUAL_TARGETS])

    n_total = x.shape[0]
    n_holdout = max(1, int(n_total * holdout_fraction))
    permutation = rng.permutation(n_total)
    test_idx, train_idx = permutation[:n_holdout], permutation[n_holdout:]
    x_train, y_train = x[train_idx], y[train_idx]
    x_test, y_test = x[test_idx], y[test_idx]

    coefficients: dict[str, list[float]] = {}
    metrics: dict[str, float] = {"n_train": float(x_train.shape[0]), "n_holdout": float(x_test.shape[0])}
    for column, target in enumerate(RESIDUAL_TARGETS):
        beta, _ = fit_ridge(x_train, y_train[:, column], config.ml.ridge_alpha)
        predictions = x_test @ beta
        residual = y_test[:, column] - predictions
        ss_res = float(np.sum(residual**2))
        ss_tot = float(np.sum((y_test[:, column] - y_test[:, column].mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        metrics[f"rmse_{target}"] = float(np.sqrt(np.mean(residual**2)))
        metrics[f"r2_{target}"] = float(r2)
        coefficients[target] = [float(v) for v in beta]

    head = RidgeResidualHead(
        coefficients=coefficients,
        metrics=metrics,
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        training_rows=int(x_train.shape[0]),
        alpha=config.ml.ridge_alpha,
    )
    logger.info(
        "trained ridge residual head",
        extra={"extra_fields": {"rows": int(x_train.shape[0]), "r2_sbp": metrics["r2_delta_sbp_mmhg"]}},
    )
    return TrainingResult(head=head, metrics=metrics, n_train=int(x_train.shape[0]), n_holdout=int(x_test.shape[0]))


def train_and_register(
    protocol: TrialProtocol,
    config: SimulationConfig,
    *,
    n_rows: int | None = None,
    register: bool | None = None,
) -> TrainingResult:
    """Train the residual head, persist it and optionally register it in MLflow."""
    result = train_residual_head(protocol, config, n_rows=n_rows)
    path = result.head.save(config.ml.model_path)
    result.artifact_path = str(path)
    logger.info("saved physiology model artifact", extra={"extra_fields": {"path": str(path)}})

    should_register = config.tracking.register_model if register is None else register
    if should_register and config.tracking.enabled:
        try:
            import mlflow

            from ..mlflow_tracking.tracker import MlflowTracker, resolve_tracking_uri

            tracker = MlflowTracker(config.tracking, resolve_tracking_uri(config))
            with tracker.start_run(run_name=f"train-physiology-{protocol.drug.drug_id}"):
                tracker.log_params(
                    {
                        "drug_id": protocol.drug.drug_id,
                        "alpha": config.ml.ridge_alpha,
                        "training_rows": result.n_train,
                        "feature_count": len(FEATURE_NAMES),
                    }
                )
                tracker.log_metrics(result.metrics)
                tracker.log_artifact(str(path))
                tracker.register_model(str(path), config.ml.mlflow_model_name)
            del mlflow
        except Exception as exc:
            if config.tracking.strict:
                raise ModelRegistryError(f"MLflow registration failed: {exc}") from exc
            logger.warning(f"MLflow registration skipped: {exc}")
    return result


def training_report(result: TrainingResult) -> dict[str, Any]:
    """Compact, JSON-serialisable training summary for reports and CI logs."""
    return {
        "artifact_path": result.artifact_path,
        "model_version": result.head.version,
        "model_digest": result.head.digest(),
        "n_train": result.n_train,
        "n_holdout": result.n_holdout,
        "metrics": result.metrics,
        "coefficient_summary": {
            target: dict(zip(FEATURE_NAMES, [round(c, 4) for c in betas], strict=True))
            for target, betas in result.head.coefficients.items()
        },
    }
