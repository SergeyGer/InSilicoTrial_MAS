"""Agent-level tests: Patient Persona behaviour and Biostatistician analytics."""

from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest

from insilico_trial_mas.agents.biostatistician_agent import (
    BiostatisticianAgent,
    end_of_treatment_frame,
    extract_adverse_events,
)
from insilico_trial_mas.agents.patient_agent import MAX_LLM_CALLS_PER_PATIENT, PatientPersonaAgent
from insilico_trial_mas.engine.runtime import WorkerRuntime, reset_runtimes
from insilico_trial_mas.llm.base import BaseLLMClient, LLMRequest, LLMResponse


class CountingClient(BaseLLMClient):
    provider = "counting"
    model = "counting-v1"
    offline = True

    def __init__(self, text: str | None = None) -> None:
        self.calls = 0
        self.text = text

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.text is not None:
            body = self.text
        else:
            probabilities = request.context.get("ae_probabilities", {})
            worst = int(request.context.get("worst_grade", 1) or 1)
            top = max(probabilities.items(), key=lambda kv: kv[1])[0] if probabilities else "fatigue"
            body = json.dumps(
                {
                    "symptoms": [{"term": top, "ctcae_grade": max(1, worst), "verbatim": "persona report"}],
                    "overall_tolerability": "acceptable",
                    "adherence_intent": "continue",
                }
            )
        return LLMResponse(
            text=body,
            provider=self.provider,
            model=self.model,
            tokens_in=10,
            tokens_out=20,
            latency_ms=1.5,
        )


def _runtime(context, client: BaseLLMClient) -> WorkerRuntime:
    reset_runtimes()
    return WorkerRuntime.create(context, llm_client=client)


def test_patient_agent_produces_full_timeline(context, planned, protocol) -> None:
    _, _, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles

    profiles = frame_to_profiles(frame)
    client = CountingClient()
    runtime = _runtime(context, client)
    outcome = asyncio.run(PatientPersonaAgent(profiles[0], context, runtime).run())

    epochs = context.config.epochs or protocol.epochs
    assert [obs.epoch for obs in outcome.observations] == list(range(0, epochs + 1))
    baseline = outcome.observations[0]
    assert baseline.dose_mg == 0.0
    assert baseline.plasma_conc_mg_l == 0.0
    assert baseline.llm_used is False, "the baseline epoch is never narrated"
    dosed = outcome.observations[1]
    assert dosed.dose_mg == protocol.dose_for_epoch(profiles[0].patient_id and context.arm_of(profiles[0].patient_id), 1)
    assert dosed.observation_id.endswith("001")
    assert dosed.sim_run_id == context.run_id


def test_patient_agent_is_deterministic(context, planned) -> None:
    _, _, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles
    from insilico_trial_mas.engine.partition import comparable_rows

    profile = frame_to_profiles(frame)[0]
    first = asyncio.run(PatientPersonaAgent(profile, context, _runtime(context, CountingClient())).run())
    second = asyncio.run(PatientPersonaAgent(profile, context, _runtime(context, CountingClient())).run())
    # Wall-clock metadata (observation_ts, llm_latency_ms) is excluded: it records
    # when the run happened, not what the simulation produced.
    assert comparable_rows(first.rows()) == comparable_rows(second.rows())


def test_patient_agent_records_llm_telemetry(context, planned) -> None:
    _, _, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles

    profile = frame_to_profiles(frame)[0]
    runtime = _runtime(context, CountingClient())
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())
    narrated = [obs for obs in outcome.observations if obs.llm_used]
    if narrated:
        assert narrated[0].llm_provider == "counting"
        assert narrated[0].llm_tokens_out == 20
        assert narrated[0].prompt_hash
    assert outcome.llm_calls <= MAX_LLM_CALLS_PER_PATIENT


def test_llm_is_not_called_when_mode_is_off(protocol, planned, fast_config_factory, tmp_path) -> None:
    _, allocation, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles

    config = fast_config_factory(llm_mode="off")
    from insilico_trial_mas.engine.context import RunSpec

    context = RunSpec(
        run_id="off-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs,
    ).to_context()
    client = CountingClient()
    runtime = _runtime(context, client)
    profile = frame_to_profiles(frame)[0]
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())
    assert client.calls == 0
    assert outcome.llm_calls == 0
    assert all(obs.llm_used is False for obs in outcome.observations)


