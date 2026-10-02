"""Cohort generation, serialisation and eligibility tests."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from insilico_trial_mas.agents.base import AgentContext
from insilico_trial_mas.agents.protocol_agent import ProtocolAgent
from insilico_trial_mas.cohort.generator import CohortGenerator, cohort_digest, cohort_summary, load_priors
from insilico_trial_mas.cohort.serialization import (
    COHORT_COLUMN_NAMES,
    frame_to_profiles,
    profile_to_row,
    profiles_to_frame,
    row_to_profile,
)
from insilico_trial_mas.reproducibility import rng_for


def test_generation_is_deterministic(protocol, config) -> None:
    first = CohortGenerator(protocol, config).generate()
    second = CohortGenerator(protocol, config).generate()
    assert cohort_digest(first) == cohort_digest(second)
    assert [p.baseline_sbp for p in first[:10]] == [p.baseline_sbp for p in second[:10]]


def test_different_seed_changes_the_cohort(protocol, config) -> None:
    first = CohortGenerator(protocol, config).generate()
    other_config = config
    other_config.seed = config.seed + 1
    second = CohortGenerator(protocol, other_config, rng=rng_for(config.seed + 1, "cohort")).generate()
    assert cohort_digest(first) != cohort_digest(second)


def test_cohort_summary_reports_plausible_distributions(protocol, config) -> None:
    summary = cohort_summary(CohortGenerator(protocol, config).generate())
    assert summary["n"] == config.n_patients
    assert 40 <= summary["mean_age"] <= 70
    assert 0.35 <= summary["female_fraction"] <= 0.65
    assert 20 <= summary["mean_bmi"] <= 34
    assert 110 <= summary["mean_baseline_sbp"] <= 145
    assert abs(sum(summary["ancestry_distribution"].values()) - 1.0) < 1e-6
    assert summary["cohorts"] == config.n_cohorts


def test_genomic_markers_follow_the_priors(protocol, config) -> None:
    priors = load_priors()
    profiles = CohortGenerator(protocol, config).generate()
    pm_rate = sum(p.genomic.cyp2d6 == "PM" for p in profiles) / len(profiles)
    expected = sum(
        priors["ancestries"][ancestry] * priors["cyp2d6_by_ancestry"][ancestry]["PM"]
        for ancestry in priors["ancestries"]
    )
    assert abs(pm_rate - expected) < 0.05


def test_comorbidities_and_medications_are_consistent(protocol, config) -> None:
    profiles = CohortGenerator(protocol, config).generate()
    for profile in profiles:
        assert len(profile.comorbidities) <= len(load_priors()["comorbidities"])
        assert profile.persona_text()
        if "hypertension" in profile.comorbidities and profile.concomitant_meds:
            assert all(isinstance(med, str) for med in profile.concomitant_meds)


def test_serialisation_round_trip(cohort) -> None:
    for profile in cohort[:25]:
        row = profile_to_row(profile, "high_dose")
        restored = row_to_profile(row)
        assert restored.patient_id == profile.patient_id
        assert restored.age == pytest.approx(profile.age, abs=1e-6)
        assert restored.comorbidities == profile.comorbidities
        assert restored.concomitant_meds == profile.concomitant_meds
        assert restored.genomic.model_dump() == profile.genomic.model_dump()
        assert restored.medical_history == profile.medical_history


def test_frame_round_trip_preserves_order_and_columns(cohort) -> None:
    frame = profiles_to_frame(cohort[:40], {"PT": "placebo"})
    assert list(frame.columns) == list(COHORT_COLUMN_NAMES)
    restored = frame_to_profiles(frame)
    assert [p.patient_id for p in restored] == [p.patient_id for p in cohort[:40]]


def test_row_parsing_tolerates_missing_and_nan_values() -> None:
    row = {
        "patient_id": "PT-X",
        "cohort_id": None,
        "age": float("nan"),
        "comorbidities_json": '["hypertension"]',
        "smoking": None,
    }
    profile = row_to_profile(row)
    assert profile.patient_id == "PT-X"
    assert profile.cohort_id == "COHORT-001"
    assert profile.comorbidities == ["hypertension"]
    assert profile.smoking == "never"


def test_row_parsing_accepts_comma_separated_history() -> None:
    profile = row_to_profile({"patient_id": "PT-Y", "comorbidities_json": "asthma, obesity"})
    assert profile.comorbidities == ["asthma", "obesity"]


def test_screening_enforces_criteria(protocol, config, cohort) -> None:
    agent = ProtocolAgent(
        AgentContext(run_id="t", protocol=protocol, config=config, seed=1, total_epochs=protocol.epochs)
    )
    outcome = agent.screen(cohort)
    criteria = protocol.eligibility
    for profile in outcome.enrolled:
        assert criteria.min_age <= profile.age <= criteria.max_age
        assert profile.egfr >= criteria.min_egfr
        assert profile.alt_u_l <= criteria.max_alt_u_l
        assert not (set(criteria.exclude_conditions) & set(profile.comorbidities))
    assert outcome.n_screened == len(cohort)
    assert outcome.summary()["failure_reasons"] or outcome.screen_failure_rate == 0


def test_randomisation_is_balanced_and_deterministic(protocol, config, cohort) -> None:
    agent = ProtocolAgent(
        AgentContext(run_id="t", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
    )
    screening = agent.screen(cohort)
    first = agent.randomize(screening.enrolled)
    second = ProtocolAgent(
        AgentContext(run_id="t", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
    ).randomize(screening.enrolled)
    assert first.arm_by_patient == second.arm_by_patient
    counts = first.arm_counts()
    total = sum(counts.values())
    # 25 / 37.5 / 37.5 allocation with permuted blocks: allow Monte Carlo slack.
    assert counts["placebo"] / total == pytest.approx(0.25, abs=0.08)
    assert counts["high_dose"] / total == pytest.approx(0.375, abs=0.08)


def test_allocation_table_is_auditable(protocol, config, cohort) -> None:
    agent = ProtocolAgent(
        AgentContext(run_id="t", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
    )
    screening = agent.screen(cohort)
    allocation = agent.randomize(screening.enrolled)
    table = pd.DataFrame(allocation.allocation_table)
    assert set(table.columns) == {"patient_id", "stratum", "block_index", "arm_id", "site_id", "cohort_id"}
    assert len(table) == len(screening.enrolled)
    # Blocks must be balanced by construction.
    for _, group in table.groupby(["stratum", "block_index"]):
        if len(group) == len(protocol.arms):
            assert group["arm_id"].nunique() == len(protocol.arms)


def test_directive_reflects_arm_and_titration(protocol, config, cohort) -> None:
    agent = ProtocolAgent(
        AgentContext(run_id="t", protocol=protocol, config=config, seed=1, total_epochs=protocol.epochs)
    )
    profile = cohort[0]
    directive = agent.directive(profile, 4, arm_id="high_dose")
    assert directive.dose_mg == protocol.dose_for_epoch("high_dose", 4)
    assert "administer" in directive.instruction
    placebo = agent.directive(profile, 4, arm_id="placebo")
    assert placebo.dose_mg == 0.0
    assert "withhold" in placebo.instruction


def test_json_serialisable_summary(protocol, config) -> None:
    summary = cohort_summary(CohortGenerator(protocol, config).generate())
    assert json.loads(json.dumps(summary, default=str))
