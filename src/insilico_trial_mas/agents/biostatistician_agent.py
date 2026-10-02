"""Biostatistician Agent - aggregation, inference and safety monitoring.

The specification asks this agent to "aggregate logs, monitor adverse reactions
and auto-generate compliance-ready medical reports". It therefore owns:

* transformation of the Silver observation log into analysis-ready frames
  (end-of-treatment per patient, and a long adverse-event table),
* per-arm descriptive statistics with confidence intervals,
* treatment-vs-control inference for the primary and secondary endpoints
  (Welch t-test, bootstrap CI, Cohen's d, Newcombe risk difference, Fisher exact)
  with Benjamini-Hochberg control across the secondary family,
* CTCAE-grade safety tabulation and Data Safety Monitoring Board (DSMB) stopping
  rule evaluation per epoch,
* a data-quality and reproducibility audit that the report embeds.

Every statistic is computed with :mod:`insilico_trial_mas.stats`, i.e. without a
SciPy dependency, so the same numbers appear on a laptop, in CI and on a cluster.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..logging_utils import get_logger
from ..schemas import (
    ArmSummary,
    ComparisonResult,
    EndpointSpec,
    PatientStateObservation,
    StoppingRuleEvaluation,
)
from ..stats.estimators import (
    benjamini_hochberg,
    bootstrap_difference_ci,
    fisher_exact_two_sided,
    newcombe_difference_interval,
    two_proportion_ztest,
    welch_ttest,
    wilson_interval,
)
from .base import BaseAgent

logger = get_logger("agents.biostatistician")

AE_COLUMNS = (
    "ae_id",
    "patient_id",
    "arm_id",
    "epoch",
    "term",
    "soc",
    "ctcae_grade",
    "serious",
    "relatedness",
    "predicted_probability",
    "source",
    "reported_verbatim",
)


@dataclass(slots=True)
class AnalysisResult:
    """Everything the reporting layer needs."""

    arm_summaries: list[ArmSummary] = field(default_factory=list)
    comparisons: list[ComparisonResult] = field(default_factory=list)
    safety_summary: list[dict[str, Any]] = field(default_factory=list)
    stopping_evaluations: list[StoppingRuleEvaluation] = field(default_factory=list)
    cohort_flow: dict[str, Any] = field(default_factory=dict)
    demographics: list[dict[str, Any]] = field(default_factory=list)
    dose_response: list[dict[str, Any]] = field(default_factory=list)
    data_quality: dict[str, Any] = field(default_factory=dict)
    safety_alerts: list[dict[str, Any]] = field(default_factory=list)
    overview: dict[str, Any] = field(default_factory=dict)
    adverse_events: pd.DataFrame = field(default_factory=pd.DataFrame)
    end_of_treatment: pd.DataFrame = field(default_factory=pd.DataFrame)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary (Gold tables, MLflow metrics, reports)."""
        return {
            "arm_summaries": [asdict(summary) for summary in self.arm_summaries],
            "comparisons": [asdict(comparison) for comparison in self.comparisons],
            "safety_summary": self.safety_summary,
            "stopping_evaluations": [asdict(evaluation) for evaluation in self.stopping_evaluations],
            "cohort_flow": self.cohort_flow,
            "demographics": self.demographics,
            "dose_response": self.dose_response,
            "data_quality": self.data_quality,
            "safety_alerts": self.safety_alerts,
            "overview": self.overview,
        }


def observations_frame(observations: list[PatientStateObservation]) -> pd.DataFrame:
    """Build the Silver frame from typed observation objects."""
    return pd.DataFrame([observation.as_row() for observation in observations])


