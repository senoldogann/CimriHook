"""A/B statistics for small samples: ratios and differences of rates on the log scale.

Costs behave multiplicatively, so comparisons are made on the log scale and reported as a ratio
of geometric means. To avoid a falsely narrow interval on a small sample, the t distribution is
used instead of resampling (bootstrap); the quantile for Welch's fractional degrees of freedom
is computed exactly (rounding to a table entry widens the interval several times on a small
sample). No interval is given when an arm has fewer than two measurements or no variation at
all. For success rates, Wilson intervals and Newcombe's difference interval are used; both give
a meaningful bound even for small samples with no failures at all.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

Z_975: Final = 1.959964
T_QUANTILE_CEILING: Final = 1_000.0  # quantile at 1 degree of freedom is 12.706
BISECTION_STEPS: Final = 200
FRACTION_TERMS: Final = 300
FRACTION_TOLERANCE: Final = 1e-14
TINY: Final = 1e-300


@dataclass(frozen=True, slots=True)
class RatioEstimate:
    """Ratio of geometric means with a 95% confidence interval (None bounds without one)."""

    ratio: float
    low: float | None
    high: float | None


@dataclass(frozen=True, slots=True)
class DifferenceEstimate:
    """Difference of two rates (treatment − baseline) with a 95% confidence interval."""

    difference: float
    low: float
    high: float


def t_975(degrees_of_freedom: float) -> float:
    """The 97.5% quantile of the t distribution, also for fractional degrees of freedom (the inverse
    of the upper tail, found by bisection)."""
    if degrees_of_freedom < 1:
        raise ValueError(f"degrees of freedom must be >= 1, got {degrees_of_freedom}")
    low, high = 0.0, T_QUANTILE_CEILING
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        if t_upper_tail(middle, degrees_of_freedom) > 0.025:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def t_upper_tail(t: float, degrees_of_freedom: float) -> float:
    """P(T > t) for t >= 0, through the regularized incomplete beta function."""
    x = degrees_of_freedom / (degrees_of_freedom + t * t)
    return 0.5 * regularized_beta(x, degrees_of_freedom / 2, 0.5)


def regularized_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta function I_x(a, b): continued fraction (Lentz), by symmetry."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * beta_fraction(x, a, b) / a
    return 1 - front * beta_fraction(1 - x, b, a) / b


def beta_fraction(x: float, a: float, b: float) -> float:
    """Continued fraction of the incomplete beta function; an error if it does not converge."""
    c = 1.0
    d = guarded(1 - (a + b) * x / (a + 1)) ** -1
    result = d
    for m in range(1, FRACTION_TERMS):
        even = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = guarded(1 + even * d) ** -1
        c = guarded(1 + even / c)
        result *= d * c
        odd = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = guarded(1 + odd * d) ** -1
        c = guarded(1 + odd / c)
        step = d * c
        result *= step
        if abs(step - 1) < FRACTION_TOLERANCE:
            return result
    raise ArithmeticError(f"incomplete beta fraction did not converge for x={x}, a={a}, b={b}")


def guarded(value: float) -> float:
    """Lower bound that keeps the continued fraction from dividing by zero."""
    return value if abs(value) > TINY else TINY


def log_values(values: Sequence[float]) -> tuple[float, ...]:
    """Natural logarithms of positive values."""
    if any(value <= 0 for value in values):
        raise ValueError(f"log-scale statistics need positive values, got {list(values)}")
    return tuple(math.log(value) for value in values)


def geometric_mean(values: Sequence[float]) -> float:
    """Geometric mean of positive values."""
    return math.exp(statistics.fmean(log_values(values)))


def ratio_estimate(base: Sequence[float], treated: Sequence[float]) -> RatioEstimate:
    """Treatment/baseline ratio of geometric means; Welch t interval on the log scale."""
    log_base = log_values(base)
    log_treated = log_values(treated)
    difference = statistics.fmean(log_treated) - statistics.fmean(log_base)
    if len(log_base) < 2 or len(log_treated) < 2:
        return RatioEstimate(math.exp(difference), None, None)
    base_term = statistics.variance(log_base) / len(log_base)
    treated_term = statistics.variance(log_treated) / len(log_treated)
    variance = base_term + treated_term
    if variance == 0:
        return RatioEstimate(math.exp(difference), None, None)  # no variation: no interval
    degrees = variance**2 / (
        base_term**2 / (len(log_base) - 1) + treated_term**2 / (len(log_treated) - 1)
    )
    margin = t_975(degrees) * math.sqrt(variance)
    return RatioEstimate(
        math.exp(difference), math.exp(difference - margin), math.exp(difference + margin)
    )


def pooled_ratio(log_ratios: Sequence[float]) -> RatioEstimate:
    """Equal-weight mean of the scenario log ratios; the interval is a t interval across scenarios.

    Asks whether the result generalises to other scenarios; with two scenarios it is very wide
    because of t(1).
    """
    mean = statistics.fmean(log_ratios)
    if len(log_ratios) < 2:
        return RatioEstimate(math.exp(mean), None, None)
    margin = t_975(len(log_ratios) - 1) * statistics.stdev(log_ratios) / math.sqrt(len(log_ratios))
    return RatioEstimate(math.exp(mean), math.exp(mean - margin), math.exp(mean + margin))


def wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """95% Wilson interval of a success rate."""
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError(f"need 0 <= successes <= trials and trials > 0, got {successes}/{trials}")
    rate = successes / trials
    z2 = Z_975**2
    denominator = 1 + z2 / trials
    centre = (rate + z2 / (2 * trials)) / denominator
    half = Z_975 * math.sqrt(rate * (1 - rate) / trials + z2 / (4 * trials**2)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_difference(
    treated_successes: int, treated_trials: int, base_successes: int, base_trials: int
) -> DifferenceEstimate:
    """Difference of success rates (treatment − baseline) with Newcombe's hybrid score interval."""
    treated_rate = treated_successes / treated_trials
    base_rate = base_successes / base_trials
    treated_low, treated_high = wilson_interval(treated_successes, treated_trials)
    base_low, base_high = wilson_interval(base_successes, base_trials)
    difference = treated_rate - base_rate
    return DifferenceEstimate(
        difference=difference,
        low=difference - math.hypot(treated_rate - treated_low, base_high - base_rate),
        high=difference + math.hypot(treated_high - treated_rate, base_rate - base_low),
    )


def fixed_pooled_ratio(arms: Sequence[tuple[Sequence[float], Sequence[float]]]) -> RatioEstimate:
    """Equal-weight mean log ratio of the scenarios; the interval covers only these scenarios.

    The variance is the equal-weight sum of the within-scenario Welch variances, with degrees of
    freedom by the Satterthwaite approximation. No interval if an arm has fewer than two
    measurements or no variation at all.
    """
    differences = [
        statistics.fmean(log_values(treated)) - statistics.fmean(log_values(base))
        for base, treated in arms
    ]
    mean = statistics.fmean(differences)
    if any(len(base) < 2 or len(treated) < 2 for base, treated in arms):
        return RatioEstimate(math.exp(mean), None, None)
    terms = [
        (statistics.variance(log_values(values)) / len(values), len(values) - 1)
        for base, treated in arms
        for values in (base, treated)
    ]
    total = sum(term for term, _ in terms)
    if total == 0:
        return RatioEstimate(math.exp(mean), None, None)
    degrees = total**2 / sum(term**2 / df for term, df in terms)
    margin = t_975(degrees) * math.sqrt(total) / len(arms)
    return RatioEstimate(math.exp(mean), math.exp(mean - margin), math.exp(mean + margin))
