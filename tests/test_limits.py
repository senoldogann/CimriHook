"""The points a run moves a window are a pure transformation; tested on hand-computed runs."""

import pytest

from cimrihook.errors import ConfigError
from cimrihook.limits import LimitSample, RunPoints, WindowUse, run_points


def reading(t: float, usd: float, five: float, resets_at: str) -> LimitSample:
    return LimitSample("run", t, usd, (WindowUse("five_hour", five, resets_at),))


def test_run_points_are_last_minus_first_in_one_period() -> None:
    steady = [reading(0, 1.0, 10, "A"), reading(60, 3.0, 12, "A"), reading(120, 5.0, 14, "A")]
    assert run_points(steady, "steady") == [RunPoints("five_hour", 4, 4.0)]
    # The window reset under the run: the readings of two periods are not one count.
    assert run_points([*steady, reading(180, 7.0, 1, "B")], "reset") == []


def test_run_points_refuse_readings_that_go_backwards() -> None:
    with pytest.raises(ConfigError, match="backwards"):
        run_points([reading(0, 1.0, 12, "A"), reading(60, 3.0, 10, "A")], "backwards")
