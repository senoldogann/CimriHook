"""Kurulum: kullanıcı ayarlarına ekleme, durum satırı zincirleme, yedek ve yalnızca eklenenleri
geri alma."""

import json
import os
import subprocess
import sys
from pathlib import Path

from cimrihook.install import EMPTY_RECORD, install, run_init, run_remove
from cimrihook.settings import governor_env, guard_settings, statusline_settings

PYTHON = "/opt/cimrihook/bin/python"
USER_SETTINGS: dict[str, object] = {
    "model": "opus",
    "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000", "OTHER": "1"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}]
    },
    "statusLine": {"type": "command", "command": "'/apps/my status.sh'"},
}
BLOCKS = [guard_settings(PYTHON), statusline_settings(PYTHON), governor_env(183_000)]


def test_install_chains_the_user_status_line_and_is_idempotent() -> None:
    first = install(USER_SETTINGS, BLOCKS, EMPTY_RECORD)
    hooks = first.settings["hooks"]
    assert isinstance(hooks, dict)
    assert hooks["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}
    ]
    status = first.settings["statusLine"]
    assert isinstance(status, dict)
    assert status["command"] == (
        "/opt/cimrihook/bin/python -m cimrihook statusline --after "
        "''\"'\"'/apps/my status.sh'\"'\"''"
    )
    assert first.record.status_line == USER_SETTINGS["statusLine"]
    assert install(first.settings, BLOCKS, first.record).settings == first.settings


def test_init_backs_up_and_remove_restores_the_original(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    home = tmp_path / "home"
    report = run_init(path, BLOCKS, home, True, 1.0)
    assert "dry run" in report and json.loads(path.read_text()) == USER_SETTINGS
    run_init(path, BLOCKS, home, False, 2.0)
    assert (tmp_path / "settings.json.cimrihook-backup-2").exists()
    installed = json.loads(path.read_text())
    assert installed["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "183000"
    assert "-m cimrihook guard" in json.dumps(installed["hooks"]["UserPromptSubmit"])
    # Aynı saniyede ikinci kurulum farklı pencereyle: ilk yedek korunur, kaldırma yine
    # kullanıcının asıl değerlerini ve durum satırını geri yükler.
    run_init(path, [*BLOCKS[:2], governor_env(233_000)], home, False, 2.0)
    assert (tmp_path / "settings.json.cimrihook-backup-2-1").exists()
    run_remove(path, home, False, 4.0)
    assert json.loads(path.read_text()) == USER_SETTINGS


def test_chained_status_line_runs_the_previous_command_first(tmp_path: Path) -> None:
    payload = {
        "session_id": "s",
        "transcript_path": str(tmp_path / "none.jsonl"),
        "model": {"id": "claude-opus-5-5"},
        "context_window": {"total_input_tokens": 50_000},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "cimrihook", "statusline", "--after", "cat >/dev/null; echo theirs"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CIMRIHOOK_HOME": str(tmp_path / "home")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "theirs · 50k ctx"
