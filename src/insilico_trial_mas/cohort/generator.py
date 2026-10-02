"""Synthetic cohort generation.

The generator builds the *screening population*: digital twins with demographics,
comorbidity history, concomitant medication and pharmacogenomic markers sampled
from the priors in ``resources/genomic_priors.yaml``. Eligibility filtering and
randomisation are the Protocol Agent's job (see
:mod:`insilico_trial_mas.agents.protocol_agent`), which keeps the CONSORT-style
screen-failure accounting honest.

No real patient data is used: every profile is synthesised from population-level
priors, which is what makes the platform usable without a data-use agreement.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from importlib import resources
from typing import Any, Literal

import numpy as np
import yaml

from ..config import SimulationConfig
from ..errors import CohortGenerationError
from ..logging_utils import get_logger
from ..reproducibility import rng_for, stable_hash
from ..schemas import GenomicProfile, PatientProfile, TrialProtocol

logger = get_logger("cohort.generator")

PRIORS_RESOURCE = "genomic_priors.yaml"

SMOKING_STATES = ("never", "former", "current")
SMOKING_PROBS = (0.55, 0.28, 0.17)


def load_priors() -> dict[str, Any]:
    """Load the packaged population priors."""
    try:
        text = resources.files("insilico_trial_mas.resources").joinpath(PRIORS_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:  # pragma: no cover - packaging failure
        raise CohortGenerationError(f"population priors resource not found: {PRIORS_RESOURCE}") from exc
    priors = yaml.safe_load(text)
    if not isinstance(priors, dict):
        raise CohortGenerationError("population priors must be a mapping")
    return priors


def _normalise(weights: dict[str, float]) -> tuple[list[str], np.ndarray]:
    keys = list(weights)
    values = np.array([max(0.0, float(weights[k])) for k in keys], dtype=float)
    total = values.sum()
    if total <= 0:
        raise CohortGenerationError("prior weights must have a positive sum")
    return keys, values / total


class CohortGenerator:
    """Deterministic generator of synthetic patient personas."""

    def __init__(
        self,
        protocol: TrialProtocol,
        config: SimulationConfig,
        *,
        priors: dict[str, Any] | None = None,
        rng: np.random.Generator | None = None,
    ) -> None:
        self.protocol = protocol
        self.config = config
        self.priors = priors or load_priors()
        self.rng = rng if rng is not None else rng_for(config.seed, protocol.protocol_id, "cohort")

    # -- public API --------------------------------------------------------
    def generate(self) -> list[PatientProfile]:
        """Generate the full screening population."""
        n_patients = self.config.n_patients
        if n_patients <= 0:
            raise CohortGenerationError("n_patients must be positive")
        return list(self.iter_profiles(n_patients))

    def iter_profiles(self, n_patients: int) -> Iterator[PatientProfile]:
        """Stream profiles (keeps memory flat for very large cohorts)."""
        n_cohorts = max(1, self.config.n_cohorts)
        ancestries, ancestry_p = _normalise(self.priors["ancestries"])
        sites = list(self.priors.get("site_ids", ["SITE-01"]))
        for index in range(n_patients):
            cohort_index = index % n_cohorts
            yield self._make_profile(
                index=index,
                cohort_id=f"COHORT-{cohort_index + 1:03d}",
                ancestries=ancestries,
                ancestry_p=ancestry_p,
                sites=sites,
            )

    # -- internals ---------------------------------------------------------
    def _make_profile(
        self,
        *,
        index: int,
        cohort_id: str,
        ancestries: list[str],
        ancestry_p: np.ndarray,
        sites: list[str],
    ) -> PatientProfile:
        rng = self.rng
        patient_id = f"PT-{stable_hash(self.config.seed, self.protocol.protocol_id, index, length=10).upper()}"
        ancestry = str(rng.choice(ancestries, p=ancestry_p))
        site_id = str(rng.choice(sites))

        sex: Literal["F", "M"] = "F" if rng.random() < 0.52 else "M"
        phys = self.priors["physiology"]
        height = max(140.0, float(rng.normal(phys["height_cm"]["mean_male" if sex == "M" else "mean_female"], phys["height_cm"]["sd"])))
        weight = max(40.0, float(rng.normal(phys["weight_kg"]["mean_male" if sex == "M" else "mean_female"], phys["weight_kg"]["sd"])))
        bmi = weight / ((height / 100.0) ** 2)

        age = float(np.clip(rng.normal(55.0, 12.0), 18.0, 90.0))
        smoking = str(rng.choice(SMOKING_STATES, p=SMOKING_PROBS))
        alcohol = float(max(0.0, rng.normal(4.0, 5.0)))

        comorbidities = self._sample_comorbidities(age, bmi, smoking)
        meds = self._sample_meds(comorbidities)

        sbp = (
            phys["baseline_sbp"]["intercept"]
            + phys["baseline_sbp"]["age_slope_per_year"] * (age - 40.0)
            + phys["baseline_sbp"]["bmi_slope_per_unit"] * (bmi - 25.0)
            + (6.0 if "hypertension" in comorbidities else 0.0)
            + (4.0 if "chronic_kidney_disease" in comorbidities else 0.0)
            + float(rng.normal(0.0, phys["baseline_sbp"]["sd"]))
        )
        dbp = (
            phys["baseline_dbp"]["intercept"]
            + phys["baseline_dbp"]["age_slope_per_year"] * (age - 40.0)
            + phys["baseline_dbp"]["bmi_slope_per_unit"] * (bmi - 25.0)
            + (3.0 if "hypertension" in comorbidities else 0.0)
            + float(rng.normal(0.0, phys["baseline_dbp"]["sd"]))
        )
        hr = (
            phys["baseline_hr"]["intercept"]
            + phys["baseline_hr"]["age_slope_per_year"] * (age - 40.0)
            + phys["baseline_hr"]["bmi_slope_per_unit"] * (bmi - 25.0)
            + float(rng.normal(0.0, phys["baseline_hr"]["sd"]))
        )
        alt = float(
            np.exp(
                phys["alt_u_l"]["log_mean"]
                + phys["alt_u_l"]["bmi_slope_per_unit"] * (bmi - 25.0)
                + float(rng.normal(0.0, phys["alt_u_l"]["log_sd"]))
            )
        )
        ast = float(np.exp(phys["ast_u_l"]["log_mean"] + float(rng.normal(0.0, phys["ast_u_l"]["log_sd"]))))
        creatinine = max(
            0.4,
            float(
                rng.normal(phys["creatinine_mg_dl"]["mean_male" if sex == "M" else "mean_female"], phys["creatinine_mg_dl"]["sd"])
                + phys["creatinine_mg_dl"]["age_slope_per_year"] * (age - 55.0)
            ),
        )
        egfr = self._egfr(age, sex, creatinine)

        genomic = self._sample_genomics(ancestry, rng)
        profile = PatientProfile(
            patient_id=patient_id,
            cohort_id=cohort_id,
            age=round(age, 1),
            sex=sex,
            weight_kg=round(weight, 1),
            height_cm=round(height, 1),
            bmi=round(bmi, 1),
            baseline_sbp=round(float(sbp), 1),
            baseline_dbp=round(float(dbp), 1),
            baseline_hr=round(float(hr), 1),
            egfr=round(egfr, 1),
            alt_u_l=round(alt, 1),
            ast_u_l=round(ast, 1),
            creatinine_mg_dl=round(creatinine, 3),
            comorbidities=comorbidities,
            concomitant_meds=meds,
            genomic=genomic,
            smoking=smoking,  # type: ignore[arg-type]
            alcohol_units_week=round(alcohol, 1),
            site_id=site_id,
        )
        profile.medical_history = self._history_text(profile)
        return profile

    def _sample_comorbidities(self, age: float, bmi: float, smoking: str) -> list[str]:
        tokens: list[str] = []
        for entry in self.priors["comorbidities"]:
            rate = float(entry["base_rate"])
            rate *= float(entry["age_multiplier"]) ** (age - 55.0)
            rate *= float(entry["bmi_multiplier"]) ** max(0.0, bmi - 25.0)
            if smoking == "current":
                rate *= float(entry["smoking_multiplier"])
            rate = min(0.95, max(0.0, rate))
            if self.rng.random() < rate:
                tokens.append(str(entry["token"]))
        return tokens

    def _sample_meds(self, comorbidities: list[str]) -> list[str]:
        mapping = self.priors.get("concomitant_meds", {})
        meds: list[str] = []
        for token in comorbidities:
            options = mapping.get(token, [])
            if options and self.rng.random() < 0.75:
                meds.append(str(self.rng.choice(options)))
        return sorted(set(meds))

    def _sample_genomics(self, ancestry: str, rng: np.random.Generator) -> GenomicProfile:
        def phenotype(table_name: str) -> str:
            weights = self.priors[table_name][ancestry]
            keys, probs = _normalise(weights)
            return str(rng.choice(keys, p=probs))

        def marker(name: str) -> bool:
            table = self.priors["marker_frequencies"][name]
            return bool(rng.random() < float(table.get(ancestry, 0.0)))

        return GenomicProfile(
            ancestry=ancestry,
            cyp2d6=phenotype("cyp2d6_by_ancestry"),  # type: ignore[arg-type]
            cyp3a4=phenotype("cyp3a4_by_ancestry"),  # type: ignore[arg-type]
            hla_b_57_01=marker("hla_b_57_01"),
            hla_dq2_2=marker("hla_dq2_2"),
            adrb1_arg389gly=marker("adrb1_arg389gly"),
            ace_dd=marker("ace_dd"),
            slco1b1_decreased=marker("slco1b1_decreased"),
        )

    def _egfr(self, age: float, sex: str, creatinine: float) -> float:
        """CKD-EPI-2021-style estimate (illustrative constants, no race coefficient)."""
        kappa = 0.7 if sex == "F" else 0.9
        alpha = -0.241 if sex == "F" else -0.302
        ratio = creatinine / kappa
        egfr = 142.0 * min(ratio, 1.0) ** alpha * max(ratio, 1.0) ** -1.200 * 0.9938**age
        if sex == "F":
            egfr *= 1.012
        noise = float(self.rng.normal(0.0, float(self.priors["physiology"]["egfr"]["sd"]) * 0.25))
        return float(np.clip(egfr + noise, 5.0, 160.0))

    @staticmethod
    def _history_text(profile: PatientProfile) -> str:
        """Narrative persona used as the LLM prompt's medical-history slot."""
        history = ", ".join(profile.comorbidities) if profile.comorbidities else "no significant medical history"
        meds = ", ".join(profile.concomitant_meds) if profile.concomitant_meds else "none"
        genotype_bits = []
        if profile.genomic.cyp2d6 != "NM":
            genotype_bits.append(f"CYP2D6 {profile.genomic.cyp2d6} metaboliser")
        if profile.genomic.hla_b_57_01:
            genotype_bits.append("HLA-B*57:01 carrier")
        if profile.genomic.slco1b1_decreased:
            genotype_bits.append("reduced SLCO1B1 function")
        genotype = f" Pharmacogenomics: {', '.join(genotype_bits)}." if genotype_bits else ""
        return (
            f"{int(profile.age)}-year-old {'female' if profile.sex == 'F' else 'male'} patient "
            f"(BMI {profile.bmi:.1f} kg/m2, {profile.smoking} smoker, {profile.alcohol_units_week:.0f} alcohol units/week) "
            f"with {history}. Concomitant medication: {meds}. "
            f"Baseline BP {profile.baseline_sbp:.0f}/{profile.baseline_dbp:.0f} mmHg, HR {profile.baseline_hr:.0f} bpm, "
            f"eGFR {profile.egfr:.0f} mL/min/1.73m2, ALT {profile.alt_u_l:.0f} U/L.{genotype}"
        )


