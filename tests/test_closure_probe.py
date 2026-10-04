"""A cache loss on the first request must not be hidden by the warm requests after it."""

import json
from pathlib import Path

from cimrihook.closure_probe import (
    CONTRACT,
    OBSERVATION,
    cache_gate,
    contract_gate,
    full_observation,
)


def row(read: int, started: int = 1000) -> dict[str, object]:
    return {
        "index": 0,
        "model": "fixed",
        "effort": "medium",
        "started": started,
        "ended": started + 100,
        "usage": {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": read,
            "model": "fixed",
        },
    }


def test_cache_gate_requires_warm_conversation_and_first_request_coverage() -> None:
    phases = {"warmup": [row(0)], "fix": [row(10000)], "closed": [row(9600, 1200)]}
    assert cache_gate(phases)["passed"] is True
    phases["closed"] = [row(500, 1200), row(10000, 1400)]
    assert cache_gate(phases)["passed"] is False
    phases["closed"] = [row(10000, 302000)]
    assert cache_gate(phases)["passed"] is False


def test_insufficient_anchor_and_missing_measurements_are_negative() -> None:
    assert cache_gate({})["passed"] is False
    phases = {"warmup": [row(0)], "fix": [row(2000)], "closed": [row(2000, 1200)]}
    assert cache_gate(phases)["passed"] is False
    phases["fix"] = [row(10000)]
    phases["closed"] = [row(10000, 1200) | {"usage": None}]
    assert cache_gate(phases)["passed"] is False


def test_model_change_invalidates_cache_comparison() -> None:
    phases = {
        "warmup": [row(0)],
        "fix": [row(10000)],
        "closed": [row(10000, 1200) | {"model": "changed"}],
    }
    assert cache_gate(phases)["reason"] == "model or effort changed"


def test_missing_first_request_cannot_pass_with_later_cached_request() -> None:
    phases = {"warmup": [row(0)], "fix": [row(10000)], "closed": [row(10000, 1200) | {"index": 1}]}
    assert cache_gate(phases)["passed"] is False


def test_full_observation_rejects_sampling_and_requires_read_result(tmp_path: Path) -> None:
    (tmp_path / "disposable.txt").write_text(f"{OBSERVATION}\nlast-record abc123\n")
    transcript = tmp_path / "transcript.jsonl"
    args = {"file_path": str(tmp_path / "disposable.txt")}
    use = {"type": "tool_use", "name": "Read", "id": "read-1", "input": args}
    result = {"type": "tool_result", "tool_use_id": "read-1", "content": OBSERVATION}

    def save() -> None:
        transcript.write_text(json.dumps({"message": {"content": [use, result]}}) + "\n")

    save()
    assert full_observation(transcript, tmp_path) is False
    result["content"] = f"{OBSERVATION}\nabc123"
    save()
    assert full_observation(transcript, tmp_path) is True
    args["limit"] = "10"
    save()
    assert full_observation(transcript, tmp_path) is False


def test_correct_value_without_test_provenance_does_not_pass_contract() -> None:
    answer = {"contract": CONTRACT, "discount_100_25": 75, "tests_verified": False}
    answers = {phase: json.dumps(answer) for phase in ("closed", "resumed")}
    assert contract_gate(answers)["passed"] is False
    answer["tests_verified"] = True
    answers = {phase: json.dumps(answer) for phase in ("closed", "resumed")}
    assert contract_gate(answers)["passed"] is True
