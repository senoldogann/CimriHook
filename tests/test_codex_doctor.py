import json
from pathlib import Path

from cimrihook.codex_doctor import diagnose_codex, read_rollout, render_codex_doctor

START = 1_790_000_000.0
# (uncached input, output) per request; at 1e-5 and 2e-4 points per token each moves 1 or 2 points
PATTERN = ((100_000, 0), (0, 5_000), (100_000, 5_000), (200_000, 0), (0, 10_000))


def token_count(second: int, uncached: int, output: int, total: int, used: float) -> str:
    """A token_count event as Codex writes it, with both subscription windows."""
    window = {"used_percent": used, "resets_at": 1_790_100_000}
    clock = f"{second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}"
    return json.dumps(
        {
            "timestamp": f"2026-09-22T{clock}Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": uncached + 1_000,
                        "cached_input_tokens": 1_000,
                        "output_tokens": output,
                        "reasoning_output_tokens": output // 2,
                    },
                    "total_token_usage": {"total_tokens": total},
                    "model_context_window": 258_400,
                },
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {**window, "window_minutes": 300},
                    "secondary": {**window, "used_percent": used / 10, "window_minutes": 10_080},
                },
            },
        }
    )


def write_rollout(path: Path) -> None:
    """Four rounds of the pattern, a repeated event and a compaction."""
    lines = [json.dumps({"type": "session_meta", "payload": {"cwd": "/work"}})]
    used, total = 0.0, 0
    for index, (uncached, output) in enumerate(PATTERN * 4):
        used += uncached * 1e-5 + output * 2e-4
        total += uncached + 1_000 + output
        lines.append(token_count(60 * index, uncached, output, total, used))
    lines.append(lines[-1])  # the same cumulative total again: no new request
    lines.append(json.dumps({"type": "compacted", "payload": {}}))
    path.write_text("\n".join(lines) + "\n")


def test_rollout_requests_and_window_cost(tmp_path: Path) -> None:
    rollout_path = tmp_path / "2026" / "rollout-a.jsonl"
    rollout_path.parent.mkdir()
    write_rollout(rollout_path)

    rollout = read_rollout(rollout_path)
    assert len(rollout.requests) == 20
    assert rollout.requests[1].output == 5_000 and rollout.requests[1].cached == 1_000
    assert rollout.compaction_triggers == (1_000,)  # context of the last request: cached only
    assert rollout.context_window == 258_400

    diagnosis = diagnose_codex(tmp_path, tmp_path / "missing.toml", 7, rollout_path.stat().st_mtime)
    assert diagnosis.requests == 20 and diagnosis.compactions == 1
    five_hour = diagnosis.windows[0]
    assert five_hour.input_weight is not None and five_hour.output_weight is not None
    assert 10 < five_hour.output_weight.weight / five_hour.input_weight.weight < 40
    report = render_codex_doctor(diagnosis)
    assert "5-hour window: 1 point is about" in report
    assert "no model_auto_compact_token_limit" in report
