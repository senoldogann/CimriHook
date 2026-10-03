"""Maliyet anatomisi elle hesaplanmış küçük bir transcript üzerinde (saf dönüşüm)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cimrihook.doctor import IDLE_HOUR, build_anatomy, scan_transcript

T0 = 1_791_000_000.0


def assistant(message_id: str, model: str, seconds: float, read: int, written: int) -> str:
    """1 saatlik önbellek yazımıyla, 100 çıktı tokenlı asistan satırı."""
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
    """Epoch saniyeyi transcript'teki ISO biçimine çevirir."""
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
    # Opus 5.x: $4/MTok taban; okuma 0.05, 1 saatlik yazım 2.0, çıktı 5.
    # Taban birimler: 60500 + 6000 + 31000 + 300500 = 398000 -> $1.592.
    assert anatomy.requests == 4
    assert anatomy.total_usd == pytest.approx(1.592)
    assert sum(share.usd for share in anatomy.bands) == pytest.approx(anatomy.total_usd)
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    assert idle.count == 1
    assert idle.usd == pytest.approx(1.2)
    assert (anatomy.compactions, anatomy.compaction_trigger) == (1, 900_000)
    assert (anatomy.after_compaction_context, anatomy.prefix_main) == (25_000, 30_000)
