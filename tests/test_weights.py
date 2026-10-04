"""Window weights are a pure transformation; tested on hand-computed readings and spans."""

import pytest

from cimrihook.limits import LimitSample, WindowUse
from cimrihook.weights import (
    Crossing,
    Span,
    SpendCurve,
    crossing_spans,
    fit_weights,
    render_limits,
    spend_at,
    spend_curve,
    weight_ratio,
    window_crossings,
)


def reading(session: str, t: float, usd: float, five: float) -> LimitSample:
    return LimitSample(session, t, usd, (WindowUse("five_hour", five, "A"),))


# Two sessions in one window period; merged, the 5-hour readings are
# (0, 10) (50, 10) (100, 11) (150, 12) (200, 13).
FIRST = [reading("a", 0, 0.0, 10), reading("a", 100, 2.0, 11), reading("a", 200, 4.0, 13)]
SECOND = [reading("b", 50, 0.0, 10), reading("b", 150, 1.0, 12)]


def test_crossings_are_bracketed_by_the_readings_on_either_side() -> None:
    assert window_crossings([*FIRST, *SECOND], "five_hour") == [
        Crossing("A", 11, 50, 100),
        Crossing("A", 12, 100, 150),
        Crossing("A", 13, 150, 200),
    ]
    assert window_crossings([*FIRST, *SECOND], "seven_day") == []


def test_spend_is_linear_between_readings_and_zero_before_the_first() -> None:
    curve = SpendCurve((10.0, 20.0), (1.0, 3.0))
    assert [spend_at(curve, moment) for moment in (5.0, 10.0, 15.0, 25.0)] == [0.0, 1.0, 2.0, 3.0]


def test_spend_counts_from_the_first_reading() -> None:
    # A resumed session already carries $5 that never filled the window during its readings.
    curve = spend_curve([reading("c", 10, 5.0, 10), reading("c", 20, 8.0, 11)])
    assert curve == SpendCurve((10.0, 20.0), (0.0, 3.0))


def test_spans_run_between_bracket_midpoints_and_read_each_class_off_its_curve() -> None:
    crossings = window_crossings([*FIRST, *SECOND], "five_hour")
    classes = [[spend_curve(FIRST)], [spend_curve(SECOND)]]
    # Midpoints 75, 125, 175: the first session spends $1 and $1, the second $0.50 and $0.25.
    assert crossing_spans(crossings, classes, 0, 200) == [
        Span(75, 125, 1, (1.0, 0.5)),
        Span(125, 175, 1, (1.0, 0.25)),
    ]
    assert crossing_spans(crossings, classes, 100, 200) == [Span(125, 175, 1, (1.0, 0.25))]


def test_fit_recovers_the_weights_of_exact_spans() -> None:
    spends = [(10.0, 0.0), (0.0, 10.0), (5.0, 5.0), (20.0, 5.0), (5.0, 20.0), (10.0, 10.0)]
    spans = [Span(0, 1, 0.2 * a + 0.4 * b, (a, b)) for a, b in spends]
    fit = fit_weights(spans, ["a", "b"])
    assert fit is not None
    assert [weight.weight for weight in fit.weights] == pytest.approx([0.2, 0.4])
    assert fit.degrees == 4
    assert fit.residual == pytest.approx(0, abs=1e-9)
    ratio = weight_ratio(fit, "b", "a")
    assert ratio is not None
    assert ratio.ratio == pytest.approx(2.0)


def test_fit_is_none_without_enough_spans_or_with_spends_that_move_together() -> None:
    few = [Span(0, 1, 1.0, (float(n), float(n) * 3)) for n in (1, 2, 4)]
    assert fit_weights(few, ["a", "b"]) is None  # 3 spans for 2 classes
    together = [Span(0, 1, 0.3 * n, (float(n), 2.0 * n)) for n in range(1, 8)]
    assert fit_weights(together, ["a", "b"]) is None  # the second spend is always twice the first


def test_limits_state_the_cost_of_a_point_from_the_spans_between_crossings() -> None:
    # One session spends $4 per point, read at every point: five spans of one point and $4.
    steady = [reading("s", 60.0 * n, 4.0 * n, 10 + n) for n in range(7)]
    assert (
        "5-hour window: 1 point is about $4.00 of usage at API list prices [95% $4.00-$4.00] "
        "(5 spans between whole-percent crossings, 5 points, $20.00 measured)"
    ) in render_limits(steady)
    assert "needs at least 4 spans to estimate" in render_limits(steady[:3])
