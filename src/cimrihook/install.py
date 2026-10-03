"""Kurulum: CimriHook bileşenlerini Claude Code ayar dosyasına ekler ya da çıkarır.

Kullanıcının ayarları yalnızca bu komutla değişir. Yazmadan önce dosyanın yedeği alınır, yazma
atomiktir ve sembolik bağlantılı ayar dosyasında hedef dosya güncellenir; --dry-run yalnızca
yapılacak değişikliği gösterir. Aynı kurulum ikinci kez çalıştırıldığında bir şey eklemez.

Kullanıcının kendi durum satırı komutu varsa ezilmez, zincirlenir: CimriHook önce o komutu aynı
girdiyle çalıştırır, kendi parçasını sona ekler. Kaldırma yalnızca CimriHook'un eklediklerini geri
alır: komutu `-m cimrihook` içeren hook'lar, kurulum kaydındaki ortam değişkenleri (önceki
değerleri geri yüklenir) ve durum satırı (zincirlenen önceki komut geri gelir).
"""

import difflib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard

from cimrihook.errors import ConfigError
from cimrihook.settings import chained_statusline_command

MARKER: Final = " -m cimrihook "  # CimriHook'un yazdığı komutları tanır
INSTALL_RECORD: Final = "installed.json"

type Settings = dict[str, object]


@dataclass(frozen=True, slots=True)
class EnvChange:
    """Kurulumun yazdığı ortam değişkeni ve CimriHook'tan önceki değeri."""

    name: str
    value: str
    previous: str | None


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """Kaldırmada geri alınacaklar."""

    env: tuple[EnvChange, ...]
    status_line: object | None  # CimriHook'tan önceki durum satırı (zincirlenen); yoksa None


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Kurulumdan sonraki ayarlar, güncel kayıt ve kullanıcıya söylenecek notlar."""

    settings: Settings
    record: InstallRecord
    notes: tuple[str, ...]


EMPTY_RECORD: Final = InstallRecord(env=(), status_line=None)


def install(
    settings: Settings, blocks: Sequence[Settings], earlier: InstallRecord
) -> InstallResult:
    """Blokları ayarlara ekler; var olan aynı komutlar korunur, durum satırı zincirlenir."""
    hooks = hooks_of(settings)
    env = env_of(settings)
    notes: list[str] = []
    changes: list[EnvChange] = []
    current = settings.get("statusLine")
    status_line = current
    previous_status = earlier.status_line if is_ours(current) else None
    for block in blocks:
        for event, entries in hooks_of(block).items():
            hooks[event] = [*hooks.get(event, []), *new_entries(hooks.get(event, []), entries)]
        for name, value in env_of(block).items():
            previous = env.get(name)
            changes.append(EnvChange(name, str(value), None if previous is None else str(previous)))
            env[name] = value
        wanted = block.get("statusLine")
        if wanted is None:
            continue
        if current is not None and not is_ours(current) and not is_command(current):
            notes.append("kept your own statusLine: it is not a command, so it cannot be chained")
            continue
        if current is not None and not is_ours(current):
            previous_status = current
            notes.append("your status line still runs first; CimriHook's part is added after it")
        status_line = wanted if previous_status is None else chain(wanted, previous_status)
    return InstallResult(
        settings=with_values(
            settings, {"hooks": hooks or None, "env": env or None, "statusLine": status_line}
        ),
        record=InstallRecord(merge_env(earlier.env, changes), previous_status),
        notes=tuple(notes),
    )


def uninstall(settings: Settings, record: InstallRecord) -> Settings:
    """CimriHook'un eklediği hook'ları, ortam değişkenlerini ve durum satırını çıkarır."""
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
    current = settings.get("statusLine")
    return with_values(
        settings,
        {
            "hooks": hooks or None,
            "env": env or None,
            "statusLine": record.status_line if is_ours(current) else current,
        },
    )


def chain(ours: object, previous: object) -> dict[str, object]:
    """CimriHook durum satırı, kullanıcının önceki komutunu önce çalıştıracak biçimde."""
    if not is_command(ours) or not is_command(previous):
        raise ConfigError(f"cannot chain status lines {ours!r} and {previous!r}")
    command = chained_statusline_command(str(ours["command"]), str(previous["command"]))
    return {**ours, "command": command}


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


def is_command(status_line: object) -> TypeGuard[dict[str, object]]:
    """Durum satırı bir kabuk komutu mu?"""
    return (
        isinstance(status_line, dict)
        and status_line.get("type") == "command"
        and isinstance(status_line.get("command"), str)
    )


def merge_env(old: Sequence[EnvChange], new: Sequence[EnvChange]) -> tuple[EnvChange, ...]:
    """Ortam değişkeni kaydı; değişkeni daha önce CimriHook yazdıysa asıl önceki değer korunur."""
    earlier = {change.name: change for change in old}
    merged = {
        change.name: EnvChange(change.name, change.value, earlier[change.name].previous)
        if change.name in earlier and earlier[change.name].value == change.previous
        else change
        for change in new
    }
    return tuple(earlier[name] for name in earlier if name not in merged) + tuple(merged.values())


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
    """Ayarları atomik olarak yazar; dosya varsa önce yedeğini alır ve yedeğin yolunu döndürür.

    Sembolik bağlantılı ayar dosyasında bağlantı korunur, hedef dosya güncellenir.
    """
    target = path.resolve()
    backup = backup_path(target, now) if target.exists() else None
    if backup is not None:
        shutil.copy2(target, backup)
    write_atomically(target, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return backup


def backup_path(target: Path, now: float) -> Path:
    """Var olan bir yedeğin üzerine yazmayan yedek yolu."""
    stem = f"{target.name}.cimrihook-backup-{int(now)}"
    candidates = (target.with_name(stem if n == 0 else f"{stem}-{n}") for n in range(1000))
    found = next((candidate for candidate in candidates if not candidate.exists()), None)
    if found is None:
        raise ConfigError(f"too many CimriHook backups named {stem}* next to {target}")
    return found


def write_atomically(target: Path, text: str) -> None:
    """Metni geçici dosyaya yazıp yerine taşır; yarıda kalan yazma eski dosyayı bozmaz."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.cimrihook-tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def save_record(home: Path, path: Path, record: InstallRecord) -> None:
    """Kaldırmada geri almak için kurulum kaydını yazar."""
    data = {
        "settings": str(path),
        "env": [{"name": c.name, "value": c.value, "previous": c.previous} for c in record.env],
        "status_line": record.status_line,
    }
    write_atomically(home / INSTALL_RECORD, json.dumps(data, indent=2) + "\n")


