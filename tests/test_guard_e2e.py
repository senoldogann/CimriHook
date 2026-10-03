"""Uçtan uca: gerçek CLI süreci, gerçek transcript dosyası ve SQLite defteriyle korumalar."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from cimrihook.guard import COMPACTION_BRIEF


def transcript(tmp: Path, idle_seconds: float, context: int) -> Path:
    """Son yanıtı `idle_seconds` önce gelmiş, 1 saatlik önbellekle yazılmış bir oturum."""
    stamp = datetime.fromtimestamp(time.time() - idle_seconds, UTC)
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 2_000,
        "cache_read_input_tokens": context - 2_000,
        "output_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 2_000},
    }
    entry = {
        "type": "assistant",
        "timestamp": stamp.isoformat().replace("+00:00", "Z"),
        "message": {"id": "m", "model": "claude-opus-5-5", "usage": usage, "content": []},
    }
    path = tmp / "session.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return path


def run(command: str, payload: dict[str, object], home: Path, disable: str) -> str:
    """CimriHook alt komutunu ayrı süreçte çalıştırır ve stdout'unu döndürür."""
    completed = subprocess.run(
        [sys.executable, "-m", "cimrihook", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CIMRIHOOK_HOME": str(home), "CIMRIHOOK_DISABLE": disable},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def prompt(path: Path, text: str) -> dict[str, object]:
    """UserPromptSubmit yükü."""
    return {
        "session_id": "s",
        "transcript_path": str(path),
        "cwd": str(path.parent),
        "hook_event_name": "UserPromptSubmit",
        "prompt": text,
    }


def test_cold_large_session_is_stopped_once_then_passes(tmp_path: Path) -> None:
    path = transcript(tmp_path, idle_seconds=2 * 3_600, context=300_000)
    home = tmp_path / "home"
    decision = json.loads(run("guard", prompt(path, "next step please"), home, ""))
    assert decision["decision"] == "block"
    # 300000 x 2.0 (1 saatlik yazım) x $4/MTok = $2.40
    assert "re-cache the whole 300k-token conversation (about $2.40" in decision["reason"]
    assert "/compact" in decision["reason"]
    assert run("guard", prompt(path, "next step please"), home, "") == ""


def test_commands_warm_caches_small_sessions_and_the_switch_pass(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cold = transcript(tmp_path, idle_seconds=2 * 3_600, context=300_000)
    assert run("guard", prompt(cold, "/compact"), home, "") == ""
    assert run("guard", prompt(cold, "go on"), home, "guard") == ""
    warm = transcript(tmp_path, idle_seconds=600, context=300_000)
    assert run("guard", prompt(warm, "go on"), home, "") == ""
    small = transcript(tmp_path, idle_seconds=2 * 3_600, context=50_000)
    assert run("guard", prompt(small, "go on"), home, "") == ""


def test_brief_is_appended_to_compaction(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "session_id": "s",
        "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": "PreCompact",
        "trigger": "auto",
        "custom_instructions": "",
    }
    assert run("brief", payload, tmp_path / "home", "") == COMPACTION_BRIEF
