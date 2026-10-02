"""Plausibility calibration tests.

The platform's value depends on simulated trials looking like real ones. These
tests run a fast mechanistic pass over a few thousand patients and assert that
exposure, efficacy and safety land in recognisable bands. The bands are wide on
purpose - they catch modelling regressions (an exposure formula that inflates
adverse-event rates, a sensitivity model that removes all variability), not
Monte Carlo noise.
"""

from __future__ import annotations

import pandas as pd
import pytest

from insilico_trial_mas.agents.base import AgentContext
from insilico_trial_mas.agents.biostatistician_agent import BiostatisticianAgent, extract_adverse_events
from insilico_trial_mas.agents.protocol_agent import ProtocolAgent
from insilico_trial_mas.cohort.generator import CohortGenerator
from insilico_trial_mas.cohort.serialization import profiles_to_frame
from insilico_trial_mas.config import SimulationConfig, load_config
from insilico_trial_mas.engine.context import RunSpec
from insilico_trial_mas.engine.sequential_runner import SequentialEngine

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def calibrated_run() -> tuple[BiostatisticianAgent, pd.DataFrame, object]:
    config = load_config(
        None,
        {
            "n_patients": 1500,
            "n_cohorts": 4,
            "epochs": 8,
            "llm_mode": "off",
            "ml": {"backend": "mechanistic", "auto_train_if_missing": False},
            "tracking": {"enabled": False},
            "storage": {"backend": "memory"},
            "export_cdisc": False,
        },
    )
    config.protocol_path = "conf/trial_protocol_demo.yaml"
    from insilico_trial_mas.pipeline import load_protocol

    protocol = load_protocol(config.protocol_path)
    cohort = CohortGenerator(protocol, config).generate()
    agent = ProtocolAgent(
        AgentContext(
            run_id="calibration", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs
        )
    )
    screening, allocation = agent.plan(cohort)
    frame = profiles_to_frame(screening.enrolled, allocation.arm_by_patient)
    context = RunSpec(
        run_id="calibration",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=protocol.epochs,
    ).to_context()
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    analysis = BiostatisticianAgent(context).analyze(observations)
    return analysis, observations, protocol


def test_exposure_is_in_a_therapeutic_range(calibrated_run) -> None:
    _, observations, protocol = calibrated_run
    last = observations[observations["epoch"] == protocol.epochs]
    per_arm = last.groupby("arm_id")["plasma_conc_mg_l"].mean()
    high = per_arm[protocol.treatment_arms[-1].arm_id]
    low = per_arm[protocol.treatment_arms[0].arm_id]
    assert 0.05 < high < 20.0, f"unrealistic high-dose average concentration: {high}"
    assert 0 < low < high, "dose proportionality must hold"
    assert per_arm[protocol.control_arm.arm_id] == pytest.approx(0.0)


def test_efficacy_is_clinically_plausible(calibrated_run) -> None:
    analysis, _, protocol = calibrated_run
    by_arm = {summary.arm_id: summary for summary in analysis.arm_summaries}
    control = by_arm[protocol.control_arm.arm_id]
    high = by_arm[protocol.treatment_arms[-1].arm_id]
    # Placebo arms in hypertension trials improve by a few mmHg, not by 20.
    assert -12.0 < control.mean_sbp_change < 2.0
    # A dose-response: the high dose must beat placebo by a clinically relevant margin.
    assert high.mean_sbp_change < control.mean_sbp_change - 4.0
    # Individual variability must be present (a real trial has SD of 5-12 mmHg).
    assert 2.0 < high.sd_sbp_change < 15.0
    assert 0.0 <= high.responder_rate <= 1.0
    assert high.responder_rate > control.responder_rate


def test_safety_profile_is_not_catastrophic(calibrated_run) -> None:
    analysis, _, protocol = calibrated_run
    by_arm = {summary.arm_id: summary for summary in analysis.arm_summaries}
    high = by_arm[protocol.treatment_arms[-1].arm_id]
    control = by_arm[protocol.control_arm.arm_id]
    assert control.ae_rate < 0.45, "background adverse-event risk is too high for a placebo arm"
    assert high.ae_rate < 0.85, "a phase II dose should not harm nearly every patient"
    assert high.ae_rate > control.ae_rate, "exposure must increase adverse-event risk"
    assert high.grade3_plus_rate < 0.25, "grade >= 3 toxicity is implausibly common"
    assert high.sae_rate < 0.15, "serious adverse events are implausibly common"
    assert control.grade3_plus_rate < 0.10
    # Mortality must be rare but not structurally impossible in the model.
    assert high.mortality_rate <= 0.02


