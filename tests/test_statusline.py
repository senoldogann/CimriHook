"""Durum satırı: saf metin üretimi ve gerçek CLI süreciyle uçtan uca çalıştırma."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from cimrihook.statusline import LimitUse, StatusInput, render_status
from cimrihook.tail import SessionTail

NOW = 1_791_000_000.0
STATUS = StatusInput(
    session_id="s",
    transcript_path="/nonexistent",
    model="claude-opus-5-5[1m]",
    context_tokens=412_000,
    session_usd=4.12,
    limits=(LimitUse("five_hour", 42.4, 1), LimitUse("seven_day", 18.0, 2)),
)


def test_warm_cache_prices_the_next_request_as_a_read() -> None:
    # 412000 x 0.05 (Opus 5.x okuma) x $4/MTok = $0.08; 3600 - 600 saniye = 50 dakika kaldı.
    line = render_status(STATUS, SessionTail(NOW - 600, 3_600.0, 412_000, "claude-opus-5-5"), NOW)
    assert line == "412k ctx · cache warm 50m · next $0.08 · 5h 42% · 7d 18% · $4.12"


def test_cold_cache_prices_the_next_request_as_a_rewrite() -> None:
    # 5 dakikalık ömür dolmuş: 412000 x 1.25 x $4/MTok = $2.06.
    line = render_status(STATUS, SessionTail(NOW - 600, 300.0, 412_000, "claude-opus-5-5"), NOW)
    assert line == "412k ctx · cache cold · next $2.06 · 5h 42% · 7d 18% · $4.12"


def test_cli_prints_the_line_and_records_the_limits(tmp_path: Path) -> None:
    stamp = datetime.fromtimestamp(time.time() - 120, UTC).isoformat().replace("+00:00", "Z")
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 5_000,
        "cache_read_input_tokens": 95_000,
        "output_tokens": 50,
        "cache_creation": {"ephemeral_1h_input_tokens": 5_000},
    }
    entry = {
        "type": "assistant",
        "timestamp": stamp,
        "message": {"id": "m", "model": "claude-opus-5-5", "usage": usage, "content": []},
    }
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    payload = {
        "session_id": "s",
        "transcript_path": str(transcript),
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "context_window": {"total_input_tokens": 100_050},
        "rate_limits": {"five_hour": {"used_percentage": 7.0, "resets_at": 1_800_000_000}},
    }
    home = tmp_path / "home"
    completed = subprocess.run(
        [sys.executable, "-m", "cimrihook", "statusline"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CIMRIHOOK_HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("100k ctx · cache warm ")
    assert completed.stdout.endswith(" · 5h 7%")
    with sqlite3.connect(home / "ledger.sqlite3") as db:
        rows = db.execute("SELECT limit_window, used_percentage FROM quota_samples").fetchall()
    assert rows == [("five_hour", 7.0)]
