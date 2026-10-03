"""Kurulum: kullanıcı ayarlarına ekleme, idempotentlik, yedek ve yalnızca eklenenleri geri alma."""

import json
from pathlib import Path

from cimrihook.install import install, run_init, run_remove
from cimrihook.settings import governor_env, guard_settings, statusline_settings

PYTHON = "/opt/cimrihook/bin/python"
USER_SETTINGS: dict[str, object] = {
    "model": "opus",
    "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000", "OTHER": "1"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}]
    },
    "statusLine": {"type": "command", "command": "my-status"},
}
BLOCKS = [guard_settings(PYTHON), statusline_settings(PYTHON), governor_env(183_000)]


def test_install_keeps_user_parts_and_is_idempotent() -> None:
    first = install(USER_SETTINGS, BLOCKS)
    hooks = first.settings["hooks"]
    assert isinstance(hooks, dict)
    assert hooks["PreToolUse"] == USER_SETTINGS["hooks"]["PreToolUse"]  # type: ignore[index]
    assert first.settings["statusLine"] == {"type": "command", "command": "my-status"}
    assert first.notes and "kept your own statusLine" in first.notes[0]
    assert install(first.settings, BLOCKS).settings == first.settings


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
    # İkinci kurulum farklı pencereyle: kaldırma yine kullanıcının asıl değerini geri yüklemeli.
    run_init(path, [*BLOCKS[:2], governor_env(233_000)], home, False, 3.0)
    run_remove(path, home, False, 4.0)
    assert json.loads(path.read_text()) == USER_SETTINGS
