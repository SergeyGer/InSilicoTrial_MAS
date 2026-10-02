"""Pharmacokinetic / pharmacodynamic core.

The specification's blueprint used ``simulated_bp = 120 + dose * 0.1`` - a toy
that cannot support a dose-response claim. This module replaces it with a
defensible one-compartment oral PK model with first-order absorption and
superposition across repeated dosing, plus an Emax PD link.

Model
-----
``C(t)`` after a single oral dose ``D`` with bioavailability ``F``::

    C(t) = F*D*ka / (V*(ka - ke)) * (exp(-ke*t) - exp(-ka*t)),   ke = ln2 / t_half

For ``N`` identical doses given every ``tau`` hours the superposition has a
closed form (geometric series), evaluated in O(1)::

    sum_{i=0}^{N-1} exp(-x*(t + i*tau)) = exp(-x*t) * (1 - exp(-x*N*tau)) / (1 - exp(-x*tau))

Exposure drives a sigmoid Emax effect on each biomarker and a logistic risk of
each adverse event (see :mod:`insilico_trial_mas.ml.physiology`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..schemas import DrugSpec, PatientProfile

MIN_CLEARANCE_L_H = 0.05
REFERENCE_EGFR = 100.0
REFERENCE_WEIGHT_KG = 70.0


@dataclass(frozen=True, slots=True)
class PKParameters:
    """Individual PK parameters derived from the digital-twin phenotype."""

    clearance_l_h: float
    volume_l: float
    ka_per_h: float
    ke_per_h: float
    half_life_h: float
    bioavailability: float
    renal_share: float
    hepatic_share: float

    def as_dict(self) -> dict[str, float]:
        return {
            "clearance_l_h": self.clearance_l_h,
            "volume_l": self.volume_l,
            "ka_per_h": self.ka_per_h,
            "ke_per_h": self.ke_per_h,
            "half_life_h": self.half_life_h,
            "bioavailability": self.bioavailability,
        }


@dataclass(frozen=True, slots=True)
class ExposureMetrics:
    """Exposure summary for one dosing epoch."""

    dose_mg: float
    c_trough_mg_l: float
    c_avg_mg_l: float
    c_max_mg_l: float
    auc_epoch_mg_h_l: float
    cumulative_auc_mg_h_l: float
    exposure_ratio: float
    t_max_h: float

    def as_dict(self) -> dict[str, float]:
        return {
            "c_trough_mg_l": self.c_trough_mg_l,
            "c_avg_mg_l": self.c_avg_mg_l,
            "c_max_mg_l": self.c_max_mg_l,
            "auc_epoch_mg_h_l": self.auc_epoch_mg_h_l,
            "cumulative_auc_mg_h_l": self.cumulative_auc_mg_h_l,
            "exposure_ratio": self.exposure_ratio,
        }


def derive_pk_parameters(profile: PatientProfile, drug: DrugSpec) -> PKParameters:
    """Individualise drug PK from weight, age, renal/hepatic function and genotype.

    Allometric scaling on body weight, an age-related decline in clearance after
    40 years, an eGFR-scaled renal share and a CYP2D6-scaled hepatic share. Age,
    renal function and genotype therefore act as genuine effect modifiers in the
    simulation rather than decorative covariates.
    """
    weight = max(30.0, float(profile.weight_kg))
    age_factor = 1.0 - 0.008 * max(0.0, profile.age - 40.0)
    age_factor = min(1.15, max(0.55, age_factor))
    sex_factor = 0.90 if profile.sex == "F" else 1.0
    renal_factor = min(1.25, max(0.35, float(profile.egfr) / REFERENCE_EGFR))
    cyp_multiplier = float(profile.genomic.risk_flags()["cyp2d6_exposure_multiplier"])

    base_clearance = drug.clearance_ml_min_per_kg * weight * 60.0 / 1000.0  # L/h
    renal_part = drug.renal_fraction * renal_factor
    hepatic_part = drug.hepatic_fraction * cyp_multiplier
    other_part = max(0.0, 1.0 - drug.renal_fraction - drug.hepatic_fraction)
    clearance = max(MIN_CLEARANCE_L_H, base_clearance * (renal_part + hepatic_part + other_part) * age_factor * sex_factor)

    volume = drug.volume_of_distribution_l_per_kg * weight * (1.0 + 0.10 * (profile.bmi - 25.0) / 25.0)
    volume = max(5.0, volume)
    ke = clearance / volume
    half_life = math.log(2.0) / ke if ke > 0 else float("inf")
    return PKParameters(
        clearance_l_h=clearance,
        volume_l=volume,
        ka_per_h=drug.ka_per_h,
        ke_per_h=ke,
        half_life_h=half_life,
        bioavailability=drug.bioavailability,
        renal_share=renal_part,
        hepatic_share=hepatic_part,
    )


def _geometric_sum(rate: float, t: float, n_doses: int, tau: float) -> float:
    """``sum_{i=0}^{n-1} exp(-rate*(t + i*tau))`` evaluated without a loop."""
    if rate <= 0:
        return float(n_doses)
    single = math.exp(-rate * t)
    if n_doses <= 1 or tau <= 0:
        return single
    denom = 1.0 - math.exp(-rate * tau)
    if abs(denom) < 1e-12:
        return single * n_doses
    return single * (1.0 - math.exp(-rate * n_doses * tau)) / denom


def plasma_concentration(
    pk: PKParameters,
    dose_mg: float,
    *,
    time_since_last_dose_h: float,
    n_prior_doses: int = 0,
    tau_h: float = 12.0,
) -> float:
    """Plasma concentration (mg/L) after ``1 + n_prior_doses`` identical doses."""
    if dose_mg <= 0:
        return 0.0
    t = max(0.0, float(time_since_last_dose_h))
    ka, ke = pk.ka_per_h, pk.ke_per_h
    if abs(ka - ke) < 1e-9:
        ka = ke + 1e-6
    scale = pk.bioavailability * dose_mg * ka / (pk.volume_l * (ka - ke))
    decay_ke = _geometric_sum(ke, t, 1 + n_prior_doses, tau_h)
    decay_ka = _geometric_sum(ka, t, 1 + n_prior_doses, tau_h)
    return max(0.0, scale * (decay_ke - decay_ka))


def time_to_peak(pk: PKParameters) -> float:
    """``t_max = ln(ka/ke) / (ka - ke)`` (0 for a bolus-like profile)."""
    ka, ke = pk.ka_per_h, pk.ke_per_h
    if abs(ka - ke) < 1e-9 or ka <= 0 or ke <= 0:
        return 0.0
    return max(0.0, math.log(ka / ke) / (ka - ke))


def exposure_for_epoch(
    pk: PKParameters,
    drug: DrugSpec,
    *,
    dose_mg: float,
    epoch: int,
    tau_h: float,
    cumulative_auc: float = 0.0,
) -> ExposureMetrics:
    """Exposure metrics for the ``epoch``-th dose of a repeated regimen.

    ``epoch`` is 1-based; ``epoch=0`` is the untreated baseline.
    """
    if dose_mg <= 0 or epoch <= 0:
        return ExposureMetrics(
            dose_mg=0.0,
            c_trough_mg_l=0.0,
            c_avg_mg_l=0.0,
            c_max_mg_l=0.0,
            auc_epoch_mg_h_l=0.0,
            cumulative_auc_mg_h_l=cumulative_auc,
            exposure_ratio=0.0,
            t_max_h=0.0,
        )
    n_prior = epoch - 1
    t_max = time_to_peak(pk)
    c_trough = plasma_concentration(pk, dose_mg, time_since_last_dose_h=0.0, n_prior_doses=n_prior, tau_h=tau_h)
    c_max = plasma_concentration(pk, dose_mg, time_since_last_dose_h=t_max, n_prior_doses=n_prior, tau_h=tau_h)
    # For linear PK the AUC over one dosing interval at steady state equals the AUC
    # after a single dose: AUC_tau = F * D / CL. Dividing by tau gives the average
    # steady-state concentration that drives chronic (Emax) effects. Using the AUC
    # extrapolated to infinity here would overstate exposure by the accumulation
    # factor - a mistake that inflates every downstream adverse-event rate.
    auc_epoch = pk.bioavailability * dose_mg / pk.clearance_l_h
    c_avg = auc_epoch / tau_h if tau_h > 0 else 0.0
    cumulative = cumulative_auc + auc_epoch
    exposure_ratio = c_avg / drug.ec50_mg_l
    return ExposureMetrics(
        dose_mg=float(dose_mg),
        c_trough_mg_l=c_trough,
        c_avg_mg_l=c_avg,
        c_max_mg_l=c_max,
        auc_epoch_mg_h_l=auc_epoch,
        cumulative_auc_mg_h_l=cumulative,
        exposure_ratio=float(exposure_ratio),
        t_max_h=t_max,
    )


def emax_effect(concentration: float, ec50: float, emax: float, hill: float = 1.0) -> float:
    """Sigmoid Emax: ``E = Emax * C^h / (EC50^h + C^h)``."""
    if concentration <= 0:
        return 0.0
    if ec50 <= 0:
        return emax
    ch = concentration**hill
    return emax * ch / (ec50**hill + ch)


def accumulation_ratio(ke_per_h: float, tau_h: float) -> float:
    """Steady-state accumulation factor ``1 / (1 - exp(-ke*tau))``."""
    if ke_per_h <= 0 or tau_h <= 0:
        return 1.0
    denom = 1.0 - math.exp(-ke_per_h * tau_h)
    return 1.0 if denom <= 1e-12 else 1.0 / denom
