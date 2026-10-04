"""Small-sample statistics are pure data transformations; table test with known values."""

import math

import pytest

from cimrihook.stats import fixed_pooled_ratio, rate_difference, ratio_estimate, t_975


def test_t_quantile_matches_the_tables_and_handles_fractional_degrees() -> None:
    for degrees, table in ((1, 12.706), (2, 4.303), (5, 2.571), (30, 2.042), (1_000, 1.962)):
        assert t_975(degrees) == pytest.approx(table, abs=5e-4)
    # A fractional Welch df is not rounded down to a table row: 1.61 gives 5.47, not 12.706.
    assert t_975(1.61) == pytest.approx(5.473, abs=1e-3)


def test_ratio_has_no_interval_with_one_run_per_arm() -> None:
    estimate = ratio_estimate([100.0], [80.0])
    assert estimate.ratio == pytest.approx(0.8)
    assert estimate.low is None and estimate.high is None


def test_ratio_interval_is_welch_t_on_log_scale() -> None:
    # log difference ln 2, equal log variances in both arms, Welch df exactly 2 (t = 4.303).
    estimate = ratio_estimate([1.0, 4.0], [2.0, 8.0])
    margin = t_975(2.0) * math.log(4.0) / math.sqrt(2.0)
    assert estimate.ratio == pytest.approx(2.0)
    assert estimate.low == pytest.approx(2.0 * math.exp(-margin))
    assert estimate.high == pytest.approx(2.0 * math.exp(margin))


def test_no_variation_gives_no_interval() -> None:
    estimate = ratio_estimate([1.0, 1.0], [0.5, 0.5])
    assert estimate.ratio == pytest.approx(0.5)
    assert estimate.low is None and estimate.high is None


def test_fixed_pooled_interval_covers_only_the_scenarios_measured() -> None:
    # Two equal scenarios: each arm's log variance term is (ln 2)^2, Satterthwaite df exactly 4.
    estimate = fixed_pooled_ratio([([1.0, 4.0], [2.0, 8.0]), ([1.0, 4.0], [2.0, 8.0])])
    assert estimate.ratio == pytest.approx(2.0)
    assert estimate.low == pytest.approx(2.0 * 2.0 ** -t_975(4.0))
    assert estimate.high == pytest.approx(2.0 * 2.0 ** t_975(4.0))


def test_rate_difference_without_failures_is_still_uncertain() -> None:
    # The 95% Wilson lower bound of 10/10 is 0.7225; the Newcombe difference can fall this low.
    difference = rate_difference(10, 10, 10, 10)
    assert difference.difference == 0.0
    assert difference.low == pytest.approx(-0.2775, abs=1e-4)
    assert difference.high == pytest.approx(0.2775, abs=1e-4)
