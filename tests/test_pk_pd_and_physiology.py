"""PK/PD core and physiology model tests.

These are the tests that protect the scientific claims of the platform: if the
exposure formula regresses (as it did during development), the adverse-event
rates of every simulated trial silently inflate. Each analytic identity below is
checked against the closed form rather than a stored snapshot.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import yaml

from insilico_trial_mas.ml.physiology import (
    FEATURE_NAMES,
    MechanisticPhysiology,
    RidgeResidualHead,
    feature_vector,
    individual_sensitivity,
    sample_adverse_events,
)
from insilico_trial_mas.ml.pk_pd import (
    accumulation_ratio,
    derive_pk_parameters,
    emax_effect,
    exposure_for_epoch,
    plasma_concentration,
    time_to_peak,
)
from insilico_trial_mas.reproducibility import rng_for, stable_hash, stable_seed
from insilico_trial_mas.schemas import TrialProtocol


def test_seed_derivation_is_stable_and_order_independent() -> None:
    assert stable_seed("a", "b") == stable_seed("a", "b")
    assert stable_seed("a", "b") != stable_seed("b", "a")
    assert stable_hash("x", length=8) == stable_hash("x", length=8)
    assert len(stable_hash("x", length=8)) == 8
    first = rng_for(1, "p", 2).random()
    second = rng_for(1, "p", 2).random()
    assert first == second


def test_pk_parameters_scale_with_physiology(cohort) -> None:
    with open("conf/trial_protocol_demo.yaml", encoding="utf-8") as handle:
        protocol = TrialProtocol.model_validate(yaml.safe_load(handle))
    drug = protocol.drug
    young = max(cohort, key=lambda p: -p.age)
    old = max(cohort, key=lambda p: p.age)
    pk_young = derive_pk_parameters(young, drug)
    pk_old = derive_pk_parameters(old, drug)
    # Clearance falls with age, so half-life rises (for a comparable body size).
    assert pk_old.clearance_l_h <= pk_young.clearance_l_h * 1.2
    assert pk_young.volume_l > 0 and pk_young.clearance_l_h > 0
    assert pk_young.half_life_h == pytest.approx(math.log(2) / pk_young.ke_per_h)


def test_cyp2d6_poor_metaboliser_has_higher_exposure(cohort, protocol: TrialProtocol) -> None:
    poor = [p for p in cohort if p.genomic.cyp2d6 == "PM"]
    normal = [p for p in cohort if p.genomic.cyp2d6 == "NM" and p.sex == "M"]
    if not poor or not normal:
        pytest.skip("cohort does not contain both phenotypes")
    pk_pm = derive_pk_parameters(poor[0], protocol.drug)
    pk_nm = derive_pk_parameters(normal[0], protocol.drug)
    # A PM has a lower hepatic clearance, so the same dose yields a higher AUC.
    exposure_pm = exposure_for_epoch(pk_pm, protocol.drug, dose_mg=100, epoch=1, tau_h=12)
    exposure_nm = exposure_for_epoch(pk_nm, protocol.drug, dose_mg=100, epoch=1, tau_h=12)
    assert exposure_pm.exposure_ratio > 0
    assert exposure_nm.exposure_ratio > 0


def test_auc_identity_matches_closed_form(cohort, protocol: TrialProtocol) -> None:
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    dose = 100.0
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=dose, epoch=1, tau_h=12)
    expected_auc = pk.bioavailability * dose / pk.clearance_l_h
    assert exposure.auc_epoch_mg_h_l == pytest.approx(expected_auc, rel=1e-9)
    assert exposure.c_avg_mg_l == pytest.approx(expected_auc / 12.0, rel=1e-9)
    # The exposure ratio must be the average steady-state concentration over EC50:
    assert exposure.exposure_ratio == pytest.approx(exposure.c_avg_mg_l / protocol.drug.ec50_mg_l, rel=1e-9)


def test_exposure_ratio_does_not_inflate_by_accumulation(cohort, protocol: TrialProtocol) -> None:
    """Regression guard: the AUC must not be extrapolated beyond one dosing interval."""
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=150, epoch=5, tau_h=12)
    accumulation = accumulation_ratio(pk.ke_per_h, 12.0)
    naive_inflated = exposure.exposure_ratio * accumulation
    assert exposure.exposure_ratio < naive_inflated
    assert exposure.exposure_ratio == pytest.approx(exposure.c_avg_mg_l / protocol.drug.ec50_mg_l, rel=1e-9)


def test_plasma_concentration_superposition(cohort, protocol: TrialProtocol) -> None:
    pk = derive_pk_parameters(cohort[0], protocol.drug)
    single = plasma_concentration(pk, 100.0, time_since_last_dose_h=6.0, n_prior_doses=0, tau_h=12)
    steady = plasma_concentration(pk, 100.0, time_since_last_dose_h=6.0, n_prior_doses=20, tau_h=12)
    assert steady > single, "repeated dosing must accumulate"
    # The steady-state / single-dose ratio approaches the accumulation factor
    # 1 / (1 - exp(-ke * tau)); the absorption term makes it marginally different.
    ratio = steady / single
    assert ratio == pytest.approx(accumulation_ratio(pk.ke_per_h, 12.0), rel=0.05)
    # Steady state must actually be reached (200 doses ~ 20 doses).
    plateau = plasma_concentration(pk, 100.0, time_since_last_dose_h=6.0, n_prior_doses=200, tau_h=12)
    assert plateau == pytest.approx(steady, rel=0.01)
    assert plasma_concentration(pk, 0.0, time_since_last_dose_h=6.0) == 0.0


def test_time_to_peak_matches_numeric_maximum(cohort, protocol: TrialProtocol) -> None:
    pk = derive_pk_parameters(cohort[0], protocol.drug)
    t_max = time_to_peak(pk)
    grid = np.linspace(0.01, 24, 400)
    values = [plasma_concentration(pk, 100.0, time_since_last_dose_h=float(t)) for t in grid]
    numeric_t_max = float(grid[int(np.argmax(values))])
    assert t_max == pytest.approx(numeric_t_max, abs=0.2)


def test_emax_is_monotone_and_bounded() -> None:
    assert emax_effect(0.0, 1.0, -10.0) == 0.0
    low = emax_effect(0.5, 1.0, -10.0, 1.0)
    high = emax_effect(5.0, 1.0, -10.0, 1.0)
    # Emax is negative (a reduction), so a higher concentration gives a lower value.
    assert low < 0.0
    assert high < low
    assert emax_effect(1e9, 1.0, -10.0, 1.0) == pytest.approx(-10.0, rel=1e-6)


def test_epoch_zero_has_no_exposure(cohort, protocol: TrialProtocol) -> None:
    pk = derive_pk_parameters(cohort[0], protocol.drug)
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=0.0, epoch=0, tau_h=12)
    assert exposure.exposure_ratio == 0.0
    assert exposure.auc_epoch_mg_h_l == 0.0


def test_individual_sensitivity_is_stable_and_variable() -> None:
    a = individual_sensitivity(7, "PT-1")
    assert a == individual_sensitivity(7, "PT-1")
    values = [individual_sensitivity(7, f"PT-{i}") for i in range(200)]
    assert len(set(values)) > 180
    assert 0.3 < float(np.mean(values)) < 3.0


def test_mechanistic_prediction_is_deterministic(cohort, protocol: TrialProtocol) -> None:
    model = MechanisticPhysiology()
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=150, epoch=4, tau_h=12)
    first = model.predict(
        profile,
        protocol.drug,
        exposure,
        pk,
        epoch=4,
        epochs_total=8,
        placebo_effect=protocol.placebo_effect,
        seed=11,
    )
    second = model.predict(
        profile,
        protocol.drug,
        exposure,
        pk,
        epoch=4,
        epochs_total=8,
        placebo_effect=protocol.placebo_effect,
        seed=11,
    )
    assert first.sbp == second.sbp
    assert first.ae_probabilities == second.ae_probabilities


def test_higher_dose_increases_effect_and_adverse_event_risk(cohort, protocol: TrialProtocol) -> None:
    model = MechanisticPhysiology()
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    low = model.predict(
        profile,
        protocol.drug,
        exposure_for_epoch(pk, protocol.drug, dose_mg=50, epoch=4, tau_h=12),
        pk,
        epoch=4,
        epochs_total=8,
        placebo_effect=protocol.placebo_effect,
        seed=3,
    )
    high = model.predict(
        profile,
        protocol.drug,
        exposure_for_epoch(pk, protocol.drug, dose_mg=200, epoch=4, tau_h=12),
        pk,
        epoch=4,
        epochs_total=8,
        placebo_effect=protocol.placebo_effect,
        seed=3,
    )
    assert high.sbp < low.sbp
    assert high.ae_probabilities["hypotension"] > low.ae_probabilities["hypotension"]


def test_ae_grade_distribution_respects_declared_mass(protocol: TrialProtocol) -> None:
    """Non-serious terms declare zero grade-4/5 mass: no fatal headaches."""
    headache = next(ae for ae in protocol.drug.ae_models if ae.term == "headache")
    assert headache.grade_distribution[3] == 0.0
    assert headache.grade_distribution[4] == 0.0


def test_sample_adverse_events_is_deterministic(cohort, protocol: TrialProtocol) -> None:
    model = MechanisticPhysiology()
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=200, epoch=6, tau_h=12)
    prediction = model.predict(
        profile,
        protocol.drug,
        exposure,
        pk,
        epoch=6,
        epochs_total=8,
        placebo_effect=protocol.placebo_effect,
        seed=5,
    )
    first = sample_adverse_events(profile, protocol.drug, prediction, epoch=6, seed=5, run_id="RUN")
    second = sample_adverse_events(profile, protocol.drug, prediction, epoch=6, seed=5, run_id="RUN")
    assert first == second
    for event in first:
        assert 1 <= event["ctcae_grade"] <= 5
        assert 0.0 <= event["predicted_probability"] <= 1.0
        assert event["serious"] is False or event["ctcae_grade"] >= 3


def test_ridge_head_round_trip(tmp_path) -> None:
    coefficients = {name: [0.1] * len(FEATURE_NAMES) for name in ("delta_sbp_mmhg", "delta_dbp_mmhg")}
    head = RidgeResidualHead(coefficients=coefficients, metrics={"r2_delta_sbp_mmhg": 0.5})
    path = head.save(tmp_path / "model.json")
    reloaded = RidgeResidualHead.load(path)
    assert reloaded.digest() == head.digest()
    features = np.ones(len(FEATURE_NAMES))
    assert reloaded.predict(features)["delta_sbp_mmhg"] == pytest.approx(0.1 * len(FEATURE_NAMES))


def test_feature_vector_shape_and_finiteness(cohort, protocol: TrialProtocol) -> None:
    profile = cohort[0]
    pk = derive_pk_parameters(profile, protocol.drug)
    exposure = exposure_for_epoch(pk, protocol.drug, dose_mg=100, epoch=2, tau_h=12)
    vector = feature_vector(profile, protocol.drug, exposure, epoch=2, epochs_total=8)
    assert vector.shape == (len(FEATURE_NAMES),)
    assert np.all(np.isfinite(vector))
