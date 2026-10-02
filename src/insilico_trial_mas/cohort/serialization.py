"""Serialisation contract between the driver, the local engines and Spark workers.

The cohort table travels as a flat, Arrow-friendly frame: nested genomic markers
and list-valued history fields are JSON-encoded. Both directions are implemented
here and tested for round-trip fidelity so a Spark ``mapInPandas`` call cannot
silently drop a column.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandas as pd

from ..schemas import GenomicProfile, PatientProfile

#: Canonical columns of the input cohort table (``bronze.synthetic_cohort``).
COHORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("patient_id", "string"),
    ("cohort_id", "string"),
    ("site_id", "string"),
    ("arm_id", "string"),
    ("enrolled", "bool"),
    ("age", "double"),
    ("sex", "string"),
    ("weight_kg", "double"),
    ("height_cm", "double"),
    ("bmi", "double"),
    ("baseline_sbp", "double"),
    ("baseline_dbp", "double"),
    ("baseline_hr", "double"),
    ("egfr", "double"),
    ("alt_u_l", "double"),
    ("ast_u_l", "double"),
    ("creatinine_mg_dl", "double"),
    ("comorbidities_json", "string"),
    ("concomitant_meds_json", "string"),
    ("medical_history", "string"),
    ("smoking", "string"),
    ("alcohol_units_week", "double"),
    ("ancestry", "string"),
    ("cyp2d6", "string"),
    ("cyp3a4", "string"),
    ("hla_b_57_01", "bool"),
    ("hla_dq2_2", "bool"),
    ("adrb1_arg389gly", "bool"),
    ("ace_dd", "bool"),
    ("slco1b1_decreased", "bool"),
)

COHORT_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in COHORT_COLUMNS)


def profile_to_row(profile: PatientProfile, arm_id: str = "") -> dict[str, Any]:
    """Flatten a :class:`PatientProfile` into a Spark/Arrow-friendly row."""
    return {
        "patient_id": profile.patient_id,
        "cohort_id": profile.cohort_id,
        "site_id": profile.site_id,
        "arm_id": arm_id,
        "enrolled": bool(profile.enrolled),
        "age": float(profile.age),
        "sex": profile.sex,
        "weight_kg": float(profile.weight_kg),
        "height_cm": float(profile.height_cm),
        "bmi": float(profile.bmi),
        "baseline_sbp": float(profile.baseline_sbp),
        "baseline_dbp": float(profile.baseline_dbp),
        "baseline_hr": float(profile.baseline_hr),
        "egfr": float(profile.egfr),
        "alt_u_l": float(profile.alt_u_l),
        "ast_u_l": float(profile.ast_u_l),
        "creatinine_mg_dl": float(profile.creatinine_mg_dl),
        "comorbidities_json": json.dumps(profile.comorbidities),
        "concomitant_meds_json": json.dumps(profile.concomitant_meds),
        "medical_history": profile.medical_history,
        "smoking": profile.smoking,
        "alcohol_units_week": float(profile.alcohol_units_week),
        "ancestry": profile.genomic.ancestry,
        "cyp2d6": profile.genomic.cyp2d6,
        "cyp3a4": profile.genomic.cyp3a4,
        "hla_b_57_01": bool(profile.genomic.hla_b_57_01),
        "hla_dq2_2": bool(profile.genomic.hla_dq2_2),
        "adrb1_arg389gly": bool(profile.genomic.adrb1_arg389gly),
        "ace_dd": bool(profile.genomic.ace_dd),
        "slco1b1_decreased": bool(profile.genomic.slco1b1_decreased),
    }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, float) and pd.isna(value):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
        return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    return str(value)


def row_to_profile(row: Mapping[str, Any]) -> PatientProfile:
    """Rebuild a :class:`PatientProfile` from a flat row (Spark -> agent)."""
    genomic = GenomicProfile(
        ancestry=_as_str(row.get("ancestry"), "EUR") or "EUR",
        cyp2d6=_as_str(row.get("cyp2d6"), "NM") or "NM",  # type: ignore[arg-type]
        cyp3a4=_as_str(row.get("cyp3a4"), "NM") or "NM",  # type: ignore[arg-type]
        hla_b_57_01=_as_bool(row.get("hla_b_57_01")),
        hla_dq2_2=_as_bool(row.get("hla_dq2_2")),
        adrb1_arg389gly=_as_bool(row.get("adrb1_arg389gly")),
        ace_dd=_as_bool(row.get("ace_dd")),
        slco1b1_decreased=_as_bool(row.get("slco1b1_decreased")),
    )
    profile = PatientProfile(
        patient_id=_as_str(row.get("patient_id")),
        cohort_id=_as_str(row.get("cohort_id"), "COHORT-001") or "COHORT-001",
        site_id=_as_str(row.get("site_id"), "SITE-01") or "SITE-01",
        age=_as_float(row.get("age"), 55.0),
        sex="F" if _as_str(row.get("sex"), "F").upper().startswith("F") else "M",
        weight_kg=_as_float(row.get("weight_kg"), 75.0),
        height_cm=_as_float(row.get("height_cm"), 170.0),
        bmi=_as_float(row.get("bmi"), 26.0),
        baseline_sbp=_as_float(row.get("baseline_sbp"), 130.0),
        baseline_dbp=_as_float(row.get("baseline_dbp"), 80.0),
        baseline_hr=_as_float(row.get("baseline_hr"), 72.0),
        egfr=_as_float(row.get("egfr"), 90.0),
        alt_u_l=_as_float(row.get("alt_u_l"), 25.0),
        ast_u_l=_as_float(row.get("ast_u_l"), 24.0),
        creatinine_mg_dl=_as_float(row.get("creatinine_mg_dl"), 0.9),
        comorbidities=_as_list(row.get("comorbidities_json")),
        concomitant_meds=_as_list(row.get("concomitant_meds_json")),
        genomic=genomic,
        medical_history=_as_str(row.get("medical_history")),
        smoking=_as_str(row.get("smoking"), "never") or "never",  # type: ignore[arg-type]
        alcohol_units_week=_as_float(row.get("alcohol_units_week"), 0.0),
        enrolled=_as_bool(row.get("enrolled", True)),
    )
    return profile


def profiles_to_frame(profiles: Sequence[PatientProfile], arm_by_patient: Mapping[str, str] | None = None) -> pd.DataFrame:
    """Build the cohort DataFrame (driver side)."""
    arm_by_patient = arm_by_patient or {}
    rows = [profile_to_row(p, arm_by_patient.get(p.patient_id, "")) for p in profiles]
    frame = pd.DataFrame(rows, columns=list(COHORT_COLUMN_NAMES))
    return frame


def frame_to_profiles(frame: pd.DataFrame) -> list[PatientProfile]:
    """Rebuild profiles from a cohort DataFrame (worker side)."""
    columns = [c for c in COHORT_COLUMN_NAMES if c in frame.columns]
    return [row_to_profile(record) for record in frame[columns].to_dict(orient="records")]


def arm_map_from_frame(frame: pd.DataFrame) -> dict[str, str]:
    """Extract ``patient_id -> arm_id`` from a cohort frame (worker side)."""
    if "arm_id" not in frame.columns:
        return {}
    return {
        str(pid): str(arm)
        for pid, arm in zip(frame["patient_id"].tolist(), frame["arm_id"].tolist(), strict=True)
        if str(arm)
    }
