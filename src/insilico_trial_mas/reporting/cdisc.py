"""Compliance-oriented exports: CDISC-inspired SDTM/ADaM datasets.

The platform does not claim regulatory submission readiness, but the export is
deliberately shaped like the datasets a sponsor would hand to a statistical
programming team: ``DM`` (demographics), ``EX`` (exposure), ``VS`` (vital signs),
``AE`` (adverse events), plus analysis-ready ``ADSL``/``ADVS``/``ADAE`` views with
variable labels in a machine-readable data definition file.

Because the source data is simulated, this is the *structure* of a submission
package, not a substitute for one - which is exactly what makes it useful as a
contract for the production pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..logging_utils import get_logger
from ..schemas import PatientProfile

logger = get_logger("reporting.cdisc")

#: Dataset -> variable -> label (drives both the CSV headers and define.json).
VARIABLE_LABELS: dict[str, dict[str, str]] = {
    "DM": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "SITEID": "Study site identifier",
        "ARM": "Planned arm",
        "ARMCD": "Planned arm code",
        "AGE": "Age at enrolment (years)",
        "SEX": "Sex",
        "RACE": "Ancestry group (synthetic)",
        "BMIBL": "Baseline BMI (kg/m2)",
        "COMORBN": "Number of baseline comorbidities",
        "CYP2D6": "CYP2D6 metaboliser phenotype (synthetic)",
        "HLAB5701": "HLA-B*57:01 carrier status (synthetic)",
    },
    "EX": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "EXTRT": "Name of study treatment",
        "EXDOSE": "Dose administered (mg)",
        "EXDOSU": "Dose units",
        "EPOCH": "Simulated dosing epoch",
        "EXSTDTC": "Simulated start of dose (hours from first dose)",
    },
    "VS": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "EPOCH": "Simulated epoch",
        "VSTESTCD": "Vital signs test code",
        "VSORRES": "Result",
        "VSORRESU": "Result units",
        "VSBLFL": "Baseline flag",
    },
    "AE": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "AETERM": "Reported term for the adverse event",
        "AESOC": "System organ class",
        "AETOXGR": "CTCAE grade",
        "AESER": "Serious event flag",
        "AEREL": "Causality",
        "AESOURCE": "Origin of the report (model, llm, hybrid)",
        "AEPREDP": "Model-predicted probability",
        "AESTDTC": "Simulated onset epoch",
    },
    "ADSL": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "TRT01P": "Planned treatment",
        "TRT01A": "Actual treatment",
        "SAFFL": "Safety population flag",
        "ITTFL": "Intention-to-treat flag",
        "DISCONFL": "Discontinued from treatment flag",
        "DCSREAS": "Reason for discontinuation",
    },
    "ADVS": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "PARAMCD": "Parameter code",
        "AVAL": "Analysis value",
        "CHG": "Change from baseline",
        "EPOCH": "Simulated epoch",
    },
    "ADAE": {
        "STUDYID": "Study identifier",
        "USUBJID": "Unique subject identifier",
        "TRTA": "Actual treatment",
        "AEDECOD": "Dictionary-derived term",
        "AETOXGR": "CTCG grade",
        "TRTEMFL": "Treatment-emergent flag",
        "AESER": "Serious flag",
    },
}

STUDY_ID = "INSILICO-001"


@dataclass(slots=True)
class ExportResult:
    """Paths of the exported datasets plus the data-definition file."""

    directory: str
    datasets: dict[str, str] = field(default_factory=dict)
    define_path: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"directory": self.directory, "datasets": self.datasets, "define": self.define_path, "rows": self.row_counts}


def _usubjid(patient_id: str) -> str:
    return f"{STUDY_ID}-{patient_id}"


def build_dm(profiles: list[PatientProfile], arm_by_patient: dict[str, str]) -> pd.DataFrame:
    rows = []
    for profile in profiles:
        arm_id = arm_by_patient.get(profile.patient_id, "")
        rows.append(
            {
                "STUDYID": STUDY_ID,
                "USUBJID": _usubjid(profile.patient_id),
                "SITEID": profile.site_id,
                "ARM": arm_id,
                "ARMCD": arm_id.upper()[:8],
                "AGE": profile.age,
                "SEX": profile.sex,
                "RACE": profile.genomic.ancestry,
                "BMIBL": profile.bmi,
                "COMORBN": profile.comorbidity_count,
                "CYP2D6": profile.genomic.cyp2d6,
                "HLAB5701": "Y" if profile.genomic.hla_b_57_01 else "N",
            }
        )
    return pd.DataFrame(rows, columns=list(VARIABLE_LABELS["DM"]))


def build_ex(observations: pd.DataFrame, drug_name: str) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["EX"]))
    dosed = observations[pd.to_numeric(observations["dose_mg"], errors="coerce").fillna(0) > 0]
    return pd.DataFrame(
        {
            "STUDYID": STUDY_ID,
            "USUBJID": dosed["patient_id"].map(_usubjid),
            "EXTRT": drug_name,
            "EXDOSE": pd.to_numeric(dosed["dose_mg"], errors="coerce"),
            "EXDOSU": "mg",
            "EPOCH": pd.to_numeric(dosed["epoch"], errors="coerce").astype("Int64"),
            "EXSTDTC": pd.to_numeric(dosed["time_hours"], errors="coerce"),
        }
    ).reset_index(drop=True)


def build_vs(observations: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["VS"]))
    working = observations.copy()
    working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").astype("Int64")
    records = []
    for code, column, unit in (
        ("SYSBP", "sbp", "mmHg"),
        ("DIABP", "dbp", "mmHg"),
        ("HR", "hr", "beats/min"),
        ("QTcF", "qtc_ms", "ms"),
        ("ALT", "alt_u_l", "U/L"),
    ):
        if column not in working.columns:
            continue
        records.append(
            pd.DataFrame(
                {
                    "STUDYID": STUDY_ID,
                    "USUBJID": working["patient_id"].map(_usubjid),
                    "EPOCH": working["epoch"],
                    "VSTESTCD": code,
                    "VSORRES": pd.to_numeric(working[column], errors="coerce"),
                    "VSORRESU": unit,
                    "VSBLFL": working["epoch"].eq(0).map({True: "Y", False: ""}),
                }
            )
        )
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame(columns=list(VARIABLE_LABELS["VS"]))


def build_ae(adverse_events: pd.DataFrame) -> pd.DataFrame:
    if adverse_events.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["AE"]))
    return pd.DataFrame(
        {
            "STUDYID": STUDY_ID,
            "USUBJID": adverse_events["patient_id"].map(_usubjid),
            "AETERM": adverse_events["term"],
            "AESOC": adverse_events["soc"],
            "AETOXGR": adverse_events["ctcae_grade"],
            "AESER": adverse_events["serious"].map({True: "Y", False: "N"}),
            "AEREL": adverse_events["relatedness"],
            "AESOURCE": adverse_events["source"],
            "AEPREDP": adverse_events["predicted_probability"],
            "AESTDTC": adverse_events["epoch"],
        }
    ).reset_index(drop=True)


def build_adsl(observations: pd.DataFrame, adverse_events: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["ADSL"]))
    working = observations.copy()
    working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").fillna(0).astype(int)
    last = working.groupby("patient_id")["epoch"].transform("max")
    eot = working[working["epoch"] == last]
    grade3 = (
        adverse_events[adverse_events["ctcae_grade"] >= 3].groupby("patient_id").size().rename("grade3_events")
        if not adverse_events.empty
        else pd.Series(dtype=int, name="grade3_events")
    )
    merged = eot.merge(grade3, left_on="patient_id", right_index=True, how="left")
    return pd.DataFrame(
        {
            "STUDYID": STUDY_ID,
            "USUBJID": merged["patient_id"].map(_usubjid),
            "TRT01P": merged["arm_label"],
            "TRT01A": merged["arm_label"],
            "SAFFL": "Y",
            "ITTFL": "Y",
            "DISCONFL": merged.get("discontinued", pd.Series(False, index=merged.index)).fillna(False).map({True: "Y", False: "N"}),
            "DCSREAS": merged.get("discontinuation_reason", pd.Series("", index=merged.index)).fillna(""),
        }
    ).reset_index(drop=True)


def build_advs(observations: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["ADVS"]))
    working = observations.copy()
    working["epoch"] = pd.to_numeric(working["epoch"], errors="coerce").astype("Int64")
    records = []
    for code, column, change_column in (
        ("SYSBP", "sbp", "sbp_change"),
        ("DIABP", "dbp", "dbp_change"),
        ("HR", "hr", "hr_change"),
        ("COMPOSITE", "biomarker_composite", None),
    ):
        if column not in working.columns:
            continue
        records.append(
            pd.DataFrame(
                {
                    "STUDYID": STUDY_ID,
                    "USUBJID": working["patient_id"].map(_usubjid),
                    "PARAMCD": code,
                    "AVAL": pd.to_numeric(working[column], errors="coerce"),
                    "CHG": pd.to_numeric(working[change_column], errors="coerce") if change_column else pd.NA,
                    "EPOCH": working["epoch"],
                }
            )
        )
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame(columns=list(VARIABLE_LABELS["ADVS"]))


def build_adae(adverse_events: pd.DataFrame, observations: pd.DataFrame) -> pd.DataFrame:
    if adverse_events.empty:
        return pd.DataFrame(columns=list(VARIABLE_LABELS["ADAE"]))
    arm_map = (
        observations.drop_duplicates("patient_id").set_index("patient_id")["arm_label"].to_dict()
        if not observations.empty
        else {}
    )
    return pd.DataFrame(
        {
            "STUDYID": STUDY_ID,
            "USUBJID": adverse_events["patient_id"].map(_usubjid),
            "TRTA": adverse_events["patient_id"].map(arm_map).fillna(""),
            "AEDECOD": adverse_events["term"],
            "AETOXGR": adverse_events["ctcae_grade"],
            "TRTEMFL": pd.to_numeric(adverse_events["epoch"], errors="coerce").fillna(0).gt(0).map({True: "Y", False: "N"}),
            "AESER": adverse_events["serious"].map({True: "Y", False: "N"}),
        }
    ).reset_index(drop=True)


def write_define_xml(directory: Path, row_counts: dict[str, int]) -> Path:
    """Write a machine-readable data definition file (define.json)."""
    payload = {
        "study": STUDY_ID,
        "standard": "CDISC-inspired SDTM/ADaM subset (structural contract only, simulated data)",
        "generated_by": "insilico-trial-mas",
        "datasets": [
            {
                "name": name,
                "rows": int(row_counts.get(name, 0)),
                "variables": [
                    {"name": variable, "label": label} for variable, label in VARIABLE_LABELS[name].items()
                ],
            }
            for name in VARIABLE_LABELS
        ],
    }
    path = directory / "define.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def export_cdisc(
    directory: str | Path,
    *,
    profiles: list[PatientProfile],
    arm_by_patient: dict[str, str],
    observations: pd.DataFrame,
    adverse_events: pd.DataFrame,
    drug_name: str = "investigational product",
) -> ExportResult:
    """Write DM/EX/VS/AE/ADSL/ADVS/ADAE CSVs plus ``define.json``."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    datasets = {
        "DM": build_dm(profiles, arm_by_patient),
        "EX": build_ex(observations, drug_name),
        "VS": build_vs(observations),
        "AE": build_ae(adverse_events),
        "ADSL": build_adsl(observations, adverse_events),
        "ADVS": build_advs(observations),
        "ADAE": build_adae(adverse_events, observations),
    }
    result = ExportResult(directory=str(target))
    for name, frame in datasets.items():
        path = target / f"{name.lower()}.csv"
        frame.to_csv(path, index=False)
        result.datasets[name] = str(path)
        result.row_counts[name] = len(frame)
    result.define_path = str(write_define_xml(target, result.row_counts))
    logger.info(f"exported CDISC-inspired datasets to {target}")
    return result
