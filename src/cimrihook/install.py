"""Kurulum: CimriHook bileşenlerini Claude Code ayar dosyasına ekler ya da çıkarır.

Kullanıcının ayarları yalnızca bu komutla değişir. Yazmadan önce dosyanın zaman damgalı yedeği
alınır; --dry-run yalnızca yapılacak değişikliği gösterir. Aynı kurulum ikinci kez çalıştırıldığında
bir şey eklemez. Kullanıcının kendi durum satırı varsa ezilmez. Kaldırma yalnızca CimriHook'un
eklediklerini geri alır: komutu `-m cimrihook` içeren hook'lar ve durum satırı ile kurulum kaydına
yazılmış ortam değişkenleri (önceki değerleri geri yüklenir); kullanıcının kendi hook'larına
dokunmaz.
"""

import difflib
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.errors import ConfigError

MARKER: Final = " -m cimrihook "  # CimriHook'un yazdığı komutları tanır
INSTALL_RECORD: Final = "installed.json"

type Settings = dict[str, object]


@dataclass(frozen=True, slots=True)
class EnvChange:
    """Kurulumun yazdığı ortam değişkeni ve kurulumdan önceki değeri."""

    name: str
    value: str
    previous: str | None


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Kurulumdan sonraki ayarlar ve kullanıcıya söylenecek notlar."""

    settings: Settings
    env_changes: tuple[EnvChange, ...]
    notes: tuple[str, ...]


def install(settings: Settings, blocks: Sequence[Settings]) -> InstallResult:
    """Blokları ayarlara ekler; var olan aynı komutlar ve kullanıcının durum satırı korunur."""
    hooks = hooks_of(settings)
    env = env_of(settings)
    notes: list[str] = []
    changes: list[EnvChange] = []
    status_line = settings.get("statusLine")
    for block in blocks:
        for event, entries in hooks_of(block).items():
            hooks[event] = [*hooks.get(event, []), *new_entries(hooks.get(event, []), entries)]
        for name, value in env_of(block).items():
            previous = env.get(name)
            changes.append(EnvChange(name, str(value), None if previous is None else str(previous)))
            env[name] = value
        wanted = block.get("statusLine")
        if wanted is not None and status_line is not None and not is_ours(status_line):
            notes.append("kept your own statusLine; set it to `cimrihook statusline` to see ours")
        elif wanted is not None:
            status_line = wanted
    return InstallResult(
        settings=with_values(
            settings, {"hooks": hooks or None, "env": env or None, "statusLine": status_line}
        ),
        env_changes=tuple(changes),
        notes=tuple(notes),
    )


def uninstall(settings: Settings, env_changes: Sequence[EnvChange]) -> Settings:
    """CimriHook'un eklediği hook'ları, durum satırını ve ortam değişkenlerini çıkarır."""
    hooks = {
        event: kept
        for event, entries in hooks_of(settings).items()
        if (kept := [entry for entry in map(without_ours, entries) if entry is not None])
    }
    env = env_of(settings)
    for change in env_changes:
        if env.get(change.name) == change.value:
            env = {
                name: change.previous if name == change.name else value
                for name, value in env.items()
                if name != change.name or change.previous is not None
            }
    status_line = settings.get("statusLine")
    return with_values(
        settings,
        {
            "hooks": hooks or None,
            "env": env or None,
            "statusLine": None if is_ours(status_line) else status_line,
        },
    )


def with_values(settings: Settings, values: dict[str, object | None]) -> Settings:
    """Sırayı koruyarak değerleri günceller; None anahtarı çıkarır, yeni anahtarlar sona eklenir."""
    kept = {
        key: values.get(key, value)
        for key, value in settings.items()
        if not (key in values and values[key] is None)
    }
    return kept | {
        key: value for key, value in values.items() if key not in settings and value is not None
    }


def hooks_of(settings: Settings) -> dict[str, list[object]]:
    """Ayarlardaki hook grupları, olay başına (kopya)."""
    hooks = settings.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise ConfigError(f"settings 'hooks' must be an object, got {type(hooks).__name__}")
    return {
        str(event): list(entries) for event, entries in hooks.items() if isinstance(entries, list)
    }


def env_of(settings: Settings) -> dict[str, object]:
    """Ayarlardaki ortam değişkenleri (kopya)."""
    env = settings.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ConfigError(f"settings 'env' must be an object, got {type(env).__name__}")
    return {str(name): value for name, value in env.items()}


def commands_in(entry: object) -> set[str]:
    """Bir hook grubundaki komutlar."""
    handlers = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(handlers, list):
        return set()
    return {
        str(handler["command"])
        for handler in handlers
        if isinstance(handler, dict) and isinstance(handler.get("command"), str)
    }


def new_entries(existing: Sequence[object], entries: Sequence[object]) -> list[object]:
    """Henüz kayıtlı olmayan komutları taşıyan gruplar."""
    present = set().union(*(commands_in(entry) for entry in existing))
    return [entry for entry in entries if not commands_in(entry) <= present]


def without_ours(entry: object) -> object | None:
    """Gruptan CimriHook komutlarını çıkarır; grupta komut kalmazsa None."""
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return entry
    kept = [
        handler
        for handler in entry["hooks"]
        if not (isinstance(handler, dict) and MARKER in str(handler.get("command", "")))
    ]
    return ({**entry, "hooks": kept}) if kept else None


def is_ours(status_line: object) -> bool:
    """Durum satırı CimriHook'un mu?"""
    return isinstance(status_line, dict) and MARKER in str(status_line.get("command", ""))


