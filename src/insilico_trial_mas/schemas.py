"""Domain contracts for InSilicoTrial MAS.

Two families of models live here:

* **Boundary models** (pydantic) - trial protocols, drug definitions, LLM
  responses, report payloads. They are validated at the edges of the system
  where bad data is cheap to reject.
* **Hot-path models** (dataclasses) - per-epoch patient observations. They are
  plain dataclasses because a 10,000-patient x 16-epoch run creates hundreds of
  thousands of them and pydantic validation overhead would dominate runtime.

``SILVER_COLUMNS`` is the single source of truth for the Silver
``patient_states`` table: the local Parquet writer, the Delta Lake writer and
the Spark ``StructType`` are all derived from it, which is what keeps the
time-travel demo reproducible across engines.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .version import PROTOCOL_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Trial protocol (boundary model)
# ---------------------------------------------------------------------------


class EmaxProfile(BaseModel):
    """Maximal effect and potency of the drug on each modelled biomarker."""

    model_config = ConfigDict(extra="forbid")

    sbp_mmhg: float = Field(-12.0, description="Maximum reduction of systolic blood pressure")
    dbp_mmhg: float = Field(-7.0, description="Maximum reduction of diastolic blood pressure")
    hr_bpm: float = Field(3.0, description="Maximum change of heart rate")
    qtc_ms: float = Field(8.0, description="Maximum QTc prolongation (safety signal)")
    alt_multiplier: float = Field(1.6, description="Peak fold-change of ALT at maximal exposure")


class AEProbabilityModel(BaseModel):
    """Logistic model of one adverse-event term.

    ``coefficients`` are applied to standardised covariates:
    ``exposure_ratio`` (C_avg / EC50), ``age_z``, ``bmi_z``, ``comorbidity_z``,
    ``egfr_z``, ``female``, ``cyp2d6_pm``, ``hla_b_57_01``, ``prior_ae``.
    """

    model_config = ConfigDict(extra="forbid")

    term: str = Field(..., description="MedDRA-style preferred term")
    soc: str = Field("General disorders", description="System organ class")
    intercept: float = Field(-3.0, description="Baseline log-odds at reference exposure")
    coefficients: dict[str, float] = Field(default_factory=dict)
    exposure_slope: float = Field(1.0, description="Weight of log(1 + exposure_ratio) in the log-odds")
    grade_distribution: list[float] = Field(
        default_factory=lambda: [0.60, 0.28, 0.09, 0.025, 0.005],
        description="Probability mass of CTCAE grades 1..5 (renormalised on use)",
    )
    serious: bool = Field(False, description="Whether grade >= 3 counts as serious for this term")

    @field_validator("grade_distribution")
    @classmethod
    def _normalise(cls, value: list[float]) -> list[float]:
        if len(value) != 5:
            raise ValueError("grade_distribution must contain exactly 5 probabilities (CTCAE grades 1-5)")
        if any(p < 0 for p in value):
            raise ValueError("grade_distribution must be non-negative")
        total = sum(value)
        if total <= 0:
            raise ValueError("grade_distribution must have a positive sum")
        return [p / total for p in value]


class DrugSpec(BaseModel):
    """Pharmacokinetic / pharmacodynamic description of the investigational drug."""

    model_config = ConfigDict(extra="forbid")

    drug_id: str
    name: str
    drug_class: str = "small_molecule"
    route: Literal["oral", "iv", "sc"] = "oral"
    bioavailability: float = Field(0.80, gt=0, le=1.0)
    half_life_h: float = Field(12.0, gt=0, description="Terminal elimination half-life")
    volume_of_distribution_l_per_kg: float = Field(0.60, gt=0)
    clearance_ml_min_per_kg: float = Field(1.20, gt=0)
    ka_per_h: float = Field(1.00, gt=0, description="First-order absorption rate constant")
    ec50_mg_l: float = Field(1.00, gt=0)
    hill: float = Field(1.00, gt=0)
    renal_fraction: float = Field(0.30, ge=0, le=1, description="Share of clearance that is renal")
    hepatic_fraction: float = Field(0.50, ge=0, le=1, description="Share of clearance that is CYP-mediated")
    emax: EmaxProfile = Field(default_factory=EmaxProfile)  # type: ignore[arg-type]
    ae_models: list[AEProbabilityModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> DrugSpec:
        if self.renal_fraction + self.hepatic_fraction > 1.0:
            raise ValueError("renal_fraction + hepatic_fraction must be <= 1.0")
        terms = [ae.term for ae in self.ae_models]
        if len(terms) != len(set(terms)):
            raise ValueError("duplicate adverse-event terms in ae_models")
        return self

    @property
    def elimination_rate_per_h(self) -> float:
        """First-order elimination constant ``ke = ln(2) / t_half``."""
        return 0.6931471805599453 / self.half_life_h


class TitrationSpec(BaseModel):
    """Optional dose-escalation schedule applied by the Protocol Agent."""

    model_config = ConfigDict(extra="forbid")

    start_epoch: int = Field(1, ge=0)
    step_mg: float = Field(0.0, ge=0)
    max_dose_mg: float | None = Field(None, gt=0)


class ArmSpec(BaseModel):
    """One randomisation arm of the trial."""

    model_config = ConfigDict(extra="forbid")

    arm_id: str
    label: str = ""
    dose_mg: float = Field(0.0, ge=0)
    allocation_weight: float = Field(1.0, gt=0)
    is_control: bool = False
    titration: TitrationSpec | None = None

    @model_validator(mode="after")
    def _defaults(self) -> ArmSpec:
        if not self.label:
            self.label = self.arm_id.replace("_", " ").title()
        return self


class EndpointSpec(BaseModel):
    """Primary/secondary endpoint definition used by the Biostatistician Agent."""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str
    kind: Literal["continuous", "binary", "count"] = "continuous"
    direction: Literal["increase", "decrease", "two_sided"] = "two_sided"
    mcid: float | None = Field(None, description="Minimal clinically important difference")
    is_primary: bool = False
    description: str = ""
    #: For binary endpoints: value of ``column`` at/above which a patient responds.
    response_threshold: float | None = None
    #: Restrict the endpoint to a subset of epochs (default: the final epoch).
    epoch: int | None = None


class StoppingRuleSpec(BaseModel):
    """Safety stopping rule evaluated by the Biostatistician Agent each epoch."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    description: str = ""
    metric: Literal["grade3_plus_rate", "sae_rate", "mortality_rate", "alt_elevation_rate"] = "grade3_plus_rate"
    threshold: float = Field(..., ge=0, le=1)
    comparator: Literal["greater_than", "greater_or_equal"] = "greater_or_equal"
    action: Literal["review", "pause", "stop"] = "review"
    min_epoch: int = Field(0, ge=0, description="Do not evaluate the rule before this epoch")
    applies_to_arms: list[str] = Field(default_factory=list)


