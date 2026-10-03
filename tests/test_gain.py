"""Kazanç raporu: kurulumdan önceki ve sonraki istekler, gerçek dosyalarla."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cimrihook.errors import ConfigError
from cimrihook.gain import measure_gain, render_gain
from cimrihook.install import INSTALL_RECORD, InstallRecord, save_record

T0 = 1_791_000_000.0


def assistant(message_id: str, seconds: float, read: int, written: int) -> str:
    """T0'dan `seconds` sonra gelmiş, 1 saatlik önbellekle yazılmış bir Opus 5.5 yanıtı."""
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
    # Kurulumdan önce 300k bağlamlı iki istek, sonra 100k bağlamlı iki istek (Opus 5.x).
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
