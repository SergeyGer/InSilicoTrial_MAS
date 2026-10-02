"""Hybrid physiology model: mechanistic PK/PD + learned tabular residual.

Design rationale
----------------
A pure mechanistic model cannot capture the interaction structure that makes
in-silico cohorts behave like real ones; a pure black-box model cannot be
audited. The platform therefore composes them:

``final_biomarker = mechanistic(baseline, PK/PD, placebo, progression) + ridge_residual(features)``

The ridge residual is trained (see :mod:`insilico_trial_mas.ml.training`) on
synthetic historical cohorts and stored as a small JSON artifact that is
versioned, digest-hashed and optionally registered in the MLflow Model Registry.
Adverse-event probabilities come from the drug's logistic AE models, corrected
by the learned residual logit - this is the "classical ML prediction of side
effects" the specification asks for.

Everything is deterministic: all stochastic draws use
:func:`insilico_trial_mas.reproducibility.rng_for`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..reproducibility import rng_for, stable_hash
from ..schemas import AEProbabilityModel, DrugSpec, PatientProfile
from .pk_pd import ExposureMetrics, PKParameters, emax_effect

#: Feature contract shared by training and inference (prevents train/serve skew).
FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "log1p_exposure_ratio",
    "exposure_ratio",
    "age_z",
    "bmi_z",
    "egfr_z",
    "comorbidity_z",
    "female",
    "cyp2d6_pm",
    "hla_b_57_01",
    "epoch_norm",
    "baseline_sbp_z",
    "alt_z",
    "smoker_current",
)

#: Targets predicted by the learned residual head.
RESIDUAL_TARGETS: tuple[str, ...] = (
    "delta_sbp_mmhg",
    "delta_dbp_mmhg",
    "delta_hr_bpm",
    "delta_alt_ratio",
    "delta_ae_logit",
)

# Population reference scales used to standardise covariates.
AGE_MEAN, AGE_SD = 55.0, 12.0
BMI_MEAN, BMI_SD = 27.0, 5.0
EGFR_MEAN, EGFR_SD = 90.0, 20.0
COMORB_MEAN, COMORB_SD = 2.0, 1.5
SBP_MEAN, SBP_SD = 132.0, 14.0
ALT_MEAN, ALT_SD = 26.0, 12.0

BASELINE_NOISE_SD = 0.0  # baseline is the patient's own phenotype; no extra noise

#: Inter-individual variability of drug sensitivity (log-normal sigma). Without it
#: a trial shows an implausibly narrow response distribution (SD ~4 mmHg instead of
#: the 6-10 mmHg seen in real antihypertensive studies).
SENSITIVITY_SIGMA = 0.35
SENSITIVITY_BOUNDS = (0.35, 2.50)

#: Within-patient measurement noise (device + biological variation), mmHg / bpm / ms.
MEASUREMENT_NOISE = {"sbp": 2.4, "dbp": 1.5, "hr": 2.0, "qtc_ms": 5.0, "alt_pct": 0.08}

_sensitivity_cache: dict[tuple[int, str], float] = {}
_SENSITIVITY_CACHE_LIMIT = 200_000


def individual_sensitivity(seed: int, patient_id: str) -> float:
    """Per-patient drug sensitivity multiplier (deterministic, cached per process)."""
    key = (seed, patient_id)
    cached = _sensitivity_cache.get(key)
    if cached is not None:
        return cached
    if len(_sensitivity_cache) > _SENSITIVITY_CACHE_LIMIT:  # pragma: no cover - memory guard
        _sensitivity_cache.clear()
    value = float(
        np.clip(
            rng_for(seed, "drug-sensitivity", patient_id).lognormal(mean=0.0, sigma=SENSITIVITY_SIGMA),
            *SENSITIVITY_BOUNDS,
        )
    )
    _sensitivity_cache[key] = value
    return value


@dataclass(slots=True)
class PhysiologyPrediction:
    """Model output for one patient-epoch (before LLM narration)."""

    sbp: float
    dbp: float
    hr: float
    qtc_ms: float
    alt_u_l: float
    ast_u_l: float
    creatinine_mg_dl: float
    egfr: float
    biomarker_composite: float
    ae_probabilities: dict[str, float]
    pk: dict[str, float] = field(default_factory=dict)
    model_version: str = "mechanistic-v1"
    backend: str = "mechanistic"
    residual: dict[str, float] = field(default_factory=dict)


class PhysiologyModel(Protocol):
    """Interface implemented by every physiology backend."""

    version: str
    backend: str

    def predict(
        self,
        profile: PatientProfile,
        drug: DrugSpec,
        exposure: ExposureMetrics,
        pk: PKParameters,
        *,
        epoch: int,
        epochs_total: int,
        placebo_effect: dict[str, float],
        seed: int,
    ) -> PhysiologyPrediction:  # pragma: no cover - protocol
        raise NotImplementedError("implemented by MechanisticPhysiology and HybridPhysiologyModel")


def feature_vector(
    profile: PatientProfile,
    drug: DrugSpec,
    exposure: ExposureMetrics,
    *,
    epoch: int,
    epochs_total: int,
) -> np.ndarray:
    """Build the standardised feature vector used by the learned residual head."""
    flags = profile.genomic.risk_flags()
    return np.array(
        [
            1.0,
            math.log1p(max(0.0, exposure.exposure_ratio)),
            min(exposure.exposure_ratio, 20.0),
            (profile.age - AGE_MEAN) / AGE_SD,
            (profile.bmi - BMI_MEAN) / BMI_SD,
            (profile.egfr - EGFR_MEAN) / EGFR_SD,
            (profile.comorbidity_count - COMORB_MEAN) / COMORB_SD,
            1.0 if profile.sex == "F" else 0.0,
            float(flags["cyp2d6_pm"]),
            float(flags["hla_b_57_01"]),
            epoch / max(1, epochs_total),
            (profile.baseline_sbp - SBP_MEAN) / SBP_SD,
            (profile.alt_u_l - ALT_MEAN) / ALT_SD,
            1.0 if profile.smoking == "current" else 0.0,
        ],
        dtype=float,
    )


# ---------------------------------------------------------------------------
# Mechanistic backbone
# ---------------------------------------------------------------------------


def _placebo_delta(placebo_effect: dict[str, float], epoch: int) -> float:
    """Non-specific effect common to all arms (regression to the mean + care effect)."""
    onset = max(0.5, float(placebo_effect.get("onset_epochs", 3.0)))
    return 1.0 - math.exp(-max(0, epoch) / onset)


class MechanisticPhysiology:
    """Interpretable PK/PD backbone - the fallback when no ML artifact is present."""

    version = "mechanistic-v1"
    backend = "mechanistic"

    def predict(
        self,
        profile: PatientProfile,
        drug: DrugSpec,
        exposure: ExposureMetrics,
        pk: PKParameters,
        *,
        epoch: int,
        epochs_total: int,
        placebo_effect: dict[str, float],
        seed: int,
    ) -> PhysiologyPrediction:
        onset = _placebo_delta(placebo_effect, epoch)
        c_avg = exposure.c_avg_mg_l
        flags = profile.genomic.risk_flags()
        beta1_blockade = 1.0 + (0.5 if profile.genomic.adrb1_arg389gly else 0.0)
        ace_effect = 1.0 + (0.25 if profile.genomic.ace_dd else 0.0)
        # Individual sensitivity: two patients with identical exposure do not respond
        # identically. This is a per-patient effect, constant across epochs.
        sensitivity = individual_sensitivity(seed, profile.patient_id)

        # --- PD: sigmoid Emax on the average steady-state concentration -------
        e_sbp = (
            emax_effect(c_avg, drug.ec50_mg_l, drug.emax.sbp_mmhg, drug.hill)
            * beta1_blockade
            * ace_effect
            * sensitivity
        )
        e_dbp = emax_effect(c_avg, drug.ec50_mg_l, drug.emax.dbp_mmhg, drug.hill) * sensitivity
        e_hr = emax_effect(c_avg, drug.ec50_mg_l, drug.emax.hr_bpm, drug.hill) * sensitivity
        e_qtc = emax_effect(exposure.c_max_mg_l, drug.ec50_mg_l, drug.emax.qtc_ms, drug.hill) * sensitivity
        e_alt = emax_effect(c_avg, drug.ec50_mg_l, drug.emax.alt_multiplier - 1.0, drug.hill) * sensitivity

        # --- disease progression & comorbidity burden -------------------------
        progression_sbp = 0.20 * epoch
        progression_dbp = 0.10 * epoch
        comorbidity_pressure = 0.8 * profile.comorbidity_count

        sbp = (
            profile.baseline_sbp
            + placebo_effect.get("sbp_mmhg", -3.0) * onset
            + e_sbp
            + progression_sbp
            + 0.25 * comorbidity_pressure
        )
        dbp = (
            profile.baseline_dbp
            + placebo_effect.get("dbp_mmhg", -1.8) * onset
            + e_dbp
            + progression_dbp
            + 0.15 * comorbidity_pressure
        )
        hr = profile.baseline_hr + placebo_effect.get("hr_bpm", -1.0) * onset + e_hr
        qtc = 412.0 + 0.35 * (profile.age - 50.0) + e_qtc + (6.0 if profile.sex == "F" else 0.0)

        # Hepatotoxicity: exposure-driven fold change, amplified by HLA risk and
        # by a reduced SLCO1B1 transporter function (statin-style risk marker).
        hla_multiplier = 1.9 if flags["hla_b_57_01"] else 1.0
        slco_multiplier = 1.35 if flags["slco1b1_decreased"] else 1.0
        alt = profile.alt_u_l * (1.0 + e_alt * hla_multiplier * slco_multiplier)
        ast = profile.ast_u_l * (1.0 + 0.85 * e_alt * hla_multiplier)

        # Renal function: modest exposure-related decline, faster in the elderly.
        egfr = profile.egfr * (1.0 - 0.004 * exposure.exposure_ratio) - 0.05 * max(0, epoch - 1)
        creatinine = profile.creatinine_mg_dl * (profile.egfr / max(1.0, egfr))

        # --- measurement noise (device + biological variation) ----------------
        # Applied to the *observed* value only, so the underlying trajectory stays
        # smooth while the recorded vitals look like real measurements.
        noise = rng_for(seed, "measurement", profile.patient_id, epoch)
        sbp += float(noise.normal(0.0, MEASUREMENT_NOISE["sbp"]))
        dbp += float(noise.normal(0.0, MEASUREMENT_NOISE["dbp"]))
        hr += float(noise.normal(0.0, MEASUREMENT_NOISE["hr"]))
        qtc += float(noise.normal(0.0, MEASUREMENT_NOISE["qtc_ms"]))
        alt *= float(max(0.5, 1.0 + noise.normal(0.0, MEASUREMENT_NOISE["alt_pct"])))

        composite = (
            -(sbp - profile.baseline_sbp) / 20.0
            - (dbp - profile.baseline_dbp) / 12.0
            - 0.4 * (hr - profile.baseline_hr) / 10.0
            - 0.3 * max(0.0, qtc - 450.0) / 30.0
            - 0.5 * max(0.0, alt / max(1.0, profile.alt_u_l) - 1.0)
        )

        ae_probabilities = self._ae_probabilities(profile, drug, exposure, epoch)
        return PhysiologyPrediction(
            sbp=sbp,
            dbp=dbp,
            hr=hr,
            qtc_ms=qtc,
            alt_u_l=alt,
            ast_u_l=ast,
            creatinine_mg_dl=creatinine,
            egfr=max(5.0, egfr),
            biomarker_composite=composite,
            ae_probabilities=ae_probabilities,
            pk=pk.as_dict(),
            model_version=self.version,
            backend=self.backend,
        )

    # -- logistic AE head --------------------------------------------------
    @staticmethod
    def ae_logit(profile: PatientProfile, ae: AEProbabilityModel, exposure: ExposureMetrics, epoch: int) -> float:
        flags = profile.genomic.risk_flags()
        covariates = {
            "age_z": (profile.age - AGE_MEAN) / AGE_SD,
            "bmi_z": (profile.bmi - BMI_MEAN) / BMI_SD,
            "egfr_z": (profile.egfr - EGFR_MEAN) / EGFR_SD,
            "comorbidity_z": (profile.comorbidity_count - COMORB_MEAN) / COMORB_SD,
            "female": 1.0 if profile.sex == "F" else 0.0,
            "cyp2d6_pm": flags["cyp2d6_pm"],
            "hla_b_57_01": flags["hla_b_57_01"],
            "hla_dq2_2": flags["hla_dq2_2"],
            "slco1b1_decreased": flags["slco1b1_decreased"],
            "prior_ae": 0.0,
            "smoker_current": 1.0 if profile.smoking == "current" else 0.0,
            "exposure_ratio": min(exposure.exposure_ratio, 20.0),
        }
        logit = ae.intercept + ae.exposure_slope * math.log1p(max(0.0, exposure.exposure_ratio))
        for name, coefficient in ae.coefficients.items():
            logit += coefficient * covariates.get(name, 0.0)
        # Tolerance/habituation: early epochs carry the highest risk.
        logit += 0.25 * math.exp(-max(0, epoch) / 2.0)
        return logit

    def _ae_probabilities(
        self, profile: PatientProfile, drug: DrugSpec, exposure: ExposureMetrics, epoch: int
    ) -> dict[str, float]:
        return {
            ae.term: _sigmoid(self.ae_logit(profile, ae, exposure, epoch)) for ae in drug.ae_models
        }


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ---------------------------------------------------------------------------
# Learned residual head
# ---------------------------------------------------------------------------


@dataclass
class RidgeResidualHead:
    """Ridge-regression residual model with per-target coefficients."""

    coefficients: dict[str, list[float]]
    targets: tuple[str, ...] = RESIDUAL_TARGETS
    feature_names: tuple[str, ...] = FEATURE_NAMES
    metrics: dict[str, float] = field(default_factory=dict)
    trained_at: str = ""
    training_rows: int = 0
    alpha: float = 1.0
    version: str = "ridge-residual-v1"

    def predict(self, features: np.ndarray) -> dict[str, float]:
        out: dict[str, float] = {}
        for target in self.targets:
            beta = self.coefficients.get(target)
            if beta is None:
                out[target] = 0.0
                continue
            vector = np.asarray(beta, dtype=float)
            if vector.size != features.size:  # defensive: contract changed since training
                out[target] = 0.0
                continue
            out[target] = float(features @ vector)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "model_type": "ridge_residual",
            "version": self.version,
            "feature_names": list(self.feature_names),
            "targets": list(self.targets),
            "coefficients": self.coefficients,
            "metrics": self.metrics,
            "trained_at": self.trained_at,
            "training_rows": self.training_rows,
            "alpha": self.alpha,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RidgeResidualHead:
        return cls(
            coefficients={k: list(v) for k, v in payload["coefficients"].items()},
            targets=tuple(payload.get("targets", RESIDUAL_TARGETS)),
            feature_names=tuple(payload.get("feature_names", FEATURE_NAMES)),
            metrics=dict(payload.get("metrics", {})),
            trained_at=str(payload.get("trained_at", "")),
            training_rows=int(payload.get("training_rows", 0)),
            alpha=float(payload.get("alpha", 1.0)),
            version=str(payload.get("version", "ridge-residual-v1")),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> RidgeResidualHead:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_json(payload)

    def digest(self) -> str:
        return stable_hash(json.dumps(self.to_json(), sort_keys=True), length=16)


# ---------------------------------------------------------------------------
# Composed model
# ---------------------------------------------------------------------------


class HybridPhysiologyModel:
    """Mechanistic backbone + learned residual corrections."""

    backend = "ridge"

    def __init__(self, head: RidgeResidualHead, *, residual_scale: float = 1.0) -> None:
        self.head = head
        self.residual_scale = float(residual_scale)
        self.version = f"{head.version}-{head.digest()[:8]}"
        self._mechanistic = MechanisticPhysiology()

    def predict(
        self,
        profile: PatientProfile,
        drug: DrugSpec,
        exposure: ExposureMetrics,
        pk: PKParameters,
        *,
        epoch: int,
        epochs_total: int,
        placebo_effect: dict[str, float],
        seed: int,
    ) -> PhysiologyPrediction:
        base = self._mechanistic.predict(
            profile,
            drug,
            exposure,
            pk,
            epoch=epoch,
            epochs_total=epochs_total,
            placebo_effect=placebo_effect,
            seed=seed,
        )
        features = feature_vector(profile, drug, exposure, epoch=epoch, epochs_total=epochs_total)
        residual = self.head.predict(features)
        scale = self.residual_scale

        base.sbp += scale * residual.get("delta_sbp_mmhg", 0.0)
        base.dbp += scale * residual.get("delta_dbp_mmhg", 0.0)
        base.hr += scale * residual.get("delta_hr_bpm", 0.0)
        alt_ratio_delta = scale * residual.get("delta_alt_ratio", 0.0)
        base.alt_u_l = max(1.0, base.alt_u_l * (1.0 + alt_ratio_delta))
        base.ast_u_l = max(1.0, base.ast_u_l * (1.0 + 0.85 * alt_ratio_delta))
        logit_shift = scale * residual.get("delta_ae_logit", 0.0)
        if logit_shift:
            base.ae_probabilities = {
                term: _sigmoid(math.log(max(1e-9, p) / max(1e-9, 1 - p)) + logit_shift)
                for term, p in base.ae_probabilities.items()
            }
        base.biomarker_composite = (
            -(base.sbp - profile.baseline_sbp) / 20.0
            - (base.dbp - profile.baseline_dbp) / 12.0
            - 0.4 * (base.hr - profile.baseline_hr) / 10.0
            - 0.3 * max(0.0, base.qtc_ms - 450.0) / 30.0
            - 0.5 * max(0.0, base.alt_u_l / max(1.0, profile.alt_u_l) - 1.0)
        )
        base.residual = residual
        base.model_version = self.version
        base.backend = self.backend
        return base


# ---------------------------------------------------------------------------
# Adverse-event sampling
# ---------------------------------------------------------------------------


def sample_adverse_events(
    profile: PatientProfile,
    drug: DrugSpec,
    prediction: PhysiologyPrediction,
    *,
    epoch: int,
    seed: int,
    run_id: str,
) -> list[dict[str, Any]]:
    """Realise adverse events for one patient-epoch from the predicted probabilities.

    Returns plain dicts (not pydantic models) because this runs on the hot path
    of a 10,000-agent simulation. One dedicated RNG per (patient, epoch, term)
    keeps the draw independent of evaluation order, so every engine agrees.
    """
    events: list[dict[str, Any]] = []
    for ae in drug.ae_models:
        probability = float(prediction.ae_probabilities.get(ae.term, 0.0))
        if probability <= 0.0:
            continue
        rng = rng_for(seed, run_id, profile.patient_id, epoch, ae.term)
        if rng.random() >= probability:
            continue
        grade = int(rng.choice(np.arange(1, 6), p=np.asarray(ae.grade_distribution, dtype=float)))
        relatedness = "probable" if probability > 0.25 else "possible" if probability > 0.08 else "unlikely"
        events.append(
            {
                "ae_id": stable_hash(run_id, profile.patient_id, epoch, ae.term, length=20),
                "patient_id": profile.patient_id,
                "arm_id": "",  # filled by the patient agent (it knows its arm)
                "epoch": epoch,
                "term": ae.term,
                "soc": ae.soc,
                "ctcae_grade": grade,
                "serious": bool(ae.serious and grade >= 3),
                "relatedness": relatedness,
                "predicted_probability": round(probability, 6),
                "reported_verbatim": "",
                "source": "model",
            }
        )
    return events


def build_mechanistic_model() -> MechanisticPhysiology:
    return MechanisticPhysiology()