class EligibilityCriteria(BaseModel):
    """Inclusion/exclusion criteria enforced by the Protocol Agent."""

    model_config = ConfigDict(extra="forbid")

    min_age: float = 18.0
    max_age: float = 75.0
    min_egfr: float = 45.0
    max_alt_u_l: float = 120.0
    require_conditions_any: list[str] = Field(default_factory=list)
    exclude_conditions: list[str] = Field(default_factory=list)
    exclude_genomic_markers: list[str] = Field(default_factory=list)
    max_comorbidities: int = 6


class RandomizationSpec(BaseModel):
    """Deterministic stratified block randomisation."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["stratified_block", "simple"] = "stratified_block"
    strata: list[str] = Field(default_factory=lambda: ["sex", "age_band"])
    block_size: int = Field(4, ge=2)


class TrialProtocol(BaseModel):
    """A complete, validated clinical trial protocol."""

    model_config = ConfigDict(extra="forbid")

    protocol_id: str
    title: str = ""
    phase: Literal["PHASE_I", "PHASE_II", "PHASE_III", "PHASE_IV"] = "PHASE_II"
    therapeutic_area: str = "cardiology"
    indication: str = ""
    design: Literal["parallel", "crossover"] = "parallel"
    epochs: int = Field(8, ge=1, le=512, description="Number of simulated dosing epochs")
    epoch_duration_hours: float = Field(12.0, gt=0)
    washout_hours: float = Field(0.0, ge=0)
    arms: list[ArmSpec] = Field(..., min_length=1)
    drug: DrugSpec
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    stopping_rules: list[StoppingRuleSpec] = Field(default_factory=list)
    eligibility: EligibilityCriteria = Field(default_factory=EligibilityCriteria)
    randomization: RandomizationSpec = Field(default_factory=RandomizationSpec)  # type: ignore[arg-type]
    seed: int = 20240517
    version: str = PROTOCOL_SCHEMA_VERSION
    placebo_effect: dict[str, float] = Field(
        default_factory=lambda: {"sbp_mmhg": -3.5, "dbp_mmhg": -2.0, "hr_bpm": -1.0, "onset_epochs": 3.0},
        description="Non-specific (placebo/regression-to-the-mean) effect seen in all arms",
    )
    notes: str = ""

    @field_validator("arms")
    @classmethod
    def _unique_arms(cls, arms: list[ArmSpec]) -> list[ArmSpec]:
        ids = [a.arm_id for a in arms]
        if len(ids) != len(set(ids)):
            raise ValueError("arm_id values must be unique")
        return arms

    @model_validator(mode="after")
    def _design_invariants(self) -> TrialProtocol:
        if self.design == "parallel" and not any(a.is_control for a in self.arms):
            raise ValueError("a parallel-group protocol requires exactly one control arm (is_control: true)")
        controls = [a for a in self.arms if a.is_control]
        if len(controls) > 1:
            raise ValueError("at most one control arm is supported")
        if controls and controls[0].dose_mg != 0:
            raise ValueError("the control arm must receive dose_mg: 0 (placebo)")
        if not self.endpoints:
            self.endpoints = [
                EndpointSpec(
                    name="change_in_systolic_bp",
                    column="sbp_change",
                    kind="continuous",
                    direction="decrease",
                    mcid=5.0,
                    is_primary=True,
                    description="Change from baseline in systolic blood pressure at end of treatment",
                )
            ]
        if not any(e.is_primary for e in self.endpoints):
            self.endpoints[0].is_primary = True
        for endpoint in self.endpoints:
            if endpoint.kind == "binary" and endpoint.response_threshold is None:
                raise ValueError(f"binary endpoint {endpoint.name!r} requires response_threshold")
            if endpoint.epoch is not None and endpoint.epoch > self.epochs:
                raise ValueError(f"endpoint {endpoint.name!r} references epoch beyond protocol length")
        unknown = sorted({a for rule in self.stopping_rules for a in rule.applies_to_arms} - {a.arm_id for a in self.arms})
        if unknown:
            raise ValueError(f"stopping rules reference unknown arms: {unknown}")
        return self

    # -- convenience -------------------------------------------------------
    @property
    def control_arm(self) -> ArmSpec | None:
        return next((a for a in self.arms if a.is_control), None)

    @property
    def treatment_arms(self) -> list[ArmSpec]:
        return [a for a in self.arms if not a.is_control]

    @property
    def primary_endpoint(self) -> EndpointSpec:
        return next(e for e in self.endpoints if e.is_primary)

    @property
    def responder_endpoint(self) -> EndpointSpec:
        """Endpoint that defines a *responder* (a binary endpoint if declared).

        The Patient agent uses this to set the ``responder`` flag. Falling back to
        the primary endpoint's MCID keeps protocols without an explicit binary
        endpoint usable, but a declared binary endpoint always takes precedence -
        otherwise the placebo arm of a trial with MCID 5 mmHg would be counted as
        responding at a -5 mmHg change while the protocol calls -10 mmHg a response.
        """
        for endpoint in self.endpoints:
            if endpoint.kind == "binary":
                return endpoint
        return self.primary_endpoint

    def arm(self, arm_id: str) -> ArmSpec:
        for candidate in self.arms:
            if candidate.arm_id == arm_id:
                return candidate
        raise KeyError(f"unknown arm_id {arm_id!r}")

    def dose_for_epoch(self, arm_id: str, epoch: int) -> float:
        """Dose administered in ``epoch`` for ``arm_id`` (titration-aware)."""
        arm = self.arm(arm_id)
        if epoch <= 0:
            return 0.0
        dose = arm.dose_mg
        titration = arm.titration
        if titration and epoch >= titration.start_epoch:
            steps = epoch - titration.start_epoch + 1
            dose = arm.dose_mg + titration.step_mg * steps
            if titration.max_dose_mg is not None:
                dose = min(dose, titration.max_dose_mg)
        return float(round(dose, 6))

    def digest(self) -> str:
        """Stable content hash used for provenance and prompt caching."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Patient personas
