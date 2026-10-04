"""Real Git/files/process checks for literal preparation and native-log accounting."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cimrihook.bench import (
    Agent,
    Protocol,
    RunSpec,
    Variant,
    claude_failure,
    claude_reported_usage,
    load_task,
    prepared_prompt,
    spec_problem,
)
from cimrihook.errors import PreparationError
from cimrihook.preparation import (
    build_packet,
    packet_json,
    parse_target,
    render_packet,
    validate_packet,
)
from cimrihook.preparation_report import (
    NativeLog,
    ToolCall,
    claude_log,
    codex_log,
    summarize_logs,
)


def repository(root: Path) -> None:
    root.mkdir()
    (root / "calc.py").write_bytes(
        b"HEADER = 1\r\n\r\ndef price(value):\r\n    return value + HEADER\r\n"
    )
    (root / "failure.txt").write_text("Observed assertion failure; not a passing test.\n")
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )


def test_packet_preserves_request_literal_bytes_scope_and_rejects_stale_inputs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    repository(root)
    request = "Fiyatı düzelt.\nKullanıcının kısıtlarını koru.\n"
    packet = build_packet(root, request, (parse_target("calc.py::price"),), ("failure.txt",))
    source = packet.sources[0]
    assert (source.start, source.end, source.complete) == (3, 4, False)
    assert source.text == "def price(value):\r\n    return value + HEADER\r\n"
    assert render_packet(packet, 48_000).startswith(request)
    assert json.loads(packet_json(packet, 48_000))["request"] == request
    assert packet.evidence[0].text.startswith("Observed assertion")
    validate_packet(root, packet)
    with pytest.raises(PreparationError, match="budget"):
        render_packet(packet, 10)
    with pytest.raises(PreparationError, match="budget"):
        packet_json(packet, len(render_packet(packet, 48_000).encode("utf-8")))
    (root / "calc.py").write_text("HEADER = 1\ndef price(value):\n    return value\n")
    with pytest.raises(PreparationError, match=r"changed: calc\.py"):
        validate_packet(root, packet)


def test_prepare_cli_and_invalid_targets_use_real_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repository(root)
    request = tmp_path / "request.txt"
    request.write_text("Fix price.\n")
    command = (
        sys.executable,
        "-m",
        "cimrihook",
        "prepare",
        "--root",
        str(root),
        "--request-file",
        str(request),
        "--source",
        "calc.py:1:4",
        "--json",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sources"][0]["complete"] is True
    for raw in ("calc.py:2:99", "calc.py::missing", "../outside.py", "/etc/passwd"):
        with pytest.raises(PreparationError):
            build_packet(root, "Fix price.", (parse_target(raw),), ())
    (root / "escape.py").symlink_to(request)
    with pytest.raises(PreparationError, match="escapes"):
        build_packet(root, "Fix price.", (parse_target("escape.py"),), ())
    (root / "binary.py").write_bytes(b"\xff")
    with pytest.raises(PreparationError, match="UTF-8"):
        build_packet(root, "Fix price.", (parse_target("binary.py"),), ())
    result = subprocess.run(
        (*command, "--max-bytes", "1"), capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 1 and "budget" in result.stderr


def save_lines(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_claude_streams_copies_parallel_batches_and_failed_results(tmp_path: Path) -> None:
    def call(identity: str, name: str) -> dict[str, object]:
        return {
            "type": "tool_use",
            "id": identity,
            "name": name,
            "input": {"file_path": "private-file.py"},
        }

    def result(identity: str, failed: bool) -> dict[str, object]:
        return {
            "type": "tool_result",
            "tool_use_id": identity,
            "content": "private-content",
            "is_error": failed,
        }

    first: dict[str, object] = {
        "type": "assistant",
        "message": {"id": "batch-1", "content": [call("r1", "Read"), call("r2", "Read")]},
    }
    rows: list[dict[str, object]] = [
        {"type": "user", "uuid": "prompt", "message": {"content": "private-request"}},
        first,
        first,
        {"type": "user", "message": {"content": [result("r1", False), result("r2", False)]}},
        {"type": "assistant", "message": {"id": "batch-2", "content": [call("r3", "Read")]}},
        {"type": "user", "message": {"content": [result("r3", True)]}},
        {"type": "assistant", "message": {"id": "batch-3", "content": [call("e1", "Edit")]}},
        {"type": "assistant", "message": {"id": "batch-4", "content": [call("r4", "Read")]}},
    ]
    path = tmp_path / "claude.jsonl"
    save_lines(path, rows)
    log = claude_log(path)
    report = summarize_logs("claude", 7, (log, log))
    assert report.calls == 5
    assert report.research_calls == 4 and report.edit_calls == 1
    assert report.research_calls_before_first_edit == 3
    assert report.claude_research_only_batches_before_edit == 2
    assert report.identical_successful_research_observations == 1
    assert report.calls_without_results == 2
    assert report.duplicate_call_records == 9
    assert "private-content" not in str(report) and "private-file" not in str(report)


def test_codex_calls_are_not_model_requests_and_unknown_wrappers_stay_visible(
    tmp_path: Path,
) -> None:
    rows: list[dict[str, object]] = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "rg --files"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Process exited with code 0\nprivate-file.py",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c2",
                "input": "arbitrary program text",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c2",
                "output": "private output without status",
            },
        },
    ]
    path = tmp_path / "rollout-test.jsonl"
    save_lines(path, rows)
    with path.open("a") as handle:
        handle.write("{broken\n")
    report = summarize_logs("codex", 7, (codex_log(path),))
    assert report.calls == 2 and report.research_calls == 1 and report.other_calls == 1
    assert report.claude_research_only_batches_before_edit is None
    assert report.results_without_success_status == 1 and report.malformed_lines == 1


def test_pilot_shares_scope_and_failure_without_using_mutation_location(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repository(root)
    task = load_task(Path(__file__).resolve().parents[1] / "bench/tasks/preparation-smoke.json")
    targets = (("calc.py::price",),)
    task = replace(task, preparation_targets=targets)
    control = RunSpec(
        task,
        Protocol.SINGLE,
        Agent.CLAUDE,
        Variant.TARGETED_GOVERNOR,
        "same-model",
        "medium",
        183_000,
        1,
    )
    prepared = replace(control, variant=Variant.PREPARED_GOVERNOR)
    request, failure = (
        "Original request and constraint.",
        "Observed failure, identical in both arms.",
    )
    base = prepared_prompt(control, request, root, tmp_path, 1, targets[0], failure)
    enhanced = prepared_prompt(prepared, request, root, tmp_path, 2, targets[0], failure)
    assert enhanced.startswith(base)
    assert "SOURCE calc.py" not in base and "SOURCE calc.py" in enhanced
    assert task.mutations[0].path not in enhanced
    assert (tmp_path / "preparation.2.prompt.txt").read_bytes().decode("utf-8") == enhanced
    assert spec_problem(prepared) is None
    assert spec_problem(replace(prepared, agent=Agent.CODEX)) is None
    assert spec_problem(replace(prepared, protocol=Protocol.DEEP)) is not None
    assert spec_problem(replace(prepared, task=replace(task, preparation_targets=()))) is not None


def test_copied_edits_keep_continuation_reads_after_first_edit() -> None:
    read = ToolCall("r1", "batch-1", "span", "research", "Read:hash")
    edit = ToolCall("e1", "batch-2", "span", "edit", "Edit:hash")
    continuation = ToolCall("r2", "batch-3", "span", "research", "Read:hash")
    report = summarize_logs(
        "claude",
        7,
        (
            NativeLog((read, edit), (), 0),
            NativeLog((read, edit, continuation), (), 0),
        ),
    )
    assert report.calls == 3 and report.research_calls == 2
    assert report.research_calls_before_first_edit == 1
    assert report.claude_research_only_batches_before_edit == 1


def test_partial_copies_share_native_spans_batches_and_union_categories() -> None:
    first = ToolCall("r1", "batch", "prompt-uuid", "research", "Read:hash")
    parallel = ToolCall("r2", "batch", "prompt-uuid", "research", "Read:hash")
    edit = ToolCall("e1", "batch", "prompt-uuid", "edit", "Edit:hash")
    partial = NativeLog((first,), (), 0)
    report = summarize_logs("claude", 7, (partial, NativeLog((first, parallel), (), 0)))
    assert report.calls == 2 and report.research_calls_before_first_edit == 2
    assert report.spans_with_research_before_edit == 1
    assert report.claude_research_only_batches_before_edit == 1
    mixed = summarize_logs("claude", 7, (partial, NativeLog((first, edit), (), 0)))
    assert mixed.claude_research_only_batches_before_edit == 0


def test_python_symbol_uses_physical_lines_and_budget_stop_keeps_provider_cost(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    repository(root)
    literal = 'def price(value):\n    return "first\u2028second"\n'
    (root / "calc.py").write_text(literal)
    packet = build_packet(root, "Fix price.", (parse_target("calc.py::price"),), ())
    assert packet.sources[0].text == literal
    rendered = render_packet(packet, 48_000)
    assert '2:     return "first\u2028second"' in rendered and "3:" not in rendered
    stdout = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "subtype": "error_max_budget_usd",
            "total_cost_usd": 3.01,
            "modelUsage": {
                "fixed": {
                    "inputTokens": 100,
                    "outputTokens": 200,
                    "cacheReadInputTokens": 300,
                    "cacheCreationInputTokens": 400,
                    "costUSD": 3.01,
                }
            },
        }
    )
    assert claude_failure(stdout, False) is None
    usage = claude_reported_usage(stdout)
    assert usage is not None and usage.cost_usd == 3.01
