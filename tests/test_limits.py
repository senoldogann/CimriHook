"""Limit ölçerin kuru saf bir dönüşümdür; elle hesaplanmış iki oturumla test."""

import json
from pathlib import Path

from cimrihook.limits import read_samples, window_rates


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
    # İki oturum aynı 5 saatlik dönemde: biri $3, öbürü $1 harcar; pencere 10'dan 14'e çıkar.
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
