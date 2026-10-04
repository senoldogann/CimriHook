"""Mod'un tamamlanan turda kaydettiği etkin bağlam kategorileri.

Deferred araçlar, boş alan, buffer ve konuşma prefix toplamına katılmaz. Kategorinin medyanı
yalnız bulunduğu kayıtlardan hesaplanır; kaç oturumda bulunduğu ayrıca gösterilir. Bu anlık
kayıtlar her isteğin sabit prefix'i veya kaldırılabilecek maliyetin ölçümü değildir.
"""

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.errors import ConfigError

PREFIX_DIR: Final = "prefix"
USED: Final = "used"  # the kind of a row that occupies the window
MESSAGES_ROW: Final = "Messages"  # the one used row that is the conversation, not the prefix


@dataclass(frozen=True, slots=True)
class PrefixRow:
    """One row of a session's breakdown."""

    name: str
    tokens: int
    kind: str  # used, free, buffer or deferred


@dataclass(frozen=True, slots=True)
class PrefixRecord:
    """The breakdown the mod recorded for one session."""

    session: str
    time: float  # epoch seconds
    rows: tuple[PrefixRow, ...]


@dataclass(frozen=True, slots=True)
class PrefixPart:
    """Bir kategorinin bulunduğu oturumlardaki medyanı ve kayıt sayısı."""

    name: str
    tokens: int
    sessions: int


def prefix_dir(home: Path) -> Path:
    """Directory of the mod's breakdown records."""
    return home / PREFIX_DIR


def read_records(directory: Path, since: float) -> list[PrefixRecord]:
    """The records made since a moment, oldest first; none if the directory does not exist."""
    if not directory.is_dir():
        return []
    records = [parse_record(path) for path in sorted(directory.glob("*.json"))]
    return sorted((r for r in records if r.time >= since), key=lambda record: record.time)


def parse_record(path: Path) -> PrefixRecord:
    """One record file; a malformed file is an error naming it."""
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: a prefix record must be an object")
    stamp, rows = raw.get("t"), raw.get("rows")
    if not isinstance(stamp, int | float) or not isinstance(rows, list):
        raise ConfigError(f"{path}: a prefix record needs a numeric t and a rows list")
    return PrefixRecord(
        session=path.stem,
        time=stamp / 1000,
        rows=tuple(parse_row(row, str(path)) for row in rows),
    )


def parse_row(raw: object, where: str) -> PrefixRow:
    """One row of a record; a malformed row is an error naming the file."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a breakdown row must be an object")
    name, tokens, kind = raw.get("name"), raw.get("tokens"), raw.get("kind")
    if not isinstance(name, str) or not isinstance(tokens, int) or not isinstance(kind, str):
        raise ConfigError(f"{where}: a breakdown row needs a name, integer tokens and a kind")
    return PrefixRow(name, tokens, kind)


def prefix_parts(records: list[PrefixRecord]) -> list[PrefixPart]:
    """The parts of the prefix, largest first: the median of each used row but the messages."""
    tokens: dict[str, list[int]] = defaultdict(list)
    for record in records:
        for row in record.rows:
            if row.kind == USED and row.name != MESSAGES_ROW:
                tokens[row.name].append(row.tokens)
    parts = [
        PrefixPart(name, int(statistics.median(values)), len(values))
        for name, values in tokens.items()
    ]
    return sorted(parts, key=lambda part: part.tokens, reverse=True)


def prefix_text(parts: list[PrefixPart]) -> str:
    """Seyrek kategori tüm oturumların yükü gibi görünmesin; kayıt sayısını yaz."""
    return ", ".join(
        f"{part.name} {part.tokens / 1000:.1f}k "
        f"({part.sessions} {'session' if part.sessions == 1 else 'sessions'})"
        for part in parts
    )
