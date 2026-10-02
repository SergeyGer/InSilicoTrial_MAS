"""Statistical estimators implemented without a SciPy dependency.

Databricks clusters have SciPy available, but the local/CI profile of this
project must run on a bare ``numpy`` install, so the handful of distributions
and confidence intervals the Biostatistician Agent needs are implemented here
(regularised incomplete beta function, Wilson/Newcombe intervals, exact Fisher
test). Every function is pure and unit-tested against closed-form values.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Special functions
# ---------------------------------------------------------------------------

_EPS = 3.0e-16
_FPMIN = 1.0e-300


def _betacf(a: float, b: float, x: float, max_iter: int = 200) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: float) -> float:
    """Upper tail probability of Student's t distribution."""
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    p = 0.5 * betainc(df / 2.0, 0.5, x)
    return p if t > 0 else 1.0 - p


def student_t_two_sided_p(t: float, df: float) -> float:
    return float(min(1.0, 2.0 * student_t_sf(abs(t), df)))


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_sided_normal_p(z: float) -> float:
    return float(min(1.0, 2.0 * normal_sf(abs(z))))


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (better than Wald at extremes)."""
    if total <= 0:
        return (0.0, 1.0)
    z = _z_for(confidence)
    phat = successes / total
    denom = 1.0 + z * z / total
    centre = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def newcombe_difference_interval(
    s1: int, n1: int, s2: int, n2: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Newcombe hybrid-score interval for ``p1 - p2`` (used for risk differences)."""
    if n1 <= 0 or n2 <= 0:
        return (-1.0, 1.0)
    l1, u1 = wilson_interval(s1, n1, confidence)
    l2, u2 = wilson_interval(s2, n2, confidence)
    p1, p2 = s1 / n1, s2 / n2
    lower = (p1 - p2) - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = (p1 - p2) + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


def mean_confidence_interval(values: Sequence[float] | np.ndarray, confidence: float = 0.95) -> tuple[float, float, float]:
    """Return ``(mean, low, high)`` using the normal approximation (n is large in simulation)."""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    mean = float(arr.mean())
    if arr.size == 1:
        return (mean, mean, mean)
    se = float(arr.std(ddof=1) / math.sqrt(arr.size))
    z = _z_for(confidence)
    return (mean, mean - z * se, mean + z * se)


def _z_for(confidence: float) -> float:
    """Inverse standard normal CDF via rational approximation (Acklam)."""
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    p = 1.0 - (1.0 - confidence) / 2.0
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02,
         -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01,
         -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00,
         4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


# ---------------------------------------------------------------------------
# Hypothesis tests
# ---------------------------------------------------------------------------


def welch_ttest(sample_a: Sequence[float] | np.ndarray, sample_b: Sequence[float] | np.ndarray) -> tuple[float, float]:
    """Welch's unequal-variance t-test. Returns ``(t_statistic, two_sided_p)``."""
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if a.size < 2 or b.size < 2:
        return (float("nan"), float("nan"))
    var_a, var_b = a.var(ddof=1), b.var(ddof=1)
    se2 = var_a / a.size + var_b / b.size
    if se2 <= 0:
        return (0.0, 1.0)
    t = float((a.mean() - b.mean()) / math.sqrt(se2))
    df = se2**2 / ((var_a / a.size) ** 2 / (a.size - 1) + (var_b / b.size) ** 2 / (b.size - 1))
    return (t, student_t_two_sided_p(t, df))


