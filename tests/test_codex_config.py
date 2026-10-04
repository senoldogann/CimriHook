"""Codex config.toml editing: only the threshold's line changes, removal restores the old one."""

import tomllib
from pathlib import Path

import pytest

from cimrihook.codex_config import (
    apply_codex_remove,
    apply_codex_window,
    plan_codex_remove,
    plan_codex_window,
    with_top_level_key,
)
from cimrihook.errors import ConfigError

CONFIG = """# my codex config
model = "gpt-6.1-sol"
model_reasoning_effort = "high"

[model_providers.ollama]
name = "Ollama"
env_key = "OLLAMA_SECRET"
"""


def test_the_key_goes_before_the_first_table_and_nothing_else_changes() -> None:
    edited = with_top_level_key(CONFIG, 100_000)
    assert edited.splitlines()[3] == "model_auto_compact_token_limit = 100000"
    parsed = tomllib.loads(edited)
    assert parsed["model_auto_compact_token_limit"] == 100_000
    assert parsed["model_providers"] == tomllib.loads(CONFIG)["model_providers"]
    assert with_top_level_key(edited, None) == CONFIG


def test_init_and_remove_restore_the_users_own_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.replace(
            'model = "gpt-6.1-sol"\n',
            'model = "gpt-6.1-sol"\nmodel_auto_compact_token_limit = 200000\n',
        ),
        encoding="utf-8",
    )
    original = path.read_text(encoding="utf-8")
    path.chmod(0o600)
    home = tmp_path / "home"
    report = apply_codex_window(plan_codex_window(path, 100_000, home), home, 5.0)
    assert "+model_auto_compact_token_limit = 100000" in report
    assert "OLLAMA_SECRET" not in report  # no context line
    assert tomllib.loads(path.read_text())["model_auto_compact_token_limit"] == 100_000
    assert path.stat().st_mode & 0o777 == 0o600
    apply_codex_window(plan_codex_window(path, 120_000, home), home, 6.0)  # second setting
    apply_codex_remove(plan_codex_remove(path, home), home, 7.0)
    assert path.read_text(encoding="utf-8") == original


def test_a_non_integer_value_is_left_to_the_user(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model_auto_compact_token_limit = "auto"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="set it by hand"):
        plan_codex_window(path, 100_000, tmp_path / "home")


def test_a_threshold_counted_after_the_prefix_is_left_to_the_user(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = 'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError, match="after the stable prompt prefix"):
        plan_codex_window(path, 100_000, tmp_path / "home")
    assert path.read_text(encoding="utf-8") == original
