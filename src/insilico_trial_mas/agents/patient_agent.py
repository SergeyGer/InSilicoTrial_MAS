"""Synthetic Patient Persona Agent - the heart of the multi-agent system.

For every simulated epoch the agent:

1. receives a :class:`~insilico_trial_mas.schemas.DoseDirective` from the Protocol Agent,
2. computes individual PK and exposure (:mod:`insilico_trial_mas.ml.pk_pd`),
3. predicts physiology and adverse-event probabilities with the hybrid
   mechanistic + learned model (:mod:`insilico_trial_mas.ml.physiology`),
4. samples structured adverse events,
5. *narrates* the experience with an LLM persona when the configured trigger
   fires, and merges the qualitative symptoms with the modelled events,
6. applies adherence logic (dose hold / discontinuation) and emits one Silver
   observation row.

Compared with the specification's blueprint this implementation fixes:

* ``simulated_bp = 120 + dose * 0.1`` -> full PK/PD with placebo, progression and genetics,
* unbounded ``asyncio.gather`` -> bounded concurrency (:mod:`insilico_trial_mas.async_utils`),
* errors swallowed into a free-text column -> typed ``llm_error`` plus a
  deterministic offline fallback,
* no seeding -> every stochastic draw derived from ``(seed, run, patient, epoch)``,
* an ``epoch`` column that no loop ever produced -> an explicit baseline + dosing timeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import LLMRequest, LLMResponse
from ..llm.prompts import build_patient_prompt, parse_symptom_response
from ..logging_utils import error_kind
from ..ml.physiology import PhysiologyPrediction, sample_adverse_events
from ..ml.pk_pd import ExposureMetrics, derive_pk_parameters, exposure_for_epoch
from ..reproducibility import rng_for, stable_hash
from ..schemas import (
    AdverseEventRecord,
    DoseDirective,
    PatientProfile,
    PatientStateObservation,
    SymptomReport,
)
from .base import AgentContext, BaseAgent, utc_now_iso

MAX_LLM_SYMPTOMS = 5
#: Hard budget: personas narrate at most this many epochs each, whatever the mode.
MAX_LLM_CALLS_PER_PATIENT = 6

#: Endpoint column -> attribute of :class:`PhysiologyPrediction` (or baseline delta).
ENDPOINT_VALUE_RESOLVERS: dict[str, str] = {
    "sbp_change": "delta_sbp",
    "dbp_change": "delta_dbp",
    "hr_change": "delta_hr",
    "biomarker_composite": "biomarker_composite",
    "alt_u_l": "alt_u_l",
    "sbp": "sbp",
    "dbp": "dbp",
    "hr": "hr",
    "qtc_ms": "qtc_ms",
    "egfr": "egfr",
}


@dataclass(slots=True)
class Narration:
    """Outcome of one persona LLM call (symptoms plus adherence signals)."""

    symptoms: list[SymptomReport] = field(default_factory=list)
    response: LLMResponse | None = None
    error: str = ""
    tolerability: str = "acceptable"
    adherence_intent: str = "continue"


@dataclass(slots=True)
class PatientRunOutcome:
    """Everything one Patient agent produced during a run."""

    observations: list[PatientStateObservation] = field(default_factory=list)
    adverse_events: list[dict[str, Any]] = field(default_factory=list)
    symptoms: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: int = 0
    llm_errors: int = 0
    llm_cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    discontinued_at_epoch: int | None = None
    discontinuation_reason: str = ""
    worst_ctcae_grade: int = 0

    def rows(self) -> list[dict[str, Any]]:
        return [obs.as_row() for obs in self.observations]


class PatientPersonaAgent(BaseAgent):
    """One digital patient: phenotype in, epoch-by-epoch observations out."""

    role = "patient"

    def __init__(
        self,
        profile: PatientProfile,
        context: AgentContext,
        runtime: Any,  # WorkerRuntime - untyped to avoid an import cycle
        *,
        protocol_agent: Any | None = None,
    ) -> None:
        super().__init__(context)
        self.profile = profile
        self.runtime = runtime
        self.protocol_agent = protocol_agent or runtime.protocol_agent
        self.protocol = context.protocol
        self.config = context.config
        self.arm_id = context.arm_of(profile.patient_id)
        self.arm = self.protocol.arm(self.arm_id)
        self.pk = derive_pk_parameters(profile, self.protocol.drug)
        self.cumulative_auc = 0.0
        self.observation_ts = utc_now_iso()
        self._llm_calls = 0

    # -- public API --------------------------------------------------------
    async def run(self) -> PatientRunOutcome:
        """Simulate every epoch for this patient (baseline epoch 0 included)."""
        outcome = PatientRunOutcome()
        epochs = self.config.epochs or self.protocol.epochs
        start_epoch = 0 if self.config.include_baseline_epoch else 1
        discontinue_from: int | None = None

        for epoch in range(start_epoch, epochs + 1):
            directive: DoseDirective = self.protocol_agent.directive(self.profile, epoch, arm_id=self.arm_id)
            withheld = discontinue_from is not None and epoch >= discontinue_from
            dose_mg = 0.0 if withheld and epoch > 0 else directive.dose_mg

            exposure = exposure_for_epoch(
                self.pk,
                self.protocol.drug,
                dose_mg=dose_mg,
                epoch=epoch,
                tau_h=self.protocol.epoch_duration_hours,
                cumulative_auc=self.cumulative_auc,
            )
            self.cumulative_auc = exposure.cumulative_auc_mg_h_l

            prediction = self.runtime.model.predict(
                self.profile,
                self.protocol.drug,
                exposure,
                self.pk,
                epoch=epoch,
                epochs_total=epochs,
                placebo_effect=self.protocol.placebo_effect,
                seed=self.context.seed,
            )
            events = [
                AdverseEventRecord.model_validate(event)
                for event in sample_adverse_events(
                    self.profile,
                    self.protocol.drug,
                    prediction,
                    epoch=epoch,
                    seed=self.context.seed,
                    run_id=self.run_id,
                )
            ]
            for event in events:
                event.arm_id = self.arm_id

            observation = self._build_observation(epoch, directive.time_hours, dose_mg, exposure, prediction, events)
            if withheld and epoch > 0:
                observation.discontinued = True
                observation.discontinuation_reason = outcome.discontinuation_reason

            worst_grade = observation.worst_ctcae_grade
            if self._should_consult_llm(epoch, worst_grade):
                narration = await self._narrate(observation, prediction, worst_grade)
                self._apply_narration(observation, narration)
                symptoms = narration.symptoms
                response = narration.response
                outcome.symptoms.extend(symptom.model_dump(mode="json") for symptom in symptoms)
                if response is not None:
                    outcome.llm_calls += 1
                    outcome.llm_cache_hits += int(response.cache_hit)
                    outcome.tokens_in += response.tokens_in
                    outcome.tokens_out += response.tokens_out
                if narration.error:
                    outcome.llm_errors += 1

            outcome.observations.append(observation)
            outcome.adverse_events.extend(observation.adverse_events())
            outcome.worst_ctcae_grade = max(outcome.worst_ctcae_grade, observation.worst_ctcae_grade)

            if discontinue_from is None:
                reason = self._discontinuation_reason(observation)
                if reason:
                    discontinue_from = epoch + 1
                    outcome.discontinued_at_epoch = epoch
                    outcome.discontinuation_reason = reason
                    observation.discontinued = True
                    observation.discontinuation_reason = reason
                    self.runtime.protocol_agent.protocol_deviation(self.profile, epoch, reason)

        return outcome

    # -- epoch construction ------------------------------------------------
    def _build_observation(
        self,
        epoch: int,
        time_hours: float,
        dose_mg: float,
        exposure: ExposureMetrics,
        prediction: PhysiologyPrediction,
        adverse_events: list[AdverseEventRecord],
    ) -> PatientStateObservation:
        profile = self.profile
        responder_definition = self.protocol.responder_endpoint
        worst_grade = max((event.ctcae_grade for event in adverse_events), default=0)
        return PatientStateObservation(
            observation_id=f"{profile.patient_id}:{epoch:03d}",
            sim_run_id=self.run_id,
            protocol_id=self.protocol.protocol_id,
            protocol_version=self.protocol.version,
            patient_id=profile.patient_id,
            cohort_id=profile.cohort_id,
            site_id=profile.site_id,
            arm_id=self.arm_id,
            arm_label=self.arm.label,
            epoch=epoch,
            time_hours=time_hours,
            dose_mg=dose_mg,
            plasma_conc_mg_l=round(exposure.c_avg_mg_l, 6),
            auc_epoch_mg_h_l=round(exposure.auc_epoch_mg_h_l, 6),
            cumulative_exposure=round(exposure.cumulative_auc_mg_h_l, 6),
            sbp=round(prediction.sbp, 2),
            dbp=round(prediction.dbp, 2),
            hr=round(prediction.hr, 2),
            qtc_ms=round(prediction.qtc_ms, 2),
            alt_u_l=round(prediction.alt_u_l, 2),
            ast_u_l=round(prediction.ast_u_l, 2),
            creatinine_mg_dl=round(prediction.creatinine_mg_dl, 4),
            egfr=round(prediction.egfr, 2),
            sbp_change=round(prediction.sbp - profile.baseline_sbp, 2),
            dbp_change=round(prediction.dbp - profile.baseline_dbp, 2),
            hr_change=round(prediction.hr - profile.baseline_hr, 2),
            biomarker_composite=round(prediction.biomarker_composite, 4),
            responder=self._is_responder(prediction, epoch, responder_definition),
            worst_ctcae_grade=worst_grade,
            n_adverse_events=len(adverse_events),
            adverse_events_json=json.dumps([event.model_dump(mode="json") for event in adverse_events]),
            rng_seed=self.context.seed,
            physiology_model_version=prediction.model_version,
            physiology_backend=prediction.backend,
            observation_ts=self.observation_ts,
        )

    def _is_responder(self, prediction: PhysiologyPrediction, epoch: int, endpoint: Any) -> bool:
        """Evaluate the primary endpoint's response definition at the evaluation epoch."""
        eval_epoch = endpoint.epoch or (self.config.epochs or self.protocol.epochs)
        if epoch != eval_epoch:
            return False
        resolver = ENDPOINT_VALUE_RESOLVERS.get(endpoint.column)
        if resolver is None:
            return False
        if resolver == "delta_sbp":
            value = prediction.sbp - self.profile.baseline_sbp
        elif resolver == "delta_dbp":
            value = prediction.dbp - self.profile.baseline_dbp
        elif resolver == "delta_hr":
            value = prediction.hr - self.profile.baseline_hr
        else:
            value = float(getattr(prediction, resolver))
        if endpoint.kind == "binary" and endpoint.response_threshold is not None:
            threshold = endpoint.response_threshold
            if endpoint.direction == "decrease":
                return bool(value <= threshold)
            if endpoint.direction == "increase":
                return bool(value >= threshold)
            return bool(abs(value) >= abs(threshold))
        mcid = endpoint.mcid if endpoint.mcid is not None else 0.0
        if endpoint.direction == "decrease":
            return bool(value <= -abs(mcid))
        if endpoint.direction == "increase":
            return bool(value >= abs(mcid))
        return bool(abs(value) >= abs(mcid))

    # -- LLM policy --------------------------------------------------------
    def _should_consult_llm(self, epoch: int, worst_grade: int) -> bool:
        """Decide whether the persona narrates this epoch (cost control)."""
        if epoch == 0 or self.config.llm_mode == "off":
            return False
        if self._llm_calls >= MAX_LLM_CALLS_PER_PATIENT:
            return False
        mode = self.config.llm_mode
        if mode == "all":
            return True
        if mode == "triggered" and worst_grade >= self.config.llm_trigger_grade:
            return True
        if mode in {"triggered", "sample"}:
            rng = rng_for(self.context.seed, self.run_id, self.profile.patient_id, epoch, "llm-sample")
            return bool(rng.random() < self.config.llm_sample_rate)
        return False

    def _discontinuation_reason(self, observation: PatientStateObservation) -> str:
        """Adherence rule: grade >= 4 toxicity, or the persona asks to stop."""
        if observation.epoch == 0:
            return ""
        if observation.worst_ctcae_grade >= 4:
            return f"CTCAE grade {observation.worst_ctcae_grade} adverse event"
        if observation.llm_used and observation.adherence_intent == "discontinue":
            return "patient withdrew consent after reported symptoms"
        return ""

    async def _narrate(
        self, observation: PatientStateObservation, prediction: PhysiologyPrediction, worst_grade: int
    ) -> Narration:
        """One LLM call: build the prompt, invoke the provider, parse the answer."""
        prompt = build_patient_prompt(
            self.profile,
            arm_label=self.arm.label,
            dose_mg=observation.dose_mg,
            epoch=observation.epoch,
            sbp=observation.sbp,
            dbp=observation.dbp,
            hr=observation.hr,
            qtc_ms=observation.qtc_ms,
            alt_u_l=observation.alt_u_l,
            ae_probabilities=prediction.ae_probabilities,
        )
        request = LLMRequest(
            prompt=prompt.user,
            system=prompt.system,
            context={**prompt.context, "worst_grade": worst_grade, "prompt_hash": prompt.prompt_hash},
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
            timeout_seconds=self.config.llm.timeout_seconds,
        )
        self._llm_calls += 1
        try:
            response = await self.runtime.llm.acomplete(request)
        except Exception as exc:
            # Log and store a fixed label, never the provider message: SDK errors can
            # echo the prompt or the API key (CodeQL py/clear-text-logging-sensitive-data).
            # The data-quality audit only needs to know *that* narration failed and how.
            kind = error_kind(exc)
            self.log.warning(
                f"LLM narration failed for {self.profile.patient_id} epoch {observation.epoch}: {kind}"
            )
            return Narration(symptoms=[], response=None, error=kind)
        # Keep the prompt hash with the response so the Silver row can be traced
        # back to the exact prompt that produced it.
        response.raw.setdefault("prompt_hash", prompt.prompt_hash)
        parsed = parse_symptom_response(response.text, source="llm")
        error = response.error or parsed.parse_error
        self._record_span(observation, response, prompt.prompt_hash, error)
        return Narration(
            symptoms=parsed.symptoms,
            response=response,
            error=error,
            tolerability=parsed.tolerability,
            adherence_intent=parsed.adherence_intent,
        )

    def _record_span(
        self,
        observation: PatientStateObservation,
        response: LLMResponse,
        prompt_hash: str,
        error: str,
    ) -> None:
        """Emit an MLflow-Tracing-style span for one persona call (best effort)."""
        recorder = getattr(self.runtime, "trace", None)
        if recorder is None:
            return
        try:
            recorder.record_llm_call(
                patient_id=self.profile.patient_id,
                epoch=observation.epoch,
                provider=response.provider,
                model=response.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                duration_ms=response.latency_ms,
                cache_hit=response.cache_hit,
                prompt_hash=prompt_hash,
                error=error,
            )
        except Exception as exc:
            self.log.debug(f"could not record trace span: {error_kind(exc)}")

    def _apply_narration(self, observation: PatientStateObservation, narration: Narration) -> None:
        """Merge the qualitative narration into the structured observation."""
        symptoms = narration.symptoms
        response = narration.response
        events = observation.adverse_events()
        by_term = {str(event["term"]).strip().lower(): event for event in events}

        for symptom in symptoms[:MAX_LLM_SYMPTOMS]:
            key = symptom.term.strip().lower()
            existing = by_term.get(key)
            if existing is not None:
                # The persona may escalate the severity reported by the model.
                if symptom.ctcae_grade > int(existing["ctcae_grade"]):
                    existing["ctcae_grade"] = symptom.ctcae_grade
                    existing["serious"] = bool(existing.get("serious")) or symptom.ctcae_grade >= 3
                    existing["source"] = "hybrid"
                if symptom.verbatim:
                    existing["reported_verbatim"] = symptom.verbatim
            else:
                # A term the drug model does not contain: keep it, flagged as LLM-only.
                events.append(
                    {
                        "ae_id": stable_hash(self.run_id, self.profile.patient_id, observation.epoch, key, length=20),
                        "patient_id": self.profile.patient_id,
                        "arm_id": self.arm_id,
                        "epoch": observation.epoch,
                        "term": symptom.term,
                        "soc": "Reported by patient (LLM)",
                        "ctcae_grade": symptom.ctcae_grade,
                        "serious": symptom.ctcae_grade >= 3,
                        "relatedness": "possible",
                        "predicted_probability": 0.0,
                        "reported_verbatim": symptom.verbatim,
                        "source": "llm",
                    }
                )

        observation.adverse_events_json = json.dumps(events)
        observation.symptoms_json = json.dumps([s.model_dump(mode="json") for s in symptoms])
        observation.symptom_summary = "; ".join(
            f"{s.term} (G{s.ctcae_grade})" for s in sorted(symptoms, key=lambda s: -s.ctcae_grade)
        )[:480]
        observation.n_adverse_events = len(events)
        observation.worst_ctcae_grade = max([int(event["ctcae_grade"]) for event in events] + [0])

        if response is not None:
            observation.llm_used = True
            observation.llm_provider = response.provider
            observation.llm_model = response.model
            observation.llm_latency_ms = round(response.latency_ms, 3)
            observation.llm_tokens_in = response.tokens_in
            observation.llm_tokens_out = response.tokens_out
            observation.llm_cache_hit = response.cache_hit
            observation.llm_attempts = response.attempts
            observation.prompt_hash = str((response.raw or {}).get("prompt_hash", ""))
            # Adherence is part of the persona's answer: without this the
            # "patient withdrew consent" branch could never trigger.
            observation.adherence_intent = narration.adherence_intent
        observation.llm_error = narration.error


async def simulate_patient(profile: PatientProfile, context: AgentContext, runtime: Any) -> PatientRunOutcome:
    """Convenience coroutine: build and run one Patient agent."""
    return await PatientPersonaAgent(profile, context, runtime).run()


__all__ = ["PatientPersonaAgent", "PatientRunOutcome", "simulate_patient"]