def two_proportion_ztest(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """Pooled two-proportion z-test. Returns ``(z, two_sided_p)``."""
    if n1 <= 0 or n2 <= 0:
        return (float("nan"), float("nan"))
    p1, p2 = s1 / n1, s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se <= 0:
        return (0.0, 1.0)
    z = float((p1 - p2) / se)
    return (z, two_sided_normal_p(z))


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test p-value (sum of tables no more likely than observed)."""
    for value in (a, b, c, d):
        if value < 0:
            raise ValueError("counts must be non-negative")
    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, col1 = a + b, a + c

    def _log_prob(x: int) -> float:
        return (
            math.lgamma(row1 + 1)
            + math.lgamma(n - row1 + 1)
            + math.lgamma(col1 + 1)
            + math.lgamma(n - col1 + 1)
            - math.lgamma(n + 1)
            - math.lgamma(x + 1)
            - math.lgamma(row1 - x + 1)
            - math.lgamma(col1 - x + 1)
            - math.lgamma(n - row1 - col1 + x + 1)
        )

    observed = _log_prob(a)
    low, high = max(0, col1 - (n - row1)), min(row1, col1)
    p_value = 0.0
    for x in range(low, high + 1):
        lp = _log_prob(x)
        if lp <= observed + 1e-9:
            p_value += math.exp(lp)
    return float(min(1.0, p_value))


def mann_whitney_u(
    sample_a: Sequence[float] | np.ndarray, sample_b: Sequence[float] | np.ndarray
) -> tuple[float, float]:
    """Mann-Whitney U test with tie-corrected normal approximation."""
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    n1, n2 = a.size, b.size
    if n1 == 0 or n2 == 0:
        return (float("nan"), float("nan"))
    pooled = np.concatenate([a, b])
    order = pooled.argsort(kind="mergesort")
    ranks = np.empty(pooled.size, dtype=float)
    ranks[order] = np.arange(1, pooled.size + 1, dtype=float)
    # Average ranks inside tie groups.
    sorted_vals = pooled[order]
    i = 0
    tie_correction = 0.0
    while i < sorted_vals.size:
        j = i
        while j + 1 < sorted_vals.size and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        if j > i:
            avg = (i + j + 2) / 2.0
            ranks[order[i : j + 1]] = avg
            tie_correction += (j - i + 1) ** 3 - (j - i + 1)
        i = j + 1
    r1 = float(ranks[:n1].sum())
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    n = n1 + n2
    sigma_sq = (n1 * n2 / 12.0) * ((n + 1) - tie_correction / (n * (n - 1))) if n > 1 else 0.0
    if sigma_sq <= 0:
        return (u1, 1.0)
    z = (u1 - mu) / math.sqrt(sigma_sq)
    return (float(u1), two_sided_normal_p(float(z)))


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.05) -> list[float]:
    """Benjamini-Hochberg FDR adjusted q-values, returned in the input order."""
    p = np.asarray(list(p_values), dtype=float)
    n = p.size
    if n == 0:
        return []
    order = np.argsort(p, kind="mergesort")
    ranked = p[order]
    q = ranked * n / (np.arange(1, n + 1))
    q = np.minimum.accumulate(q[::-1])[::-1]
    q = np.clip(q, 0.0, 1.0)
    out = np.empty(n, dtype=float)
    out[order] = q
    return [float(x) for x in out]


def bootstrap_difference_ci(
    sample_a: Sequence[float] | np.ndarray,
    sample_b: Sequence[float] | np.ndarray,
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 12345,
) -> tuple[float, float, float]:
    """Seeded bootstrap CI for ``mean(a) - mean(b)``; deterministic across engines."""
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if a.size == 0 or b.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        diffs[i] = rng.choice(a, size=a.size, replace=True).mean() - rng.choice(b, size=b.size, replace=True).mean()
    alpha = (1.0 - confidence) / 2.0
    return (
        float(a.mean() - b.mean()),
        float(np.quantile(diffs, alpha)),
        float(np.quantile(diffs, 1.0 - alpha)),
    )


def cohens_d(sample_a: Sequence[float] | np.ndarray, sample_b: Sequence[float] | np.ndarray) -> float:
    """Standardised effect size (pooled SD)."""
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if a.size < 2 or b.size < 2:
        return float("nan")
    pooled_var = ((a.size - 1) * a.var(ddof=1) + (b.size - 1) * b.var(ddof=1)) / (a.size + b.size - 2)
    if pooled_var <= 0:
        return 0.0
    return float((a.mean() - b.mean()) / math.sqrt(pooled_var))
