"""Provider format sınırları ve gerçek alt süreç kontrol bağlantısı."""

import os
import sys

import pytest

from cimrihook.quota import (
    CliControl,
    QuotaError,
    claude_quota,
    codex_quota,
    codex_response,
)

STAMP = "2026-10-04T18:00:00+00:00"


def test_claude_control_percentage_is_not_a_streamed_fraction() -> None:
    snapshot = claude_quota(
        {
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 0.75, "resets_at": STAMP},
                "seven_day": {"utilization": 47.2},
                "model_scoped": [{"display_name": "Fable", "utilization": 3.1}],
            },
        },
        STAMP,
    )
    assert [window.used_percent for window in snapshot.windows] == [0.75, 47.2, 3.1]
    assert snapshot.windows[0].resets_at == STAMP
    assert snapshot.windows[2].id == "seven_day_model:Fable"
    assert claude_quota({"rate_limits_available": False}, STAMP).available is False


def test_codex_prefers_main_bucket_and_preserves_unknown_duration() -> None:
    snapshot = codex_quota(
        {
            "accountId": "private-account",
            "rateLimits": {"limitId": "codex-spark", "primary": {"usedPercent": 90}},
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 16.25,
                        "resetsAt": 1791136800,
                    },
                }
            },
        },
        STAMP,
    )
    assert snapshot.available is True
    assert snapshot.windows[0].used_percent == 16.25
    assert snapshot.windows[0].duration_minutes is None
    assert snapshot.windows[0].resets_at is not None
    assert snapshot.account_fingerprint != "private-account"
    assert codex_quota({"rateLimits": {"limitId": "codex-spark"}}, STAMP).available is False
    assert codex_quota({}, STAMP).windows == ()


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 101, "20"])
def test_invalid_provider_utilization_is_an_error(value: object) -> None:
    with pytest.raises(QuotaError):
        claude_quota(
            {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": value}}},
            STAMP,
        )


def test_line_rpc_drains_stderr_and_matches_id_across_split_frames() -> None:
    script = """import json, os, sys
request = json.loads(sys.stdin.readline())
assert request == {'id': 7, 'method': 'account/rateLimits/read', 'params': {}}
os.write(2, b'x' * 100000)
os.write(1, b'{"method":"notification"}\\n{"id":8,"result":{}}\\n')
os.write(1, b'{"id":7,"res')
os.write(1, b'ult":{"rateLimits":null}}\\n')
sys.stdin.read()
"""
    control = CliControl((sys.executable, "-c", script), os.environ, 3)
    try:
        assert codex_response(control, 7, "account/rateLimits/read", {}) == {"rateLimits": None}
    finally:
        control.close()
    assert control.process.poll() is not None


def test_timeout_terminates_control_child() -> None:
    control = CliControl((sys.executable, "-c", "import sys; sys.stdin.read()"), os.environ, 0.1)
    try:
        with pytest.raises(QuotaError, match="timed out"):
            control.receive()
    finally:
        control.close()
    assert control.process.poll() is not None
