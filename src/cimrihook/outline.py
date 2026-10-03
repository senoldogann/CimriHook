"""Büyük dosyalar için dil duyarlı, bağımlılıksız yapısal iskelet çıkarımı."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Final

MAX_ENTRY_CHARS: Final = 120
MAX_INDENT_CHARS: Final = 16
TAB_WIDTH: Final = 4


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """İskeletteki tek bildirim: dosyadaki satır numarası ve kısaltılmış kod satırı."""

    line: int
    text: str


_JVM_MODIFIERS: Final = (
    r"(?:(?:public|private|protected|internal|static|final|abstract|sealed|open|override|data|"
    r"inline|suspend|async|virtual|partial|readonly|extern|unsafe|fileprivate|mutating|required|"
    r"convenience|synchronized)\s+)"
)

PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "python": re.compile(r"^\s*(?:async\s+def|def|class)\s+\w+"),
    "javascript": re.compile(
        r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
        r"(?:function\*?|class|interface|enum|type|namespace)\s+[\w$]+"
        r"|^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*(?:async\s+)?"
        r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>)"
        r"|^\s+(?:(?:public|private|protected|static|readonly|async|override|abstract|get|set)\s+)*"
        r"(?!(?:if|for|while|switch|catch|return|function|else|do|try|with|new|await|typeof)\b)"
        r"[\w$]+\s*(?:<[^>]*>)?\([^)]*\)\s*(?::\s*[^{;]+)?\{\s*$"
    ),
    "go": re.compile(r"^func\s|^type\s+\w+\s+(?:struct|interface)\b"),
    "rust": re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern\s+\"[^\"]*\")\s+)*"
        r"(?:fn|struct|enum|trait|impl|mod|type|union|macro_rules!)\s*[\w<]"
    ),
    "jvm": re.compile(
        r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*" + _JVM_MODIFIERS + r"*"
        r"(?:class|interface|enum|struct|record|object|protocol|extension|trait|fun|func|def|"
        r"namespace)\s+[\w.<>`]+"
        r"|^\s+" + _JVM_MODIFIERS + r"+[\w<>\[\],.?]+(?:\s+[\w<>\[\],.?]+)*\s+\w+\s*\([^;]*$"
    ),
    "c": re.compile(
        r"^(?:class|struct|namespace|enum|union)\s+\w+"
        r"|^(?!(?:if|for|while|switch|return|else|do|case|goto|sizeof)\b)"
        r"[A-Za-z_][\w\s*&:<>,~]*?[\s*&]+~?[A-Za-z_][\w:~]*\s*\([^;]*$"
    ),
    "ruby": re.compile(r"^\s*(?:def|class|module)\s+\S"),
    "php": re.compile(
        r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*"
        r"(?:function|class|interface|trait|enum)\s+\w+"
    ),
    "shell": re.compile(r"^\s*(?:function\s+[\w:-]+|[\w:-]+\s*\(\)\s*\{)"),
    "sql": re.compile(
        r"^\s*create\s+(?:or\s+replace\s+)?(?:table|view|materialized\s+view|function|procedure|"
        r"index|unique\s+index|trigger|type|schema)\b",
        re.IGNORECASE,
    ),
    "yaml": re.compile(r"^[A-Za-z_][\w.-]*\s*:"),
    "toml": re.compile(r"^\s*\[\[?[^\]]+\]\]?"),
}

EXTENSIONS: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".mts": "javascript",
    ".cts": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "jvm",
    ".kt": "jvm",
    ".kts": "jvm",
    ".cs": "jvm",
    ".scala": "jvm",
    ".swift": "jvm",
    ".dart": "jvm",
    ".groovy": "jvm",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".hpp": "c",
    ".hh": "c",
    ".hxx": "c",
    ".m": "c",
    ".mm": "c",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".mdx": "markdown",
    ".markdown": "markdown",
}

_FENCE: Final = re.compile(r"^\s*(?:```|~~~)")
_HEADING: Final = re.compile(r"^#{1,6}\s+\S")


def language_of(path: str) -> str | None:
    """Dosya uzantısından iskelet dilini bulur; desteklenmiyorsa None."""
    return EXTENSIONS.get(PurePath(path).suffix.lower())


def extract_outline(
    path: str, lines: Sequence[str], start_line: int
) -> tuple[OutlineEntry, ...] | None:
    """Desteklenen dillerde satır numaralı bildirim listesi döndürür; desteklenmeyen dilde None."""
    language = language_of(path)
    if language is None:
        return None
    if language == "markdown":
        return markdown_outline(lines, start_line)
    pattern = PATTERNS[language]
    return tuple(
        outline_entry(start_line + index, line)
        for index, line in enumerate(lines)
        if pattern.match(line)
    )


def markdown_outline(lines: Sequence[str], start_line: int) -> tuple[OutlineEntry, ...]:
    """Kod blokları dışındaki Markdown başlıklarını toplar."""
    entries: list[OutlineEntry] = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and _HEADING.match(line):
            entries.append(outline_entry(start_line + index, line))
    return tuple(entries)


def outline_entry(line_no: int, line: str) -> OutlineEntry:
    """Girintiyi (sınırlı) koruyarak satırı kısaltır."""
    expanded = line.expandtabs(TAB_WIDTH).rstrip()
    body = expanded.lstrip()
    indent = min(len(expanded) - len(body), MAX_INDENT_CHARS)
    return OutlineEntry(line_no, (" " * indent + body)[:MAX_ENTRY_CHARS])


def limit_outline(
    entries: Sequence[OutlineEntry], max_entries: int
) -> tuple[tuple[OutlineEntry, ...], int]:
    """Çok uzun iskelette yalnızca en dış seviyeyi tutar; atılan girdi sayısını da döndürür."""
    if len(entries) <= max_entries:
        return tuple(entries), 0
    top = tuple(entry for entry in entries if not entry.text.startswith(" "))[:max_entries]
    return top, len(entries) - len(top)
