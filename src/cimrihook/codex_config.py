"""Codex CLI configuration: the auto-compaction threshold (~/.codex/config.toml).

Codex hooks cannot change tool output and the lifetime of OpenAI's cache is not documented, so
on Codex CimriHook's lever is the compaction threshold: `model_auto_compact_token_limit` (Codex
caps it at 90% of the model's window and applies it to the whole context). Python's standard
library cannot write TOML: the file is edited as text with the smallest change (only the line
of this key) and is not written until the result, read back with tomllib, shows that only this
key changed. The previous value is kept in the install record per settings file; removal puts
it back if the value is still CimriHook's. The diff output has no context lines (the
configuration may hold API keys).
"""

import difflib
import re
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from cimrihook.errors import ConfigError
from cimrihook.install import (
    EMPTY_RECORD,
    PRIVATE_FILE_MODE,
    InstallRecord,
    SettingChange,
    backup_path,
    load_record,
    save_record,
    write_atomically,
)

KEY: Final = "model_auto_compact_token_limit"
TABLE_HEADER: Final = re.compile(r"^\s*\[")
KEY_LINE: Final = re.compile(rf"^\s*{KEY}\s*=")

type TomlTable = dict[str, object]


@dataclass(frozen=True, slots=True)
class CodexPlan:
    """The change to make in config.toml."""

    path: Path
    before: str
    after: str
    record: InstallRecord
    notes: tuple[str, ...]


def plan_codex_window(path: Path, window: int, home: Path) -> CodexPlan:
    """Plan to set the threshold; the user's own value stays recorded if CimriHook set it before."""
    if window <= 0:
        raise ConfigError(f"--compact-window must be positive, got {window}")
    before = read_config(path)
    earlier = load_record(home, path)
    current = top_level_integer(parse_toml(before, path), path)
    ours = next((change for change in earlier.settings if change.key == KEY), None)
    previous = ours.previous if ours is not None and ours.value == current else current
    after = checked_edit(before, window, path)
    record = InstallRecord((), (SettingChange(KEY, window, previous),), None)
    notes = ("Codex caps the limit at 90% of the model's context window",)
    return CodexPlan(path, before, after, record, notes)


def plan_codex_remove(path: Path, home: Path) -> CodexPlan:
    """Plan to restore the previous threshold, if the value is still the one CimriHook wrote."""
    before = read_config(path)
    current = top_level_integer(parse_toml(before, path), path)
    ours = next((c for c in load_record(home, path).settings if c.key == KEY), None)
    if ours is None or ours.value != current:
        return CodexPlan(path, before, before, EMPTY_RECORD, ())
    if isinstance(ours.previous, str):
        raise ConfigError(f"the install record holds a text value for {KEY}: {ours.previous!r}")
    return CodexPlan(path, before, checked_edit(before, ours.previous, path), EMPTY_RECORD, ())


def read_config(path: Path) -> str:
    """Text of config.toml; empty if the file does not exist."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def parse_toml(text: str, path: Path) -> TomlTable:
    """Reads the TOML text; a corrupt file is left untouched."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error


def top_level_integer(table: TomlTable, path: Path) -> int | None:
    """The threshold's value in the file; None if absent, an error if it is not an integer."""
    value = table.get(KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: {KEY} is {value!r}; set it by hand or remove it first")
    return value


def checked_edit(text: str, value: int | None, path: Path) -> str:
    """Sets the threshold (None: deletes it) and verifies that only this key changed."""
    before = parse_toml(text, path)
    edited = with_top_level_key(text, value)
    after = parse_toml(edited, path)
    expected = {key: item for key, item in before.items() if key != KEY} | (
        {} if value is None else {KEY: value}
    )
    if after != expected:
        raise ConfigError(
            f"editing {KEY} in {path} would change more than that key; set it there by hand"
        )
    return edited


def with_top_level_key(text: str, value: int | None) -> str:
    """Writes, replaces or deletes the line of a top-level key before the first table header."""
    lines = text.splitlines(keepends=True)
    first_table = next(
        (index for index, line in enumerate(lines) if TABLE_HEADER.match(line)), len(lines)
    )
    existing = next((index for index in range(first_table) if KEY_LINE.match(lines[index])), None)
    new_line = [] if value is None else [f"{KEY} = {value}\n"]
    if existing is not None:
        return "".join([*lines[:existing], *new_line, *lines[existing + 1 :]])
    if value is None:
        return text
    insert_at = first_table
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1  # the key goes above the blank lines before the table
    head = lines[:insert_at]
    if head and not head[-1].endswith("\n"):
        head = [*head[:-1], head[-1] + "\n"]
    return "".join([*head, *new_line, *lines[insert_at:]])


def render_codex_plan(plan: CodexPlan) -> str:
    """Changed lines (without context lines) and notes."""
    diff = "\n".join(
        difflib.unified_diff(
            plan.before.splitlines(),
            plan.after.splitlines(),
            str(plan.path),
            f"{plan.path} (after)",
            lineterm="",
            n=0,
        )
    )
    return "\n".join([diff or f"{plan.path}: no change", *(f"note: {n}" for n in plan.notes)])


def apply_codex_window(plan: CodexPlan, home: Path, now: float) -> str:
    """Applies the threshold plan: record first, then an atomic write with backup, same mode."""
    if plan.after == plan.before:
        return f"{render_codex_plan(plan)}\nalready set"
    save_record(home, plan.path, replace(plan.record, installed_at=now))
    backup = write_config(plan.path, plan.after, now)
    saved = f"backup: {backup}" if backup is not None else "created a new config file"
    return f"{render_codex_plan(plan)}\nwritten: {plan.path}\n{saved}"


def apply_codex_remove(plan: CodexPlan, home: Path, now: float) -> str:
    """Applies the removal plan; the record is cleared after the write."""
    if plan.after == plan.before:
        return f"{render_codex_plan(plan)}\nnothing to remove"
    backup = write_config(plan.path, plan.after, now)
    save_record(home, plan.path, plan.record)
    return f"{render_codex_plan(plan)}\nwritten: {plan.path}\nbackup: {backup}"


def write_config(path: Path, text: str, now: float) -> Path | None:
    """Writes the text atomically, with a backup, keeping the file's permissions."""
    target = path.resolve()
    exists = target.exists()
    backup = backup_path(target, now) if exists else None
    if backup is not None:
        backup.write_bytes(target.read_bytes())
        backup.chmod(stat.S_IMODE(target.stat().st_mode))
    mode = stat.S_IMODE(target.stat().st_mode) if exists else PRIVATE_FILE_MODE
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(target, text, mode)
    return backup
