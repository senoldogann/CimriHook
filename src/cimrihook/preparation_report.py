"""Aggregate native tool observations; research counts are not measured recoverable usage."""

import hashlib
import json
import re
import shlex
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from cimrihook.errors import ConfigError, TranscriptError
from cimrihook.transcripts import (
    BENCH_PROJECT_MARKER,
    SECONDS_PER_DAY,
    parse_line,
    recent_transcripts,
)

type Category = Literal["research", "edit", "other"]
RESEARCH_TOOLS: Final = frozenset({"Read", "Glob", "Grep"})
EDIT_TOOLS: Final = frozenset({"Edit", "MultiEdit", "Write", "apply_patch"})
SHELL_TOOLS: Final = frozenset({"Bash", "exec_command", "shell_command", "shell"})
READ_COMMANDS: Final = frozenset({"rg", "grep", "cat", "head", "tail", "ls", "pwd", "wc"})
SHELL_OPERATORS: Final = frozenset({";", "&&", "||", "|", ">", ">>", "<", "&"})


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One stable native call ID, its prompt span and request batch if exposed by the host."""

    identity: str
    batch: str | None
    span: str
    category: Category
    signature: str


@dataclass(frozen=True, slots=True)
class Observation:
    """Exact observation hash and explicit success, when the native record establishes it."""

    identity: str
    fingerprint: str
    success: bool | None


@dataclass(frozen=True, slots=True)
class NativeLog:
    """Projected native calls only; private text never enters the aggregate report."""

    calls: tuple[ToolCall, ...]
    observations: tuple[Observation, ...]
    malformed_lines: int


@dataclass(frozen=True, slots=True)
class PreparationReport:
    """Local observed opportunities, without cost or quota attribution."""

    agent: str
    days: int
    logs: int
    calls: int
    research_calls: int
    edit_calls: int
    other_calls: int
    duplicate_call_records: int
    identical_successful_research_observations: int
    calls_without_results: int
    results_without_success_status: int
    malformed_lines: int
    spans_with_research_before_edit: int
    research_calls_before_first_edit: int
    claude_research_only_batches_before_edit: int | None
    tools: tuple[tuple[str, int], ...]
    limitations: tuple[str, ...]


def fingerprint(value: object) -> str:
    """Hash canonical JSON without retaining private content in the report."""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def shell_category(arguments: dict[str, object]) -> Category:
    """Recognize only simple read commands, leaving compound scripts and wrappers unclassified."""
    raw = arguments.get("command", arguments.get("cmd"))
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        parts = [str(item) for item in raw]
        if len(parts) == 3 and parts[1] in ("-c", "-lc"):
            raw = parts[2]
    if not isinstance(raw, str) or "$(" in raw or "`" in raw:
        return "other"
    try:
        parts = shlex.split(raw)
    except ValueError:
        return "other"
    if not parts or any(part in SHELL_OPERATORS for part in parts):
        return "other"
    if any(character in raw for character in (";", "\n", "|", ">", "<", "&")):
        return "other"
    executable = Path(parts[0]).name
    if executable in READ_COMMANDS and not any(part.startswith("--pre") for part in parts[1:]):
        return "research"
    if executable == "git" and len(parts) > 1 and parts[1] in ("status", "diff", "show", "log"):
        return "research"
    return "other"


def tool_category(name: str, arguments: dict[str, object]) -> Category:
    """Classify the exposed native tool, without interpreting arbitrary program text."""
    short = name.rsplit(".", 1)[-1]
    if short in RESEARCH_TOOLS:
        return "research"
    if short in EDIT_TOOLS:
        return "edit"
    if short in SHELL_TOOLS:
        return shell_category(arguments)
    return "other"


def object_fields(value: object) -> dict[str, object]:
    """Project JSON objects to string-keyed fields."""
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def make_call(
    identity: str,
    name: str,
    arguments: dict[str, object],
    batch: str | None,
    span: str,
) -> ToolCall:
    """Stable normalized call input and literal host batch identity."""
    return ToolCall(
        identity,
        batch,
        span,
        tool_category(name, arguments),
        f"{name}:{fingerprint(arguments)}",
    )


def claude_log(path: Path) -> NativeLog:
    """Read native Claude tool batches and observations, ignoring user text in the projection."""
    calls: list[ToolCall] = []
    observations: list[Observation] = []
    namespace = fingerprint(str(path))
    span = f"unknown:{namespace}:initial"
    malformed = 0
    with path.open("rb") as handle:
        for index, raw in enumerate(handle):
            row = parse_line(raw)
            if row is None:
                malformed += bool(raw.strip())
                continue
            message = object_fields(row.get("message"))
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            results = [
                block
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            if row.get("type") == "user" and not results:
                identity = row.get("uuid")
                span = (
                    f"claude:{identity}"
                    if isinstance(identity, str)
                    else f"unknown:{namespace}:line-{index}"
                )
            batch = message.get("id")
            for item in blocks:
                block = object_fields(item)
                identity = block.get("id")
                name = block.get("name")
                if (
                    block.get("type") == "tool_use"
                    and isinstance(identity, str)
                    and isinstance(name, str)
                ):
                    calls.append(
                        make_call(
                            identity,
                            name,
                            object_fields(block.get("input")),
                            batch if isinstance(batch, str) else None,
                            span,
                        )
                    )
                result_id = block.get("tool_use_id")
                if block.get("type") == "tool_result" and isinstance(result_id, str):
                    observations.append(
                        Observation(
                            result_id,
                            fingerprint(block.get("content")),
                            block.get("is_error") is not True,
                        )
                    )
    return NativeLog(tuple(calls), tuple(observations), malformed)


def codex_success(output: object) -> bool | None:
    """Use explicit process exit records; output prose is not a success signal."""
    if isinstance(output, dict):
        code = output.get("exit_code")
        if isinstance(code, int):
            return code == 0
    if isinstance(output, str):
        match = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", output)
        if match is not None:
            return int(match[1]) == 0
    return None


def codex_log(path: Path) -> NativeLog:
    """Read Codex native calls; a tool-call sequence is never labelled model requests."""
    calls: list[ToolCall] = []
    observations: list[Observation] = []
    namespace = fingerprint(str(path))
    span = f"unknown:{namespace}:initial"
    malformed = 0
    with path.open("rb") as handle:
        for index, raw in enumerate(handle):
            row = parse_line(raw)
            if row is None:
                malformed += bool(raw.strip())
                continue
            payload = object_fields(row.get("payload"))
            kind = payload.get("type")
            if row.get("type") == "event_msg" and kind == "task_started":
                turn_id = payload.get("turn_id")
                span = (
                    f"codex:{turn_id}"
                    if isinstance(turn_id, str)
                    else f"unknown:{namespace}:line-{index}"
                )
            if row.get("type") != "response_item":
                continue
            identity = payload.get("call_id")
            if not isinstance(identity, str):
                continue
            if kind in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                if not isinstance(name, str):
                    continue
                arguments = payload.get("arguments", payload.get("input"))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"literal_input": arguments}
                calls.append(make_call(identity, name, object_fields(arguments), None, span))
            elif kind in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output")
                observations.append(
                    Observation(identity, fingerprint(output), codex_success(output))
                )
    return NativeLog(tuple(calls), tuple(observations), malformed)


def codex_paths(root: Path, days: int, now: float) -> tuple[Path, ...]:
    """Recent rollout files, with known benchmark workspaces excluded."""
    paths: list[Path] = []
    for path in root.rglob("rollout-*.jsonl"):
        if path.stat().st_mtime < now - days * SECONDS_PER_DAY:
            continue
        with path.open("rb") as handle:
            first = parse_line(handle.readline())
        metadata = object_fields(first.get("payload")) if first is not None else {}
        cwd = metadata.get("cwd")
        if isinstance(cwd, str) and BENCH_PROJECT_MARKER in cwd:
            continue
        paths.append(path)
    return tuple(sorted(paths, key=lambda path: path.stat().st_mtime))


def summarize_logs(agent: str, days: int, logs: Sequence[NativeLog]) -> PreparationReport:
    """Deduplicate copied native IDs globally and compare exact observations within a log."""
    known: set[str] = set()
    calls: list[ToolCall] = []
    observations: dict[str, Observation] = {}
    duplicates = 0
    identical = 0
    before_edit = 0
    spans_before: set[str] = set()
    batches_before: set[str] = set()
    batch_categories: dict[str, set[Category]] = {}
    for log in logs:
        for call in log.calls:
            if call.batch is not None:
                batch_categories.setdefault(call.batch, set()).add(call.category)
    for log in logs:
        results = {item.identity: item for item in log.observations}
        seen_observations: set[tuple[str, str]] = set()
        edited: set[str] = set()
        for call in log.calls:
            observation = results.get(call.identity)
            if observation is not None:
                observations[call.identity] = observation
            if call.category == "edit":
                edited.add(call.span)
            repeated_observation = False
            if (
                call.category == "research"
                and observation is not None
                and observation.success is True
            ):
                key = (call.signature, observation.fingerprint)
                repeated_observation = key in seen_observations
                seen_observations.add(key)
            if call.identity in known:
                duplicates += 1
                continue
            known.add(call.identity)
            calls.append(call)
            if call.category == "research":
                if call.span not in edited:
                    before_edit += 1
                    spans_before.add(call.span)
                    if call.batch is not None and batch_categories[call.batch] == {"research"}:
                        batches_before.add(call.batch)
                identical += repeated_observation
    categories = Counter(call.category for call in calls)
    tools = Counter(call.signature.split(":", 1)[0] for call in calls)
    return PreparationReport(
        agent,
        days,
        len(logs),
        len(calls),
        categories["research"],
        categories["edit"],
        categories["other"],
        duplicates,
        identical,
        sum(call.identity not in observations for call in calls),
        sum(
            observations[call.identity].success is None
            for call in calls
            if call.identity in observations
        ),
        sum(log.malformed_lines for log in logs),
        len(spans_before),
        before_edit,
        len(batches_before) if agent == "claude" else None,
        tuple(tools.most_common()),
        (
            "Research calls and prompt/turn spans are not completed tasks or recoverable savings.",
            "Identical observations do not establish redundant reasoning or unchanged files.",
            "Compound shell scripts and orchestration wrappers remain other/unclassified.",
            "Modified-file selection can include historical events outside the requested days.",
            "Spans without native IDs use per-file identities and may split copied history.",
            "No dollar or subscription-point attribution is inferred from these counts.",
        ),
    )


def diagnose_preparation(agent: str, root: Path, days: int, now: float) -> PreparationReport:
    """Local aggregate analysis with the same recent-file convention as doctor."""
    if days < 1 or agent not in ("claude", "codex"):
        raise ConfigError("preparation-report requires --days >= 1 and agent claude or codex")
    try:
        paths = (
            recent_transcripts(root, days, now)
            if agent == "claude"
            else codex_paths(root, days, now)
        )
        if not paths:
            raise ConfigError(
                f"no {agent} transcripts under {root} modified in the last {days} days"
            )
        reader = claude_log if agent == "claude" else codex_log
        return summarize_logs(agent, days, tuple(reader(path) for path in paths))
    except OSError as error:
        raise TranscriptError(
            f"cannot analyze {agent} preparation logs under {root}: {error}"
        ) from error


def report_json(report: PreparationReport) -> str:
    """Aggregate JSON contains no prompt, file content or observation text."""
    return json.dumps(asdict(report), ensure_ascii=False, indent=2)


def render_report(report: PreparationReport) -> str:
    """Separate measured call counts from the hypothesis that some work can be prepared locally."""
    lines = [
        f"CimriHook preparation ({report.agent}, logs modified in last {report.days} days): "
        f"{report.logs:,} logs, {report.calls:,} unique native calls",
        f"Research {report.research_calls:,}; edits {report.edit_calls:,}; "
        f"other/unclassified {report.other_calls:,}",
        f"Before first edit: {report.research_calls_before_first_edit:,} research calls across "
        f"{report.spans_with_research_before_edit:,} prompt/turn spans",
        f"Identical successful research observations within a log: "
        f"{report.identical_successful_research_observations:,}",
        f"Copied/streaming call records deduplicated: {report.duplicate_call_records:,}; "
        f"missing results {report.calls_without_results:,}; "
        f"unknown result success {report.results_without_success_status:,}; "
        f"malformed lines {report.malformed_lines:,}",
    ]
    if report.claude_research_only_batches_before_edit is not None:
        lines.append(
            f"Claude research-only request batches before edit: "
            f"{report.claude_research_only_batches_before_edit:,} (parallel calls count once)"
        )
    lines.extend(f"Limit: {item}" for item in report.limitations)
    return "\n".join(lines)
