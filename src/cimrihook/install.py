"""Kurulum: CimriHook bileşenlerini Claude Code ayar dosyasına ekler ya da çıkarır.

Kullanıcının ayarları yalnızca bu komutla değişir. Yazmadan önce dosyanın yedeği alınır, yazma
atomiktir ve dosyanın izinleri korunur; sembolik bağlantılı ayar dosyasında hedef dosya güncellenir.
Önce plan çıkarılır; kuru çalıştırma yalnızca planın farkını gösterir. Fark çıktısında her `env`
ve `headers` nesnesinin (ör. MCP sunucularınınki) değerleri gizlenir, CimriHook'un yönettikleri
hariç: API anahtarları terminale ve bir ajanın bağlamına girmesin.

Kurulum bildirimseldir: önce CimriHook'un önceki kurulumu geri alınır, sonra seçilen bileşenler
eklenir. Aynı kurulum ikinci kez bir şey değiştirmez; eski sürümün komutları yenileriyle değişir,
yanlarında kalmaz.

Kullanıcının kendi durum satırı komutu varsa ezilmez, zincirlenir: CimriHook önce o komutu aynı
girdiyle çalıştırır, kendi parçasını sona ekler. Önceki komut yalnızca ayar dosyasındaki zincirleme
komutun `--after` argümanında durur ve kaldırmada oradan geri yüklenir; kurulum kaydından
çalıştırılabilir bir komut okunmaz. Kayıt, ayar dosyası başına yalnızca CimriHook'un yazdığı ortam
değişkenlerinin ve üst düzey ayarların (sıkıştırma penceresi) önceki değerlerini tutar.
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

MARKER: Final = " -m cimrihook "  # CimriHook'un yazdığı komutları tanır
AFTER_FLAG: Final = "--after"  # zincirlenen önceki durum satırı komutunun argümanı
INSTALL_RECORD: Final = "installed.json"
RECORD_VERSION: Final = 2
# Farkta değeri görünen, CimriHook'un yönettiği ortam değişkenleri.
MANAGED_ENV: Final = frozenset({"CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_PLUGIN_DIRS"})
# Yol listesi değişkenleri: kullanıcının değeri korunur, CimriHook'un yolu sona eklenir.
PATH_LIST_ENV: Final = frozenset({"CLAUDE_CODE_PLUGIN_DIRS"})
WINDOW_ENV: Final = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"  # ayarlanmışsa autoCompactWindow'u ezer
# CimriHook'un yazdığı üst düzey ayarlar: sıkıştırma penceresi ve önbellek ömürleri.
MANAGED_KEYS: Final = ("autoCompactWindow", "promptCacheTtl", "subagentPromptCacheTtl")
CACHE_TTL_ENV: Final = (
    "FORCE_PROMPT_CACHING_5M",
    "ENABLE_PROMPT_CACHING_1H",
)  # ömür ayarlarını ezer
SECRET_CONTAINERS: Final = frozenset({"env", "headers"})  # değerleri farkta gizlenen nesneler
HIDDEN: Final = "<hidden>"
PRIVATE_FILE_MODE: Final = 0o600
PRIVATE_DIR_MODE: Final = 0o700

type Settings = dict[str, object]
type SettingValue = int | str


@dataclass(frozen=True, slots=True)
class EnvChange:
    """Kurulumun yazdığı ortam değişkeni ve CimriHook'tan önceki değeri."""

    name: str
    value: str
    previous: str | None


@dataclass(frozen=True, slots=True)
class SettingChange:
    """Kurulumun yazdığı üst düzey ayar (tam sayı ya da metin) ve CimriHook'tan önceki değeri."""

    key: str
    value: SettingValue
    previous: SettingValue | None


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """Kaldırmada geri alınacak ortam değişkenleri ve üst düzey ayarlar."""

    env: tuple[EnvChange, ...]
    settings: tuple[SettingChange, ...]
    installed_at: float | None  # ayarları değiştiren son kurulumun zamanı (cimrihook gain)


@dataclass(frozen=True, slots=True)
class Plan:
    """Ayar dosyasında yapılacak değişiklik."""

    path: Path
    before: Settings
    after: Settings
    record: InstallRecord  # değişiklikten sonraki kurulum kaydı
    notes: tuple[str, ...]


EMPTY_RECORD: Final = InstallRecord(env=(), settings=(), installed_at=None)


