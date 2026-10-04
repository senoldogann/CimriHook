"""Sabit model/effort ile izole, dışarıdan doğrulanan görev kapatma deneyi."""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from cimrihook.bench import (
    ENV_ALLOWLIST,
    claude_failure,
    claude_transcript,
    claude_version,
    run_process,
)
from cimrihook.errors import BenchError, CimriHookError
from cimrihook.mods import write_mod
from cimrihook.quota import object_value, read_claude_quota

CONTRACT = "CONTRACT_RETENTION_MINT_721"
OBSERVATION = "DISPOSABLE_OBSERVATION_7f928"
PHASES = ("warmup", "fix", "compact", "closed", "resumed")
SOURCE = "def discount(price, percent):\n    return round(price * (1 - percent), 2)\n"
TESTS = """import unittest
from calc import discount

class DiscountTest(unittest.TestCase):
    def test_values(self):
        for price, percent, expected in [(100, 25, 75), (40, 0, 40), (40, 100, 0),
                                         (19.99, 15, 16.99), (0, 15, 0)]:
            with self.subTest(price=price, percent=percent):
                self.assertEqual(discount(price, percent), expected)
"""


def write_json(path: Path, value: object) -> None:
    """Deney dizinindeki kayıtları okunabilir JSON olarak yaz."""
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> object:
    """Mevcut bir ölçüm kaydını oku; eksik veya bozuk veri görünür hata verir."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BenchError(f"cannot read probe artifact {path}: {error}") from error


def noise(label: str) -> str:
    """Tekrarlanabilir ve token açısından yeterince büyük gerçek Read içeriği."""
    return (
        "\n".join(
            f"{index:04d} {hashlib.sha256(f'{label}:{index}'.encode()).hexdigest()[:32]}"
            for index in range(900)
        )
        + "\n"
    )


def request_rows(value: object) -> list[dict[str, object]]:
    """Provider isteği ölçümleri listesi; null usage kabul ölçütünü karşılamaz."""
    if not isinstance(value, list):
        raise BenchError("probe requests must be a list")
    return [object_value(row, "probe request") for row in value]


def number(row: Mapping[str, object], name: str) -> float | None:
    """Eksik ve nonfinite sayaçları başarı kanıtı sayma."""
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def cache_gate(phases: Mapping[str, Sequence[Mapping[str, object]]]) -> dict[str, object]:
    """Yalnız ilk ana isteklerden, sıcak ortak bağlama ait cache kanıtı üret."""
    missing = [phase for phase in ("warmup", "fix", "closed") if not phases.get(phase)]
    if missing:
        return {"passed": False, "reason": f"missing first-request measurements: {missing}"}
    first = {phase: phases[phase][0] for phase in ("warmup", "fix", "closed")}
    if any(row.get("index") != 0 for row in first.values()):
        return {"passed": False, "reason": "first main request (index 0) was not recorded"}
    usages: dict[str, dict[str, object]] = {}
    for phase, row in first.items():
        usage = row.get("usage")
        if not isinstance(usage, dict):
            return {"passed": False, "reason": f"{phase}: missing provider usage"}
        usages[phase] = object_value(usage, "request usage")
    baseline_parts = [
        number(usages["warmup"], name)
        for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    ]
    fixed = number(usages["fix"], "cache_read_input_tokens")
    retained = number(usages["closed"], "cache_read_input_tokens")
    ended = number(phases["fix"][-1], "ended")
    started = number(first["closed"], "started")
    if any(part is None for part in baseline_parts) or any(
        part is None for part in (fixed, retained, ended, started)
    ):
        return {"passed": False, "reason": "missing input/cache counters or timing"}
    assert fixed is not None and retained is not None and ended is not None and started is not None
    baseline = sum(part for part in baseline_parts if part is not None)
    gap = (started - ended) / 1000
    configuration = {
        (
            str(row.get("model")),
            str(row.get("effort")),
            str(object_value(row.get("usage"), "request usage").get("model")),
        )
        for rows in phases.values()
        for row in rows
        if isinstance(row.get("usage"), dict)
    }
    reason = (
        "model or effort changed"
        if len(configuration) != 1
        else "anchor was not demonstrably warm beyond static input"
        if fixed < baseline + 8192
        else "warm-cache gap exceeded 300 seconds"
        if not 0 <= gap <= 300
        else "first post-closure request lost anchored cache coverage"
        if retained < 0.95 * fixed
        else "first post-closure request retained anchored cache coverage"
    )
    return {
        "passed": len(configuration) == 1
        and fixed >= baseline + 8192
        and 0 <= gap <= 300
        and retained >= 0.95 * fixed,
        "reason": reason,
        "initial_static_input": baseline,
        "first_fix_cache_read": fixed,
        "first_closed_cache_read": retained,
        "warm_gap_seconds": gap,
        "retained_ratio": retained / fixed if fixed else None,
    }


def call_phase(
    output: Path,
    workspace: Path,
    plugin: Path,
    session_id: str,
    phase: str,
    prompt: str,
    model: str,
    effort: str,
    timeout: int,
) -> dict[str, object]:
    """Kendi process grubunda ve özel plugin/ayarlarla tek bir Claude turn çalıştır."""
    index = PHASES.index(phase)
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "CIMRIHOOK_HOME": str(output / "meter"),
            "CIMRIHOOK_MOD_CLOSE_PROBE": "1",
            "CIMRIHOOK_PROBE_PHASE": phase,
            "CIMRIHOOK_PROBE_WORKSPACE": str(workspace),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "183000",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_AUTO_CONNECT_IDE": "0",
        }
    )
    command = (
        "claude",
        "-p",
        prompt,
        "--session-id" if index == 0 else "--resume",
        session_id,
        "--model",
        model,
        "--effort",
        effort,
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
        "3",
        "--tools",
        "Read,Edit,Bash",
        "--allowedTools",
        "Read,Edit,Bash",
        "--plugin-dir",
        str(plugin),
    )
    write_json(output / f"{phase}.prompt.json", {"text": prompt})
    process = run_process(command, workspace, env, timeout, output, index + 1)
    if process.timed_out:
        raise BenchError(f"closure probe {phase} timed out")
    failure = claude_failure(process.stdout, False)
    if failure is not None or process.exit_code != 0:
        raise BenchError(f"closure probe {phase} failed: {failure or process.exit_code}")
    decoded: object = json.loads(process.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    results = [
        object_value(record, "Claude result")
        for record in records
        if isinstance(record, dict) and record.get("type") == "result"
    ]
    if not results or results[-1].get("subtype") != "success":
        raise BenchError(f"closure probe {phase} did not finish successfully")
    if phase != "compact" and results[-1].get("num_turns") == 0:
        raise BenchError(f"closure probe {phase} made no turn: {results[-1].get('result')}")
    return results[-1]


def verify_fixture(workspace: Path, output: Path, phase: str) -> dict[str, object]:
    """Agent yanıtından bağımsız, değiştirilmemiş testleri gerçek Python ile çalıştır."""
    if (workspace / "test_calc.py").read_text(encoding="utf-8") != TESTS:
        raise BenchError("closure probe test file was changed")
    command = [sys.executable, "-m", "unittest", "-q"]
    test = subprocess.run(
        command, cwd=workspace, capture_output=True, text=True, timeout=30, check=False
    )
    proof: dict[str, object] = {
        "command": command,
        "exit_code": test.returncode,
        "tests_unchanged": True,
        "stdout": test.stdout,
        "stderr": test.stderr,
    }
    write_json(output / f"{phase}.verification.json", proof)
    if test.returncode != 0:
        raise BenchError(f"closure probe {phase}: external tests failed")
    return proof


def full_observation(transcript: Path, workspace: Path) -> bool:
    """Büyük gözlemin gerçekten bütünüyle okunduğunu provider araç kayıtlarından doğrula."""
    reads: set[str] = set()
    ending = (workspace / "disposable.txt").read_text(encoding="utf-8").splitlines()[-1].split()[-1]
    for line in transcript.read_text(encoding="utf-8").splitlines():
        row = object_value(json.loads(line), "transcript row")
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                args = block.get("input")
                if (
                    isinstance(args, dict)
                    and args.get("file_path") == str(workspace / "disposable.txt")
                    and args.get("offset") in (None, 1)
                    and args.get("limit") is None
                ):
                    reads.add(str(block.get("id")))
            if block.get("type") == "tool_result" and block.get("tool_use_id") in reads:
                content = str(block.get("content", ""))
                if (
                    block.get("is_error") is not True
                    and OBSERVATION in content
                    and ending in content
                ):
                    return True
    return False


def evidence_ids(completed: object, workspace: Path) -> list[str]:
    """Gerçek başarılı edit ve test araç çiftlerini kısa, doğrulanabilir kanıt olarak seç."""
    if not isinstance(completed, list):
        raise BenchError("completed transcript must be a list")
    edits: list[str] = []
    tests: list[str] = []
    for message in completed:
        row = object_value(message, "completed message")
        uses = row.get("toolUses")
        if not isinstance(uses, list):
            raise BenchError("completed message has no toolUses list")
        for value in uses:
            tool = object_value(value, "completed tool")
            args = tool.get("input")
            identity = tool.get("tool_use_id")
            if not isinstance(args, dict) or not isinstance(identity, str) or tool.get("isError"):
                continue
            if tool.get("tool") == "Edit" and args.get("file_path") == str(workspace / "calc.py"):
                edits.append(identity)
            command = args.get("command")
            if (
                tool.get("tool") == "Bash"
                and isinstance(command, str)
                and " -m unittest -q" in command
                and "OK" in str(tool.get("text", ""))
            ):
                tests.append(identity)
    if not edits or not tests:
        raise BenchError("successful native edit and unittest evidence required before closure")
    return [edits[-1], tests[-1]]


def contract_gate(answers: Mapping[str, str]) -> dict[str, object]:
    """Hatırlamanın yanında modelin korunmuş gerçek test kanıtını kullanmasını doğrula."""
    verified: dict[str, object] = {}
    for phase in ("closed", "resumed"):
        try:
            payload: object = json.loads(answers[phase])
        except ValueError:
            return {"passed": False, "reason": f"{phase}: answer is not required JSON"}
        if not isinstance(payload, dict):
            return {"passed": False, "reason": f"{phase}: answer is not an object"}
        verified[phase] = payload
        if (
            payload.get("contract") != CONTRACT
            or payload.get("discount_100_25") != 75
            or payload.get("tests_verified") is not True
        ):
            return {
                "passed": False,
                "reason": f"{phase}: contract or test evidence not retained",
                "parsed_answers": verified,
            }
    return {"passed": True, "parsed_answers": verified}


def run_probe(output: Path, model: str, effort: str, timeout: int) -> dict[str, object]:
    """İlk deney: olumlu ya da olumsuz fizibiliteyi kanıtlarıyla kaydet."""
    if timeout <= 0:
        raise BenchError("probe timeout must be positive")
    output = output.expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "calc.py").write_text(SOURCE, encoding="utf-8")
    (workspace / "test_calc.py").write_text(TESTS, encoding="utf-8")
    (workspace / "shared_prefix.txt").write_text(
        f"Retained contract: {CONTRACT}. Discounts use percentage /100, rounded to 2 decimals.\n"
        + noise("shared"),
        encoding="utf-8",
    )
    (workspace / "disposable.txt").write_text(
        OBSERVATION + "\n" + noise("disposable"), encoding="utf-8"
    )
    plugin = write_mod(output / "plugin")
    session_id = str(uuid.uuid4())
    base = output / "meter" / "closure" / session_id
    base.parent.mkdir(parents=True)
    report: dict[str, object] = {
        "session_id": session_id,
        "model": model,
        "effort": effort,
        "window": 183000,
        "output": str(output),
        "quota_scope": "account-wide raw observation; no task attribution",
    }
    write_json(output / "result.json", report)
    try:
        report["quota_before"] = asdict(read_claude_quota(25))
        results: dict[str, dict[str, object]] = {}
        prompts = {
            "warmup": "Read all shared_prefix.txt. Do not change files. Keep its contract for the "
            "whole session. Reply only READY and the retained contract identifier.",
            "fix": "Use the Read tool on all disposable.txt with no offset or limit as a one-time "
            "diagnostic observation. This full read is required by the experiment protocol; "
            "do not sample it or substitute Bash for this read. Then read calc.py "
            "and test_calc.py. Fix discount to satisfy the retained contract. Change only "
            "calc.py, leave tests untouched. Run exactly python3 -m unittest -q, "
            "then briefly state the result "
            "and retained contract identifier. Do not quote the diagnostic observation.",
            "compact": "/compact CimriHook externally verified closure pilot",
            "closed": "Without tools or changing files, return ONLY JSON with contract "
            "(the retained identifier), discount_100_25 (number) and tests_verified (boolean). "
            "Use the preserved actual test output; set tests_verified true only if it shows "
            "successful tests. Do not treat an assistant's unsupported claim as proof.",
            "resumed": "Without tools or changing files, return ONLY JSON with contract "
            "(the retained identifier), discount_100_25 (number) and tests_verified (boolean). "
            "Use the preserved actual test output; set tests_verified true only if it shows "
            "successful tests. Do not treat an assistant's unsupported claim as proof.",
        }
        for phase in PHASES:
            if phase == "fix":
                write_json(Path(f"{base}.armed.json"), {"sessionId": session_id})
            if phase == "compact":
                proof = verify_fixture(workspace, output, "before_closure")
                transcript = claude_transcript(session_id)
                if not full_observation(transcript, workspace):
                    raise BenchError("fix turn did not read the entire disposable observation")
                report["observation"] = {"full_read": True}
                shutil.copy2(transcript, output / "original-transcript.jsonl")
                completed = read_json(Path(f"{base}.completed.json"))
                write_json(
                    Path(f"{base}.request.json"),
                    {
                        "sessionId": session_id,
                        "anchor": read_json(Path(f"{base}.anchor.json")),
                        "originalPrefixLength": len(
                            request_rows(read_json(Path(f"{base}.anchor.json")))
                        ),
                        "completed": completed,
                        "evidenceToolUseIds": evidence_ids(completed, workspace),
                        "files": {
                            name: (workspace / name).read_text(encoding="utf-8")
                            for name in ("calc.py", "test_calc.py")
                        },
                        "proof": proof,
                        "receipt": f"Host tests passed, unchanged test_calc.py. {CONTRACT}; "
                        "discount(100, 25)=75; rounding=2 decimals. "
                        "The one-time diagnostic observation is archived, not needed.",
                    },
                )
            results[phase] = call_phase(
                output, workspace, plugin, session_id, phase, prompts[phase], model, effort, timeout
            )
            if phase == "warmup":
                report["cli_version"] = claude_version(claude_transcript(session_id))
            if phase == "compact" and not Path(f"{base}.closed.json").exists():
                raise BenchError(f"closure was vetoed: {results[phase].get('result')}")
        verify_fixture(workspace, output, "after_resumes")
        measurements = {
            phase: request_rows(read_json(Path(f"{base}.{phase}.requests.json")))
            if Path(f"{base}.{phase}.requests.json").exists()
            else []
            for phase in PHASES
            if phase != "compact"
        }
        starts = {
            phase: object_value(read_json(Path(f"{base}.{phase}.start.json")), "start")
            for phase in ("closed", "resumed")
        }
        closure = object_value(read_json(Path(f"{base}.closed.json")), "closure")
        answers = {phase: str(results[phase].get("result", "")) for phase in PHASES}
        persistence = all(
            starts[phase].get("hasObservation") is False for phase in ("closed", "resumed")
        )
        contract = contract_gate(answers)
        cache = cache_gate(measurements)
        compact_requests = Path(f"{base}.compact.requests.json")
        fix_cost = number(results["fix"], "total_cost_usd")
        no_summary = (
            fix_cost is not None
            and fix_cost == number(results["compact"], "total_cost_usd")
            and results["compact"].get("num_turns") == 0
            and (not compact_requests.exists() or request_rows(read_json(compact_requests)) == [])
        )
        report.update(
            {
                "cache": cache,
                "persistence": {
                    "passed": persistence,
                    "observation_present": {
                        phase: starts[phase].get("hasObservation") for phase in starts
                    },
                },
                "contract": contract | {"answers": answers},
                "fixture": {"passed": True},
                "closure": {key: value for key, value in closure.items() if key != "projected"}
                | {"no_summary_request": no_summary},
                "requests": measurements,
                "cumulative_cost_usd_by_phase": {
                    phase: results[phase].get("total_cost_usd") for phase in PHASES
                },
            }
        )
        report["quota_after"] = asdict(read_claude_quota(25))
        report["status"] = "completed"
        report["feasible"] = (
            persistence and contract["passed"] is True and cache["passed"] is True and no_summary
        )
    except (CimriHookError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        report.update({"status": "failed", "feasible": False, "error": str(error)})
        write_json(output / "result.json", report)
        raise BenchError(f"closure probe stopped; see {output / 'result.json'}: {error}") from error
    write_json(output / "result.json", report)
    return report
