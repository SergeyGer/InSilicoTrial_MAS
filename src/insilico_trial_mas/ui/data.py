"""Run directory -> dashboard payload.

The dashboard is a *view* of an existing run, so it reads only artefacts the
pipeline already wrote and never re-runs the simulation:

* ``run_manifest.json``           - provenance, cohort, engine, screening, lineage, replay recipe
* ``report/trial_report.json``    - the full biostatistical analysis (when produced)
* ``silver_observations.parquet`` - per-epoch trajectories and the patient explorer

Everything is defensive: a dashboard on a partially written run (or on a run
whose report formats excluded JSON) must still render, with the missing block
replaced by an explicit "not available" message rather than an exception. That
matters because the Studio opens the dashboard the moment a run finishes, and a
crash there would look like a failed simulation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..logging_utils import get_logger

logger = get_logger("ui.data")

RUN_MANIFEST_FILE = "run_manifest.json"
REPORT_JSON = Path("report") / "trial_report.json"
SILVER_PARQUET = "silver_observations.parquet"

#: Columns the dashboard needs from Silver (keeps memory bounded on huge runs).
SILVER_COLUMNS: tuple[str, ...] = (
    "patient_id",
    "cohort_id",
    "site_id",
    "arm_id",
    "arm_label",
    "epoch",
    "time_hours",
    "dose_mg",
    "plasma_conc_mg_l",
    "sbp",
    "dbp",
    "hr",
    "qtc_ms",
    "alt_u_l",
    "egfr",
    "sbp_change",
    "dbp_change",
    "hr_change",
    "biomarker_composite",
    "responder",
    "worst_ctcae_grade",
    "n_adverse_events",
    "adverse_events_json",
    "symptoms_json",
    "symptom_summary",
    "discontinued",
    "discontinuation_reason",
    "adherence_intent",
    "llm_used",
    "llm_provider",
    "llm_model",
    "llm_latency_ms",
    "llm_tokens_in",
    "llm_tokens_out",
    "llm_cache_hit",
    "llm_error",
    "prompt_hash",
    "physiology_model_version",
    "physiology_backend",
)

#: Environment fields the reproducibility panel actually renders. Whitelisting
#: them keeps the dashboard payload to what the UI shows (data minimisation) and
#: stops it from carrying anything the run manifest happens to record - for example
#: the credential-presence map, which the panel never displays.
DASHBOARD_ENVIRONMENT_FIELDS: tuple[str, ...] = (
    "python_version",
    "platform",
    "cpu_count",
    "memory_gb",
    "pyspark_available",
    "java_available",
    "databricks",
    "databricks_community",
    "recommended_backend",
)

#: Guard rails: a dashboard is a demo artefact, not an archive.
MAX_PATIENTS_IN_DETAIL = 150
MAX_PATIENT_ROWS = 2_000_000


@dataclass
class DashboardData:
    """Everything the HTML renderer needs, already reduced to chart-sized arrays."""

    run_dir: str
    run_id: str = ""
    protocol: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)
    cohort: dict[str, Any] = field(default_factory=dict)
    screening: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    trajectories: list[dict[str, Any]] = field(default_factory=list)
    dose_response: list[dict[str, Any]] = field(default_factory=list)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    arm_summaries: list[dict[str, Any]] = field(default_factory=list)
    safety_summary: list[dict[str, Any]] = field(default_factory=list)
    safety_alerts: list[dict[str, Any]] = field(default_factory=list)
    stopping: list[dict[str, Any]] = field(default_factory=list)
    stopping_rules: list[dict[str, Any]] = field(default_factory=list)
    demographics: list[dict[str, Any]] = field(default_factory=list)
    cohort_flow: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    ae_heatmap: dict[str, Any] = field(default_factory=dict)
    grade_stacks: dict[str, Any] = field(default_factory=dict)
    patients: list[dict[str, Any]] = field(default_factory=list)
    patient_details: dict[str, Any] = field(default_factory=dict)
    traces: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    lineage_graph: dict[str, Any] = field(default_factory=dict)
    lineage_mermaid: str = ""
    notes: list[str] = field(default_factory=list)
    row_count: int = 0
    patient_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "protocol": self.protocol,
            "provenance": self.provenance,
            "engine": self.engine,
            "cohort": self.cohort,
            "screening": self.screening,
            "estimate": self.estimate,
            "environment": self.environment,
            "replay": self.replay,
            "storage": self.storage,
            "warnings": self.warnings,
            "analysis": self.analysis,
            "trajectories": self.trajectories,
            "dose_response": self.dose_response,
            "comparisons": self.comparisons,
            "arm_summaries": self.arm_summaries,
            "safety_summary": self.safety_summary,
            "safety_alerts": self.safety_alerts,
            "stopping": self.stopping,
            "stopping_rules": self.stopping_rules,
            "demographics": self.demographics,
            "cohort_flow": self.cohort_flow,
            "data_quality": self.data_quality,
            "ae_heatmap": self.ae_heatmap,
            "grade_stacks": self.grade_stacks,
            "patients": self.patients,
            "patient_details": self.patient_details,
            "traces": self.traces,
            "lineage": self.lineage,
            "lineage_graph": self.lineage_graph,
            "lineage_mermaid": self.lineage_mermaid,
            "notes": self.notes,
            "row_count": self.row_count,
            "patient_count": self.patient_count,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"could not read {path}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_silver(run_dir: Path) -> pd.DataFrame:
    path = run_dir / SILVER_PARQUET
    if not path.exists():
        return pd.DataFrame()
    try:
        import pyarrow.parquet as pq

        available = set(pq.ParquetFile(path).schema_arrow.names)
        columns = [c for c in SILVER_COLUMNS if c in available]
        frame = pd.read_parquet(path, columns=columns)
    except Exception as exc:
        logger.warning(f"could not read {path}: {type(exc).__name__}: {exc}")
        return pd.DataFrame()
    if len(frame) > MAX_PATIENT_ROWS:
        logger.info(f"sampling {MAX_PATIENT_ROWS} of {len(frame)} Silver rows for the dashboard")
        frame = frame.sample(MAX_PATIENT_ROWS, random_state=0)
    return frame


def resolve_run_dir(path: str | Path) -> Path:
    """Accept a run directory, or a parent directory containing runs.

    Convenience for demos and CI: ``--run artifacts/`` picks the newest run that
    actually has a manifest, instead of forcing the caller to know the run id.
    """
    candidate = Path(path)
    if (candidate / RUN_MANIFEST_FILE).exists() or (candidate / SILVER_PARQUET).exists():
        return candidate
    if not candidate.is_dir():
        raise FileNotFoundError(f"run directory not found: {candidate}")
    runs = [
        child
        for child in candidate.iterdir()
        if child.is_dir() and ((child / RUN_MANIFEST_FILE).exists() or (child / SILVER_PARQUET).exists())
    ]
    if not runs:
        raise FileNotFoundError(
            f"{candidate} contains no run directory (expected a child with {RUN_MANIFEST_FILE})"
        )
    newest = max(runs, key=lambda item: item.stat().st_mtime)
    logger.info(f"resolved {candidate} to the newest run: {newest.name}")
    return newest


def load_dashboard_data(run_dir: str | Path) -> DashboardData:
    """Build the dashboard payload for a finished run."""
    directory = Path(run_dir)
    if not directory.exists():
        raise FileNotFoundError(f"run directory not found: {directory}")

    manifest = _read_json(directory / RUN_MANIFEST_FILE)
    report = _read_json(directory / REPORT_JSON)
    analysis = dict(report.get("analysis") or {})
    silver = _read_silver(directory)

    if not manifest and silver.empty:
        raise FileNotFoundError(
            f"{directory} does not look like a run directory "
            f"(expected {RUN_MANIFEST_FILE} or {SILVER_PARQUET})"
        )

    data = DashboardData(run_dir=str(directory))
    data.run_id = str(manifest.get("run_id") or directory.name)
    data.protocol = dict(manifest.get("protocol") or report.get("protocol") or {})
    data.provenance = dict(manifest.get("provenance") or report.get("provenance") or {})
    data.engine = dict(manifest.get("engine") or {})
    data.cohort = dict(manifest.get("cohort") or {})
    data.screening = dict(manifest.get("screening") or {})
    data.estimate = dict(manifest.get("estimate") or {})
    environment = manifest.get("environment") or {}
    data.environment = {name: environment[name] for name in DASHBOARD_ENVIRONMENT_FIELDS if name in environment}
    data.replay = dict(manifest.get("replay") or {})
    data.storage = dict(manifest.get("storage") or {})
    data.warnings = list(manifest.get("protocol_warnings") or report.get("protocol_warnings") or [])
    data.traces = dict(manifest.get("traces") or report.get("traces") or {})
    data.lineage = dict(manifest.get("lineage") or {})
    data.lineage_graph = dict(manifest.get("lineage_graph") or {})
    data.lineage_mermaid = str(manifest.get("lineage_mermaid") or report.get("lineage_mermaid") or "")

    if analysis:
        data.analysis = analysis
        data.arm_summaries = list(analysis.get("arm_summaries") or [])
        data.comparisons = list(analysis.get("comparisons") or [])
        data.safety_summary = list(analysis.get("safety_summary") or [])
        data.safety_alerts = list(analysis.get("safety_alerts") or [])
        data.stopping = list(analysis.get("stopping_evaluations") or [])
        data.demographics = list(analysis.get("demographics") or [])
        data.dose_response = list(analysis.get("dose_response") or [])
        data.cohort_flow = dict(analysis.get("cohort_flow") or {})
        data.data_quality = dict(analysis.get("data_quality") or {})
    else:
        data.notes.append(
            "report/trial_report.json is missing (report_formats may exclude 'json'); "
            "analysis tables are derived from the Silver observations instead."
        )

    if not silver.empty:
        data.row_count = len(silver)
        data.patient_count = int(silver["patient_id"].nunique())
        data.trajectories = _trajectories(silver)
        if not analysis:
            data.safety_summary = _safety_from_silver(silver)
        data.ae_heatmap = _ae_heatmap(silver)
        data.grade_stacks = _grade_stacks(silver)
        data.patients, data.patient_details = _patient_explorer(silver)
        data.stopping_rules = _stopping_rule_specs(data.protocol)
    else:
        data.notes.append("silver_observations.parquet is missing; trajectory and patient views are unavailable.")

    if not data.traces:
        data.traces = _traces_from_silver(silver)
    else:
        # The manifest carries aggregate trace counters; the distributions below
        # the charts come from the Silver columns, so merge them in.
        silver_traces = _traces_from_silver(silver) if not silver.empty else {}
        for key, value in silver_traces.items():
            if key.endswith("_values") and not data.traces.get(key):
                data.traces[key] = value
            elif key in {"narrated_rows", "cache_hit_rate", "error_rate", "tokens_in_total", "tokens_out_total"}:
                data.traces.setdefault(key, value)

    return data


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


def _arm_order(protocol: dict[str, Any]) -> list[str]:
    return [str(arm.get("arm_id")) for arm in (protocol.get("arms") or [])]


def _trajectories(silver: pd.DataFrame) -> list[dict[str, Any]]:
    """Mean +/- 95% CI per arm and epoch for the modelled biomarkers."""
    metrics = {
        "sbp": ("Systolic BP", "mmHg"),
        "dbp": ("Diastolic BP", "mmHg"),
        "hr": ("Heart rate", "bpm"),
        "alt_u_l": ("ALT", "U/L"),
        "plasma_conc_mg_l": ("Plasma concentration", "mg/L"),
    }
    out: list[dict[str, Any]] = []
    for column, (label, unit) in metrics.items():
        if column not in silver.columns:
            continue
        grouped = (
            silver.groupby(["arm_id", "arm_label", "epoch"], dropna=False)[column]
            .agg(["mean", "std", "count"])
            .reset_index()
        )
        series: list[dict[str, Any]] = []
        for (arm_id, arm_label), block in grouped.groupby(["arm_id", "arm_label"], dropna=False):
            block = block.sort_values("epoch")
            means = [float(value) if pd.notna(value) else float("nan") for value in block["mean"]]
            counts = [int(value) for value in block["count"]]
            stds = [float(value) if pd.notna(value) else 0.0 for value in block["std"]]
            half = [1.96 * (sd / max(1, n) ** 0.5) for sd, n in zip(stds, counts, strict=True)]
            series.append(
                {
                    "arm_id": str(arm_id),
                    "name": str(arm_label or arm_id),
                    "epochs": [int(value) for value in block["epoch"]],
                    "values": means,
                    "low": [m - h for m, h in zip(means, half, strict=True)],
                    "high": [m + h for m, h in zip(means, half, strict=True)],
                    "n": counts,
                }
            )
        out.append({"metric": column, "label": label, "unit": unit, "series": series})
    return out


def _safety_from_silver(silver: pd.DataFrame) -> list[dict[str, Any]]:
    """Fallback safety table when the JSON report is unavailable."""
    from ..agents.biostatistician_agent import end_of_treatment_frame, extract_adverse_events

    events = extract_adverse_events(silver)
    if events.empty:
        return []
    eot = end_of_treatment_frame(silver)
    sizes = eot.groupby("arm_id")["patient_id"].nunique().to_dict()
    rows: list[dict[str, Any]] = []
    for (arm_id, term), group in events.groupby(["arm_id", "term"], dropna=False):
        n_arm = int(sizes.get(arm_id, 0)) or 1
        rows.append(
            {
                "arm_id": str(arm_id),
                "term": str(term),
                "soc": str(group["soc"].mode().iloc[0]) if not group["soc"].mode().empty else "",
                "n_patients": n_arm,
                "n_events": len(group),
                "n_patients_with_event": int(group["patient_id"].nunique()),
                "rate": float(group["patient_id"].nunique()) / n_arm,
                "grade3_plus": int(group[group["ctcae_grade"] >= 3]["patient_id"].nunique()),
                "serious": int(group[group["serious"]]["patient_id"].nunique()),
                "control_rate": 0.0,
                "risk_difference": 0.0,
                "p_value": None,
                "q_value": None,
            }
        )
    rows.sort(key=lambda row: -row["rate"])
    return rows


def _ae_heatmap(silver: pd.DataFrame) -> dict[str, Any]:
    """Patient-level adverse-event rate per arm x term (top terms only)."""
    from ..agents.biostatistician_agent import extract_adverse_events

    events = extract_adverse_events(silver)
    if events.empty:
        return {}
    sizes = silver.groupby("arm_id")["patient_id"].nunique().to_dict()
    pivot = (
        events.groupby(["term", "arm_id"])["patient_id"]
        .nunique()
        .unstack(fill_value=0)
    )
    total = pivot.sum(axis=1).sort_values(ascending=False)
    terms = list(total.index[:8])
    arms = [arm for arm in sizes if arm in pivot.columns]
    values = [
        [100.0 * float(pivot.loc[term, arm]) / max(1, int(sizes.get(arm, 0))) for arm in arms]
        for term in terms
    ]
    return {"terms": [str(term) for term in terms], "arms": [str(arm) for arm in arms], "values": values, "unit": "%"}


def _grade_stacks(silver: pd.DataFrame) -> dict[str, Any]:
    """Event counts by CTCAE grade and arm (events, not patients)."""
    from ..agents.biostatistician_agent import extract_adverse_events

    events = extract_adverse_events(silver)
    if events.empty:
        return {}
    arms = list(dict.fromkeys(silver["arm_id"].astype(str)))
    stacks: list[list[float]] = []
    labels: list[str] = []
    for arm in arms:
        subset = events[events["arm_id"] == arm]
        labels.append(arm)
        stacks.append([float((subset["ctcae_grade"] == grade).sum()) for grade in range(1, 6)])
    return {"labels": labels, "stacks": stacks, "grade_labels": ["G1", "G2", "G3", "G4", "G5"]}


def _patient_explorer(silver: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compact patient index plus per-patient epoch series for the explorer."""
    if silver.empty or "patient_id" not in silver.columns:
        return [], {}
    working = silver.copy()
    working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").fillna(0).astype(int)
    working = working.sort_values(["patient_id", "epoch"])

    baseline = working[working["epoch"] == 0].set_index("patient_id")
    last_epoch = working.groupby("patient_id")["epoch"].transform("max")
    final = working[working["epoch"] == last_epoch].set_index("patient_id")

    index: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    ranked = sorted(
        working["patient_id"].unique().tolist(),
        key=lambda patient_id: (
            -int(final.loc[patient_id].get("worst_ctcae_grade") or 0),
            str(patient_id),
        ),
    )
    for patient_id in ranked:
        rows = working[working["patient_id"] == patient_id]
        final_row = final.loc[patient_id]
        base_row = baseline.loc[patient_id] if patient_id in baseline.index else rows.iloc[0]
        events_by_epoch: dict[str, list[str]] = {}
        symptom_rows: list[dict[str, Any]] = []
        for row in rows.itertuples(index=False):
            try:
                events = json.loads(getattr(row, "adverse_events_json", "[]") or "[]")
            except (TypeError, json.JSONDecodeError):
                events = []
            if events:
                events_by_epoch[str(int(row.epoch))] = [str(event.get("term", "")) for event in events]
            if getattr(row, "symptom_summary", ""):
                symptom_rows.append(
                    {"epoch": int(row.epoch), "summary": str(row.symptom_summary), "llm": bool(getattr(row, "llm_used", False))}
                )
        index.append(
            {
                "patient_id": str(patient_id),
                "arm_id": str(final_row.get("arm_id", "")),
                "arm_label": str(final_row.get("arm_label", "")),
                "age": None,
                "baseline_sbp": _safe_float(base_row.get("sbp")),
                "final_sbp_change": _safe_float(final_row.get("sbp_change")),
                "worst_grade": int(final_row.get("worst_ctcae_grade") or 0),
                "n_adverse_events": int(rows["n_adverse_events"].fillna(0).sum()),
                "responder": bool(final_row.get("responder") or False),
                "discontinued": bool(rows["discontinued"].fillna(False).astype(bool).any()),
                "llm_used": bool(rows["llm_used"].fillna(False).astype(bool).any()),
            }
        )
        if len(details) < MAX_PATIENTS_IN_DETAIL:
            details[str(patient_id)] = {
                "epochs": [int(value) for value in rows["epoch"]],
                "dose_mg": [_safe_float(value) for value in rows["dose_mg"]],
                "concentration": [_safe_float(value) for value in rows["plasma_conc_mg_l"]],
                "sbp": [_safe_float(value) for value in rows["sbp"]],
                "dbp": [_safe_float(value) for value in rows["dbp"]],
                "hr": [_safe_float(value) for value in rows["hr"]],
                "alt_u_l": [_safe_float(value) for value in rows["alt_u_l"]],
                "adverse_by_epoch": events_by_epoch,
                "symptoms": symptom_rows,
                "arm_label": str(final_row.get("arm_label", "")),
                "cohort_id": str(final_row.get("cohort_id", "")),
                "site_id": str(final_row.get("site_id", "")),
                "discontinued": bool(rows["discontinued"].fillna(False).astype(bool).any()),
                "discontinuation_reason": str(final_row.get("discontinuation_reason", "")),
            }
    return index, details