# ---------------------------------------------------------------------------


class GenomicProfile(BaseModel):
    """Illustrative pharmacogenomic markers, sampled from published frequencies.

    These markers are *synthetic*. They are sampled from population priors in
    ``resources/genomic_priors.yaml`` and only used to modulate clearance and
    adverse-event risk inside the simulation.
    """

    model_config = ConfigDict(extra="forbid")

    ancestry: str = "EUR"
    cyp2d6: Literal["PM", "IM", "NM", "UM"] = "NM"
    cyp3a4: Literal["PM", "IM", "NM", "UM"] = "NM"
    hla_b_57_01: bool = False
    hla_dq2_2: bool = False
    adrb1_arg389gly: bool = False
    ace_dd: bool = False
    slco1b1_decreased: bool = False

    def risk_flags(self) -> dict[str, float]:
        """Map genotypes onto the covariates used by the AE logistic models."""
        cyp_multiplier = {"PM": 1.0, "IM": 0.5, "NM": 0.0, "UM": -0.5}[self.cyp2d6]
        return {
            "cyp2d6_pm": 1.0 if self.cyp2d6 == "PM" else 0.0,
            "cyp3a4_pm": 1.0 if self.cyp3a4 == "PM" else 0.0,
            "hla_b_57_01": 1.0 if self.hla_b_57_01 else 0.0,
            "hla_dq2_2": 1.0 if self.hla_dq2_2 else 0.0,
            "slco1b1_decreased": 1.0 if self.slco1b1_decreased else 0.0,
            "cyp2d6_exposure_multiplier": 1.0 + 0.35 * cyp_multiplier,
        }