def cohort_digest(profiles: list[PatientProfile]) -> str:
    """Stable digest of a cohort (used in run provenance)."""
    payload = "|".join(f"{p.patient_id}:{p.age}:{p.sex}:{p.baseline_sbp:.1f}" for p in profiles)
    return stable_hash(payload, length=16)


def cohort_summary(profiles: list[PatientProfile]) -> dict[str, Any]:
    """Descriptive summary used in reports and smoke tests."""
    if not profiles:
        return {"n": 0}
    ages = np.array([p.age for p in profiles])
    sbp = np.array([p.baseline_sbp for p in profiles])
    bmi = np.array([p.bmi for p in profiles])
    comorb = np.array([p.comorbidity_count for p in profiles], dtype=float)
    return {
        "n": len(profiles),
        "mean_age": float(ages.mean()),
        "female_fraction": float(np.mean([p.sex == "F" for p in profiles])),
        "mean_bmi": float(bmi.mean()),
        "mean_baseline_sbp": float(sbp.mean()),
        "mean_comorbidities": float(comorb.mean()),
        "ancestry_distribution": {
            ancestry: float(np.mean([p.genomic.ancestry == ancestry for p in profiles]))
            for ancestry in sorted({p.genomic.ancestry for p in profiles})
        },
        "cyp2d6_pm_fraction": float(np.mean([p.genomic.cyp2d6 == "PM" for p in profiles])),
        "hla_b_57_01_fraction": float(np.mean([p.genomic.hla_b_57_01 for p in profiles])),
        "cohorts": len({p.cohort_id for p in profiles}),
        "sites": len({p.site_id for p in profiles}),
        "digest": cohort_digest(profiles),
    }


def expected_epoch_count(protocol: TrialProtocol, config: SimulationConfig) -> int:
    """Number of simulated epochs including the baseline epoch (0)."""
    epochs = config.epochs or protocol.epochs
    return epochs + (1 if config.include_baseline_epoch else 0)


def cohort_size_estimate(protocol: TrialProtocol, config: SimulationConfig) -> dict[str, float]:
    """Rough cost estimate before launching a run (rows, LLM calls, Spark tasks)."""
    epochs = expected_epoch_count(protocol, config)
    rows = config.n_patients * epochs
    llm_calls = 0.0
    if config.llm_mode == "all":
        llm_calls = rows
    elif config.llm_mode == "sample":
        llm_calls = rows * config.llm_sample_rate
    elif config.llm_mode == "triggered":
        llm_calls = rows * max(config.llm_sample_rate, 0.02)
    partitions = max(1, math.ceil(config.n_cohorts / max(1, config.engine.cohorts_per_partition)))
    return {
        "rows": float(rows),
        "llm_calls_estimate": float(llm_calls),
        "spark_partitions": float(partitions),
        "epochs": float(epochs),
    }