def install(
    settings: Settings, blocks: Sequence[Settings], earlier: InstallRecord
) -> tuple[Settings, InstallRecord, tuple[str, ...]]:
    """Önceki CimriHook kurulumunu geri alır ve seçilen blokları ekler; durum satırı zincirlenir.

    Anahtarların sırası korunur; ortam değişkenlerinin önceki değerleri temizlenmiş ayarlardan
    alınır, böylece yeniden kurulum kullanıcının asıl değerini kaybetmez.
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
    """Yol listesine yolu sona ekler; listede zaten olan aynı yol önce çıkarılır."""
    kept = [] if current is None else [p for p in current.split(os.pathsep) if p and p != path]
    return os.pathsep.join([*kept, path])


def setting_value(value: object, key: str) -> SettingValue:
    """Bloktaki ayar değeri: tam sayı ya da metin."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"setting {key!r} must be an integer or a string, got {value!r}")
    return value


def current_setting(settings: Settings, key: str) -> SettingValue | None:
    """Ayar dosyasındaki değer; yoksa None. Başka tipte bir değer el ile ayarlanmıştır: üzerine
    yazmak yerine hata."""
    value = settings.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"settings {key!r} is {value!r}; set it by hand or remove it first")
    return value


def uninstall(settings: Settings, record: InstallRecord) -> Settings:
    """CimriHook'un eklediği hook'ları, ortam değişkenlerini ve durum satırını çıkarır.

    Ortam değişkeni yalnızca hâlâ CimriHook'un yazdığı değerdeyse önceki değerine döner; kullanıcı
    sonradan değiştirdiyse dokunulmaz.
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
    """CimriHook'un durum satırı yerine zincirlediği önceki durum satırı; zincir yoksa None
    (anahtar silinir). CimriHook'un olmayan durum satırı olduğu gibi kalır."""
    if not is_ours(current) or not is_command(current):
        return current
    previous = after_argument(str(current["command"]))
    return None if previous is None else {**current, "command": previous}


def after_argument(command: str) -> str | None:
    """Zincirlenmiş CimriHook durum satırı komutundaki önceki komut; zincir yoksa None."""
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
    """CimriHook durum satırı, kullanıcının önceki komutunu önce çalıştıracak biçimde; önceki
    durum satırının diğer alanları (ör. padding, refreshInterval) korunur."""
    if not is_command(ours) or not is_command(previous):
        raise ConfigError(f"cannot chain status lines {ours!r} and {previous!r}")
    command = chained_statusline_command(str(ours["command"]), str(previous["command"]))
    return {**previous, **ours, "command": command}


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
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ConfigError(f"settings 'hooks.{event}' must be a list of hook groups")
    return {str(event): list(entries) for event, entries in hooks.items()}


def env_of(settings: Settings) -> dict[str, object]:
    """Ayarlardaki ortam değişkenleri (kopya)."""
    env = settings.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ConfigError(f"settings 'env' must be an object, got {type(env).__name__}")
    return {str(name): value for name, value in env.items()}


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


