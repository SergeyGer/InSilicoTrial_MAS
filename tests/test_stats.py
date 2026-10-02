"""Statistical estimator tests (checked against closed forms and known values)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from insilico_trial_mas.stats.estimators import (
    _z_for,
    benjamini_hochberg,
    betainc,
    bootstrap_difference_ci,
    cohens_d,
    fisher_exact_two_sided,
    mann_whitney_u,
    mean_confidence_interval,
    newcombe_difference_interval,
    normal_cdf,
    student_t_two_sided_p,
    two_proportion_ztest,
    welch_ttest,
    wilson_interval,
)


def test_incomplete_beta_matches_known_values() -> None:
    assert betainc(1, 1, 0.5) == pytest.approx(0.5)
    assert betainc(0.5, 0.5, 0.5) == pytest.approx(0.5)
    # I_0.5(2, 3) = 0.6875
    assert betainc(2, 3, 0.5) == pytest.approx(0.6875, rel=1e-9)
    # Beta(3, 3) CDF at 0.25 = 30 * [x^3/3 - x^4/2 + x^5/5] evaluated at x = 0.25
    assert betainc(3, 3, 0.25) == pytest.approx(0.103515625, rel=1e-9)
    assert betainc(2, 2, 0.5) == pytest.approx(0.5)


def test_inverse_normal_cdf_known_quantiles() -> None:
    assert _z_for(0.95) == pytest.approx(1.959964, abs=1e-5)
    assert _z_for(0.99) == pytest.approx(2.575829, abs=1e-5)
    assert _z_for(0.90) == pytest.approx(1.644854, abs=1e-5)
    assert normal_cdf(1.96) == pytest.approx(0.975, abs=1e-4)


def test_student_t_two_sided_p_known_values() -> None:
    # Two-sided p for t = 1.0 with 4 degrees of freedom is 0.3739
    # (scipy: 2 * stats.t.sf(1.0, 4) = 0.37390).
    assert student_t_two_sided_p(1.0, 4) == pytest.approx(0.37390, abs=1e-4)
    # t = 2.776, df = 4 -> p = 0.05
    assert student_t_two_sided_p(2.776445, 4) == pytest.approx(0.05, abs=1e-5)


def test_wilson_interval_properties() -> None:
    low, high = wilson_interval(5, 10)
    assert low < 0.5 < high
    assert 0.0 <= low <= high <= 1.0
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(1, 100)
    assert low == pytest.approx(0.0, abs=0.02)


def test_newcombe_interval_contains_the_difference() -> None:
    low, high = newcombe_difference_interval(8, 40, 4, 40)
    assert low < 0.1 < high
    assert low >= -1.0 and high <= 1.0


def test_mean_confidence_interval_is_sane() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    mean, low, high = mean_confidence_interval(values)
    assert mean == pytest.approx(3.0)
    assert low < mean < high
    assert mean_confidence_interval([])[0] != mean_confidence_interval([])[0]  # NaN


def test_welch_ttest_matches_reference() -> None:
    a = [5.1, 4.9, 5.4, 5.2, 5.6, 5.0]
    b = [4.6, 4.5, 4.8, 4.7, 4.9, 4.4]
    t, p = welch_ttest(a, b)
    assert t > 4.0
    assert p < 0.01
    assert math.isnan(welch_ttest([1.0], [2.0])[0])


def test_two_proportion_ztest_reference() -> None:
    # Pooled two-proportion z-test: p_pool = 0.15, se = 0.050497, z = 1.9803.
    z, p = two_proportion_ztest(20, 100, 10, 100)
    assert z == pytest.approx(1.9803, abs=0.001)
    assert p == pytest.approx(0.0477, abs=0.001)
    assert math.isnan(two_proportion_ztest(0, 0, 1, 10)[0])


def test_fisher_exact_known_values() -> None:
    # Tea-tasting style table: classic p = 0.4857
    assert fisher_exact_two_sided(3, 1, 1, 3) == pytest.approx(0.4857, abs=1e-3)
    # All successes on one side
    assert fisher_exact_two_sided(10, 0, 0, 10) == pytest.approx(1.07e-5, abs=1e-5)
    assert fisher_exact_two_sided(0, 0, 0, 0) == 1.0


def test_mann_whitney_detects_shift() -> None:
    a = [1, 2, 3, 4, 5]
    b = [6, 7, 8, 9, 10]
    u, p = mann_whitney_u(a, b)
    assert u == pytest.approx(0.0)
    assert p < 0.02
    _, p_same = mann_whitney_u(a, a)
    assert p_same > 0.9


def test_benjamini_hochberg_monotone_and_bounded() -> None:
    p_values = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    q_values = benjamini_hochberg(p_values)
    assert all(0.0 <= q <= 1.0 for q in q_values)
    assert q_values[0] == pytest.approx(0.01, abs=0.001)
    ordered = [q for _, q in sorted(zip(p_values, q_values, strict=True))]
    assert ordered == sorted(ordered), "q-values must be monotone in p"
    assert benjamini_hochberg([]) == []


def test_bootstrap_is_deterministic_and_brackets_the_estimate() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(5.0, 1.0, 200)
    b = rng.normal(3.0, 1.0, 200)
    first = bootstrap_difference_ci(a, b, n_resamples=400, seed=42)
    second = bootstrap_difference_ci(a, b, n_resamples=400, seed=42)
    assert first == second
    estimate, low, high = first
    assert low < estimate < high
    assert estimate == pytest.approx(2.0, abs=0.4)


def test_cohens_d_sign_and_magnitude() -> None:
    a = [10.0, 11.0, 12.0, 13.0]
    b = [1.0, 2.0, 3.0, 4.0]
    d = cohens_d(a, b)
    assert d > 3.0
    assert cohens_d(a, a) == pytest.approx(0.0)