def test_llm_failure_is_recorded_not_fatal(protocol, planned, fast_config_factory, tmp_path) -> None:
    _, allocation, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles
    from insilico_trial_mas.engine.context import RunSpec

    config = fast_config_factory(llm_mode="all")
    context = RunSpec(
        run_id="fail-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs,
    ).to_context()

    class Broken(BaseLLMClient):
        provider = "broken"
        model = "broken-v1"

        async def acomplete(self, request: LLMRequest) -> LLMResponse:
            raise RuntimeError("provider exploded")

    runtime = _runtime(context, Broken())
    profile = frame_to_profiles(frame)[0]
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())
    assert outcome.llm_errors > 0
    assert all(obs.llm_error for obs in outcome.observations if obs.epoch > 0)
    # The simulation still produced a complete, usable timeline.
    assert len(outcome.observations) == (config.epochs or protocol.epochs) + 1


def test_llm_symptoms_are_merged_with_modelled_events(protocol, planned, fast_config_factory, tmp_path) -> None:
    _, allocation, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles
    from insilico_trial_mas.engine.context import RunSpec

    config = fast_config_factory(llm_mode="all")
    context = RunSpec(
        run_id="merge-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs,
    ).to_context()
    client = CountingClient(
        text='{"symptoms": [{"term": "unusual_dreams", "ctcae_grade": 2, "verbatim": "vivid dreams"}], '
        '"overall_tolerability": "poor", "adherence_intent": "unsure"}'
    )
    runtime = _runtime(context, client)
    profile = frame_to_profiles(frame)[0]
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())
    narrated = [obs for obs in outcome.observations if obs.llm_used]
    assert narrated, "mode 'all' must narrate dosing epochs"
    terms = {event["term"] for event in narrated[0].adverse_events()}
    assert "unusual_dreams" in terms
    assert any(event["source"] == "llm" for event in narrated[0].adverse_events())
    assert narrated[0].symptom_summary


def test_grade_four_event_discontinues_dosing(protocol, planned, fast_config_factory, tmp_path) -> None:
    _, allocation, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles
    from insilico_trial_mas.engine.context import RunSpec

    config = fast_config_factory(llm_mode="all", epochs=6)
    context = RunSpec(
        run_id="discontinue-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs,
    ).to_context()
    client = CountingClient(
        text='{"symptoms": [{"term": "hypotension", "ctcae_grade": 4, "verbatim": "I fainted"}], '
        '"overall_tolerability": "poor", "adherence_intent": "discontinue"}'
    )
    runtime = _runtime(context, client)
    profile = frame_to_profiles(frame)[0]
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())
    assert outcome.discontinued_at_epoch is not None
    stop = outcome.discontinued_at_epoch
    later = [obs for obs in outcome.observations if obs.epoch > stop]
    assert later, "there must be epochs after the discontinuation"
    assert all(obs.dose_mg == 0.0 for obs in later), "no study drug after discontinuation"
    assert all(obs.discontinued for obs in later)


def test_biostatistician_produces_coherent_analytics(context, planned, protocol) -> None:
    from insilico_trial_mas.engine.sequential_runner import SequentialEngine

    _, _, frame = planned
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    analysis = BiostatisticianAgent(context).analyze(
        observations, screening_summary={"screened": 200, "screen_failures": 40, "failure_reasons": {"age": 40}}
    )
    assert analysis.arm_summaries, "every arm must be summarised"
    assert sum(summary.n_patients for summary in analysis.arm_summaries) == len(observations["patient_id"].unique())
    for summary in analysis.arm_summaries:
        assert 0.0 <= summary.responder_rate <= 1.0
        assert 0.0 <= summary.grade3_plus_rate <= 1.0
        assert summary.n_observations > 0
    primary_comparisons = [c for c in analysis.comparisons if c.endpoint == protocol.primary_endpoint.name]
    assert primary_comparisons, "the primary endpoint must be compared against control"
    for comparison in primary_comparisons:
        assert comparison.control_arm_id == protocol.control_arm.arm_id
        assert comparison.ci_low <= comparison.effect_estimate <= comparison.ci_high
        assert 0.0 <= comparison.p_value <= 1.0
    assert analysis.cohort_flow["screened"] == 200
    assert analysis.data_quality["rows"] == len(observations)
    assert json.dumps(analysis.as_dict(), default=str)


