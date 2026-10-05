"""Claude Code transcript files: which ones to read and what a line says about token use."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cimrihook.claude import JsonObject
from cimrihook.errors import ConfigError

CHARS_PER_TOKEN: Final = 4
SECONDS_PER_DAY: Final = 86_400
WORKFLOW_JOURNAL: Final = "journal.jsonl"
# Workspaces of CimriHook's own runs: the A/B harness (.../cimrihook-bench/...) and the pilots
# under ~/.cimrihook/experiments/. They are not the user's work and would distort the cost
# anatomy, the gain and the window recommendation.
BENCH_LOCATION_MARKERS: Final = ("cimrihook-bench", "cimrihook-experiments")
# Multipliers on the base input price (Anthropic prompt caching pricing).
WRITE_5M_WEIGHT: Final = 1.25
WRITE_1H_WEIGHT: Final = 2.0


@dataclass(frozen=True, slots=True)
class Usage:
    """Input-side token use of one API request."""

    uncached: int
    write_5m: int
    write_1h: int
    read: int
    output: int


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token); measurements use the real API usage."""
    return -(-len(text) // CHARS_PER_TOKEN)


def recent_transcripts(projects_dir: Path, days: int, now: float) -> tuple[Path, ...]:
    """Transcripts modified in the last `days` days, oldest first, without CimriHook's own A/B
    runs. An error if there are none: an empty report would read as "no spend"."""
    files = [
        path
        for path in transcript_files(projects_dir, now - days * SECONDS_PER_DAY)
        if not is_bench_transcript(path, projects_dir)
    ]
    if not files:
        raise ConfigError(
            f"no Claude Code transcripts under {projects_dir} modified in the last {days} days"
        )
    return tuple(sorted(files, key=lambda path: path.stat().st_mtime))


def bench_transcripts(projects_dir: Path, days: int, now: float) -> int:
    """Number of transcripts of CimriHook's own A/B runs modified in the last `days` days."""
    return sum(
        1
        for path in transcript_files(projects_dir, now - days * SECONDS_PER_DAY)
        if is_bench_transcript(path, projects_dir)
    )


def is_bench_transcript(path: Path, projects_dir: Path) -> bool:
    """Does the transcript belong to a workspace of CimriHook's own runs?"""
    return any(is_bench_location(part) for part in path.relative_to(projects_dir).parts)


def is_bench_location(location: str) -> bool:
    """Is a working directory, or Claude Code's project directory name for one, a workspace of
    CimriHook's own runs? Claude Code names a project by its path with `/` and `.` as `-`, so the
    path is compared in that form."""
    encoded = location.replace("/", "-").replace(".", "-")
    return any(marker in encoded for marker in BENCH_LOCATION_MARKERS)


def entry_time(entry: JsonObject) -> float | None:
    """Time of a transcript line (epoch seconds); None if the line has no time."""
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def transcript_files(projects_dir: Path, min_mtime: float) -> tuple[Path, ...]:
    """Main and subagent transcripts (workflow journals excluded)."""
    return tuple(
        sorted(
            path
            for path in projects_dir.rglob("*.jsonl")
            if path.name != WORKFLOW_JOURNAL and path.stat().st_mtime >= min_mtime
        )
    )


def parse_line(raw_line: bytes) -> JsonObject | None:
    """Decodes a transcript line; None for half-written or corrupt lines."""
    try:
        decoded: object = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(key): value for key, value in decoded.items()}


def message_usage(message: dict[object, object]) -> Usage | None:
    """Input-side usage of a message; None if it has no token fields."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    uncached = usage.get("input_tokens")
    written = usage.get("cache_creation_input_tokens")
    read = usage.get("cache_read_input_tokens")
    output = usage.get("output_tokens")
    if not (
        isinstance(uncached, int)
        and isinstance(written, int)
        and isinstance(read, int)
        and isinstance(output, int)
    ):
        return None
    split = usage.get("cache_creation")
    write_1h = split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else None
    hour = write_1h if isinstance(write_1h, int) else 0  # without the split, writes are 5-minute
    return Usage(
        uncached=uncached, write_5m=written - hour, write_1h=hour, read=read, output=output
    )


def average_write_weight(usage: Usage) -> float:
    """Average price multiplier of the observed mix of 5-minute and 1-hour cache writes."""
    written = usage.write_5m + usage.write_1h
    if written == 0:
        return WRITE_5M_WEIGHT
    return (usage.write_5m * WRITE_5M_WEIGHT + usage.write_1h * WRITE_1H_WEIGHT) / written
