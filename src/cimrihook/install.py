"""Install: adds or removes the CimriHook components in the Claude Code settings file.

The user's settings change only through this command. Before writing, the file is backed up, the
write is atomic and the file's permissions are kept; for a settings file that is a symlink the
target file is updated. A plan is made first; a dry run shows only the plan's diff. In the diff
output the values of every `env` and `headers` object (for example those of MCP servers) are
hidden, except what CimriHook manages: API keys must not reach the terminal or an agent's context.

The install is declarative: CimriHook's earlier install is undone first, then the selected
components are added. The same install changes nothing a second time; the commands of an old
version are replaced by the new ones, not left next to them.

The user's own status line command is not overwritten but chained: CimriHook first runs that
command with the same input and appends its own part at the end. The previous command is kept
only in the `--after` argument of the chained command in the settings file and is restored from
there on removal; no runnable command is ever read from the install record. Per settings file the
record holds only the previous values of the environment variables and top-level settings
(the compaction window) that CimriHook writes.
"""

import difflib
import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, TypeGuard

from cimrihook.errors import ConfigError
from cimrihook.settings import chained_statusline_command

MARKER: Final = " -m cimrihook "  # recognises the commands CimriHook wrote
AFTER_FLAG: Final = "--after"  # argument of the chained previous status line command
INSTALL_RECORD: Final = "installed.json"
RECORD_VERSION: Final = 2
# Environment variables CimriHook manages; their values stay visible in the diff.
MANAGED_ENV: Final = frozenset({"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_PLUGIN_DIRS"})
# Path list variables: the user's value is kept and CimriHook's path is appended.
PATH_LIST_ENV: Final = frozenset({"CLAUDE_CODE_PLUGIN_DIRS"})
WINDOW_ENV: Final = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"  # overrides autoCompactWindow if set
# Top-level settings CimriHook writes: the compaction window and the cache lifetimes.
MANAGED_KEYS: Final = ("autoCompactWindow", "promptCacheTtl", "subagentPromptCacheTtl")
CACHE_TTL_ENV: Final = (
    "FORCE_PROMPT_CACHING_5M",
    "ENABLE_PROMPT_CACHING_1H",
)  # overrides the lifetime settings
SECRET_CONTAINERS: Final = frozenset({"env", "headers"})  # objects whose values the diff hides
HIDDEN: Final = "<hidden>"
PRIVATE_FILE_MODE: Final = 0o600
PRIVATE_DIR_MODE: Final = 0o700

type Settings = dict[str, object]
type SettingValue = int | str


@dataclass(frozen=True, slots=True)
class EnvChange:
    """An environment variable the install wrote and its value before CimriHook."""

    name: str
    value: str
    previous: str | None


@dataclass(frozen=True, slots=True)
class SettingChange:
    """A top-level setting the install wrote (integer or text) and its value before CimriHook."""

    key: str
    value: SettingValue
    previous: SettingValue | None


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """Environment variables and top-level settings to restore on removal."""

    env: tuple[EnvChange, ...]
    settings: tuple[SettingChange, ...]
    installed_at: float | None  # time of the last install that changed settings (cimrihook gain)


@dataclass(frozen=True, slots=True)
class Plan:
    """A change to make in the settings file."""

    path: Path
    before: Settings
    after: Settings
    record: InstallRecord  # the install record after the change
    notes: tuple[str, ...]


EMPTY_RECORD: Final = InstallRecord(env=(), settings=(), installed_at=None)


def install(
    settings: Settings, blocks: Sequence[Settings], earlier: InstallRecord
) -> tuple[Settings, InstallRecord, tuple[str, ...]]:
    """Undoes the previous install and adds the selected blocks; the status line is chained.

    Key order is preserved; the previous values of environment variables are taken from the cleaned
    settings, so a reinstall never loses the user's original value.
    """
    clean = uninstall(settings, earlier)
    hooks = hooks_of(clean)
    env = env_of(clean)
    status_line = clean.get("statusLine")
    values: dict[str, object | None] = {}
    changes: list[EnvChange] = []
    setting_changes: list[SettingChange] = []
    notes: list[str] = []
    for block in blocks:
        for event, entries in hooks_of(block).items():
            hooks[event] = [*hooks.get(event, []), *entries]
        for name, value in env_of(block).items():
            previous = env.get(name)
            merged = (
                joined_paths(None if previous is None else str(previous), str(value))
                if name in PATH_LIST_ENV
                else str(value)
            )
            changes.append(EnvChange(name, merged, None if previous is None else str(previous)))
            env[name] = merged
        for key in MANAGED_KEYS:
            wanted_value = block.get(key)
            if wanted_value is None:
                continue
            setting_changes.append(
                SettingChange(key, setting_value(wanted_value, key), current_setting(clean, key))
            )
            values[key] = wanted_value
            overriding = [WINDOW_ENV] if key == "autoCompactWindow" else list(CACHE_TTL_ENV)
            notes.extend(
                f"{name} in your env overrides {key}; remove it for this setting to apply"
                for name in overriding
                if name in env
            )
        wanted = block.get("statusLine")
        if wanted is None:
            continue
        if status_line is None:
            status_line = wanted
        elif is_command(status_line):
            status_line = chain(wanted, status_line)
            notes.append("your status line still runs first; CimriHook's part is added after it")
        else:
            notes.append("kept your own statusLine: it is not a command, so it cannot be chained")
    after = with_values(
        with_values(settings, {key: clean.get(key) for key in MANAGED_KEYS}),
        {"hooks": hooks or None, "env": env or None, "statusLine": status_line, **values},
    )
    return after, InstallRecord(tuple(changes), tuple(setting_changes), None), tuple(notes)


