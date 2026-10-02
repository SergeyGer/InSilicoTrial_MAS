"""Protocol, configuration and schema contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from insilico_trial_mas.config import ConfigurationError, load_config, validate_config
from insilico_trial_mas.errors import ProtocolValidationError
from insilico_trial_mas.pipeline import load_protocol
from insilico_trial_mas.schemas import (
    SILVER_COLUMN_NAMES,
    PatientStateObservation,
    TrialProtocol,
)


def protocol_payload() -> dict:
    return yaml.safe_load(Path("conf/trial_protocol_demo.yaml").read_text(encoding="utf-8"))


def test_protocol_loads_and_is_consistent(protocol: TrialProtocol) -> None:
    assert protocol.protocol_id == "INS-HTN-201"
    assert protocol.control_arm is not None and protocol.control_arm.is_control
    assert len(protocol.treatment_arms) == 2
    assert protocol.primary_endpoint.column == "sbp_change"
    assert protocol.responder_endpoint.kind == "binary"
    assert protocol.digest() == protocol.digest(), "digest must be stable"


def test_digest_changes_when_protocol_changes(protocol: TrialProtocol) -> None:
    modified = protocol.model_copy(deep=True)
    modified.arms[1].dose_mg += 1
    assert modified.digest() != protocol.digest()


def test_dose_for_epoch_applies_titration(protocol: TrialProtocol) -> None:
    # high_dose: 150 mg from epoch 1, +25 mg per epoch from epoch 3, capped at 200 mg.
    assert protocol.dose_for_epoch("high_dose", 0) == 0.0
    assert protocol.dose_for_epoch("high_dose", 1) == 150.0
    assert protocol.dose_for_epoch("high_dose", 2) == 150.0
    assert protocol.dose_for_epoch("high_dose", 3) == 175.0
    assert protocol.dose_for_epoch("high_dose", 4) == 200.0
    assert protocol.dose_for_epoch("high_dose", 5) == 200.0  # capped
    assert protocol.dose_for_epoch("placebo", 4) == 0.0


def test_parallel_design_requires_a_control_arm() -> None:
    payload = protocol_payload()
    for arm in payload["arms"]:
        arm["is_control"] = False
    with pytest.raises(ValidationError, match="control arm"):
        TrialProtocol.model_validate(payload)


def test_control_arm_must_be_placebo() -> None:
    payload = protocol_payload()
    payload["arms"][0]["dose_mg"] = 10
    with pytest.raises(ValidationError, match="placebo"):
        TrialProtocol.model_validate(payload)


def test_binary_endpoint_requires_threshold() -> None:
    payload = protocol_payload()
    payload["endpoints"][1].pop("response_threshold")
    with pytest.raises(ValidationError, match="response_threshold"):
        TrialProtocol.model_validate(payload)


def test_stopping_rules_reference_known_arms() -> None:
    payload = protocol_payload()
    payload["stopping_rules"][0]["applies_to_arms"] = ["ghost_arm"]
    with pytest.raises(ValidationError, match="unknown arms"):
        TrialProtocol.model_validate(payload)


def test_duplicate_ae_terms_are_rejected() -> None:
    payload = protocol_payload()
    payload["drug"]["ae_models"].append(copy.deepcopy(payload["drug"]["ae_models"][0]))
    with pytest.raises(ValidationError, match="duplicate adverse-event terms"):
        TrialProtocol.model_validate(payload)


def test_ae_grade_distribution_is_normalised() -> None:
    payload = protocol_payload()
    payload["drug"]["ae_models"][0]["grade_distribution"] = [1, 1, 1, 1, 1]
    protocol = TrialProtocol.model_validate(payload)
    assert sum(protocol.drug.ae_models[0].grade_distribution) == pytest.approx(1.0)
    assert protocol.drug.ae_models[0].grade_distribution[0] == pytest.approx(0.2)


def test_endpoint_beyond_protocol_length_is_rejected() -> None:
    payload = protocol_payload()
    payload["endpoints"][0]["epoch"] = payload["epochs"] + 5
    with pytest.raises(ValidationError, match="beyond protocol length"):
        TrialProtocol.model_validate(payload)


def test_silver_contract_is_self_consistent() -> None:
    assert len(SILVER_COLUMN_NAMES) == len(set(SILVER_COLUMN_NAMES))
    assert "observation_id" in SILVER_COLUMN_NAMES
    assert "adverse_events_json" in SILVER_COLUMN_NAMES
    observation = PatientStateObservation(
        observation_id="PT-1:001",
        sim_run_id="RUN-1",
        protocol_id="P",
        protocol_version="1.0.0",
        patient_id="PT-1",
        cohort_id="COHORT-001",
        site_id="SITE-01",
        arm_id="placebo",
        arm_label="Placebo",
        epoch=1,
        time_hours=12.0,
        dose_mg=0.0,
        plasma_conc_mg_l=0.0,
        auc_epoch_mg_h_l=0.0,
        cumulative_exposure=0.0,
        sbp=130.0,
        dbp=80.0,
        hr=70.0,
        qtc_ms=410.0,
        alt_u_l=25.0,
        ast_u_l=24.0,
        creatinine_mg_dl=0.9,
        egfr=90.0,
        sbp_change=-2.0,
        dbp_change=-1.0,
        hr_change=0.5,
        biomarker_composite=0.1,
        responder=False,
        worst_ctcae_grade=0,
        n_adverse_events=0,
    )
    row = observation.as_row()
    assert list(row) == list(SILVER_COLUMN_NAMES)
    assert json.dumps(row, default=str)


def test_config_defaults_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSILICO_ENGINE__BACKEND", "spark")
    monkeypatch.setenv("INSILICO_N_PATIENTS", "42")
    config = load_config(None)
    assert config.engine.backend == "spark"
    assert config.n_patients == 42


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration keys"):
        load_config(None, {"engine": {"turbo": True}})


def test_config_rejects_invalid_values() -> None:
    with pytest.raises(ConfigurationError, match="n_patients"):
        load_config(None, {"n_patients": 0})
    with pytest.raises(ConfigurationError, match="llm_sample_rate"):
        load_config(None, {"llm_sample_rate": 1.5})
    with pytest.raises(ConfigurationError, match="safety cap"):
        load_config(None, {"n_patients": 10, "max_patients": 5})


def test_config_literal_validation() -> None:
    with pytest.raises(ConfigurationError, match="not one of"):
        load_config(None, {"llm_mode": "sometimes"})


def test_schema_for_maps_medallion_layers() -> None:
    config = load_config(None, {"storage": {"silver_schema": "silver_custom"}})
    assert config.storage.schema_for("silver") == "silver_custom"
    assert config.storage.schema_for("other") == "other"


def test_validate_config_accepts_repository_profiles() -> None:
    for path in ("conf/simulation_local.yaml", "conf/simulation_cluster.yaml", "conf/simulation_community_edition.yaml"):
        config = load_config(path)
        validate_config(config)
        assert config.protocol_path


def test_missing_protocol_file_raises() -> None:
    with pytest.raises(ConfigurationError, match="protocol file not found"):
        load_protocol("conf/does_not_exist.yaml")


def test_protocol_validation_error_type(protocol: TrialProtocol) -> None:
    from insilico_trial_mas.agents.base import AgentContext
    from insilico_trial_mas.agents.protocol_agent import ProtocolAgent

    broken = protocol.model_copy(deep=True)
    broken.arms[1].dose_mg = 0.0  # treatment arm without a dose
    agent = ProtocolAgent(
        AgentContext(run_id="x", protocol=broken, config=load_config(None), seed=1, total_epochs=broken.epochs)
    )
    with pytest.raises(ProtocolValidationError):
        agent.validate()
