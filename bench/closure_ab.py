"""Aynı 20 gerçek hatada governor ve doğrulanmış görev kapatmayı karşılaştır.

Ürün komutu değildir. Hazır benchmark görevini, workspace/test/process yardımcılarını ve
mevcut mod'u kullanır. Otomatik sınıflandırıcı veya özet modeli yoktur. Her kapatma öncesinde
testler dışarıdan geçmeli ve test dosyaları değişmemiş olmalıdır. Her iki kol aynı plugin,
model, effort, araçlar ve governor penceresiyle çalışır; yalnız kapatma kolu /compact gönderir.
"""

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from cimrihook.bench import (
    ENV_ALLOWLIST,
    FIRST_BUG_PROMPT,
    NEXT_BUG_PROMPT,
    Agent,
    Protocol,
    RunSpec,
    Variant,
    apply_mutation,
    claude_failure,
    claude_reported_usage,
    claude_transcript,
    claude_version,
    ensure_repo,
    load_task,
    prepare_workspace,
    run_process,
    run_suite,
    seal,
    touched_test_files,
)
from cimrihook.closure_probe import read_json, request_rows, write_json
from cimrihook.errors import BenchError, CimriHookError
from cimrihook.mods import write_mod
from cimrihook.quota import object_value, read_claude_quota

COMPACT = "/compact CimriHook externally verified closure pilot"
TOOLS = "Read,Edit,Bash,Glob,Grep"
PROOF_PROMPT = (
    " Use the Edit tool for library changes. Run exactly `.venv/bin/python -m pytest -q` "
    "to verify the whole suite. Keep your final answer brief."
)