def joined_paths(current: str | None, path: str) -> str:
    """Appends the path to a path list; the same path already in the list is removed first."""
    kept = [] if current is None else [p for p in current.split(os.pathsep) if p and p != path]
    return os.pathsep.join([*kept, path])


def setting_value(value: object, key: str) -> SettingValue:
    """A setting value in a block: integer or text."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"setting {key!r} must be an integer or a string, got {value!r}")
    return value


def current_setting(settings: Settings, key: str) -> SettingValue | None:
    """The value in the settings file; None if absent. A value of another type was set by hand: an
    error instead of overwriting it."""
    value = settings.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"settings {key!r} is {value!r}; set it by hand or remove it first")
    return value


def uninstall(settings: Settings, record: InstallRecord) -> Settings:
    """Removes the hooks, environment variables and status line CimriHook added.

    An environment variable returns to its previous value only if it still has the value CimriHook
    wrote; if the user changed it since, it is left alone.
    """
    hooks = {
        event: kept
        for event, entries in hooks_of(settings).items()
        if (kept := [entry for entry in map(without_ours, entries) if entry is not None])
    }
    env = env_of(settings)
    for change in record.env:
        if env.get(change.name) == change.value:
            env = {
                name: change.previous if name == change.name else value
                for name, value in env.items()
                if name != change.name or change.previous is not None
            }
    restored = {
        change.key: change.previous
        for change in record.settings
        if settings.get(change.key) == change.value
    }
    return with_values(
        settings,
        {
            "hooks": hooks or None,
            "env": env or None,
            "statusLine": restored_status_line(settings.get("statusLine")),
            **restored,
        },
    )


def restored_status_line(current: object) -> object | None:
    """The previous status line to restore in place of CimriHook's; None if there was none (the key
    is deleted). A status line that is not CimriHook's stays as it is."""
    if not is_ours(current) or not is_command(current):
        return current
    previous = after_argument(str(current["command"]))
    return None if previous is None else {**current, "command": previous}


def after_argument(command: str) -> str | None:
    """The previous command in a chained CimriHook status line command; None without a chain."""
    try:
        words = shlex.split(command)
    except ValueError as error:
        raise ConfigError(f"cannot parse the CimriHook status line {command!r}: {error}") from error
    if AFTER_FLAG not in words:
        return None
    position = words.index(AFTER_FLAG)
    if position + 1 == len(words):
        raise ConfigError(f"the CimriHook status line ends with {AFTER_FLAG}: {command!r}")
    return words[position + 1]


def chain(ours: object, previous: object) -> dict[str, object]:
    """CimriHook's status line that runs the user's previous command first; the other fields of the
    previous status line (for example padding, refreshInterval) are kept."""
    if not is_command(ours) or not is_command(previous):
        raise ConfigError(f"cannot chain status lines {ours!r} and {previous!r}")
    command = chained_statusline_command(str(ours["command"]), str(previous["command"]))
    return {**previous, **ours, "command": command}


def with_values(settings: Settings, values: dict[str, object | None]) -> Settings:
    """Updates values in order; a None value removes the key, new keys are appended."""
    kept = {
        key: values.get(key, value)
        for key, value in settings.items()
        if not (key in values and values[key] is None)
    }
    return kept | {
        key: value for key, value in values.items() if key not in settings and value is not None
    }


def hooks_of(settings: Settings) -> dict[str, list[object]]:
    """The hook groups in the settings, per event (a copy)."""
    hooks = settings.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise ConfigError(f"settings 'hooks' must be an object, got {type(hooks).__name__}")
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ConfigError(f"settings 'hooks.{event}' must be a list of hook groups")
    return {str(event): list(entries) for event, entries in hooks.items()}