class PatientProfile(BaseModel):
    """Digital twin of one synthetic patient (a Persona agent's genome + history)."""

    model_config = ConfigDict(extra="forbid")

    patient_id: str
    cohort_id: str
    age: float
    sex: Literal["F", "M"]
    weight_kg: float
    height_cm: float
    bmi: float
    baseline_sbp: float
    baseline_dbp: float
    baseline_hr: float
    egfr: float = Field(..., description="Estimated GFR, mL/min/1.73m2")
    alt_u_l: float
    ast_u_l: float
    creatinine_mg_dl: float
    comorbidities: list[str] = Field(default_factory=list)
    concomitant_meds: list[str] = Field(default_factory=list)
    genomic: GenomicProfile = Field(default_factory=GenomicProfile)
    medical_history: str = ""
    smoking: Literal["never", "former", "current"] = "never"
    alcohol_units_week: float = 0.0
    site_id: str = "SITE-01"
    enrolled: bool = True
    screen_failure_reason: str = ""

    @property
    def comorbidity_count(self) -> int:
        return len(self.comorbidities)

    @property
    def age_band(self) -> str:
        if self.age < 40:
            return "18-39"
        if self.age < 60:
            return "40-59"
        return "60+"

    def persona_text(self) -> str:
        """Compact natural-language persona - the LLM prompt's ``{history}`` slot."""
        if self.medical_history:
            return self.medical_history
        history = ", ".join(self.comorbidities) if self.comorbidities else "no significant history"
        meds = ", ".join(self.concomitant_meds) if self.concomitant_meds else "none"
        return (
            f"{int(self.age)}-year-old {'female' if self.sex == 'F' else 'male'} "
            f"({self.bmi:.1f} kg/m2, {self.smoking} smoker) with {history}; "
            f"concomitant medication: {meds}; eGFR {self.egfr:.0f} mL/min/1.73m2."
        )