def load_record(home: Path, path: Path) -> InstallRecord:
    """Bu ayar dosyası için kurulum kaydı; kayıt yoksa ya da başka dosyaya aitse boş kayıt."""
    file = home / INSTALL_RECORD
    if not file.exists():
        return EMPTY_RECORD
    data: object = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("settings") != str(path):
        return EMPTY_RECORD
    entries = data.get("env")
    if not isinstance(entries, list):
        raise ConfigError(f"{file}: 'env' must be a list")
    return InstallRecord(
        env=tuple(
            EnvChange(
                str(entry["name"]),
                str(entry["value"]),
                previous if isinstance(previous := entry.get("previous"), str) else None,
            )
            for entry in entries
            if isinstance(entry, dict)
        ),
        status_line=data.get("status_line"),
    )


def run_init(path: Path, blocks: Sequence[Settings], home: Path, dry_run: bool, now: float) -> str:
    """Kurulumu uygular ya da (dry_run) yalnızca farkını gösterir; kullanıcıya rapor döndürür."""
    before = load_settings(path)
    result = install(before, blocks, load_record(home, path))
    lines = [settings_diff(path, before, result.settings), *(f"note: {n}" for n in result.notes)]
    if dry_run or result.settings == before:
        return "\n".join([*lines, "dry run: nothing written" if dry_run else "already installed"])
    backup = write_settings(path, result.settings, now)
    save_record(home, path, result.record)
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
