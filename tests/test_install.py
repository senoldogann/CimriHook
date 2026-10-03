"""Kurulum: kullanıcı ayarlarına ekleme, durum satırı zincirleme, yedek ve yalnızca eklenenleri
geri alma."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from cimrihook.install import (
    EMPTY_RECORD,
    INSTALL_RECORD,
    EnvChange,
    InstallRecord,
    apply_init,
    apply_remove,
    install,
    plan_init,
    plan_remove,
    settings_diff,
)
from cimrihook.settings import governor_settings, guard_settings, statusline_settings

PYTHON = "/opt/cimrihook/bin/python"
USER_SETTINGS: dict[str, object] = {
    "model": "opus",
    "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000", "OTHER": "secret-value"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}]
    },
    "statusLine": {"type": "command", "command": "'/apps/my status.sh'", "padding": 0},
}
BLOCKS = [guard_settings(PYTHON), statusline_settings(PYTHON), governor_settings(183_000)]


def test_install_chains_the_user_status_line_and_is_idempotent() -> None:
    settings, record, _ = install(USER_SETTINGS, BLOCKS, EMPTY_RECORD)
    hooks = settings["hooks"]
    assert isinstance(hooks, dict)
    assert hooks["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}
    ]
    assert settings["statusLine"] == {
        "type": "command",
        "command": "/opt/cimrihook/bin/python -I -m cimrihook statusline --after "
        "''\"'\"'/apps/my status.sh'\"'\"''",
        "padding": 0,
    }
    assert settings["autoCompactWindow"] == 183_000
    assert install(settings, BLOCKS, record)[0] == settings


def test_the_users_window_variable_is_reported_because_it_overrides_the_setting() -> None:
    _, _, notes = install(USER_SETTINGS, BLOCKS, EMPTY_RECORD)
    assert any("CLAUDE_CODE_AUTO_COMPACT_WINDOW in your env overrides" in note for note in notes)


def test_an_install_that_used_the_environment_variable_moves_to_the_setting() -> None:
    earlier: dict[str, object] = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "183000"}}
    record = InstallRecord((EnvChange("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "183000", None),), (), 1.0)
    settings, new_record, _ = install(earlier, [governor_settings(183_000)], record)
    assert settings == {"autoCompactWindow": 183_000}
    assert new_record.env == () and new_record.settings[0].previous is None


def test_reinstall_from_another_interpreter_replaces_our_hooks() -> None:
    first, record, _ = install(USER_SETTINGS, BLOCKS, EMPTY_RECORD)
    moved = [guard_settings("/new/python"), statusline_settings("/new/python")]
    second, _, _ = install(first, moved, record)
    assert json.dumps(second).count("-m cimrihook guard") == 1
    assert "/opt/cimrihook" not in json.dumps(second)
    env = second["env"]
    assert isinstance(env, dict) and env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    assert "autoCompactWindow" not in second  # bu kurulum pencere seçmedi


def test_diff_hides_values_of_variables_cimrihook_does_not_manage(tmp_path: Path) -> None:
    server = {"command": "mcp", "env": {"API_KEY": "server-secret"}}
    before = USER_SETTINGS | {"mcpServers": {"tool": server}}
    settings, _, _ = install(before, BLOCKS, EMPTY_RECORD)
    diff = settings_diff(tmp_path / "settings.json", before, settings)
    assert "secret-value" not in diff and "server-secret" not in diff
    assert '"autoCompactWindow": 183000' in diff


def test_init_backs_up_and_remove_restores_the_original(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    path.chmod(0o600)
    home = tmp_path / "home"
    apply_init(plan_init(path, BLOCKS, home), home, 2.0)
    assert (tmp_path / "settings.json.cimrihook-backup-2").exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / INSTALL_RECORD).stat().st_mode) == 0o600
    installed = json.loads(path.read_text())
    assert installed["autoCompactWindow"] == 183_000
    assert "-I -m cimrihook guard" in json.dumps(installed["hooks"]["UserPromptSubmit"])
    # Aynı saniyede ikinci kurulum farklı pencereyle: ilk yedek korunur, kaldırma yine
    # kullanıcının asıl değerlerini ve durum satırını geri yükler.
    apply_init(plan_init(path, [*BLOCKS[:2], governor_settings(233_000)], home), home, 2.0)
    assert (tmp_path / "settings.json.cimrihook-backup-2-1").exists()
    apply_remove(plan_remove(path, home), home, 4.0)
    assert json.loads(path.read_text()) == USER_SETTINGS


def test_remove_without_a_record_still_restores_the_chained_status_line(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    home = tmp_path / "home"
    apply_init(plan_init(path, BLOCKS[:2], home), home, 2.0)
    (home / INSTALL_RECORD).unlink()
    apply_remove(plan_remove(path, home), home, 3.0)
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
