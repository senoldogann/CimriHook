"""Codex rollout kayıtlarının ayrıştırılması ve çalıştırma matrisinin doğrulanması."""

import json
from pathlib import Path

import pytest

from cimrihook.bench import (
    Agent,
    Protocol,
    RunSpec,
    Task,
    Variant,
    claude_settings,
    codex_provider,
    load_codex_records,
    spec_problem,
)
from cimrihook.errors import BenchError

type Line = dict[str, object]


def usage_record(response_id: str, input_tokens: int, cached: int, output: int) -> Line:
    """Codex 0.160 token_usage_record satırı."""
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output,
    }
    return {"type": "token_usage_record", "payload": {"response_id": response_id, "usage": usage}}


def task_started() -> Line:
    """Yeni bir görevin (adımın) başlangıcı."""
    return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}}


def write_rollout(path: Path, lines: list[Line]) -> Path:
    """Satırları JSONL rollout dosyası olarak yazar."""
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_compaction_request_is_part_of_the_provider_cost(tmp_path: Path) -> None:
    rollout = write_rollout(
        tmp_path / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread", "cli_version": "0.160.0"}},
            task_started(),
            usage_record("r1", 1000, 0, 10),
            usage_record("r2", 1200, 1000, 20),
            usage_record("compaction", 1300, 1200, 100),
            {"type": "compacted", "payload": {"compaction_response_id": "compaction"}},
            task_started(),
            usage_record("r3", 500, 0, 5),
        ],
    )
    records = load_codex_records(rollout)
    provider = codex_provider(records, 2)
    assert records.cli_version == "0.160.0"
    assert [len(step) for step in records.steps] == [3, 1]
    # Adım 1: 1060 + 420 + sıkıştırma isteği 820; adım 2: 530 (önbelleksiz 1, okuma 0.1, çıktı 6).
    assert provider.cost_by_step == pytest.approx((2300.0, 2830.0))
    assert provider.cache_read == 2200


def test_step_without_model_requests_is_a_measurement_error(tmp_path: Path) -> None:
    rollout = write_rollout(
        tmp_path / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread", "cli_version": "0.160.0"}},
            task_started(),
            usage_record("r1", 1000, 0, 10),
            task_started(),
        ],
    )
    with pytest.raises(BenchError, match=r"no model requests in steps \[2\]"):
        codex_provider(load_codex_records(rollout), 2)


def spec(agent: Agent, protocol: Protocol, variant: Variant, window: int) -> RunSpec:
    """Doğrulama için asgari çalıştırma tanımı."""
    task = Task("t", "repo", "ref", (), (), 0, "prompt", ())
    return RunSpec(task, protocol, agent, variant, "model", "medium", window, 1)


def test_run_matrix_rejects_arms_the_agent_cannot_apply() -> None:
    sequential = Protocol.SEQUENTIAL
    assert spec_problem(spec(Agent.CODEX, sequential, Variant.CODEC, 60_000)) is not None
    assert spec_problem(spec(Agent.CODEX, sequential, Variant.BRIEF, 60_000)) is not None
    assert spec_problem(spec(Agent.CODEX, Protocol.DEEP, Variant.GOVERNOR, 60_000)) is not None
    assert spec_problem(spec(Agent.CLAUDE, sequential, Variant.GOVERNOR, 40_000)) is not None
    assert spec_problem(spec(Agent.CLAUDE, sequential, Variant.COMBINED, 100_000)) is None
    assert spec_problem(spec(Agent.CLAUDE, Protocol.DEEP, Variant.BRIEF, 183_000)) is None
    assert spec_problem(spec(Agent.CODEX, sequential, Variant.GOVERNOR, 60_000)) is None


def test_brief_arm_sets_the_window_and_the_compaction_brief_only() -> None:
    settings = claude_settings(spec(Agent.CLAUDE, Protocol.DEEP, Variant.BRIEF, 183_000))
    assert settings["env"] == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "183000"}
    hooks = settings["hooks"]
    assert isinstance(hooks, dict) and list(hooks) == ["PreCompact"]
    assert "-m cimrihook brief" in json.dumps(hooks["PreCompact"])
