"""Protocol Agent - the trial organiser.

Responsibilities (specification section 1):

* validate the protocol before any compute is spent,
* screen the synthetic cohort against the eligibility criteria (with an auditable
  CONSORT-style screen-failure log),
* randomise eligible patients with deterministic, stratified, permuted-block
  allocation,
* issue per-epoch dosing directives (titration aware),
* record protocol deviations (dose holds after severe toxicity).

All randomness is derived from :func:`~insilico_trial_mas.reproducibility.rng_for`,
so allocation is identical regardless of execution engine or partition order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ProtocolValidationError
from ..reproducibility import rng_for
from ..schemas import DoseDirective, PatientProfile
from ..version import PROTOCOL_SCHEMA_VERSION
from .base import AgentContext, BaseAgent


@dataclass(slots=True)
class ScreeningOutcome:
    """Result of applying the eligibility criteria to the screening population."""

    enrolled: list[PatientProfile] = field(default_factory=list)
    screen_failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n_screened(self) -> int:
        return len(self.enrolled) + len(self.screen_failures)

    @property
    def screen_failure_rate(self) -> float:
        return len(self.screen_failures) / self.n_screened if self.n_screened else 0.0

    def summary(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for failure in self.screen_failures:
            reasons[failure["reason"]] = reasons.get(failure["reason"], 0) + 1
        return {
            "screened": self.n_screened,
            "enrolled": len(self.enrolled),
            "screen_failures": len(self.screen_failures),
            "screen_failure_rate": round(self.screen_failure_rate, 4),
            "failure_reasons": reasons,
        }


@dataclass(slots=True)
class AllocationOutcome:
    """Randomisation result with the audit table required by ICH E9."""

    arm_by_patient: dict[str, str] = field(default_factory=dict)
    allocation_table: list[dict[str, Any]] = field(default_factory=list)

    def arm_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for arm_id in self.arm_by_patient.values():
            counts[arm_id] = counts.get(arm_id, 0) + 1
        return counts


class ProtocolAgent(BaseAgent):
    """Organises the trial: validation, screening, randomisation, dosing."""

    role = "protocol"

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        self.protocol = context.protocol

    # -- validation --------------------------------------------------------
    def validate(self) -> list[str]:
        """Return human-readable validation warnings (raises on hard errors)."""
        protocol = self.protocol
        warnings: list[str] = []
        if protocol.version != PROTOCOL_SCHEMA_VERSION:
            warnings.append(
                f"protocol schema version {protocol.version} differs from the supported {PROTOCOL_SCHEMA_VERSION}"
            )
        if not protocol.drug.ae_models:
            warnings.append("drug defines no adverse-event models: safety analysis will be empty")
        placebo_effect = protocol.placebo_effect
        if abs(float(placebo_effect.get("sbp_mmhg", 0.0))) > abs(protocol.drug.emax.sbp_mmhg):
            warnings.append("placebo effect is larger than the maximal drug effect: the trial is unlikely to succeed")
        for arm in protocol.treatment_arms:
            if arm.dose_mg <= 0:
                raise ProtocolValidationError(f"treatment arm {arm.arm_id!r} has a non-positive dose")
            if arm.titration and arm.titration.max_dose_mg and arm.titration.max_dose_mg < arm.dose_mg:
                raise ProtocolValidationError(
                    f"arm {arm.arm_id!r}: titration max_dose_mg is below the starting dose_mg"
                )
        if protocol.epoch_duration_hours * protocol.epochs > 24 * 365:
            warnings.append("protocol duration exceeds one year; check epoch_duration_hours")
        if not protocol.stopping_rules:
            warnings.append("no safety stopping rules defined; the Data Safety Monitoring Board has no trigger")
        return warnings

    # -- screening ---------------------------------------------------------
    def screen(self, profiles: list[PatientProfile]) -> ScreeningOutcome:
        """Apply the eligibility criteria and record every screen failure."""
        outcome = ScreeningOutcome()
        for profile in profiles:
            reason = self._screen_failure_reason(profile)
            if reason:
                outcome.screen_failures.append(
                    {
                        "patient_id": profile.patient_id,
                        "cohort_id": profile.cohort_id,
                        "reason": reason,
                        "age": profile.age,
                        "egfr": profile.egfr,
                        "alt_u_l": profile.alt_u_l,
                        "comorbidities": len(profile.comorbidities),
                    }
                )
                profile.enrolled = False
                profile.screen_failure_reason = reason
            else:
                profile.enrolled = True
                outcome.enrolled.append(profile)
        self.log.info(
            f"screening complete: {len(outcome.enrolled)}/{outcome.n_screened} enrolled "
            f"({outcome.screen_failure_rate:.1%} screen failures)"
        )
        return outcome

    def _screen_failure_reason(self, profile: PatientProfile) -> str:
        criteria = self.protocol.eligibility
        if not criteria.min_age <= profile.age <= criteria.max_age:
            return "age_out_of_range"
        if profile.egfr < criteria.min_egfr:
            return "renal_impairment"
        if profile.alt_u_l > criteria.max_alt_u_l:
            return "hepatic_impairment"
        if profile.comorbidity_count > criteria.max_comorbidities:
            return "comorbidity_burden"
        excluded = set(criteria.exclude_conditions)
        if excluded & set(profile.comorbidities):
            return "excluded_condition"
        required = set(criteria.require_conditions_any)
        if required and not (required & set(profile.comorbidities)):
            return "required_condition_absent"
        for marker in criteria.exclude_genomic_markers:
            if getattr(profile.genomic, marker, False):
                return f"excluded_genotype_{marker}"
        return ""

    # -- randomisation -----------------------------------------------------
    def randomize(self, enrolled: list[PatientProfile]) -> AllocationOutcome:
        """Deterministic stratified permuted-block randomisation."""
        outcome = AllocationOutcome()
        strata: dict[str, list[PatientProfile]] = {}
        for profile in enrolled:
            strata.setdefault(self._stratum_key(profile), []).append(profile)

        arms = self.protocol.arms
        weights = {arm.arm_id: arm.allocation_weight for arm in arms}
        block_size = max(len(arms), self.protocol.randomization.block_size)
        if block_size % len(arms) != 0:
            # Round the block size up to a multiple of the arm count so blocks are balanced.
            block_size = len(arms) * max(1, round(block_size / len(arms)))
        self.log.info(
            f"randomising {len(enrolled)} patients over {len(strata)} strata, block size {block_size}"
        )

        for stratum in sorted(strata):
            members = sorted(strata[stratum], key=lambda p: p.patient_id)
            rng = rng_for(self.context.seed, self.protocol.protocol_id, "randomisation", stratum)
            for start in range(0, len(members), block_size):
                block = members[start : start + block_size]
                labels = self._block_labels(weights, len(block), rng)
                for profile, arm_id in zip(block, labels, strict=True):
                    outcome.arm_by_patient[profile.patient_id] = arm_id
                    outcome.allocation_table.append(
                        {
                            "patient_id": profile.patient_id,
                            "stratum": stratum,
                            "block_index": start // block_size,
                            "arm_id": arm_id,
                            "site_id": profile.site_id,
                            "cohort_id": profile.cohort_id,
                        }
                    )
        self.context.arm_by_patient.update(outcome.arm_by_patient)
        return outcome

    def _stratum_key(self, profile: PatientProfile) -> str:
        parts: list[str] = []
        for field_name in self.protocol.randomization.strata:
            if field_name == "age_band":
                parts.append(profile.age_band)
            elif field_name == "site_id":
                parts.append(profile.site_id)
            elif field_name == "cohort_id":
                parts.append(profile.cohort_id)
            elif field_name == "ancestry":
                parts.append(profile.genomic.ancestry)
            elif field_name == "comorbidity_count":
                parts.append(str(min(profile.comorbidity_count, 4)))
            else:
                parts.append(str(getattr(profile, field_name, "NA")))
        return "|".join(parts) if parts else "ALL"

    @staticmethod
    def _block_labels(weights: dict[str, float], size: int, rng) -> list[str]:
        """Largest-remainder allocation of ``size`` slots, then a random permutation."""
        total = sum(weights.values())
        exact = {arm: size * w / total for arm, w in weights.items()}
        counts = {arm: int(value) for arm, value in exact.items()}
        remainder = size - sum(counts.values())
        if remainder > 0:
            order = sorted(exact, key=lambda arm: (-(exact[arm] - counts[arm]), arm))
            for arm in order[:remainder]:
                counts[arm] += 1
        labels = [arm for arm, count in counts.items() for _ in range(count)]
        rng.shuffle(labels)
        return labels

    # -- dosing ------------------------------------------------------------
    def directive(self, profile: PatientProfile, epoch: int, *, arm_id: str | None = None) -> DoseDirective:
        """Build the dosing directive for one patient-epoch."""
        resolved_arm = arm_id or self.context.arm_of(profile.patient_id)
        dose = self.protocol.dose_for_epoch(resolved_arm, epoch)
        arm = self.protocol.arm(resolved_arm)
        instruction = (
            "withhold study drug (placebo arm)"
            if arm.is_control
            else f"administer {dose:g} mg {self.protocol.drug.route.upper()}"
        )
        return DoseDirective(
            directive_id=f"{profile.patient_id}-E{epoch:03d}",
            patient_id=profile.patient_id,
            arm_id=resolved_arm,
            epoch=epoch,
            dose_mg=dose,
            time_hours=epoch * self.protocol.epoch_duration_hours,
            instruction=instruction,
        )

    def protocol_deviation(self, profile: PatientProfile, epoch: int, reason: str) -> dict[str, Any]:
        """Record a deviation (dose hold, discontinuation) for the audit trail."""
        return {
            "patient_id": profile.patient_id,
            "epoch": epoch,
            "arm_id": self.context.arm_of(profile.patient_id),
            "reason": reason,
            "run_id": self.run_id,
        }

    # -- convenience -------------------------------------------------------
    def plan(self, profiles: list[PatientProfile]) -> tuple[ScreeningOutcome, AllocationOutcome]:
        """Screen + randomise in one call (used by the driver-side orchestration)."""
        warnings = self.validate()
        for warning in warnings:
            self.log.warning(f"protocol check: {warning}")
        screening = self.screen(profiles)
        allocation = self.randomize(screening.enrolled)
        return screening, allocation
