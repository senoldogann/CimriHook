"""Küçük örneklem istatistikleri saf veri dönüşümleridir; bilinen değerlerle tablo testi."""

import math

import pytest

from cimrihook.stats import rate_difference, ratio_estimate, t_975


def test_t_quantile_floors_fractional_degrees_and_caps_at_thirty() -> None:
    assert t_975(1) == 12.706
    assert t_975(2.7) == 4.303
    assert t_975(250) == 2.042


def test_ratio_has_no_interval_with_one_run_per_arm() -> None:
    estimate = ratio_estimate([100.0], [80.0])
    assert estimate.ratio == pytest.approx(0.8)
    assert estimate.low is None and estimate.high is None


def test_ratio_interval_is_welch_t_on_log_scale() -> None:
    # log farkı ln 2, iki kolun log varyansı eşit, Welch serbestlik derecesi tam 2 (t = 4.303).
    estimate = ratio_estimate([1.0, 4.0], [2.0, 8.0])
    margin = 4.303 * math.log(4.0) / math.sqrt(2.0)
    assert estimate.ratio == pytest.approx(2.0)
    assert estimate.low == pytest.approx(2.0 * math.exp(-margin))
    assert estimate.high == pytest.approx(2.0 * math.exp(margin))


def test_rate_difference_without_failures_is_still_uncertain() -> None:
    # 10/10'un %95 Wilson alt sınırı 0.7225'tir; Newcombe farkı bu kadar aşağı inebilir.
    difference = rate_difference(10, 10, 10, 10)
    assert difference.difference == 0.0
    assert difference.low == pytest.approx(-0.2775, abs=1e-4)
    assert difference.high == pytest.approx(0.2775, abs=1e-4)
