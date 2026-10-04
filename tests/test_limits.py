"""The limit meter is a pure transformation; tested on hand-computed sessions and runs."""

import json
from pathlib import Path

import pytest

from cimrihook.errors import ConfigError
from cimrihook.limits import (
    LimitSample,
    RunPoints,
    WindowUse,
    read_samples,
    run_points,
    window_rates,
)


def sample(t: float, usd: float, five: float, week: float) -> str:
    return json.dumps(
        {
            "t": t * 1000,
            "usd": usd,
            "limits": [
                {"kind": "five_hour", "percentUsed": five, "resetsAt": "A"},
                {"kind": "seven_day", "percentUsed": week, "resetsAt": "W"},
            ],
        }
    )


def test_rates_join_sessions_within_a_window_period(tmp_path: Path) -> None:
    # Two sessions in one 5-hour period: one spends $3, the other $1; the window goes 10 -> 14.
    (tmp_path / "a.jsonl").write_text(
        "\n".join([sample(0, 1.0, 10, 30), sample(60, 4.0, 13, 31)]) + "\n", encoding="utf-8"
    )
    (tmp_path / "b.jsonl").write_text(
        "\n".join([sample(30, 0.5, 11, 30), sample(90, 1.5, 14, 31)]) + "\n", encoding="utf-8"
    )
    rates = {rate.kind: rate for rate in window_rates(read_samples(tmp_path))}
    assert rates["five_hour"].points == 4
    assert rates["five_hour"].usd == 4.0
    assert rates["five_hour"].usd_per_point() == 1.0
    assert rates["seven_day"].points == 1
    assert rates["seven_day"].usd_per_point() is None  # too few points to estimate


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