def extract_adverse_events(frame: pd.DataFrame) -> pd.DataFrame:
    """Explode ``adverse_events_json`` into a long adverse-event table."""
    if frame.empty or "adverse_events_json" not in frame.columns:
        return pd.DataFrame(columns=list(AE_COLUMNS))
    records: list[dict[str, Any]] = []
    for row in frame[["patient_id", "arm_id", "epoch", "adverse_events_json"]].itertuples(index=False):
        try:
            events = json.loads(row.adverse_events_json or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            records.append({column: event.get(column) for column in AE_COLUMNS})
    if not records:
        return pd.DataFrame(columns=list(AE_COLUMNS))
    table = pd.DataFrame(records, columns=list(AE_COLUMNS))
    table["ctcae_grade"] = pd.to_numeric(table["ctcae_grade"], errors="coerce").fillna(1).astype(int)
    table["serious"] = table["serious"].fillna(False).astype(bool)
    table["predicted_probability"] = pd.to_numeric(table["predicted_probability"], errors="coerce").fillna(0.0)
    return table


def end_of_treatment_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per patient: last simulated epoch plus baseline-derived deltas."""
    if frame.empty:
        return frame.copy()
    working = frame.copy()
    working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").fillna(0).astype(int)
    baseline = (
        working.loc[working["epoch"] == 0, ["patient_id", "sbp", "dbp", "hr", "alt_u_l", "egfr"]]
        .rename(columns={"sbp": "baseline_sbp_sim", "dbp": "baseline_dbp_sim", "hr": "baseline_hr_sim", "alt_u_l": "baseline_alt_sim", "egfr": "baseline_egfr_sim"})
    )
    last_epoch = working.groupby("patient_id")["epoch"].transform("max")
    eot = working.loc[working["epoch"] == last_epoch].copy()
    if not baseline.empty:
        eot = eot.merge(baseline, on="patient_id", how="left")
        eot["alt_ratio"] = eot["alt_u_l"] / eot["baseline_alt_sim"].replace(0, np.nan)
    else:
        eot["alt_ratio"] = np.nan
    eot = eot.sort_values(["arm_id", "patient_id"]).reset_index(drop=True)
    return eot


class BiostatisticianAgent(BaseAgent):
    """Aggregates the simulation log into Gold-layer analytics and a report payload."""

    role = "biostatistician"

    def __init__(self, context, *, bootstrap_resamples: int = 2000) -> None:
        super().__init__(context)
        self.protocol = context.protocol
        self.bootstrap_resamples = bootstrap_resamples

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def analyze(
        self,
        frame: pd.DataFrame,
        *,
        screening_summary: dict[str, Any] | None = None,
        allocation: dict[str, Any] | None = None,
        extra_quality: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        """Run the complete analysis over a Silver observation frame."""
        if frame.empty:
            logger.warning("no observations to analyse")
            return AnalysisResult()

        result = AnalysisResult()
        ae_table = extract_adverse_events(frame)
        eot = end_of_treatment_frame(frame)
        result.adverse_events = ae_table
        result.end_of_treatment = eot

        result.arm_summaries = self._arm_summaries(eot, ae_table, frame)
        result.comparisons = self._comparisons(eot)
        result.safety_summary = self._safety_summary(ae_table, eot)
        result.stopping_evaluations = self.evaluate_stopping_rules(frame)
        result.safety_alerts = self._safety_alerts(result.safety_summary)
        result.demographics = self._demographics(eot)
        result.dose_response = self._dose_response(eot)
        result.cohort_flow = self._cohort_flow(frame, screening_summary, allocation)
        result.data_quality = self._data_quality(frame, extra_quality)
        result.overview = self._overview(result)
        logger.info(
            "analysis complete",
            extra={
                "extra_fields": {
                    "patients": int(eot["patient_id"].nunique()) if not eot.empty else 0,
                    "adverse_events": len(ae_table),
                    "comparisons": len(result.comparisons),
                    "alerts": len(result.safety_alerts),
                }
            },
        )
        return result

    # ------------------------------------------------------------------
    # Descriptive statistics
    # ------------------------------------------------------------------
    def _arm_summaries(self, eot: pd.DataFrame, ae_table: pd.DataFrame, frame: pd.DataFrame) -> list[ArmSummary]:
        summaries: list[ArmSummary] = []
        for arm in self.protocol.arms:
            subset = eot[eot["arm_id"] == arm.arm_id]
            if subset.empty:
                continue
            events = ae_table[ae_table["arm_id"] == arm.arm_id] if not ae_table.empty else ae_table
            n_patients = int(subset["patient_id"].nunique())
            grade3 = int(events[events["ctcae_grade"] >= 3]["patient_id"].nunique()) if not events.empty else 0
            serious = int(events[events["serious"]]["patient_id"].nunique()) if not events.empty else 0
            deaths = int(events[events["ctcae_grade"] >= 5]["patient_id"].nunique()) if not events.empty else 0
            with_ae = int(events["patient_id"].nunique()) if not events.empty else 0
            responders = int(subset["responder"].fillna(False).astype(bool).sum())
            alt_ratio = subset["alt_ratio"].dropna()
            summaries.append(
                ArmSummary(
                    arm_id=arm.arm_id,
                    label=arm.label,
                    n_patients=n_patients,
                    n_observations=int((frame["arm_id"] == arm.arm_id).sum()),
                    dose_mg=float(arm.dose_mg),
                    mean_sbp_change=_safe_mean(subset["sbp_change"]),
                    sd_sbp_change=_safe_std(subset["sbp_change"]),
                    mean_dbp_change=_safe_mean(subset["dbp_change"]),
                    mean_hr_change=_safe_mean(subset["hr_change"]),
                    responder_rate=responders / n_patients if n_patients else 0.0,
                    ae_rate=with_ae / n_patients if n_patients else 0.0,
                    grade3_plus_rate=grade3 / n_patients if n_patients else 0.0,
                    sae_rate=serious / n_patients if n_patients else 0.0,
                    mortality_rate=deaths / n_patients if n_patients else 0.0,
                    mean_alt_ratio=float(alt_ratio.mean()) if not alt_ratio.empty else float("nan"),
                    discontinuations=int(subset.get("discontinued", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
                    extra=_ci_extra(
                        is_control=arm.is_control,
                        responders=responders,
                        grade3=grade3,
                        n_patients=n_patients,
                    ),
                )
            )
        return summaries

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def _comparisons(self, eot: pd.DataFrame) -> list[ComparisonResult]:
        control = self.protocol.control_arm
        if control is None or eot.empty:
            return []
        control_frame = eot[eot["arm_id"] == control.arm_id]
        if control_frame.empty:
            return []
        results: list[ComparisonResult] = []
        secondary_p_values: list[tuple[int, float]] = []

        for arm in self.protocol.treatment_arms:
            arm_frame = eot[eot["arm_id"] == arm.arm_id]
            if arm_frame.empty:
                continue
            for endpoint in self.protocol.endpoints:
                column = endpoint.column if endpoint.column in arm_frame.columns else None
                if column is None:
                    continue
                treatment_values = pd.to_numeric(arm_frame[column], errors="coerce").dropna().to_numpy()
                control_values = pd.to_numeric(control_frame[column], errors="coerce").dropna().to_numpy()
                if treatment_values.size < 2 or control_values.size < 2:
                    continue

                if endpoint.kind == "binary":
                    if endpoint.response_threshold is not None:
                        # "Responder" must be counted on the side the protocol
                        # declares: a >= 10 mmHg *reduction* is `value <= -10`,
                        # not `value >= -10` (which counts non-responders and
                        # inverts the direction of the reported effect).
                        s1 = _count_responders(treatment_values, endpoint)
                        s2 = _count_responders(control_values, endpoint)
                    else:
                        s1 = int(arm_frame["responder"].fillna(False).astype(bool).sum())
                        s2 = int(control_frame["responder"].fillna(False).astype(bool).sum())
                    n1, n2 = int(treatment_values.size), int(control_values.size)
                    effect = s1 / n1 - s2 / n2 if n1 and n2 else float("nan")
                    low, high = newcombe_difference_interval(s1, n1, s2, n2)
                    _, p_value = two_proportion_ztest(s1, n1, s2, n2)
                    test_name = "two-proportion z-test (Newcombe CI)"
                    if min(s1, s2, n1 - s1, n2 - s2) < 5:
                        p_value = fisher_exact_two_sided(s1, n1 - s1, s2, n2 - s2)
                        test_name = "Fisher exact (Newcombe CI)"
                else:
                    effect = float(np.mean(treatment_values) - np.mean(control_values))
                    _, p_value = welch_ttest(treatment_values, control_values)
                    _, low, high = bootstrap_difference_ci(
                        treatment_values,
                        control_values,
                        n_resamples=self.bootstrap_resamples,
                        seed=self.context.seed,
                    )
                    test_name = "Welch t-test (bootstrap CI)"
                    n1, n2 = int(treatment_values.size), int(control_values.size)

                meets = False
                if endpoint.mcid is not None:
                    directional = effect if endpoint.direction != "increase" else -effect
                    meets = bool(directional <= -abs(endpoint.mcid)) if endpoint.direction in {"decrease", "increase"} else bool(abs(effect) >= abs(endpoint.mcid))
                comparison = ComparisonResult(
                    endpoint=endpoint.name,
                    arm_id=arm.arm_id,
                    control_arm_id=control.arm_id,
                    metric=column,
                    effect_estimate=float(effect),
                    ci_low=float(low),
                    ci_high=float(high),
                    p_value=float(p_value),
                    test=test_name,
                    n_treatment=n1,
                    n_control=n2,
                    mcid=endpoint.mcid,
                    meets_mcid=meets,
                    q_value=None,
                )
                results.append(comparison)
                if not endpoint.is_primary:
                    secondary_p_values.append((len(results) - 1, float(p_value)))

        # Benjamini-Hochberg across the secondary-endpoint family.
        if secondary_p_values:
            adjusted = benjamini_hochberg([p for _, p in secondary_p_values])
            for (index, _), q_value in zip(secondary_p_values, adjusted, strict=True):
                results[index].q_value = float(q_value)
        return results

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------
    def _safety_summary(self, ae_table: pd.DataFrame, eot: pd.DataFrame) -> list[dict[str, Any]]:
        if ae_table.empty:
            return []
        control = self.protocol.control_arm
        rows: list[dict[str, Any]] = []
        arm_sizes = eot.groupby("arm_id")["patient_id"].nunique().to_dict()
        control_size = arm_sizes.get(control.arm_id, 0) if control else 0

        p_values: list[tuple[int, float]] = []
        for (arm_id, term), group in ae_table.groupby(["arm_id", "term"], dropna=False):
            n_arm = int(arm_sizes.get(arm_id, 0))
            if n_arm == 0:
                continue
            patients_with_event = int(group["patient_id"].nunique())
            grade3 = int(group[group["ctcae_grade"] >= 3]["patient_id"].nunique())
            grade5 = int(group[group["ctcae_grade"] >= 5]["patient_id"].nunique())
            serious = int(group[group["serious"]]["patient_id"].nunique())
            llm_reported = int(group[group["source"].isin(["llm", "hybrid"])]["patient_id"].nunique())
            control_events = ae_table[(ae_table["arm_id"] == control.arm_id) & (ae_table["term"] == term)] if control else ae_table.iloc[0:0]
            control_patients = int(control_events["patient_id"].nunique())
            risk_treatment = patients_with_event / n_arm
            risk_control = control_patients / control_size if control_size else 0.0
            risk_difference = risk_treatment - risk_control
            low, high = newcombe_difference_interval(patients_with_event, n_arm, control_patients, control_size) if control_size else (float("nan"), float("nan"))
            if control_size:
                p_value = fisher_exact_two_sided(
                    patients_with_event, n_arm - patients_with_event, control_patients, control_size - control_patients
                )
            else:
                p_value = float("nan")
            row = {
                "arm_id": arm_id,
                "term": term,
                "soc": str(group["soc"].mode().iloc[0]) if not group["soc"].mode().empty else "",
                "n_patients": n_arm,
                "n_events": len(group),
                "n_patients_with_event": patients_with_event,
                "rate": risk_treatment,
                "control_rate": risk_control,
                "risk_difference": risk_difference,
                "rd_ci_low": low,
                "rd_ci_high": high,
                "p_value": p_value,
                "q_value": None,
                "grade3_plus": grade3,
                "grade5": grade5,
                "serious": serious,
                "llm_reported_patients": llm_reported,
                "mean_predicted_probability": float(group["predicted_probability"].mean()),
                "grade_distribution": json.dumps(
                    {str(int(grade)): int(count) for grade, count in group["ctcae_grade"].value_counts().sort_index().items()}
                ),
            }
            p_values.append((len(rows), p_value))
            rows.append(row)

        valid = [(index, p) for index, p in p_values if p is not None and not math.isnan(p)]
        if valid:
            adjusted = benjamini_hochberg([p for _, p in valid])
            for (index, _), q_value in zip(valid, adjusted, strict=True):
                rows[index]["q_value"] = float(q_value)
        rows.sort(key=lambda row: (-(row["rate"]), row["term"]))
        return rows

    def _safety_alerts(self, safety_summary: list[dict[str, Any]], *, min_patients: int = 5) -> list[dict[str, Any]]:
        """Flag signals a DSMB would want to see before the final analysis."""
        alerts: list[dict[str, Any]] = []
        for row in safety_summary:
            if row["n_patients_with_event"] < min_patients:
                continue
            q_value = row.get("q_value")
            if row["grade3_plus"] > 0 and (q_value is not None and q_value < 0.10):
                alerts.append(
                    {
                        "severity": "high" if row["grade3_plus"] >= 3 else "moderate",
                        "term": row["term"],
                        "arm_id": row["arm_id"],
                        "message": (
                            f"{row['term']}: grade>=3 in {row['grade3_plus']} patients, rate difference "
                            f"{row['risk_difference']:+.1%} vs control (q={q_value:.3f})"
                        ),
                    }
                )
            elif row["rate"] > 0.25 and row["risk_difference"] > 0.10:
                alerts.append(
                    {
                        "severity": "moderate",
                        "term": row["term"],
                        "arm_id": row["arm_id"],
                        "message": (
                            f"{row['term']}: {row['rate']:.0%} of patients affected vs {row['control_rate']:.0%} "
                            "in control"
                        ),
                    }
                )
        return alerts

    # ------------------------------------------------------------------
    # Safety stopping rules (DSMB)
    # ------------------------------------------------------------------
    def evaluate_stopping_rules(self, frame: pd.DataFrame, *, upto_epoch: int | None = None) -> list[StoppingRuleEvaluation]:
        """Evaluate every protocol stopping rule for every arm and epoch."""
        evaluations: list[StoppingRuleEvaluation] = []
        if frame.empty:
            return evaluations
        working = frame.copy()
        working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").fillna(0).astype(int)
        ae_table = extract_adverse_events(working)
        if not ae_table.empty:
            ae_table["epoch"] = pd.to_numeric(ae_table["epoch"], errors="coerce").fillna(0).astype(int)
        max_epoch = int(working["epoch"].max()) if upto_epoch is None else min(upto_epoch, int(working["epoch"].max()))

        for rule in self.protocol.stopping_rules:
            arms = rule.applies_to_arms or [arm.arm_id for arm in self.protocol.arms]
            for arm_id in arms:
                for epoch in range(rule.min_epoch, max_epoch + 1):
                    at_risk = int(working[(working["arm_id"] == arm_id) & (working["epoch"] == epoch)]["patient_id"].nunique())
                    if at_risk == 0:
                        continue
                    observed = self._rule_metric(rule.metric, working, ae_table, arm_id, epoch, at_risk)
                    triggered = (
                        observed > rule.threshold
                        if rule.comparator == "greater_than"
                        else observed >= rule.threshold
                    )
                    evaluations.append(
                        StoppingRuleEvaluation(
                            rule_id=rule.rule_id,
                            epoch=epoch,
                            arm_id=arm_id,
                            metric=rule.metric,
                            observed=float(observed),
                            threshold=float(rule.threshold),
                            comparator=rule.comparator,
                            triggered=bool(triggered),
                            action=rule.action,
                            n_at_risk=at_risk,
                        )
                    )
        return evaluations

    @staticmethod
    def _rule_metric(
        metric: str, frame: pd.DataFrame, ae_table: pd.DataFrame, arm_id: str, epoch: int, at_risk: int
    ) -> float:
        """Compute one DSMB metric cumulatively up to ``epoch``."""
        if metric in {"grade3_plus_rate", "sae_rate", "mortality_rate"}:
            if ae_table.empty:
                return 0.0
            subset = ae_table[(ae_table["arm_id"] == arm_id) & (ae_table["epoch"] <= epoch)]
            if metric == "grade3_plus_rate":
                hit = subset[subset["ctcae_grade"] >= 3]["patient_id"].nunique()
            elif metric == "sae_rate":
                hit = subset[subset["serious"]]["patient_id"].nunique()
            else:
                hit = subset[subset["ctcae_grade"] >= 5]["patient_id"].nunique()
            return float(hit) / at_risk if at_risk else 0.0
        if metric == "alt_elevation_rate":
            subset = frame[(frame["arm_id"] == arm_id) & (frame["epoch"] <= epoch)]
            baseline = frame[(frame["arm_id"] == arm_id) & (frame["epoch"] == 0)][["patient_id", "alt_u_l"]]
            if baseline.empty or subset.empty:
                return 0.0
            merged = subset.merge(baseline.rename(columns={"alt_u_l": "baseline_alt"}), on="patient_id", how="inner")
            if merged.empty:
                return 0.0
            elevated = merged[(merged["alt_u_l"] >= 3.0 * merged["baseline_alt"]) & (merged["alt_u_l"] > 100.0)]
            return float(elevated["patient_id"].nunique()) / at_risk if at_risk else 0.0
        return 0.0

    # ------------------------------------------------------------------
    # Demographics, dose response, flow, quality
    # ------------------------------------------------------------------
    def _demographics(self, eot: pd.DataFrame) -> list[dict[str, Any]]:
        if eot.empty:
            return []
        rows: list[dict[str, Any]] = []
        for arm_id, group in eot.groupby("arm_id"):
            age = pd.to_numeric(group.get("age", pd.Series(dtype=float)), errors="coerce")
            sbp = pd.to_numeric(group["sbp"], errors="coerce")
            rows.append(
                {
                    "arm_id": arm_id,
                    "n": int(group["patient_id"].nunique()),
                    "age_mean": float(age.mean()) if not age.empty else float("nan"),
                    "age_sd": float(age.std(ddof=1)) if age.notna().sum() > 1 else 0.0,
                    "female_pct": float((group.get("sex", pd.Series(dtype=str)) == "F").mean() * 100) if "sex" in group else float("nan"),
                    "sbp_end_mean": float(sbp.mean()),
                    "sbp_change_mean": float(pd.to_numeric(group["sbp_change"], errors="coerce").mean()),
                    "discontinued": int(group.get("discontinued", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
                }
            )
        return rows

    def _dose_response(self, eot: pd.DataFrame) -> list[dict[str, Any]]:
        """Mean primary-endpoint change by nominal dose plus a linear trend test."""
        if eot.empty:
            return []
        primary = self.protocol.primary_endpoint
        column = primary.column if primary.column in eot.columns else "sbp_change"
        rows: list[dict[str, Any]] = []
        for arm in self.protocol.arms:
            subset = pd.to_numeric(eot.loc[eot["arm_id"] == arm.arm_id, column], errors="coerce").dropna()
            if subset.empty:
                continue
            mean, low, high = _mean_ci(subset)
            rows.append(
                {
                    "arm_id": arm.arm_id,
                    "label": arm.label,
                    "dose_mg": float(arm.dose_mg),
                    "n": int(subset.size),
                    "mean": mean,
                    "ci_low": low,
                    "ci_high": high,
                }
            )
        if len(rows) >= 2:
            doses = np.array([row["dose_mg"] for row in rows], dtype=float)
            means = np.array([row["mean"] for row in rows], dtype=float)
            if np.ptp(doses) > 0:
                slope, intercept = np.polyfit(doses, means, 1)
                predicted = slope * doses + intercept
                ss_res = float(np.sum((means - predicted) ** 2))
                ss_tot = float(np.sum((means - means.mean()) ** 2))
                r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
                for row in rows:
                    row["trend_slope_per_mg"] = float(slope)
                    row["trend_r_squared"] = float(r_squared)
        return rows

    def _cohort_flow(
        self,
        frame: pd.DataFrame,
        screening_summary: dict[str, Any] | None,
        allocation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        enrolled = int(frame["patient_id"].nunique()) if not frame.empty else 0
        discontinued = 0
        if not frame.empty and "discontinued" in frame.columns:
            discontinued = int(frame[frame["discontinued"].fillna(False).astype(bool)]["patient_id"].nunique())
        completed = enrolled - discontinued
        flow = {
            "screened": int((screening_summary or {}).get("screened", enrolled)),
            "enrolled": enrolled,
            "completed": completed,
            "discontinued": discontinued,
            "screen_failures": int((screening_summary or {}).get("screen_failures", 0)),
            "failure_reasons": (screening_summary or {}).get("failure_reasons", {}),
            "allocation": (allocation or {}).get("arm_counts", {}),
        }
        return flow

    def _data_quality(self, frame: pd.DataFrame, extra: dict[str, Any] | None) -> dict[str, Any]:
        total = len(frame)
        llm_used = int(frame["llm_used"].fillna(False).astype(bool).sum()) if "llm_used" in frame else 0
        llm_errors = int((frame.get("llm_error", pd.Series(dtype=str)).fillna("") != "").sum()) if "llm_error" in frame else 0
        null_counts = {column: int(frame[column].isna().sum()) for column in frame.columns if frame[column].isna().any()}
        return {
            "rows": total,
            "patients": int(frame["patient_id"].nunique()) if total else 0,
            "llm_narrated_rows": llm_used,
            "llm_narrated_pct": round(llm_used / total * 100, 2) if total else 0.0,
            "llm_error_rows": llm_errors,
            "llm_error_pct": round(llm_errors / total * 100, 4) if total else 0.0,
            "columns_with_nulls": null_counts,
            "physiology_backends": sorted(set(frame.get("physiology_backend", pd.Series(dtype=str)).dropna().unique().tolist())),
            "model_versions": sorted(set(frame.get("physiology_model_version", pd.Series(dtype=str)).dropna().unique().tolist())),
            "epochs": int(pd.to_numeric(frame["epoch"], errors="coerce").max()) + 1 if total else 0,
            **(extra or {}),
        }

    def _overview(self, result: AnalysisResult) -> dict[str, Any]:
        primary = self.protocol.primary_endpoint
        control = self.protocol.control_arm
        headline: dict[str, Any] = {
            "protocol_id": self.protocol.protocol_id,
            "primary_endpoint": primary.name,
            "n_patients": int(sum(summary.n_patients for summary in result.arm_summaries)),
            "n_adverse_events": len(result.adverse_events),
            "grade3_plus_patients": int(
                result.adverse_events[result.adverse_events["ctcae_grade"] >= 3]["patient_id"].nunique()
            )
            if not result.adverse_events.empty
            else 0,
            "stopping_rules_triggered": int(sum(1 for evaluation in result.stopping_evaluations if evaluation.triggered)),
            "safety_alerts": len(result.safety_alerts),
        }
        for summary in result.arm_summaries:
            headline[f"sbp_change_{summary.arm_id}"] = round(summary.mean_sbp_change, 3)
            headline[f"responder_rate_{summary.arm_id}"] = round(summary.responder_rate, 4)
            headline[f"grade3_rate_{summary.arm_id}"] = round(summary.grade3_plus_rate, 4)
        for comparison in result.comparisons:
            if comparison.endpoint == primary.name:
                headline[f"effect_{comparison.arm_id}_vs_{comparison.control_arm_id}"] = round(comparison.effect_estimate, 4)
                headline[f"p_value_{comparison.arm_id}"] = round(comparison.p_value, 6)
        if control is not None:
            headline["control_arm"] = control.arm_id
        return headline


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _count_responders(values: np.ndarray, endpoint: EndpointSpec) -> int:
    """Count responders on the side defined by the endpoint's direction."""
    threshold = endpoint.response_threshold
    if threshold is None:
        return 0
    if endpoint.direction == "decrease":
        return int(np.sum(values <= threshold))
    if endpoint.direction == "increase":
        return int(np.sum(values >= threshold))
    return int(np.sum(np.abs(values) >= abs(threshold)))


def _ci_extra(*, is_control: bool, responders: int, grade3: int, n_patients: int) -> dict[str, Any]:
    """Scalar-only metadata (Parquet and Delta cannot store tuples/dicts cleanly)."""
    responder_low, responder_high = wilson_interval(responders, n_patients)
    grade_low, grade_high = wilson_interval(grade3, n_patients)
    return {
        "is_control": bool(is_control),
        "responder_ci_low": round(responder_low, 6),
        "responder_ci_high": round(responder_high, 6),
        "grade3_plus_ci_low": round(grade_low, 6),
        "grade3_plus_ci_high": round(grade_high, 6),
    }


def _safe_mean(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if not values.empty else float("nan")


def _safe_std(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.std(ddof=1)) if values.size > 1 else 0.0


def _mean_ci(values: pd.Series) -> tuple[float, float, float]:
    array = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if array.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    mean = float(array.mean())
    if array.size == 1:
        return (mean, mean, mean)
    se = float(array.std(ddof=1) / math.sqrt(array.size))
    return (mean, mean - 1.96 * se, mean + 1.96 * se)