def call(
    spec: RunSpec, output: Path, session: str, phase: str, prompt: str, index: int, timeout: int
) -> dict[str, object]:
    """İki kol için aynı provider çağrısı; ölçüm plugin'i mesajları kendiliğinden değiştirmez."""
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "CIMRIHOOK_HOME": str(output / "meter"),
            "CIMRIHOOK_MOD_CLOSE_PROBE": "1",
            "CIMRIHOOK_PROBE_PHASE": phase,
            "CIMRIHOOK_PROBE_WORKSPACE": str(output / "workspace"),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(spec.window),
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_AUTO_CONNECT_IDE": "0",
        }
    )
    command = (
        "claude",
        "-p",
        prompt,
        "--session-id" if index == 1 else "--resume",
        session,
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--output-format",
        "json",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        "{}",
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        "12",
        "--max-budget-usd",
        "12",
        "--tools",
        TOOLS,
        "--allowedTools",
        TOOLS,
        "--plugin-dir",
        str(output / "plugin"),
    )
    write_json(output / f"{phase}.prompt.json", {"text": prompt})
    process = run_process(command, output / "workspace", env, timeout, output, index)
    if process.timed_out:
        raise BenchError(f"{phase}: agent timed out after {timeout}s")
    failure = claude_failure(process.stdout, False)
    if failure is not None or process.exit_code != 0:
        raise BenchError(f"{phase}: agent failed: {failure or process.exit_code}")
    decoded: object = json.loads(process.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    results = [
        object_value(record, phase)
        for record in records
        if isinstance(record, dict) and record.get("type") == "result"
    ]
    if not results:
        raise BenchError(f"{phase}: no Claude result record")
    result = results[-1]
    if result.get("subtype") != "success":
        raise BenchError(f"{phase}: turn did not complete: {result}")
    usage = claude_reported_usage(process.stdout)
    if usage is None:
        raise BenchError(f"{phase}: cumulative provider usage is missing")
    if prompt != COMPACT and result.get("num_turns") == 0:
        raise BenchError(f"{phase}: no model request")
    return result


def native_evidence(completed: object, anchor: object, workspace: Path) -> list[str]:
    """Son görevde gerçek Edit ve geçen pytest çıktısını tut; önceki görev kanıtı seçilmez."""
    if not isinstance(completed, list) or not isinstance(anchor, list):
        raise BenchError("closure projections are not message lists")
    all_ids: list[str] = []
    edits: list[str] = []
    tests: list[str] = []
    for index, item in enumerate(completed):
        row = object_value(item, "completed message")
        tools = row.get("toolUses")
        if not isinstance(tools, list):
            raise BenchError("completed message has no toolUses list")
        for item_tool in tools:
            tool = object_value(item_tool, "completed tool")
            args = tool.get("input")
            identity = tool.get("tool_use_id")
            if isinstance(identity, str):
                all_ids.append(identity)
            if index < len(anchor):
                continue
            if not isinstance(args, dict) or not isinstance(identity, str) or tool.get("isError"):
                continue
            path = args.get("file_path")
            if (
                tool.get("tool") == "Edit"
                and isinstance(path, str)
                and Path(path).is_relative_to(workspace)
            ):
                edits.append(identity)
            command = args.get("command")
            if (
                tool.get("tool") == "Bash"
                and isinstance(command, str)
                and ".venv/bin/python -m pytest -q" in command
                and "passed" in str(tool.get("text", ""))
                and "failed" not in str(tool.get("text", ""))
            ):
                tests.append(identity)
    if len(all_ids) != len(set(all_ids)):
        raise BenchError("resume duplicated native tool IDs; closure run is invalid")
    if not edits or not tests:
        raise BenchError("closure requires native Edit and passing whole-suite pytest evidence")
    return [*edits, tests[-1]]


def phase_requests(base: Path, phase: str) -> list[dict[str, object]]:
    """Eksik model istek kaydı ölçüm hatasıdır."""
    return request_rows(read_json(Path(f"{base}.{phase}.requests.json")))


def close_task(
    spec: RunSpec, output: Path, session: str, step: int, fixed: dict[str, object], timeout: int
) -> dict[str, object]:
    """Dış doğrulamadan sonra tam transcript arşivi ve gerçek araç kanıtıyla kapat."""
    base = output / "meter" / "closure" / session
    workspace = output / "workspace"
    archive = output / f"closure-{step:02d}"
    archive.mkdir()
    shutil.copy2(claude_transcript(session), archive / "original-transcript.jsonl")
    anchor = read_json(Path(f"{base}.anchor.json"))
    completed = read_json(Path(f"{base}.completed.json"))
    paths = {mutation.path for mutation in spec.task.mutations[:step]}
    write_json(
        Path(f"{base}.request.json"),
        {
            "sessionId": session,
            "anchor": anchor,
            "completed": completed,
            "originalPrefixLength": 0,
            "evidenceToolUseIds": native_evidence(completed, anchor, workspace),
            "files": {
                path: (workspace / path).read_text(encoding="utf-8") for path in sorted(paths)
            },
            "proof": {"exit_code": 0, "tests_unchanged": True, "command": spec.task.test_command},
            "receipt": f"Task {step} verified by host: entire original pytest suite passed, "
            "all test files unchanged. Keep actual Edit/test evidence and all user constraints. "
            "Repository files remain available if a later task needs details.",
        },
    )
    write_json(archive / "completed.json", completed)
    for suffix in ("anchor", "request"):
        shutil.copy2(Path(f"{base}.{suffix}.json"), archive / f"{suffix}.json")
    compact = call(spec, output, session, f"compact-{step}", COMPACT, step * 2, timeout)
    if not Path(f"{base}.closed.json").exists():
        raise BenchError(f"task {step}: closure vetoed: {compact.get('result')}")
    summary_requests = Path(f"{base}.compact-{step}.requests.json")
    no_summary = (
        compact.get("num_turns") == 0
        and compact.get("total_cost_usd") == fixed.get("total_cost_usd")
        and (not summary_requests.exists() or request_rows(read_json(summary_requests)) == [])
    )
    if not no_summary:
        raise BenchError(f"task {step}: closure made a model request or changed cumulative cost")
    for suffix in ("projection-archive", "closed"):
        shutil.copy2(Path(f"{base}.{suffix}.json"), archive / f"{suffix}.json")
    closed = object_value(read_json(archive / "closed.json"), "closed receipt")
    return {key: value for key, value in closed.items() if key != "projected"} | {
        "no_summary_request": no_summary,
    }


def run(spec: RunSpec, output: Path, arm: str, close_every: int, timeout: int) -> dict[str, object]:
    """Yeni ve özel dizinde tek bir 20 görev oturumu; başarısız adımda deney durur."""
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    repo = ensure_repo(spec.task.repo, spec.task.ref, output.parent / "cache")
    workspace = output / "workspace"
    prepare_workspace(spec.task, repo, workspace)
    if not run_suite(spec.task, workspace).passed:
        raise BenchError("unmodified fixture suite does not pass")
    write_mod(output / "plugin")
    session = str(uuid.uuid4())
    base = output / "meter" / "closure" / session
    base.parent.mkdir(parents=True)
    report: dict[str, object] = {
        "arm": arm,
        "session_id": session,
        "task": spec.task.id,
        "ref": spec.task.ref,
        "model": spec.model,
        "effort": spec.effort,
        "window": spec.window,
        "close_every": close_every if arm == "closure" else None,
        "protocol": "sequential; no synthetic observation or forced repository warmup",
        "quota_scope": "account-wide observations; other activity and integer rounding apply",
        "quota_before": asdict(read_claude_quota(25)),
    }
    steps: list[dict[str, object]] = []
    started = time.monotonic()
    write_json(output / "result.json", report)
    try:
        for step, mutation in enumerate(spec.task.mutations, start=1):
            apply_mutation(workspace, mutation)
            if run_suite(spec.task, workspace).passed:
                raise BenchError(f"task {step}: injected mutation did not fail the suite")
            seal(workspace)
            if arm == "closure" and (step - 1) % close_every == 0:
                for suffix in ("anchor", "armed", "closed"):
                    Path(f"{base}.{suffix}.json").unlink(missing_ok=True)
                write_json(Path(f"{base}.armed.json"), {"sessionId": session})
            prompt = (FIRST_BUG_PROMPT if step == 1 else NEXT_BUG_PROMPT) + PROOF_PROMPT
            phase = f"fix-{step}"
            fixed = call(spec, output, session, phase, prompt, step * 2 - 1, timeout)
            verified = run_suite(spec.task, workspace)
            touched = touched_test_files(workspace, repo)
            requests = phase_requests(base, phase)
            row: dict[str, object] = {
                "step": step,
                "path": mutation.path,
                "tests_passed": verified.passed,
                "tests_touched": touched,
                "test_tail": verified.tail,
                "cost_usd_cumulative": fixed["total_cost_usd"],
                "requests": requests,
                "num_turns": fixed.get("num_turns"),
            }
            steps.append(row)
            report["steps"] = steps
            report["duration_seconds"] = time.monotonic() - started
            write_json(output / "result.json", report)
            print(
                json.dumps(
                    {
                        "arm": arm,
                        "step": step,
                        "passed": verified.passed,
                        "cost_usd": fixed["total_cost_usd"],
                    }
                ),
                flush=True,
            )
            if not verified.passed or touched:
                raise BenchError(f"task {step}: external tests failed or tests changed: {touched}")
            # Son görevin kapatılması tasarruf sağlayacak sonraki istek olmadığı için yapılmaz.
            if arm == "closure" and step % close_every == 0 and step < len(spec.task.mutations):
                row["closure"] = close_task(spec, output, session, step, fixed, timeout)
                write_json(output / "result.json", report)
        report["success"] = True
        report["cli_version"] = claude_version(claude_transcript(session))
        report["quota_after"] = asdict(read_claude_quota(25))
        report["cost_usd"] = steps[-1]["cost_usd_cumulative"]
        report["duration_seconds"] = time.monotonic() - started
        shutil.copy2(claude_transcript(session), output / "final-transcript.jsonl")
        write_json(output / "result.json", report)
        return report
    except CimriHookError as error:
        write_json(output / "result.json", report | {"success": False, "error": str(error)})
        raise


def main() -> int:
    """Arm, model ve effort CLI'da açıkça verilir; kullanıcı ayarları değiştirilmez."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("governor", "closure"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--window", type=int, required=True)
    parser.add_argument("--close-every", type=int, choices=(1, 5), required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()
    if args.timeout <= 0 or not 100_000 <= args.window <= 1_000_000:
        parser.error("timeout must be positive and window must be in [100000,1000000]")
    os.umask(0o077)
    spec = RunSpec(
        load_task(args.task),
        Protocol.SEQUENTIAL,
        Agent.CLAUDE,
        Variant.METER_GOVERNOR,
        args.model,
        args.effort,
        args.window,
        1,
    )
    try:
        run(spec, args.output.expanduser().resolve(), args.arm, args.close_every, args.timeout)
    except CimriHookError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