def env_of(settings: Settings) -> dict[str, object]:
    """The environment variables in the settings (a copy)."""
    env = settings.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ConfigError(f"settings 'env' must be an object, got {type(env).__name__}")
    return {str(name): value for name, value in env.items()}


def without_ours(entry: object) -> object | None:
    """Removes the CimriHook commands from a group; None if no command is left in the group."""
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return entry
    kept = [
        handler
        for handler in entry["hooks"]
        if not (isinstance(handler, dict) and MARKER in str(handler.get("command", "")))
    ]
    return ({**entry, "hooks": kept}) if kept else None


def is_ours(status_line: object) -> bool:
    """Is the status line CimriHook's?"""
    return isinstance(status_line, dict) and MARKER in str(status_line.get("command", ""))


def is_command(status_line: object) -> TypeGuard[dict[str, object]]:
    """Is the status line a shell command?"""
    return (
        isinstance(status_line, dict)
        and status_line.get("type") == "command"
        and isinstance(status_line.get("command"), str)
    )


def load_settings(path: Path) -> Settings:
    """The settings file; empty settings if it does not exist."""
    if not path.exists():
        return {}
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"{path} is not valid JSON ({error.msg} at line {error.lineno})"
        ) from error
    if not isinstance(decoded, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return {str(key): value for key, value in decoded.items()}


def masked(value: object) -> object:
    """For the diff output: the values of env and headers objects at every level are hidden."""
    if isinstance(value, dict):
        return {
            key: hidden_values(item) if key in SECRET_CONTAINERS else masked(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


def hidden_values(container: object) -> object:
    """Replaces the values CimriHook does not manage in an object with a marker."""
    if not isinstance(container, dict):
        return container
    return {name: value if name in MANAGED_ENV else HIDDEN for name, value in container.items()}


def settings_diff(path: Path, before: Settings, after: Settings) -> str:
    """The combined diff of two settings states (with hidden values)."""
    old = json.dumps(masked(before), indent=2, ensure_ascii=False).splitlines()
    new = json.dumps(masked(after), indent=2, ensure_ascii=False).splitlines()
    lines = difflib.unified_diff(old, new, str(path), f"{path} (after)", lineterm="")
    return "\n".join(lines) or f"{path}: no change"


def write_settings(path: Path, settings: Settings, now: float) -> Path | None:
    """Writes the settings atomically; backs up an existing file first and returns the backup path.

    With a symlinked settings file the link is kept and the target file is updated. The file's
    permissions are kept; a new file is readable by the user only.
    """
    target = path.resolve()
    exists = target.exists()
    backup = backup_path(target, now) if exists else None
    if backup is not None:
        shutil.copy2(target, backup)
    mode = stat.S_IMODE(target.stat().st_mode) if exists else PRIVATE_FILE_MODE
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(target, json.dumps(settings, indent=2, ensure_ascii=False) + "\n", mode)
    return backup


def backup_path(target: Path, now: float) -> Path:
    """A backup path that never overwrites an existing backup."""
    stem = f"{target.name}.cimrihook-backup-{int(now)}"
    candidates = (target.with_name(stem if n == 0 else f"{stem}-{n}") for n in range(1000))
    found = next((candidate for candidate in candidates if not candidate.exists()), None)
    if found is None:
        raise ConfigError(f"too many CimriHook backups named {stem}* next to {target}")
    return found


def write_atomically(target: Path, text: str, mode: int) -> None:
    """Writes the text to a unique temporary file next to the target and moves it into place.

    An interrupted write never corrupts the old file; the file gets the given permissions regardless
    of umask (the temporary file is readable by the user only from the start).
    """
    descriptor, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".cimrihook-tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)  # exists only if the move did not happen


def record_key(path: Path) -> str:
    """The settings file's key in the record: its absolute path with symlinks resolved."""
    return str(path.expanduser().resolve())


def load_records(home: Path) -> dict[str, InstallRecord]:
    """The install records per settings file; empty if there is no record file."""
    file = home / INSTALL_RECORD
    if not file.exists():
        return {}
    try:
        data: object = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"{file} is not valid JSON ({error.msg}); move it away to start without a record"
        ) from error
    if not isinstance(data, dict):
        raise ConfigError(f"{file} must contain a JSON object")
    installs = data.get("installs")
    if isinstance(installs, dict):
        return {str(key): parse_record(value, file) for key, value in installs.items()}
    legacy = data.get("settings")
    if isinstance(legacy, str):  # v1: one settings file; its status line field is unused
        return {record_key(Path(legacy)): parse_record(data, file)}
    raise ConfigError(f"{file}: unknown install record format")


def parse_record(value: object, file: Path) -> InstallRecord:
    """The record of one settings file; a corrupt entry is an error. A record without a 'settings'
    list is from older versions that wrote no top-level settings."""
    entries = value.get("env") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"{file}: every install record needs an 'env' list")
    raw_settings = value.get("settings", []) if isinstance(value, dict) else []
    if not isinstance(raw_settings, list):
        raise ConfigError(f"{file}: 'settings' must be a list")
    changes: list[EnvChange] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        current = entry.get("value") if isinstance(entry, dict) else None
        previous = entry.get("previous") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(current, str):
            raise ConfigError(f"{file}: malformed env entry {entry!r}")
        if previous is not None and not isinstance(previous, str):
            raise ConfigError(f"{file}: malformed previous value in {entry!r}")
        changes.append(EnvChange(name, current, previous))
    installed_at = value.get("installed_at") if isinstance(value, dict) else None
    if installed_at is not None and (
        isinstance(installed_at, bool) or not isinstance(installed_at, int | float)
    ):
        raise ConfigError(f"{file}: malformed installed_at {installed_at!r}")
    return InstallRecord(
        tuple(changes),
        tuple(parse_setting(entry, file) for entry in raw_settings),
        None if installed_at is None else float(installed_at),
    )


def parse_setting(entry: object, file: Path) -> SettingChange:
    """A top-level setting change in the record."""
    key = entry.get("key") if isinstance(entry, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    previous = entry.get("previous") if isinstance(entry, dict) else None
    if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"{file}: malformed setting entry {entry!r}")
    if previous is not None and (isinstance(previous, bool) or not isinstance(previous, int | str)):
        raise ConfigError(f"{file}: malformed previous value in {entry!r}")
    return SettingChange(key, value, previous)


def load_record(home: Path, path: Path) -> InstallRecord:
    """The install record for this settings file; an empty record if there is none."""
    return load_records(home).get(record_key(path), EMPTY_RECORD)


def save_record(home: Path, path: Path, record: InstallRecord) -> None:
    """Writes this settings file's record, keeping the records of the other files."""
    records = load_records(home) | {record_key(path): record}
    data = {
        "version": RECORD_VERSION,
        "installs": {
            key: {
                "env": [
                    {"name": c.name, "value": c.value, "previous": c.previous} for c in item.env
                ],
                "settings": [
                    {"key": c.key, "value": c.value, "previous": c.previous} for c in item.settings
                ],
                "installed_at": item.installed_at,
            }
            for key, item in records.items()
        },
    }
    home.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    write_atomically(home / INSTALL_RECORD, json.dumps(data, indent=2) + "\n", PRIVATE_FILE_MODE)


def plan_init(path: Path, blocks: Sequence[Settings], home: Path) -> Plan:
    """The install plan: the change that undoes the previous install and adds the new blocks."""
    before = load_settings(path)
    after, record, notes = install(before, blocks, load_record(home, path))
    return Plan(path, before, after, record, notes)


def plan_remove(path: Path, home: Path) -> Plan:
    """The removal plan: the change that undoes what CimriHook added."""
    before = load_settings(path)
    after = uninstall(before, load_record(home, path))
    return Plan(path, before, after, EMPTY_RECORD, ())


def render_plan(plan: Plan) -> str:
    """The plan's diff and notes."""
    return "\n".join(
        [settings_diff(plan.path, plan.before, plan.after), *(f"note: {n}" for n in plan.notes)]
    )


def apply_init(plan: Plan, home: Path, now: float) -> str:
    """Applies the install plan. The record is written first: if the settings write is interrupted,
    removal still knows the previous values."""
    if plan.after == plan.before:
        return f"{render_plan(plan)}\nalready installed"
    save_record(home, plan.path, replace(plan.record, installed_at=now))
    backup = write_settings(plan.path, plan.after, now)
    saved = f"backup: {backup}" if backup is not None else "created a new settings file"
    return f"{render_plan(plan)}\nwritten: {plan.path}\n{saved}"


def apply_remove(plan: Plan, home: Path, now: float) -> str:
    """Applies the removal plan. The record is cleared after the settings are written: if the write
    is interrupted, removal can be retried."""
    if plan.after == plan.before:
        return f"{render_plan(plan)}\nnothing to remove"
    backup = write_settings(plan.path, plan.after, now)
    save_record(home, plan.path, plan.record)
    return f"{render_plan(plan)}\nwritten: {plan.path}\nbackup: {backup}"
