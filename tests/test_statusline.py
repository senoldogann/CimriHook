"""Status line: pure text generation and an end-to-end run with a real CLI process."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from cimrihook.statusline import LimitUse, PromptCache, StatusInput, joined, render_status

NOW = 1_791_000_000.0


def status(cache: PromptCache | None, context: int | None) -> StatusInput:
    """An Opus 5.5 session with two usage limit windows."""
    return StatusInput(
        session_id="s",
        model="claude-opus-5-5[1m]",
        context_tokens=context,
        session_usd=4.12,
        limits=(LimitUse("five_hour", 42.4, 1), LimitUse("seven_day", 18.0, 2)),
        cache=cache,
    )


def test_warm_cache_prices_the_next_request_as_a_read() -> None:
    # 412000 x 0.05 (Opus 5.x read) x $4/MTok = $0.08; the lifetime ends in 50 minutes.
    # Compacting now: 412k x 0.05 + 7k x 5 + 56k x 2 = 167,600 base; every request saves
    # (412k - 56k) x 0.05 = 17,800 base: it pays back in 10 requests.
    cache = PromptCache(True, 3_600.0, NOW + 3_000, 412_000)
    line = render_status(status(cache, 412_000), NOW)
    assert line == (
        "412k ctx · cache warm 50m · next $0.08 · compact pays back in 10 requests · 5h 42% · "
        "7d 18% · $4.12"
    )


def test_cold_cache_prices_the_next_request_as_a_rewrite() -> None:
    # The 5-minute lifetime is over: Claude Code estimates 412000 tokens x 1.25 x $4/MTok = $2.06.
    cache = PromptCache(False, 300.0, NOW - 60, 412_000)
    line = render_status(status(cache, 412_000), NOW)
    assert line == "412k ctx · cache cold · next $2.06 · 5h 42% · 7d 18% · $4.12"


def test_unknown_context_and_cache_are_left_out() -> None:
    assert render_status(status(None, None), NOW) == "5h 42% · 7d 18% · $4.12"


def test_previous_lines_are_kept_and_ours_ends_the_last_one() -> None:
    assert joined("repo main\nmodel opus", "50k ctx") == "repo main\nmodel opus · 50k ctx"
    assert joined("", "50k ctx") == "50k ctx"


def run_cli(args: list[str], payload: Mapping[str, object], home: Path) -> str:
    """Runs the status line command in a separate process; the exit code must always be 0."""
    completed = subprocess.run(
        [sys.executable, "-m", "cimrihook", "statusline", *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CIMRIHOOK_HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_cli_prints_the_line_and_records_the_limits_privately(tmp_path: Path) -> None:
    payload = {
        "session_id": "s",
        "transcript_path": str(tmp_path / "t.jsonl"),
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "context_window": {"total_input_tokens": 100_050},
        "prompt_cache": {
            "warm": True,
            "ttl": "1h",
            "expires_at": int(time.time()) + 1_800,
            "recache_tokens_if_cold": 100_050,
        },
        "rate_limits": {"five_hour": {"used_percentage": 7.0, "resets_at": 1_800_000_000}},
    }
    home = tmp_path / "home"
    line = run_cli([], payload, home)
    assert line.startswith("100k ctx · cache warm ")
    assert line.endswith(" · 5h 7%")
    assert run_cli([], payload, home) == line  # the same observation is not written twice
    ledger = home / "ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        rows = db.execute("SELECT limit_window, used_percentage FROM quota_samples").fetchall()
    assert rows == [("five_hour", 7.0)]
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_our_failure_keeps_the_previous_line_and_shows_the_error(tmp_path: Path) -> None:
    line = run_cli(["--after", "cat >/dev/null; echo theirs"], {"model": {}}, tmp_path / "home")
    assert line.startswith("theirs · cimrihook: ")
    assert "session_id" in line