def test_biostatistician_end_of_treatment_and_ae_extraction(context, planned) -> None:
    from insilico_trial_mas.engine.sequential_runner import SequentialEngine

    _, _, frame = planned
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    eot = end_of_treatment_frame(observations)
    assert len(eot) == observations["patient_id"].nunique()
    assert eot["epoch"].max() == observations["epoch"].max()
    events = extract_adverse_events(observations)
    if not events.empty:
        assert set(events["ctcae_grade"].unique()) <= {1, 2, 3, 4, 5}
        assert events["ae_id"].is_unique


def test_stopping_rules_are_evaluated_per_epoch(context, planned, protocol) -> None:
    from insilico_trial_mas.engine.sequential_runner import SequentialEngine

    _, _, frame = planned
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    evaluations = BiostatisticianAgent(context).evaluate_stopping_rules(observations)
    assert evaluations, "the protocol declares stopping rules"
    assert {e.rule_id for e in evaluations} <= {rule.rule_id for rule in protocol.stopping_rules}
    assert all(e.epoch >= 0 for e in evaluations)
    assert all(0.0 <= e.observed <= 1.0 for e in evaluations)
    assert all(e.threshold == pytest.approx(rule.threshold) for e in evaluations for rule in protocol.stopping_rules if rule.rule_id == e.rule_id)


def test_persona_adherence_intent_reaches_the_silver_row(protocol, planned, fast_config_factory) -> None:
    """Regression guard: the persona's adherence signal must be persisted.

    Without it the ``adherence_intent`` column stayed "continue" forever and the
    consent-withdrawal discontinuation branch was unreachable.
    """
    _, allocation, frame = planned
    from insilico_trial_mas.cohort.serialization import frame_to_profiles
    from insilico_trial_mas.engine.context import RunSpec

    config = fast_config_factory(llm_mode="all")
    context = RunSpec(
        run_id="adherence-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs,
    ).to_context()
    client = CountingClient(
        text='{"symptoms": [{"term": "dizziness", "ctcae_grade": 2, "verbatim": "unsteady"}], '
        '"overall_tolerability": "poor", "adherence_intent": "discontinue"}'
    )
    runtime = _runtime(context, client)
    profile = frame_to_profiles(frame)[0]
    outcome = asyncio.run(PatientPersonaAgent(profile, context, runtime).run())

    narrated = [obs for obs in outcome.observations if obs.llm_used]
    assert narrated, "mode 'all' narrates every dosing epoch"
    assert {obs.adherence_intent for obs in narrated} == {"discontinue"}
    # Grade 2 symptoms alone never discontinue dosing; consent withdrawal does.
    assert outcome.discontinued_at_epoch is not None
    assert "withdrew consent" in outcome.discontinuation_reason


def test_responder_comparison_counts_the_declared_side(context, planned, protocol) -> None:
    """Regression guard: a '>= 10 mmHg reduction' responder is counted correctly.

    The first implementation counted ``value >= threshold``, which counts
    non-responders for a decrease-oriented endpoint and inverts the reported
    effect (the report showed a *negative* responder-rate difference while the
    high-dose arm had 64% responders and placebo 0%).
    """
    from insilico_trial_mas.engine.sequential_runner import SequentialEngine

    _, _, frame = planned
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    analysis = BiostatisticianAgent(context).analyze(observations)

    endpoint = protocol.responder_endpoint
    assert endpoint.kind == "binary" and endpoint.response_threshold is not None
    comparisons = {c.arm_id: c for c in analysis.comparisons if c.endpoint == endpoint.name}
    assert comparisons, "the responder endpoint must be compared against control"

    summaries = {s.arm_id: s for s in analysis.arm_summaries}
    control_rate = summaries[protocol.control_arm.arm_id].responder_rate
    for arm_id, comparison in comparisons.items():
        expected = summaries[arm_id].responder_rate - control_rate
        assert comparison.effect_estimate == pytest.approx(expected, abs=0.02), (
            f"{arm_id}: reported responder-rate difference {comparison.effect_estimate:.3f} "
            f"does not match the arm summary difference {expected:.3f}"
        )
        assert comparison.ci_low <= comparison.effect_estimate <= comparison.ci_high
