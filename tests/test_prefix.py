"""The prefix breakdown records are a pure transformation; tested on hand-written files."""

import json
from pathlib import Path

import pytest

from cimrihook.errors import ConfigError
from cimrihook.prefix import PrefixPart, prefix_parts, prefix_text, read_records


def record(path: Path, t: int, tools: int, messages: int) -> None:
    rows = [
        {"name": "System tools", "tokens": tools, "kind": "used"},
        {"name": "Messages", "tokens": messages, "kind": "used"},
        {"name": "Free space", "tokens": 900_000, "kind": "free"},
    ]
    path.write_text(json.dumps({"t": t, "rows": rows}), encoding="utf-8")


def test_parts_are_the_median_of_the_used_rows_but_the_messages(tmp_path: Path) -> None:
    record(tmp_path / "a.json", 1_000, 12_000, 50_000)
    record(tmp_path / "b.json", 2_000, 14_000, 70_000)
    record(tmp_path / "old.json", 0, 90_000, 1)  # before the window
    parts = prefix_parts(read_records(tmp_path, 0.5))
    assert parts == [PrefixPart("System tools", 13_000, 2)]
    assert prefix_text(parts) == "System tools 13.0k (2 sessions)"
    # A large MCP load in one record is not reported as if both sessions had it.
    single = {"t": 3_000, "rows": [{"name": "MCP tools", "tokens": 29_469, "kind": "used"}]}
    (tmp_path / "rare.json").write_text(json.dumps(single), encoding="utf-8")
    parts = prefix_parts(read_records(tmp_path, 0.5))
    assert parts == [PrefixPart("MCP tools", 29_469, 1), PrefixPart("System tools", 13_000, 2)]
    assert prefix_text(parts).startswith("MCP tools 29.5k (1 session)")
    assert read_records(tmp_path / "missing", 0) == []


def test_a_malformed_record_names_its_file(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text('{"t": 1, "rows": [{"name": "x"}]}', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"bad\.json"):
        read_records(tmp_path, 0)
