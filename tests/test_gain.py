"""Gain report: requests before and after the install, with real files."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cimrihook.errors import ConfigError
from cimrihook.gain import measure_gain, render_gain
from cimrihook.install import INSTALL_RECORD, InstallRecord, save_record
from cimrihook.transcripts import estimate_tokens

T0 = 1_791_000_000.0


def assistant(message_id: str, seconds: float, read: int, written: int) -> str:
    """An Opus 5.5 response that came `seconds` after T0, written with a 1-hour cache."""
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read,
        "output_tokens": 100,
        "cache_creation": {"ephemeral_1h_input_tokens": written, "ephemeral_5m_input_tokens": 0},
    }
    stamp = datetime.fromtimestamp(T0 + seconds, UTC).isoformat().replace("+00:00", "Z")
    message = {"id": message_id, "model": "claude-opus-5-5", "usage": usage, "content": []}
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": message})


def test_gain_splits_requests_at_the_install_time(tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    # Two requests with a 300k context before the install, then two with a 100k context (Opus 5.x).
    lines = [
        assistant("b1", -7_000, 299_000, 1_000),
        assistant("b2", -6_000, 299_000, 1_000),
        assistant("a1", 1_000, 99_000, 1_000),
        assistant("a2", 2_000, 99_000, 1_000),
    ]
    (projects / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    settings = tmp_path / "settings.json"
    save_record(home, settings, InstallRecord((), (), T0))
    gain = measure_gain(tmp_path / "projects", home, settings, T0 + 7_200)
    assert (gain.before.requests, gain.after.requests) == (2, 2)
    assert gain.before.mean_context == pytest.approx(300_000)
    assert gain.after.mean_context == pytest.approx(100_000)
    assert gain.before.large_context_usd == pytest.approx(gain.before.usd)
    assert gain.after.large_context_usd == 0
    report = render_gain(gain)
    assert "mean context" in report and "-67%" in report


def test_gain_needs_a_recorded_install(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / INSTALL_RECORD).write_text(json.dumps({"installs": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="run `cimrihook init` first"):
        measure_gain(tmp_path, tmp_path / "home", tmp_path / "settings.json", T0)


def test_receipt_reprices_the_same_requests_without_the_automatic_compaction(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    stamp = datetime.fromtimestamp(T0 + 1_100, UTC).isoformat().replace("+00:00", "Z")
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": stamp,
        "compactMetadata": {"trigger": "auto", "preTokens": 300_000},
    }
    summary_text = "x" * 4_000
    summary = {"type": "user", "isCompactSummary": True, "message": {"content": summary_text}}
    lines = [
        assistant("a1", 1_000, 299_000, 1_000),
        json.dumps(boundary),
        json.dumps(summary),
        assistant("a2", 1_200, 10_000, 40_000),
        assistant("a3", 1_300, 50_000, 2_000),
    ]
    (projects / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    settings = tmp_path / "settings.json"
    save_record(home, settings, InstallRecord((), (), T0))
    receipt = measure_gain(tmp_path / "projects", home, settings, T0 + 7_200).receipt
    # Opus 5.x base units: a1 17,450; a2 (after the compaction) 81,000; a3 7,000. The compaction
    # call is 300k x 0.05 + summary x 5. In the counterfactual a2 reads the whole context (50k +
    # deleted 250k): 15,500; a3 also reads the deleted 250k: 19,500. In a short session the
    # compaction does not pay for itself: the receipt shows a negative saving.
    assert (receipt.compactions, receipt.sessions) == (1, 1)
    assert receipt.requests_usd == pytest.approx(105_450 * 4e-6)
    calls = 300_000 * 0.05 + estimate_tokens(summary_text) * 5.0
    assert receipt.compaction_calls_usd == pytest.approx(calls * 4e-6)
    assert receipt.without_compactions_usd == pytest.approx((17_450 + 15_500 + 19_500) * 4e-6)