def load_settings(path: Path) -> Settings:
    """Ayar dosyası; yoksa boş ayarlar."""
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


def settings_diff(path: Path, before: Settings, after: Settings) -> str:
    """İki ayar durumunun birleşik farkı."""
    old = json.dumps(before, indent=2, ensure_ascii=False).splitlines()
    new = json.dumps(after, indent=2, ensure_ascii=False).splitlines()
    lines = difflib.unified_diff(old, new, str(path), f"{path} (after)", lineterm="")
    return "\n".join(lines) or f"{path}: no change"


def write_settings(path: Path, settings: Settings, now: float) -> Path | None:
    """Ayarları yazar; dosya varsa önce zaman damgalı yedeğini alır ve yedeğin yolunu döndürür."""
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.cimrihook-backup-{int(now)}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def save_record(home: Path, path: Path, changes: Sequence[EnvChange]) -> None:
    """Kurulumun ortam değişkeni değişikliklerini, kaldırmada geri almak için kaydeder."""
    home.mkdir(parents=True, exist_ok=True)
    record = {
        "settings": str(path),
        "env": [{"name": c.name, "value": c.value, "previous": c.previous} for c in changes],
    }
    (home / INSTALL_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def load_record(home: Path, path: Path) -> tuple[EnvChange, ...]:
    """Bu ayar dosyası için kaydedilmiş ortam değişkeni değişiklikleri; kayıt yoksa boş."""
    file = home / INSTALL_RECORD
    if not file.exists():
        return ()
    record = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("settings") != str(path):
        return ()
    entries = record.get("env")
    if not isinstance(entries, list):
        raise ConfigError(f"{file}: 'env' must be a list")
    return tuple(
        EnvChange(
            str(entry["name"]),
            str(entry["value"]),
            previous if isinstance(previous := entry.get("previous"), str) else None,
        )
        for entry in entries
        if isinstance(entry, dict)
    )


def merge_record(old: Sequence[EnvChange], new: Sequence[EnvChange]) -> tuple[EnvChange, ...]:
    """Yeni kurulumun kaydı; değişkeni daha önce CimriHook yazdıysa asıl önceki değer korunur."""
    earlier = {change.name: change for change in old}
    return tuple(
        EnvChange(change.name, change.value, earlier[change.name].previous)
        if change.name in earlier and earlier[change.name].value == change.previous
        else change
        for change in new
    )


def run_init(path: Path, blocks: Sequence[Settings], home: Path, dry_run: bool, now: float) -> str:
    """Kurulumu uygular ya da (dry_run) yalnızca farkını gösterir; kullanıcıya rapor döndürür."""
    before = load_settings(path)
    result = install(before, blocks)
    lines = [settings_diff(path, before, result.settings), *(f"note: {n}" for n in result.notes)]
    if dry_run or result.settings == before:
        return "\n".join([*lines, "dry run: nothing written" if dry_run else "already installed"])
    backup = write_settings(path, result.settings, now)
    save_record(home, path, merge_record(load_record(home, path), result.env_changes))
    saved = f"backup: {backup}" if backup is not None else "created a new settings file"
    return "\n".join([*lines, f"written: {path}", saved])


def run_remove(path: Path, home: Path, dry_run: bool, now: float) -> str:
    """CimriHook'un eklediklerini çıkarır ya da (dry_run) yalnızca farkını gösterir."""
    before = load_settings(path)
    after = uninstall(before, load_record(home, path))
    lines = [settings_diff(path, before, after)]
    if dry_run or after == before:
        return "\n".join([*lines, "dry run: nothing written" if dry_run else "nothing to remove"])
    backup = write_settings(path, after, now)
    return "\n".join([*lines, f"written: {path}", f"backup: {backup}"])