class DoseDirective(BaseModel):
    """Instruction issued by the Protocol Agent to one Patient agent for one epoch."""

    model_config = ConfigDict(extra="forbid")

    directive_id: str
    patient_id: str
    arm_id: str
    epoch: int
    dose_mg: float
    time_hours: float
    instruction: str = ""


# ---------------------------------------------------------------------------
# Agent outputs
# ---------------------------------------------------------------------------


class SymptomReport(BaseModel):
    """One qualitative symptom reported by a Patient Persona agent (LLM output)."""

    model_config = ConfigDict(extra="forbid")

    term: str
    ctcae_grade: int = Field(1, ge=1, le=5)
    verbatim: str = ""
    source: Literal["llm", "model", "hybrid", "offline"] = "llm"


class AdverseEventRecord(BaseModel):
    """Structured adverse event - the joint product of the ML model and the LLM."""

    model_config = ConfigDict(extra="forbid")

    ae_id: str
    patient_id: str
    arm_id: str
    epoch: int
    term: str
    soc: str = "General disorders"
    ctcae_grade: int = Field(1, ge=1, le=5)
    serious: bool = False
    relatedness: Literal["not_related", "unlikely", "possible", "probable", "definite"] = "possible"
    predicted_probability: float = 0.0
    reported_verbatim: str = ""
    source: Literal["model", "llm", "hybrid", "offline"] = "model"

    @property
    def is_grade3_plus(self) -> bool:
        return self.ctcae_grade >= 3


class LLMInvocationMeta(BaseModel):
    """Telemetry captured for one LLM call (feeds MLflow Tracing)."""

    model_config = ConfigDict(extra="allow")

    provider: str = "mock"
    model: str = "mock-llm"
    prompt_hash: str = ""
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: bool = False
    attempts: int = 1
    error: str = ""


# ---------------------------------------------------------------------------
# Silver table contract (hot path)
# ---------------------------------------------------------------------------

#: Canonical column order/types of ``silver.patient_states``.
SILVER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("observation_id", "string"),
    ("sim_run_id", "string"),
    ("protocol_id", "string"),
    ("protocol_version", "string"),
    ("patient_id", "string"),
    ("cohort_id", "string"),
    ("site_id", "string"),
    ("arm_id", "string"),
    ("arm_label", "string"),
    ("epoch", "int"),
    ("time_hours", "double"),
    ("dose_mg", "double"),
    ("plasma_conc_mg_l", "double"),
    ("auc_epoch_mg_h_l", "double"),
    ("cumulative_exposure", "double"),
    ("sbp", "double"),
    ("dbp", "double"),
    ("hr", "double"),
    ("qtc_ms", "double"),
    ("alt_u_l", "double"),
    ("ast_u_l", "double"),
    ("creatinine_mg_dl", "double"),
    ("egfr", "double"),
    ("sbp_change", "double"),
    ("dbp_change", "double"),
    ("hr_change", "double"),
    ("biomarker_composite", "double"),
    ("responder", "bool"),
    ("worst_ctcae_grade", "int"),
    ("n_adverse_events", "int"),
    ("adverse_events_json", "string"),
    ("symptoms_json", "string"),
    ("symptom_summary", "string"),
    ("discontinued", "bool"),
    ("discontinuation_reason", "string"),
    ("adherence_intent", "string"),
    ("llm_used", "bool"),
    ("llm_provider", "string"),
    ("llm_model", "string"),
    ("llm_latency_ms", "double"),
    ("llm_tokens_in", "int"),
    ("llm_tokens_out", "int"),
    ("llm_cache_hit", "bool"),
    ("llm_attempts", "int"),
    ("llm_error", "string"),
    ("prompt_hash", "string"),
    ("physiology_model_version", "string"),
    ("physiology_backend", "string"),
    ("rng_seed", "long"),
    ("observation_ts", "string"),
)