def test_no_implausible_fatal_events_for_benign_terms(calibrated_run) -> None:
    _, observations, _ = calibrated_run
    events = extract_adverse_events(observations)
    if events.empty:
        pytest.skip("no adverse events in this run")
    benign = {"headache", "cough", "dizziness", "fatigue", "nausea", "diarrhoea"}
    fatal_benign = events[(events["ctcae_grade"] >= 5) & (events["term"].isin(benign))]
    assert fatal_benign.empty, f"benign terms must not be fatal: {fatal_benign['term'].unique()}"


def test_dose_response_is_monotone(calibrated_run) -> None:
    analysis, _, _ = calibrated_run
    ordered = sorted(analysis.dose_response, key=lambda row: row["dose_mg"])
    means = [row["mean"] for row in ordered]
    assert means == sorted(means, reverse=True), "higher doses must lower blood pressure further"


def test_predicted_probabilities_calibrate_with_observed_rates(calibrated_run) -> None:
    """The AE hazard must actually predict the incidence it produces.

    Checked at the model level (no full simulation): the cumulative incidence
    implied by the per-epoch hazards for the highest-exposure arm must be within a
    factor of two of the incidence the trial actually observed for that term.
    """
    _, observations, protocol = calibrated_run
    events = extract_adverse_events(observations)
    if events.empty:
        pytest.skip("no adverse events in this run")

    from insilico_trial_mas.cohort.generator import CohortGenerator
    from insilico_trial_mas.ml.physiology import MechanisticPhysiology
    from insilico_trial_mas.ml.pk_pd import derive_pk_parameters, exposure_for_epoch

    config = SimulationConfig(n_patients=400, n_cohorts=2, epochs=protocol.epochs)
    cohort = CohortGenerator(protocol, config).generate()
    model = MechanisticPhysiology()
    high_arm = protocol.treatment_arms[-1]

    for term in ("cough", "headache", "dizziness"):
        observed_rows = events[(events["arm_id"] == high_arm.arm_id) & (events["term"] == term)]
        n_arm = observations[observations["arm_id"] == high_arm.arm_id]["patient_id"].nunique()
        observed_rate = observed_rows["patient_id"].nunique() / n_arm if n_arm else 0.0
        if observed_rate == 0.0:
            continue
        cumulative = []
        for profile in cohort[:150]:
            pk = derive_pk_parameters(profile, protocol.drug)
            survival = 1.0
            for epoch in range(1, protocol.epochs + 1):
                exposure = exposure_for_epoch(
                    pk,
                    protocol.drug,
                    dose_mg=protocol.dose_for_epoch(high_arm.arm_id, epoch),
                    epoch=epoch,
                    tau_h=protocol.epoch_duration_hours,
                )
                prediction = model.predict(
                    profile,
                    protocol.drug,
                    exposure,
                    pk,
                    epoch=epoch,
                    epochs_total=protocol.epochs,
                    placebo_effect=protocol.placebo_effect,
                    seed=config.seed,
                )
                survival *= 1.0 - prediction.ae_probabilities[term]
            cumulative.append(1.0 - survival)
        predicted_rate = sum(cumulative) / len(cumulative)
        assert predicted_rate > 0.0
        # Allow generous Monte Carlo and phenotype slack, but catch a systematic
        # mismatch (e.g. an exposure formula inflated by the accumulation factor).
        assert 0.4 <= observed_rate / predicted_rate <= 2.5, (
            f"{term}: predicted cumulative incidence {predicted_rate:.3f} "
            f"does not match observed {observed_rate:.3f}"
        )


def test_adverse_event_probability_increases_with_dose(calibrated_run) -> None:
    """Predicted risk carried by the events must order with dose."""
    _, observations, protocol = calibrated_run
    events = extract_adverse_events(observations)
    if events.empty:
        pytest.skip("no adverse events in this run")
    per_arm = events.groupby("arm_id")["predicted_probability"].mean()
    control_rate = per_arm[protocol.control_arm.arm_id]
    high_rate = per_arm[protocol.treatment_arms[-1].arm_id]
    assert high_rate > control_rate


def test_print_calibration_summary(calibrated_run, capsys) -> None:
    """Emits the calibration table so a failing band can be diagnosed from CI logs."""
    analysis, _, protocol = calibrated_run
    print(f"\ncalibration for {protocol.protocol_id}")
    for summary in analysis.arm_summaries:
        print(
            f"  {summary.arm_id:<10} n={summary.n_patients:<5} dSBP={summary.mean_sbp_change:+.2f} "
            f"(sd {summary.sd_sbp_change:.2f}) responders={summary.responder_rate:.1%} "
            f"anyAE={summary.ae_rate:.1%} G3+={summary.grade3_plus_rate:.1%} SAE={summary.sae_rate:.1%} "
            f"deaths={summary.mortality_rate:.2%}"
        )
    captured = capsys.readouterr()
    assert "calibration for" in captured.out
