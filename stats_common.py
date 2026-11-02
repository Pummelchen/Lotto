"""Shared probability and chi-square helpers for the lottery analyzers."""

from __future__ import annotations

import math


def chi2_sf(statistic: float, degrees_of_freedom: int) -> float:
    """Chi-square survival function using the regularized upper incomplete gamma."""
    if statistic <= 0:
        return 1.0
    if degrees_of_freedom <= 0:
        return 1.0
    return _gammaincc(degrees_of_freedom / 2, statistic / 2)


def _gammaincc(a: float, x: float) -> float:
    if x <= 0:
        return 1.0
    log_gamma = math.lgamma(a)
    if x < a + 1:
        ap = a
        total = 1 / a
        delta = total
        for _ in range(10000):
            ap += 1
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-15:
                break
        probability = total * math.exp(-x + a * math.log(x) - log_gamma)
        return max(0.0, min(1.0, 1.0 - probability))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for index in range(1, 10000):
        an = -index * (index - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return max(0.0, min(1.0, math.exp(-x + a * math.log(x) - log_gamma) * h))


def normal_two_sided_p(z_score: float) -> float:
    return math.erfc(abs(z_score) / math.sqrt(2))


def binomial_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n or p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    log_n_factorial = math.lgamma(n + 1)
    terms = [
        math.exp(
            log_n_factorial
            - math.lgamma(index + 1)
            - math.lgamma(n - index + 1)
            + index * log_p
            + (n - index) * log_q
        )
        for index in range(k + 1)
    ]
    return min(1.0, max(0.0, math.fsum(terms)))


def binomial_two_sided_p(observed: int, n: int, p: float) -> float:
    """Exact two-sided binomial tail, used where normal tails are unreliable."""
    lower = binomial_cdf(observed, n, p)
    upper = 1.0 - binomial_cdf(observed - 1, n, p)
    return min(1.0, 2.0 * min(lower, upper))


def bonferroni(p_value: float, family_size: int) -> float:
    return min(1.0, p_value * family_size)


def weighted_chi_square(observed: list[int], expected: list[float]) -> tuple[float, int, float]:
    """Goodness-of-fit with adjacent bins merged until expected counts are at least 5."""
    groups: list[tuple[float, float]] = []
    observed_sum = 0.0
    expected_sum = 0.0
    for obs, exp in zip(observed, expected):
        observed_sum += obs
        expected_sum += exp
        if expected_sum >= 5:
            groups.append((observed_sum, expected_sum))
            observed_sum = 0.0
            expected_sum = 0.0
    if expected_sum > 0:
        if groups:
            previous_obs, previous_exp = groups[-1]
            groups[-1] = (previous_obs + observed_sum, previous_exp + expected_sum)
        else:
            groups.append((observed_sum, expected_sum))
    if len(groups) < 2:
        return 0.0, 0, 1.0
    statistic = sum((obs - exp) ** 2 / exp for obs, exp in groups)
    degrees_of_freedom = len(groups) - 1
    return statistic, degrees_of_freedom, chi2_sf(statistic, degrees_of_freedom)