SILVER_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in SILVER_COLUMNS)


@dataclass(slots=True)
class PatientStateObservation:
    """One simulated patient-epoch observation (a row of ``silver.patient_states``)."""

    observation_id: str
    sim_run_id: str
    protocol_id: str
    protocol_version: str
    patient_id: str
    cohort_id: str
    site_id: str
    arm_id: str
    arm_label: str
    epoch: int
    time_hours: float
    dose_mg: float
    plasma_conc_mg_l: float
    auc_epoch_mg_h_l: float
    cumulative_exposure: float
    sbp: float
    dbp: float
    hr: float
    qtc_ms: float
    alt_u_l: float
    ast_u_l: float
    creatinine_mg_dl: float
    egfr: float
    sbp_change: float
    dbp_change: float
    hr_change: float
    biomarker_composite: float
    responder: bool
    worst_ctcae_grade: int
    n_adverse_events: int
    adverse_events_json: str = "[]"
    symptoms_json: str = "[]"
    symptom_summary: str = ""
    discontinued: bool = False
    discontinuation_reason: str = ""
    adherence_intent: str = "continue"
    llm_used: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    llm_latency_ms: float = 0.0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cache_hit: bool = False
    llm_attempts: int = 1
    llm_error: str = ""
    prompt_hash: str = ""
    physiology_model_version: str = "mechanistic-v1"
    physiology_backend: str = "mechanistic"
    rng_seed: int = 0
    observation_ts: str = ""

    def as_row(self) -> dict[str, Any]:
        """Return a dict in the canonical :data:`SILVER_COLUMNS` order."""
        record = asdict(self)
        return {name: record.get(name) for name in SILVER_COLUMN_NAMES}

    def adverse_events(self) -> list[dict[str, Any]]:
        return json.loads(self.adverse_events_json or "[]")

    def symptoms(self) -> list[dict[str, Any]]:
        return json.loads(self.symptoms_json or "[]")


# ---------------------------------------------------------------------------
# Reporting / provenance payloads
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SimulationProvenance:
    """Everything needed to replay a simulation bit-for-bit."""

    sim_run_id: str
    protocol_id: str
    protocol_digest: str
    protocol_version: str
    package_version: str
    git_revision: str
    engine_backend: str
    spark_version: str = ""
    master_url: str = ""
    seed: int = 0
    n_patients: int = 0
    n_cohorts: int = 0
    n_epochs: int = 0
    physiology_model_version: str = "mechanistic-v1"
    physiology_model_digest: str = ""
    physiology_backend: str = "mechanistic"
    llm_provider: str = "offline"
    llm_model: str = ""
    llm_temperature: float = 0.0
    llm_mode: str = "off"
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    total_llm_calls: int = 0
    llm_cache_hits: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    storage_backend: str = "local"
    silver_location: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArmSummary:
    """Per-arm aggregate produced by the Biostatistician Agent."""

    arm_id: str
    label: str
    n_patients: int
    n_observations: int
    dose_mg: float
    mean_sbp_change: float
    sd_sbp_change: float
    mean_dbp_change: float
    mean_hr_change: float
    responder_rate: float
    ae_rate: float
    grade3_plus_rate: float
    sae_rate: float
    mortality_rate: float
    mean_alt_ratio: float
    discontinuations: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ComparisonResult:
    """Treatment-vs-control inferential result for one endpoint."""

    endpoint: str
    arm_id: str
    control_arm_id: str
    metric: str
    effect_estimate: float
    ci_low: float
    ci_high: float
    p_value: float
    test: str
    n_treatment: int
    n_control: int
    mcid: float | None = None
    meets_mcid: bool = False
    q_value: float | None = None


@dataclass(slots=True)
class StoppingRuleEvaluation:
    """Outcome of one safety stopping rule at one epoch."""

    rule_id: str
    epoch: int
    arm_id: str
    metric: str
    observed: float
    threshold: float
    comparator: str
    triggered: bool
    action: str
    n_at_risk: int = 0
