"""Codex CLI yapılandırması: otomatik sıkıştırma eşiği (~/.codex/config.toml).

Codex hook'ları araç çıktısını değiştiremez ve OpenAI önbelleğinin ömrü belgelenmemiştir; Codex'te
CimriHook'un kaldıracı sıkıştırma eşiğidir: `model_auto_compact_token_limit` (Codex bunu modelin
penceresinin %90'ıyla sınırlar, toplam bağlama uygular). Python'un standart kütüphanesi TOML
yazmaz: dosya metin olarak en küçük değişiklikle düzenlenir (yalnızca bu anahtarın satırı) ve sonuç
tomllib ile yeniden okunup yalnızca bu anahtarın değiştiği doğrulanmadan yazılmaz. Önceki değer
ayar dosyası başına kurulum kaydında tutulur; kaldırma, değer hâlâ CimriHook'unkiyse onu geri koyar.
Fark çıktısı bağlam satırı içermez (yapılandırmada API anahtarları olabilir).
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
    """config.toml'da yapılacak değişiklik."""

    path: Path
    before: str
    after: str
    record: InstallRecord
    notes: tuple[str, ...]


def plan_codex_window(path: Path, window: int, home: Path) -> CodexPlan:
    """Eşiği ayarlama planı; önceki CimriHook değeri varsa kullanıcının asıl değeri korunur."""
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
    """CimriHook'un yazdığı eşiği, değer hâlâ onunki ise önceki değerine döndürme planı."""
    before = read_config(path)
    current = top_level_integer(parse_toml(before, path), path)
    ours = next((c for c in load_record(home, path).settings if c.key == KEY), None)
    after = (
        before
        if ours is None or ours.value != current
        else checked_edit(before, ours.previous, path)
    )
    return CodexPlan(path, before, after, EMPTY_RECORD, ())


def read_config(path: Path) -> str:
    """config.toml'un metni; dosya yoksa boş."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def parse_toml(text: str, path: Path) -> TomlTable:
    """TOML metnini okur; bozuk dosyaya dokunulmaz."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error


def top_level_integer(table: TomlTable, path: Path) -> int | None:
    """Eşiğin dosyadaki değeri; yoksa None, tam sayı değilse hata."""
    value = table.get(KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: {KEY} is {value!r}; set it by hand or remove it first")
    return value


def checked_edit(text: str, value: int | None, path: Path) -> str:
    """Eşiği ayarlar (None: siler) ve sonucun yalnızca bu anahtarda değiştiğini doğrular."""
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
    """Üst düzey anahtarın satırını ilk tablo başlığından önce yazar, değiştirir ya da siler."""
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
        insert_at -= 1  # anahtar, tablodan önceki boş satırların üstüne gelir
    head = lines[:insert_at]
    if head and not head[-1].endswith("\n"):
        head = [*head[:-1], head[-1] + "\n"]
    return "".join([*head, *new_line, *lines[insert_at:]])


def render_codex_plan(plan: CodexPlan) -> str:
    """Değişen satırlar (bağlam satırı olmadan) ve notlar."""
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
    """Eşik planını uygular: önce kayıt, sonra yedek ve izinleri koruyan atomik yazım."""
    if plan.after == plan.before:
        return f"{render_codex_plan(plan)}\nalready set"
    save_record(home, plan.path, replace(plan.record, installed_at=now))
    backup = write_config(plan.path, plan.after, now)
    saved = f"backup: {backup}" if backup is not None else "created a new config file"
    return f"{render_codex_plan(plan)}\nwritten: {plan.path}\n{saved}"


def apply_codex_remove(plan: CodexPlan, home: Path, now: float) -> str:
    """Kaldırma planını uygular; kayıt yazımdan sonra temizlenir."""
    if plan.after == plan.before:
        return f"{render_codex_plan(plan)}\nnothing to remove"
    backup = write_config(plan.path, plan.after, now)
    save_record(home, plan.path, plan.record)
    return f"{render_codex_plan(plan)}\nwritten: {plan.path}\nbackup: {backup}"


def write_config(path: Path, text: str, now: float) -> Path | None:
    """Metni yedekleyerek ve dosyanın izinlerini koruyarak atomik yazar."""
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
