"""Fit adverse-event model parameters from target cumulative incidences.

Why this exists
---------------
Adverse-event risks in the platform are modelled as *per-epoch hazards*: a
patient is exposed once per epoch, so a constant per-epoch probability ``h``
produces a cumulative incidence of ``1 - (1 - h)^N`` over ``N`` epochs. Hand-picked
logistic intercepts therefore produce wildly unrealistic cumulative rates - the
first version of the demo protocol gave 98% of patients an adverse event and
occasional fatal headaches.

This script inverts the relationship. Given the cumulative incidence a clinical
team expects at each dose level (placebo / low / high), it solves for the logistic
intercept and exposure slope::

    logit(h) = intercept + exposure_slope * log1p(exposure_ratio)

and it accounts for the mean covariate contribution of the actual synthetic
cohort, so the fitted values are right for the population being simulated rather
than for a hypothetical average patient.

Usage::

    python scripts/fit_ae_parameters.py --protocol conf/trial_protocol_demo.yaml

It prints a YAML fragment that can be pasted back into the protocol.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from insilico_trial_mas.cohort.generator import CohortGenerator
from insilico_trial_mas.config import SimulationConfig
from insilico_trial_mas.ml.physiology import (
    AGE_MEAN,
    AGE_SD,
    BMI_MEAN,
    BMI_SD,
    COMORB_MEAN,
    COMORB_SD,
    EGFR_MEAN,
    EGFR_SD,
)
from insilico_trial_mas.ml.pk_pd import derive_pk_parameters, exposure_for_epoch
from insilico_trial_mas.pipeline import load_protocol

#: Cumulative incidence targets over the whole treatment period (placebo, low, high).
DEFAULT_TARGETS: dict[str, dict[str, Any]] = {
    "cough": {"placebo": 0.030, "low": 0.075, "high": 0.150, "grade_distribution": [0.86, 0.125, 0.015, 0.0, 0.0], "serious": False},
    "headache": {"placebo": 0.060, "low": 0.090, "high": 0.130, "grade_distribution": [0.80, 0.175, 0.025, 0.0, 0.0], "serious": False},
    "dizziness": {"placebo": 0.020, "low": 0.045, "high": 0.095, "grade_distribution": [0.72, 0.235, 0.043, 0.002, 0.0], "serious": False},
    "hypotension": {"placebo": 0.008, "low": 0.020, "high": 0.055, "grade_distribution": [0.60, 0.290, 0.100, 0.010, 0.0], "serious": False},
    "hyperkalaemia": {"placebo": 0.004, "low": 0.010, "high": 0.030, "grade_distribution": [0.55, 0.300, 0.130, 0.020, 0.0], "serious": False},
    "acute_kidney_injury": {"placebo": 0.002, "low": 0.004, "high": 0.012, "grade_distribution": [0.35, 0.330, 0.230, 0.088, 0.002], "serious": True},
    "hepatotoxicity": {"placebo": 0.002, "low": 0.005, "high": 0.014, "grade_distribution": [0.38, 0.330, 0.210, 0.075, 0.005], "serious": True},
    "hyperkalaemia_severe": {"placebo": 0.0005, "low": 0.0015, "high": 0.005, "grade_distribution": [0.15, 0.280, 0.360, 0.240, 0.010], "serious": True},
}

#: Covariates referenced by the demo protocol (kept in sync with the YAML below).
COVARIATES = {
    "cough": {"female": 0.45, "age_z": 0.10},
    "headache": {"female": 0.35, "age_z": -0.10, "comorbidity_z": 0.12},
    "dizziness": {"age_z": 0.28, "egfr_z": -0.18, "female": 0.22},
    "hypotension": {"age_z": 0.35, "comorbidity_z": 0.20, "prior_ae": 0.40},
    "hyperkalaemia": {"egfr_z": -0.55, "comorbidity_z": 0.25, "cyp2d6_pm": 0.10},
    "acute_kidney_injury": {"egfr_z": -0.85, "age_z": 0.40, "comorbidity_z": 0.30},
    "hepatotoxicity": {"hla_b_57_01": 1.10, "slco1b1_decreased": 0.45, "bmi_z": 0.20},
    "hyperkalaemia_severe": {"egfr_z": -0.95, "age_z": 0.45, "comorbidity_z": 0.35},
}


def mean_covariate_shift(term: str, cohort) -> float:
    """Average contribution of the non-exposure covariates across the cohort."""
    coefficients = COVARIATES.get(term, {})
    if not coefficients or not cohort:
        return 0.0
    total = 0.0
    for profile in cohort:
        flags = profile.genomic.risk_flags()
        values = {
            "age_z": (profile.age - AGE_MEAN) / AGE_SD,
            "bmi_z": (profile.bmi - BMI_MEAN) / BMI_SD,
            "egfr_z": (profile.egfr - EGFR_MEAN) / EGFR_SD,
            "comorbidity_z": (profile.comorbidity_count - COMORB_MEAN) / COMORB_SD,
            "female": 1.0 if profile.sex == "F" else 0.0,
            "cyp2d6_pm": flags["cyp2d6_pm"],
            "hla_b_57_01": flags["hla_b_57_01"],
            "slco1b1_decreased": flags["slco1b1_decreased"],
            "hla_dq2_2": flags["hla_dq2_2"],
            "prior_ae": 0.0,
            "smoker_current": 1.0 if profile.smoking == "current" else 0.0,
        }
        total += sum(coefficient * values.get(name, 0.0) for name, coefficient in coefficients.items())
    return total / len(cohort)


def typical_exposure_ratio(protocol, cohort, dose_mg: float) -> float:
    """Median exposure ratio for a given dose across the cohort."""
    if dose_mg <= 0 or not cohort:
        return 0.0
    ratios = []
    for profile in cohort[: min(len(cohort), 800)]:
        pk = derive_pk_parameters(profile, protocol.drug)
        exposure = exposure_for_epoch(
            pk,
            protocol.drug,
            dose_mg=dose_mg,
            epoch=1,
            tau_h=protocol.epoch_duration_hours,
        )
        ratios.append(exposure.exposure_ratio)
    ratios.sort()
    return ratios[len(ratios) // 2]


def hazard_from_cumulative(cumulative: float, epochs: int, *, tolerance_term: float = 0.0) -> float:
    """Per-epoch hazard that yields ``cumulative`` incidence over ``epochs``."""
    epochs = max(1, epochs)
    if cumulative <= 0:
        return 1e-9
    per_epoch = 1.0 - (1.0 - min(0.999, cumulative)) ** (1.0 / epochs)
    return max(1e-9, min(0.999, per_epoch))


def fit_term(
    term: str,
    targets: dict[str, Any],
    protocol,
    cohort,
    *,
    epochs: int,
) -> dict[str, Any]:
    """Solve for intercept and exposure slope for one adverse-event term."""
    covariate_shift = mean_covariate_shift(term, cohort)
    # The model adds a decaying tolerance term (+0.25 * exp(-epoch/2)); average it
    # out over the treatment epochs so the fit matches the simulator.
    tolerance = sum(0.25 * math.exp(-epoch / 2.0) for epoch in range(1, epochs + 1)) / epochs

    doses = {arm.arm_id: arm.dose_mg for arm in protocol.arms}
    control_id = protocol.control_arm.arm_id if protocol.control_arm else None
    treatment_arms = list(protocol.treatment_arms)
    low_arm, high_arm = treatment_arms[0], treatment_arms[-1]

    points: list[tuple[float, float]] = []  # (log1p(ratio), logit of per-epoch hazard)
    for arm_id, key in ((control_id, "placebo"), (low_arm.arm_id, "low"), (high_arm.arm_id, "high")):
        cumulative = float(targets[key])
        hazard = hazard_from_cumulative(cumulative, epochs) if doses.get(arm_id, 0) >= 0 else 0.0
        ratio = 0.0 if arm_id == control_id else typical_exposure_ratio(protocol, cohort, doses[arm_id])
        logit = math.log(hazard / (1.0 - hazard)) - covariate_shift - tolerance
        points.append((math.log1p(ratio), logit))

    (x1, y1), (x2, y2), (x3, y3) = points
    if abs(x3 - x1) < 1e-9:
        slope = 0.0
        intercept = y1
    else:
        # Least-squares fit through the three (log1p(ratio), logit) points.
        n = len(points)
        sum_x = sum(p[0] for p in points)
        sum_y = sum(p[1] for p in points)
        sum_xx = sum(p[0] ** 2 for p in points)
        sum_xy = sum(p[0] * p[1] for p in points)
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x**2)
        intercept = (sum_y - slope * sum_x) / n
    del x2, y2, x3, y3
    return {
        "term": term,
        "intercept": round(intercept, 3),
        "exposure_slope": round(slope, 3),
        "coefficients": COVARIATES.get(term, {}),
        "grade_distribution": [round(p, 4) for p in targets["grade_distribution"]],
        "serious": bool(targets["serious"]),
        "targets": {k: targets[k] for k in ("placebo", "low", "high")},
        "covariate_shift": round(covariate_shift, 3),
        "tolerance_term": round(tolerance, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit AE model parameters from target cumulative incidences")
    parser.add_argument("--protocol", default="conf/trial_protocol_demo.yaml")
    parser.add_argument("--patients", type=int, default=600)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    protocol = load_protocol(args.protocol)
    config = SimulationConfig(n_patients=args.patients, n_cohorts=4)
    cohort = CohortGenerator(protocol, config).generate()

    fitted = [
        fit_term(term, targets, protocol, cohort, epochs=protocol.epochs)
        for term, targets in DEFAULT_TARGETS.items()
    ]
    if args.json:
        print(json.dumps(fitted, indent=2))
        return 0

    print(f"# Fitted AE models for {protocol.protocol_id} ({protocol.epochs} epochs)")
    print("# Cumulative-incidence targets are the input; intercept/slope are the output.")
    print("ae_models:")
    for entry in fitted:
        print(f"  - term: {entry['term']}")
        print(f"    intercept: {entry['intercept']}")
        print(f"    exposure_slope: {entry['exposure_slope']}")
        print(f"    grade_distribution: {entry['grade_distribution']}")
        if entry["serious"]:
            print("    serious: true")
        print("    coefficients:")
        for name, coefficient in entry["coefficients"].items():
            print(f"      {name}: {coefficient}")
    print("")
    print("# targets (cumulative incidence over the treatment period)")
    for entry in fitted:
        print(
            f"# {entry['term']:<22} placebo={entry['targets']['placebo']:.3%} "
            f"low={entry['targets']['low']:.3%} high={entry['targets']['high']:.3%} "
            f"(covariate shift {entry['covariate_shift']:+.2f})"
        )
    print("")
    print(yaml.safe_dump({"ae_models": fitted}, sort_keys=False)[:200] + "...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