def masked(value: object) -> object:
    """Fark çıktısı için: her düzeydeki env ve headers nesnelerinin değerleri gizli."""
    if isinstance(value, dict):
        return {
            key: hidden_values(item) if key in SECRET_CONTAINERS else masked(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


def hidden_values(container: object) -> object:
    """Nesnenin CimriHook'un yönetmediği değerleri yerine işaret; nesne değilse olduğu gibi."""
    if not isinstance(container, dict):
        return container
    return {name: value if name in MANAGED_ENV else HIDDEN for name, value in container.items()}


def settings_diff(path: Path, before: Settings, after: Settings) -> str:
    """İki ayar durumunun birleşik farkı (gizli değerlerle)."""
    old = json.dumps(masked(before), indent=2, ensure_ascii=False).splitlines()
    new = json.dumps(masked(after), indent=2, ensure_ascii=False).splitlines()
    lines = difflib.unified_diff(old, new, str(path), f"{path} (after)", lineterm="")
    return "\n".join(lines) or f"{path}: no change"


def write_settings(path: Path, settings: Settings, now: float) -> Path | None:
    """Ayarları atomik olarak yazar; dosya varsa önce yedeğini alır ve yedeğin yolunu döndürür.

    Sembolik bağlantılı ayar dosyasında bağlantı korunur, hedef dosya güncellenir. Dosyanın
    izinleri korunur; yeni dosya yalnızca kullanıcıya açıktır.
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
    """Var olan bir yedeğin üzerine yazmayan yedek yolu."""
    stem = f"{target.name}.cimrihook-backup-{int(now)}"
    candidates = (target.with_name(stem if n == 0 else f"{stem}-{n}") for n in range(1000))
    found = next((candidate for candidate in candidates if not candidate.exists()), None)
    if found is None:
        raise ConfigError(f"too many CimriHook backups named {stem}* next to {target}")
    return found


def write_atomically(target: Path, text: str, mode: int) -> None:
    """Metni aynı dizinde benzersiz bir geçici dosyaya yazar, diske işler ve yerine taşır.

    Yarıda kalan yazma eski dosyayı bozmaz; dosya umask'tan bağımsız olarak verilen izinlerle
    oluşur (geçici dosya baştan yalnızca kullanıcıya açıktır).
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
        temporary.unlink(missing_ok=True)  # yalnızca taşıma olmadıysa vardır


def record_key(path: Path) -> str:
    """Ayar dosyasının kayıttaki anahtarı: sembolik bağlantıları çözülmüş mutlak yol."""
    return str(path.expanduser().resolve())


def load_records(home: Path) -> dict[str, InstallRecord]:
    """Ayar dosyası başına kurulum kayıtları; kayıt dosyası yoksa boş."""
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
    if isinstance(legacy, str):  # 1. sürüm: tek ayar dosyası; durum satırı alanı artık okunmaz
        return {record_key(Path(legacy)): parse_record(data, file)}
    raise ConfigError(f"{file}: unknown install record format")


def parse_record(value: object, file: Path) -> InstallRecord:
    """Tek ayar dosyasının kaydı; bozuk girdi hatadır. 'settings' listesi olmayan kayıt, üst düzey
    ayar yazmayan eski sürümlerdendir."""
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
    """Kayıttaki üst düzey ayar değişikliği."""
    key = entry.get("key") if isinstance(entry, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    previous = entry.get("previous") if isinstance(entry, dict) else None
    if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"{file}: malformed setting entry {entry!r}")
    if previous is not None and (isinstance(previous, bool) or not isinstance(previous, int | str)):
        raise ConfigError(f"{file}: malformed previous value in {entry!r}")
    return SettingChange(key, value, previous)


def load_record(home: Path, path: Path) -> InstallRecord:
    """Bu ayar dosyası için kurulum kaydı; kayıt yoksa boş kayıt."""
    return load_records(home).get(record_key(path), EMPTY_RECORD)


def save_record(home: Path, path: Path, record: InstallRecord) -> None:
    """Bu ayar dosyasının kaydını, diğer dosyaların kayıtlarını koruyarak yazar."""
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
    """Kurulumun planı: önceki kurulumu geri alıp seçilen blokları ekleyen değişiklik."""
    before = load_settings(path)
    after, record, notes = install(before, blocks, load_record(home, path))
    return Plan(path, before, after, record, notes)


def plan_remove(path: Path, home: Path) -> Plan:
    """Kaldırmanın planı: CimriHook'un eklediklerini geri alan değişiklik."""
    before = load_settings(path)
    after = uninstall(before, load_record(home, path))
    return Plan(path, before, after, EMPTY_RECORD, ())


def render_plan(plan: Plan) -> str:
    """Planın farkı ve notları."""
    return "\n".join(
        [settings_diff(plan.path, plan.before, plan.after), *(f"note: {n}" for n in plan.notes)]
    )


def apply_init(plan: Plan, home: Path, now: float) -> str:
    """Kurulum planını uygular. Kayıt önce yazılır: ayar yazımı yarıda kalırsa kaldırma yine
    önceki değerleri bilir."""
    if plan.after == plan.before:
        return f"{render_plan(plan)}\nalready installed"
    save_record(home, plan.path, replace(plan.record, installed_at=now))
    backup = write_settings(plan.path, plan.after, now)
    saved = f"backup: {backup}" if backup is not None else "created a new settings file"
    return f"{render_plan(plan)}\nwritten: {plan.path}\n{saved}"


def apply_remove(plan: Plan, home: Path, now: float) -> str:
    """Kaldırma planını uygular. Kayıt ayarlar yazıldıktan sonra temizlenir: yazım yarıda kalırsa
    kaldırma yeniden denenebilir."""
    if plan.after == plan.before:
        return f"{render_plan(plan)}\nnothing to remove"
    backup = write_settings(plan.path, plan.after, now)
    save_record(home, plan.path, plan.record)
    return f"{render_plan(plan)}\nwritten: {plan.path}\nbackup: {backup}"