def _stopping_rule_specs(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for rule in protocol.get("stopping_rules") or []:
        specs.append(
            {
                "rule_id": str(rule.get("rule_id", "")),
                "metric": str(rule.get("metric", "")),
                "threshold": float(rule.get("threshold", 0.0)),
                "action": str(rule.get("action", "review")),
                "description": str(rule.get("description", "")),
                "applies_to_arms": list(rule.get("applies_to_arms") or []),
            }
        )
    return specs


def _traces_from_silver(silver: pd.DataFrame) -> dict[str, Any]:
    """Token and latency telemetry straight from the Silver columns."""
    if silver.empty or "llm_tokens_in" not in silver.columns:
        return {}
    narrated = silver[silver.get("llm_used", pd.Series(False, index=silver.index)).fillna(False).astype(bool)]
    if narrated.empty:
        return {"llm_calls": 0, "narrated_rows": 0}
    tokens_in = pd.to_numeric(narrated["llm_tokens_in"], errors="coerce").dropna()
    tokens_out = pd.to_numeric(narrated["llm_tokens_out"], errors="coerce").dropna()
    latency = pd.to_numeric(narrated.get("llm_latency_ms"), errors="coerce").dropna()
    return {
        "llm_calls": len(narrated),
        "narrated_rows": len(narrated),
        "cache_hit_rate": float(narrated["llm_cache_hit"].fillna(False).astype(bool).mean()),
        "error_rate": float((narrated.get("llm_error", pd.Series("", index=narrated.index)).fillna("") != "").mean()),
        "tokens_in_total": int(tokens_in.sum()),
        "tokens_out_total": int(tokens_out.sum()),
        "tokens_in_values": [float(value) for value in tokens_in.head(4000)],
        "latency_values": [float(value) for value in latency.head(4000)],
        "models": sorted({str(value) for value in narrated.get("llm_model", pd.Series(dtype=str)).dropna().unique()}),
        "providers": sorted({str(value) for value in narrated.get("llm_provider", pd.Series(dtype=str)).dropna().unique()}),
    }


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else round(number, 4)
