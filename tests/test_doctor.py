"""Cost anatomy on a small hand-computed transcript (a pure transformation)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cimrihook.doctor import IDLE_HOUR, build_anatomy, diagnose_claude
from cimrihook.errors import ConfigError
from cimrihook.lifetime import recommended_lifetime, replay_lifetimes
from cimrihook.scan import recent_requests, scan_transcript, unique_scans

T0 = 1_791_000_000.0


def assistant(message_id: str, model: str, seconds: float, read: int, written: int) -> str:
    """An assistant line with a 1-hour cache write and 100 output tokens."""
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read,
        "output_tokens": 0 if model == "<synthetic>" else 100,
        "cache_creation": {"ephemeral_1h_input_tokens": written},
    }
    entry = {
        "type": "assistant",
        "timestamp": seconds_to_iso(T0 + seconds),
        "message": {"id": message_id, "model": model, "usage": usage, "content": []},
    }
    return json.dumps(entry)


def seconds_to_iso(epoch: float) -> str:
    """Converts epoch seconds to the ISO format used in the transcript."""
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def test_anatomy_prices_bands_and_explains_a_cold_rewrite(tmp_path: Path) -> None:
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"preTokens": 900_000},
    }
    lines = [
        assistant("m1", "claude-opus-5-5", 0, 0, 30_000),
        assistant("m2", "claude-opus-5-5", 10, 30_000, 2_000),
        assistant("m3", "<synthetic>", 11, 0, 0),
        json.dumps(boundary),
        assistant("m4", "claude-opus-5-5", 20, 10_000, 15_000),
        assistant("m5", "claude-opus-5-5", 7_200, 0, 150_000),
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    anatomy = build_anatomy([scan_transcript(path, False)], 7)
    # Opus 5.x: $4/MTok base; read 0.05, 1-hour write 2.0, output 5.
    # Base units: 60500 + 6000 + 31000 + 300500 = 398000 -> $1.592.
    assert anatomy.requests == 4
    assert anatomy.total_usd == pytest.approx(1.592)
    assert sum(share.usd for share in anatomy.bands) == pytest.approx(anatomy.total_usd)
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    assert idle.count == 1
    assert idle.usd == pytest.approx(1.2)
    assert (anatomy.compactions, anatomy.compaction_trigger) == (1, 900_000)
    assert (anatomy.after_compaction_context, anatomy.prefix_main) == (25_000, 30_000)


def test_a_forked_transcript_and_old_requests_are_not_counted_again(tmp_path: Path) -> None:
    original = tmp_path / "a.jsonl"
    original.write_text(
        "\n".join(
            [
                assistant("old", "claude-opus-5-5", -30 * 86_400, 0, 30_000),
                assistant("m1", "claude-opus-5-5", 0, 30_000, 2_000),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fork = tmp_path / "b.jsonl"  # fork: copies the history, then continues with its own request
    fork.write_text(
        original.read_text(encoding="utf-8")
        + assistant("m2", "claude-opus-5-5", 60, 32_000, 1_000)
        + "\n",
        encoding="utf-8",
    )
    scans = [scan_transcript(original, False), scan_transcript(fork, False)]
    recent = recent_requests(unique_scans(scans), T0 - 7 * 86_400)
    assert [r.message_id for scan in recent for r in scan.requests] == ["m1", "m2"]
    assert build_anatomy(recent, 7).requests == 2


def test_an_empty_log_directory_is_an_error_not_a_zero_report(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no Claude Code transcripts"):
        diagnose_claude(tmp_path, tmp_path / "settings.json", 7, T0)


def test_cache_lifetime_replay_prices_pauses_with_both_lifetimes(tmp_path: Path) -> None:
    # Main session, 1-hour cache: 100k context, then a 10-minute gap and 2k of new input.
    lines = [
        assistant("m1", "claude-opus-5-5", 0, 0, 100_000),
        assistant("m2", "claude-opus-5-5", 600, 100_100, 2_000),
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    main, _ = replay_lifetimes([scan_transcript(path, False)])
    # 1h: the first request 100k x 2 = 200,000; the second reads 100.1k (x0.05) and writes 2k (x2):
    # 209,005 base. 5m: the 10-minute gap cools the cache, the second request rewrites 102.1k at
    # x1.25: 125,000 + 127,625 = 252,625 base. Opus 5.x: $4/MTok.
    assert main.current == "1h"
    assert main.one_hour_usd == pytest.approx(209_005 * 4e-6)
    assert main.five_minutes_usd == pytest.approx(252_625 * 4e-6)
    assert main.replay_error() == pytest.approx(0.0)
    assert recommended_lifetime(main) is None
