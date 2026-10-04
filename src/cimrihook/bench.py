"""A/B evaluation harness on real subscriptions: the same task and agent, with the CimriHook
mechanisms on and off.

Every run happens in an isolated workspace: the task repository is copied from a pinned version,
a separate virtual environment is set up for the tests, and the starting state is sealed with git.
The agent (Claude Code or Codex CLI) runs non-interactively with the user's logged-in subscription
and a clean environment: only allow-listed environment variables pass, Claude Code gets no user
settings or MCP servers, Codex no user config.toml. Success: the test suite passes after every
step and no test file was touched.

Protocols:
- single: all bugs are injected up front; the agent fixes all of them in one request.
- sequential: the bugs arrive one by one in the same session; the agent fixes each one in the
  continuation of the conversation. It mimics the long, accumulating sessions of real use.
- deep: before sequential, the agent reads all the library's source files while there is no bug
  (Claude Code only). The context starts large and most of the code that was read goes stale for
  the later steps; it mimics the high-context (above 200k) sessions of real use.
- deeper: like deep, but the warm-up also reads the test files; the session goes above 400k.

Variants (mechanism ablation):
- baseline: the agent's default behaviour.
- governor: only the compaction window (Claude Code: CLAUDE_CODE_AUTO_COMPACT_WINDOW, at least
  100000; Codex: model_auto_compact_token_limit).
- rtk and rtk-governor: RTK's Bash command output compression (PreToolUse hook, `rtk hook
  claude`), alone and together with the window (Claude Code only): RTK on or off, window on or
  off.
- mask: the window and the CimriHook mod (`--plugin-dir`) with mask-first compaction: on
  automatic compaction old tool results are replaced with a placeholder instead of an LLM summary
  (Claude Code only).
- boundary: compaction by the mod after a completed interactive main turn. Claude Code 2.1.289
  does not support this API in headless sessions, so new benchmark runs are refused; earlier
  records can still be reported.
- codec, combined and brief: retired (tool result recoding and the compaction summary
  instruction are not part of the product; their code is at the `pre-trim` tag). The recorded
  results of these arms are read and reported; new runs cannot be planned.
- meter and meter-governor: behave like baseline and governor, but the CimriHook mod is loaded
  only as a limit meter: after every turn the use percentage of the subscription's 5-hour and
  weekly windows is written next to the run as `<run>.limits.jsonl` (Claude Code only, with a
  subscription). For an A/B in window points both arms must be meter; the report then sets the
  points of meter-governor against meter. Other use of the account moves the windows meanwhile:
  given the readings of the other recorded sessions, the report fits how many points a
  list-price dollar moves the window in each arm and in those sessions (cimrihook.weights).

Measurement:
- The primary cost is at the provider level and includes compaction and helper calls. For Claude
  Code it is the cumulative total_cost_usd (USD) in each step's result record, for Codex the
  per-response token_usage_record entries in the rollout, in base input price units with an
  explicit price table.
- The secondary cost (cost_base) sums only the requests in the session records (Claude
  transcript, Codex token_count events) with the same parsers as the simulator; it does not
  include the compaction call.
- The report compares the arms per scenario with the geometric mean of the costs; the summary at
  the agent level combines with equal weight only the scenarios where both arms have the same
  number of measured runs.
"""

import itertools
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from cimrihook.errors import BenchError
from cimrihook.limits import WINDOW_NAMES, LimitSample, RunPoints, run_points, session_samples
from cimrihook.mods import MOD_NAME, write_mod
from cimrihook.settings import governor_env, merge_settings
from cimrihook.simulate import (
    CLAUDE_MIN_COMPACT_WINDOW,
    OBSERVED,
    OPENAI,
    CostModel,
    Policy,
    PriceSheet,
    SessionTrace,
    context_of,
    exact_cost,
    load_claude_trace,
    load_codex_trace,
    simulate_trace,
    total_usage,
)
from cimrihook.stats import (
    DifferenceEstimate,
    RatioEstimate,
    fixed_pooled_ratio,
    geometric_mean,
    pooled_ratio,
    rate_difference,
    ratio_estimate,
)
from cimrihook.transcripts import Usage, average_write_weight, parse_line
from cimrihook.weights import (
    WeightFit,
    crossing_spans,
    fit_weights,
    pooled_spans,
    sessions_of,
    spend_curve,
    weight_of,
    weight_ratio,
    weight_text,
    window_crossings,
)

RESULT_SCHEMA: Final = 2
MAX_TURNS: Final = 400
TEST_TIMEOUT_SECONDS: Final = 600
CLAUDE_TOOLS: Final = "Read,Edit,MultiEdit,Write,Bash,Glob,Grep"
CLAUDE_PROJECTS: Final = Path.home() / ".claude" / "projects"
CODEX_SESSIONS: Final = Path.home() / ".codex" / "sessions"
WORKFLOW_JOURNAL: Final = "journal.jsonl"
TEST_DIR_NAMES: Final = frozenset({"test", "tests", "testing"})
IGNORED_DIRS: Final = frozenset({".git", ".venv", "__pycache__", ".pytest_cache"})
WORKSPACE_EXCLUDES: Final = ".venv/\n__pycache__/\n.pytest_cache/\n"
GIT_IDENTITY: Final = (
    "-c",
    "user.name=cimrihook-bench",
    "-c",
    "user.email=bench@cimrihook.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)
# Environment variables passed to agent processes. The rest is not inherited: when the benchmark
# is started from a Claude Code session, that session's CLAUDE_CODE_*, ANTHROPIC_* and similar
# variables change the agent's model, effort level, API address and behaviour.
ENV_ALLOWLIST: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
REQUIRED_ENV: Final = ("PATH", "HOME")
TEST_TAIL_CHARS: Final = 1_500  # test output appended to the error message
SAFE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # task id and version
SAFE_PACKAGE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\],<>=!~-]*")
USD: Final = "usd"
BASE_INPUT_TOKENS: Final = "base_input_tokens"
CLAUDE_PRICE_SHEET: Final = "Claude Code total_cost_usd (includes compaction and auxiliary calls)"
CHECKPOINT_STEPS: Final = (5, 10, 20, 40)
NONINFERIORITY_MARGIN: Final = 0.05  # largest drop accepted in run success
# The decision is at the run level: the steps of a run depend on each other (a stuck agent misses
# the later steps too), and counting the steps as independent narrows the interval artificially.
# Excluding a -5 point drop takes about 75 runs per arm even with no failures at all; with fewer,
# the decision is mostly "could not be shown". Below this number no decision is made.
MIN_RUNS_FOR_VERDICT: Final = 5
# Accepted simulator estimation error (ratio points): the estimate lies this close to the
# measured ratio.
CALIBRATION_TOLERANCE: Final = 0.05
# Codex price table sensitivity: the plausible range of the cached input and output multipliers.
CODEX_SENSITIVITY: Final = tuple(
    replace(OPENAI, read=read, output=output) for read in (0.1, 0.25) for output in (4.0, 6.0, 8.0)
)
FIRST_BUG_PROMPT: Final = (
    "The test suite of this repository fails because of a bug in the library code. Find the bug "
    "and fix it in the library code. Do not modify, add, or delete any test files. The test "
    "environment is ready: run the tests with `.venv/bin/python -m pytest -q`. You are done when "
    "the whole test suite passes."
)
WARMUP_PROMPT: Final = (
    "Before any bug appears, get to know this repository. Read every source file of the library "
    "yourself, in full, with the Read tool, one file at a time and without subagents; skip the "
    "tests, documentation and packaging files. Then reply with one short line per file saying "
    "what it provides. Do not change any file."
)
DEEPER_WARMUP_PROMPT: Final = (
    "Before any bug appears, get to know this repository. Read every source file of the library "
    "and every test file yourself, in full, with the Read tool, one file at a time and without "
    "subagents; skip documentation and packaging files. Then reply with one short line per "
    "library file saying what it provides and how it is tested. Do not change any file."
)
WARMUP_PROMPTS: Final = {"deep": WARMUP_PROMPT, "deeper": DEEPER_WARMUP_PROMPT}
NEXT_BUG_PROMPT: Final = (
    "A new bug has just been introduced in the library code and the test suite fails again. Find "
    "and fix it in the library code. Do not modify, add, or delete any test files. Run the tests "
    "with `.venv/bin/python -m pytest -q`. You are done when the whole test suite passes."
)


class Agent(StrEnum):
    """The agent under evaluation."""

    CLAUDE = "claude"
    CODEX = "codex"


class Variant(StrEnum):
    """A/B arm: the CimriHook mechanisms that are on."""

    BASELINE = "baseline"
    GOVERNOR = "governor"
    CODEC = "codec"  # retired: kept so recorded results stay readable
    COMBINED = "combined"  # retired: window and codec
    BRIEF = "brief"  # retired: window and a summary instruction
    RTK = "rtk"
    RTK_GOVERNOR = "rtk-governor"
    MASK = "mask"
    BOUNDARY = "boundary"
    METER = "meter"
    METER_GOVERNOR = "meter-governor"


WINDOW_VARIANTS: Final = frozenset(
    {
        Variant.GOVERNOR,
        Variant.COMBINED,
        Variant.BRIEF,
        Variant.RTK_GOVERNOR,
        Variant.MASK,
        Variant.BOUNDARY,
        Variant.METER_GOVERNOR,
    }
)
MOD_VARIANTS: Final = frozenset(
    {Variant.MASK, Variant.BOUNDARY, Variant.METER, Variant.METER_GOVERNOR}
)
# Arms that load the mod only to record the subscription windows' use after every turn: they
# behave like baseline and governor, and each run keeps its readings next to its result.
METER_VARIANTS: Final = frozenset({Variant.METER, Variant.METER_GOVERNOR})
LIMITS_SUFFIX: Final = ".limits.jsonl"  # per-run window readings, beside the run result
RUN_MIN_POINTS: Final = 3.0  # mean points per run under which a window is too coarse to compare
OTHER_SESSIONS: Final = "other sessions"  # weight class: recorded sessions that are no arm
POOLED: Final = "pooled"  # weight class: every recorded session at one weight
METER_DIR: Final = "meter"  # CIMRIHOOK_HOME of a mod arm, inside the run directory
BOUNDARY_TOKENS: Final = 100_000  # threshold after a completed interactive main turn
RTK_VARIANTS: Final = frozenset({Variant.RTK, Variant.RTK_GOVERNOR})
RTK_HOOK_COMMAND: Final = "rtk hook claude"  # the command RTK 0.51 installs for Claude Code
# Arms of the codec and the summary instruction, which are no longer part of the product. Their
# results from earlier sets are still loaded and reported; new runs cannot be planned.
RETIRED_VARIANTS: Final = frozenset({Variant.CODEC, Variant.COMBINED, Variant.BRIEF})
CODEX_VARIANTS: Final = frozenset({Variant.BASELINE, Variant.GOVERNOR})
# The previous schema's single treatment arm: window and codec in Claude Code, only the window in
# Codex.
LEGACY_VARIANT: Final = "cimrihook"
ISOLATION: Final[dict[Agent, str]] = {
    Agent.CLAUDE: "allowlisted environment; --setting-sources project; --strict-mcp-config",
    Agent.CODEX: "allowlisted environment; --ignore-user-config; approval_policy=never",
}
LEGACY_ISOLATION: Final[dict[Agent, str]] = {
    Agent.CLAUDE: "inherited environment; --setting-sources project",
    Agent.CODEX: "inherited environment; user config.toml loaded",
}


class Protocol(StrEnum):
    """How the task is given to the agent."""

    SINGLE = "single"
    SEQUENTIAL = "sequential"
    DEEP = "deep"
    DEEPER = "deeper"


@dataclass(frozen=True, slots=True)
class Mutation:
    """The one-line bug injected into the repository."""

    path: str
    find: str
    replace: str


@dataclass(frozen=True, slots=True)
class Task:
    """Task definition (bench/tasks/*.json)."""

    id: str
    repo: str
    ref: str
    packages: tuple[str, ...]
    test_command: tuple[str, ...]
    expected_failures: int
    prompt: str
    mutations: tuple[Mutation, ...]


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Definition of a single run."""

    task: Task
    protocol: Protocol
    agent: Agent
    variant: Variant
    model: str
    effort: str
    window: int
    repetition: int


@dataclass(frozen=True, slots=True)
class SuiteRun:
    """One run of the test suite."""

    passed: bool
    tail: str  # end of the output, for error messages


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Exit of the agent process."""

    exit_code: int
    timed_out: bool
    stdout: str


@dataclass(frozen=True, slots=True)
class ReportedUsage:
    """Session totals in Claude Code's result record (all models, cumulative across resumes)."""

    cost_usd: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int


@dataclass(frozen=True, slots=True)
class AgentRun:
    """Result of a single agent call."""

    session_id: str
    exit_code: int
    timed_out: bool
    reported: ReportedUsage | None  # reported by Claude Code only; absent on timeout


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Summary of all agent calls in a run."""

    session_id: str
    exit_code: int  # exit code of the last call
    timed_out: bool  # did any call exceed the time limit
    reported: tuple[ReportedUsage | None, ...]  # Claude Code report per step
    duration_seconds: float
    step_passed: tuple[bool, ...]  # did the test suite pass after every step
    passed: bool  # does the test suite pass at the end


@dataclass(frozen=True, slots=True)
class Measurement:
    """Token usage measured from the requests in the session records (compaction call excluded)."""

    requests: int
    compactions: int
    compaction_pre_tokens: tuple[int, ...]  # context sizes that triggered the compactions
    max_context: int
    mean_context: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    cost_base: float  # in base input price units, with the provider's price ratios


@dataclass(frozen=True, slots=True)
class ProviderMeasurement:
    """Provider-level measurement: the primary cost including compaction and helper calls."""

    cost_by_step: tuple[float, ...]  # cumulative cost at each step end; the last is the total
    unit: str  # USD or BASE_INPUT_TOKENS
    price_sheet: str
    uncached: int
    cache_write: int
    cache_read: int
    output: int


@dataclass(frozen=True, slots=True)
class CodexRecords:
    """Per-response usage records in the Codex rollout, in task (step) order."""

    steps: tuple[tuple[Usage, ...], ...]
    cli_version: str


@dataclass(frozen=True, slots=True)
class AgentLogs:
    """Measurements read from a run's agent records."""

    measurement: Measurement
    provider: ProviderMeasurement | None  # None if a step has no provider total
    version: str


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """What a run is: the task, agent, arm and conditions."""

    run_id: str
    task_id: str
    protocol: str
    agent: Agent
    variant: str  # the arm as it was run
    mechanism: Variant  # the mechanism actually on
    model: str
    effort: str
    window: int
    isolation: str
    repetition: int


@dataclass(frozen=True, slots=True)
class RunBehaviour:
    """The agent's behaviour in the run and the task result."""

    session_id: str
    success: bool
    steps: int
    steps_passed: int
    step_passed: tuple[bool, ...]
    tests_touched: bool
    agent_exit: int
    timed_out: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class RunResult:
    """The persistent result of a run (bench/results/<name>/<run_id>.json)."""

    schema: int
    run_id: str
    task_id: str
    protocol: str
    agent: str
    variant: str  # the arm as it was run ("cimrihook" in the old schema)
    mechanism: str  # the mechanism actually on: baseline, governor, codec or combined
    model: str
    effort: str
    window: int  # the requested compaction window
    effective_window: int | None  # the window the agent applied; None if the arm sets no window
    isolation: str  # the agent process's environment and the configuration loaded
    agent_version: str
    repetition: int
    session_id: str
    success: bool
    steps: int
    steps_passed: int
    step_passed: tuple[bool, ...]  # result per step; empty if unknown in the old schema
    tests_touched: bool
    agent_exit: int
    timed_out: bool
    duration_seconds: float
    provider: ProviderMeasurement | None  # primary cost; None if unknown
    requests: int
    compactions: int
    compaction_pre_tokens: tuple[int, ...]
    max_context: int
    mean_context: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    cost_base: float
    error: str | None  # why the run could not be measured


def load_tasks(tasks_dir: Path) -> tuple[Task, ...]:
    """Loads the task definitions."""
    paths = sorted(tasks_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no task definitions (*.json) in {tasks_dir}")
    return tuple(load_task(path) for path in paths)


def select_tasks(tasks: Sequence[Task], task_ids: Sequence[str]) -> tuple[Task, ...]:
    """Only the requested tasks; an unknown id is an error."""
    known = {task.id for task in tasks}
    unknown = sorted(set(task_ids) - known)
    if unknown:
        raise BenchError(f"unknown task ids {unknown}; available: {sorted(known)}")
    return tuple(task for task in tasks if task.id in task_ids)


def load_task(path: Path) -> Task:
    """Reads a single task definition, validating it.

    The task definition goes into command lines and paths (git clone, uv pip install, the working
    directory); values that look like options or escape the directory are errors.
    """
    where = str(path)
    data = json_object(json.loads(path.read_text(encoding="utf-8")), where)
    task = parse_task(data, where)
    problems = task_problems(task)
    if problems:
        raise BenchError(f"{where}: {'; '.join(problems)}")
    return task


def task_problems(task: Task) -> list[str]:
    """Unsafe values in a task definition."""
    return [
        *([] if SAFE_NAME.fullmatch(task.id) else [f"id {task.id!r} is not a plain name"]),
        *([] if SAFE_NAME.fullmatch(task.ref) else [f"ref {task.ref!r} is not a plain name"]),
        *([] if task.repo.startswith("https://") else [f"repo {task.repo!r} is not https"]),
        *(
            f"package {package!r} is not a plain requirement"
            for package in task.packages
            if not SAFE_PACKAGE.fullmatch(package)
        ),
        *(
            f"mutation path {mutation.path!r} leaves the repository"
            for mutation in task.mutations
            if Path(mutation.path).is_absolute() or ".." in Path(mutation.path).parts
        ),
    ]


def parse_task(data: dict[str, object], where: str) -> Task:
    """Reads the task fields with their types."""
    return Task(
        id=text_field(data, "id", where),
        repo=text_field(data, "repo", where),
        ref=text_field(data, "ref", where),
        packages=text_list(data, "packages", where),
        test_command=text_list(data, "test_command", where),
        expected_failures=int_field(data, "expected_failures", where),
        prompt=text_field(data, "prompt", where),
        mutations=tuple(
            Mutation(
                text_field(item, "path", where),
                text_field(item, "find", where),
                text_field(item, "replace", where),
            )
            for item in object_list(data, "mutations", where)
        ),
    )


def plan_runs(
    tasks: Sequence[Task],
    protocols: Sequence[Protocol],
    agents: Sequence[Agent],
    variants: Sequence[Variant],
    repetitions: int,
    claude_model: str,
    codex_model: str,
    effort: str,
    window: int,
) -> tuple[RunSpec, ...]:
    """The run matrix; the arms of the same task and agent are ordered one after another.

    Arms and windows the agent cannot apply are errors; a different condition is never measured
    silently.
    """
    specs = tuple(
        RunSpec(
            task=task,
            protocol=protocol,
            agent=agent,
            variant=variant,
            model=claude_model if agent is Agent.CLAUDE else codex_model,
            effort=effort,
            window=window,
            repetition=repetition,
        )
        for repetition in range(1, repetitions + 1)
        for task in tasks
        for protocol in protocols
        for agent in agents
        for variant in variants
    )
    problems = sorted({problem for spec in specs if (problem := spec_problem(spec)) is not None})
    if problems:
        raise BenchError(f"invalid run matrix: {'; '.join(problems)}")
    return specs


def spec_problem(spec: RunSpec) -> str | None:
    """Why the agent cannot apply this arm or window; None if it can."""
    if spec.variant in RETIRED_VARIANTS:
        return (
            f"variant {spec.variant.value!r} is retired: its mechanism was removed from CimriHook "
            "(the code is at the git tag pre-trim); its recorded results can still be reported"
        )
    if spec.agent is Agent.CLAUDE and spec.variant is Variant.BOUNDARY:
        return (
            "claude cannot benchmark 'boundary': session.compact() is unavailable in headless "
            "(-p / SDK) sessions in Claude Code 2.1.289; use governor, mask or meter-governor. "
            "Recorded boundary results can still be reported"
        )
    if spec.agent is Agent.CODEX and spec.variant not in CODEX_VARIANTS:
        return (
            f"codex cannot run variant {spec.variant.value!r}: only the compaction window is "
            "implemented for Codex, so run codex with --variants baseline,governor"
        )
    if spec.agent is Agent.CODEX and spec.protocol in (Protocol.DEEP, Protocol.DEEPER):
        return (
            "codex cannot run the deep protocol: its warm-up turn is not mapped to a step in the "
            "Codex usage records"
        )
    if (
        spec.agent is Agent.CLAUDE
        and spec.variant in WINDOW_VARIANTS
        and spec.window < CLAUDE_MIN_COMPACT_WINDOW
    ):
        return (
            f"claude window {spec.window} is below {CLAUDE_MIN_COMPACT_WINDOW}: Claude Code raises "
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW to that minimum, so the run would not test this window"
        )
    return None


def effective_window(agent: Agent, mechanism: Variant, window: int) -> int | None:
    """The compaction window the agent actually applies; None if the arm sets no window.

    Claude Code 2.1.288 raises CLAUDE_CODE_AUTO_COMPACT_WINDOW to at least 100000 and triggers
    automatic compaction at a context of window − 20000 (output reserve) − 13000 tokens.
    """
    if mechanism not in WINDOW_VARIANTS:
        return None
    if agent is Agent.CLAUDE:
        return max(window, CLAUDE_MIN_COMPACT_WINDOW)
    return window


def run_id(spec: RunSpec) -> str:
    """The stable id of a run."""
    return (
        f"{spec.task.id}.{spec.protocol.value}.{spec.agent.value}.{spec.variant.value}"
        f".r{spec.repetition}"
    )


def run_plan(
    specs: Sequence[RunSpec], results_dir: Path, work_dir: Path, concurrency: int, timeout: int
) -> tuple[RunResult, ...]:
    """Runs the runs without a result in parallel; each result is written to disk as it finishes."""
    results_dir.mkdir(parents=True, exist_ok=True)
    pending = [spec for spec in specs if needs_run(result_path(results_dir, run_id(spec)), spec)]
    repos = {
        (spec.task.repo, spec.task.ref): ensure_repo(spec.task.repo, spec.task.ref, work_dir)
        for spec in pending
    }
    finished: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                execute_and_save,
                spec,
                repos[(spec.task.repo, spec.task.ref)],
                work_dir,
                timeout,
                results_dir,
            )
            for spec in pending
        ]
        for future in as_completed(futures):
            result = future.result()
            print(json.dumps(progress_fields(result)), flush=True)
            finished.append(result)
    return tuple(finished)


def execute_and_save(
    spec: RunSpec, repo_dir: Path, work_dir: Path, timeout: int, results_dir: Path
) -> RunResult:
    """Runs a run and writes its result immediately: an unexpected failure of another run does not
    lose the finished results."""
    result = execute_run(spec, repo_dir, work_dir, timeout)
    result_path(results_dir, result.run_id).write_text(
        json.dumps(asdict(result), indent=2), encoding="utf-8"
    )
    if spec.variant in METER_VARIANTS:
        lines = meter_lines(work_dir / result.run_id)
        limits_path(results_dir, result.run_id).write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )
    return result


def limits_path(results_dir: Path, identifier: str) -> Path:
    """File with the usage-window readings of a meter run."""
    return results_dir / f"{identifier}{LIMITS_SUFFIX}"


def result_path(results_dir: Path, identifier: str) -> Path:
    """The file of a run's result."""
    return results_dir / f"{identifier}.json"


def needs_run(path: Path, spec: RunSpec) -> bool:
    """Runs without a result, or that ended without being measured (for example a usage limit), are
    retried.

    If an existing result was measured under other conditions that affect the arm (model, effort,
    window), it is not mixed into the same set: an error.
    """
    if not path.exists():
        return True
    data = json_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    window = spec.window if spec.variant in WINDOW_VARIANTS else data.get("window")
    wanted: dict[str, object] = {"model": spec.model, "effort": spec.effort, "window": window}
    recorded = {key: data.get(key) for key in wanted}
    if recorded != wanted:
        raise BenchError(
            f"{path} was measured with {recorded}, this batch asks for {wanted}; "
            "use another --name for a different condition"
        )
    return data.get("error") is not None


def progress_fields(result: RunResult) -> dict[str, object]:
    """Structured fields of the progress line."""
    provider = result.provider
    return {
        "event": "run_finished",
        "run_id": result.run_id,
        "success": result.success,
        "steps_passed": f"{result.steps_passed}/{result.steps}",
        "provider_cost": None if provider is None else provider.cost_by_step[-1],
        "provider_unit": None if provider is None else provider.unit,
        "cost_base": round(result.cost_base),
        "requests": result.requests,
        "compactions": result.compactions,
        "compaction_pre_tokens": list(result.compaction_pre_tokens),
        "max_context": result.max_context,
        "minutes": round(result.duration_seconds / 60, 1),
        "error": result.error,
    }


def execute_run(spec: RunSpec, repo_dir: Path, work_dir: Path, timeout: int) -> RunResult:
    """Runs the task according to its protocol; measures the tests and the token use."""
    identifier = run_id(spec)
    run_dir = work_dir / identifier
    if run_dir.exists():
        shutil.rmtree(run_dir)  # leftover of an earlier attempt that was cut short
    workspace = run_dir / "workspace"
    try:
        if spec.protocol is Protocol.SINGLE:
            outcome = run_single(spec, repo_dir, workspace, run_dir, timeout)
        else:
            outcome = run_sequential(spec, repo_dir, workspace, run_dir, timeout)
        touched = touched_test_files(workspace, repo_dir)
        logs = read_agent_logs(
            spec.agent, outcome.session_id, outcome.reported, len(outcome.step_passed)
        )
        problem = arm_problem(spec, run_dir)
        if problem is not None:
            raise BenchError(problem)
    except BenchError as error:
        return failed_result(spec, identifier, str(error))
    return measured_result(
        RunIdentity(
            run_id=identifier,
            task_id=spec.task.id,
            protocol=spec.protocol.value,
            agent=spec.agent,
            variant=spec.variant.value,
            mechanism=spec.variant,
            model=spec.model,
            effort=spec.effort,
            window=spec.window,
            isolation=ISOLATION[spec.agent],
            repetition=spec.repetition,
        ),
        RunBehaviour(
            session_id=outcome.session_id,
            success=outcome.passed and all(outcome.step_passed) and not touched,
            steps=len(outcome.step_passed),
            steps_passed=sum(outcome.step_passed),
            step_passed=outcome.step_passed,
            tests_touched=bool(touched),
            agent_exit=outcome.exit_code,
            timed_out=outcome.timed_out,
            duration_seconds=outcome.duration_seconds,
        ),
        logs,
    )


def arm_problem(spec: RunSpec, run_dir: Path) -> str | None:
    """Did the arm's mechanism really run? Claude Code carries on when a hook or the mod fails; an
    arm that silently ran like baseline does not count as measured."""
    if spec.variant in METER_VARIANTS and not meter_lines(run_dir):
        return "the mod recorded no usage-window reading in the run"
    return None


def meter_lines(run_dir: Path) -> tuple[str, ...]:
    """The mod's window readings of a run, oldest first; one JSON object per line."""
    files = sorted((run_dir / METER_DIR / "limits").glob("*.jsonl"))
    lines = [
        line for file in files for line in file.read_text(encoding="utf-8").splitlines() if line
    ]
    return tuple(sorted(lines, key=lambda line: int(json.loads(line)["t"])))


def measured_result(identity: RunIdentity, behaviour: RunBehaviour, logs: AgentLogs) -> RunResult:
    """Run identity, agent behaviour and measurements from the records in one result record."""
    measurement = logs.measurement
    return RunResult(
        schema=RESULT_SCHEMA,
        run_id=identity.run_id,
        task_id=identity.task_id,
        protocol=identity.protocol,
        agent=identity.agent.value,
        variant=identity.variant,
        mechanism=identity.mechanism.value,
        model=identity.model,
        effort=identity.effort,
        window=identity.window,
        effective_window=effective_window(identity.agent, identity.mechanism, identity.window),
        isolation=identity.isolation,
        agent_version=logs.version,
        repetition=identity.repetition,
        session_id=behaviour.session_id,
        success=behaviour.success,
        steps=behaviour.steps,
        steps_passed=behaviour.steps_passed,
        step_passed=behaviour.step_passed,
        tests_touched=behaviour.tests_touched,
        agent_exit=behaviour.agent_exit,
        timed_out=behaviour.timed_out,
        duration_seconds=behaviour.duration_seconds,
        provider=logs.provider,
        requests=measurement.requests,
        compactions=measurement.compactions,
        compaction_pre_tokens=measurement.compaction_pre_tokens,
        max_context=measurement.max_context,
        mean_context=measurement.mean_context,
        uncached=measurement.uncached,
        cache_write=measurement.cache_write,
        cache_read=measurement.cache_read,
        output=measurement.output,
        cost_base=measurement.cost_base,
        error=None,
    )


def run_single(
    spec: RunSpec, repo_dir: Path, workspace: Path, run_dir: Path, timeout: int
) -> SessionOutcome:
    """All bugs are injected up front; the agent fixes them in a single request."""
    prepare_workspace(spec.task, repo_dir, workspace)
    for mutation in spec.task.mutations:
        apply_mutation(workspace, mutation)
    suite = run_suite(spec.task, workspace)
    if suite.passed:
        raise BenchError(
            f"{spec.task.id}: test suite passes after the mutations; they are inert\n{suite.tail}"
        )
    seal(workspace)
    started = time.monotonic()
    call = start_agent(spec, spec.task.prompt, workspace, run_dir, timeout, 1)
    duration = time.monotonic() - started
    passed = tests_pass(spec.task, workspace)
    return SessionOutcome(
        session_id=call.session_id,
        exit_code=call.exit_code,
        timed_out=call.timed_out,
        reported=(call.reported,),
        duration_seconds=duration,
        step_passed=(passed,),
        passed=passed,
    )


def run_sequential(
    spec: RunSpec, repo_dir: Path, workspace: Path, run_dir: Path, timeout: int
) -> SessionOutcome:
    """The bugs arrive one by one in the same session; the context accumulates as in real use.

    In the deep protocol the session starts with a warm-up call that reads the library before the
    first bug. The warm-up does not count as a step; its cost goes into the total of the first
    step thanks to Claude Code's cumulative report.
    """
    prepare_workspace(spec.task, repo_dir, workspace)
    before = run_suite(spec.task, workspace)
    if not before.passed:
        raise BenchError(
            f"{spec.task.id}: test suite fails before any mutation at {spec.task.ref}\n"
            f"{before.tail}"
        )
    started = time.monotonic()
    opening = (
        warm_up(spec, workspace, run_dir, timeout)
        if spec.protocol in (Protocol.DEEP, Protocol.DEEPER)
        else None
    )
    calls: list[AgentRun] = []
    step_passed: list[bool] = []
    for step, mutation in enumerate(spec.task.mutations, start=1):
        apply_mutation(workspace, mutation)
        mutated = run_suite(spec.task, workspace)
        if mutated.passed:
            raise BenchError(
                f"{spec.task.id}: step {step} mutation is inert after earlier fixes\n{mutated.tail}"
            )
        seal(workspace)  # so that the new bug does not show up in the git history
        prompt = FIRST_BUG_PROMPT if step == 1 else NEXT_BUG_PROMPT
        first = opening if opening is not None else (calls[0] if calls else None)
        calls.append(
            start_agent(spec, prompt, workspace, run_dir, timeout, step)
            if first is None
            else resume_agent(spec, first.session_id, prompt, workspace, run_dir, timeout, step)
        )
        step_passed.append(tests_pass(spec.task, workspace))
    return SessionOutcome(
        session_id=calls[0].session_id,
        exit_code=calls[-1].exit_code,
        timed_out=any(call.timed_out for call in calls),
        reported=tuple(call.reported for call in calls),
        duration_seconds=time.monotonic() - started,
        step_passed=tuple(step_passed),
        passed=tests_pass(spec.task, workspace),
    )


def warm_up(spec: RunSpec, workspace: Path, run_dir: Path, timeout: int) -> AgentRun:
    """The warm-up call of the deep protocol: the agent reads the library while there is no bug and
    changes no file. On a timeout the cost cannot be recorded, so the run cannot be measured."""
    seal(workspace)
    call = start_agent(spec, WARMUP_PROMPTS[spec.protocol.value], workspace, run_dir, timeout, 0)
    if call.timed_out:
        raise BenchError(f"{spec.task.id}: the warm-up call timed out after {timeout}s")
    changed = run_checked(("git", "status", "--porcelain"), workspace).stdout.strip()
    if changed:
        raise BenchError(f"{spec.task.id}: the warm-up call changed files:\n{changed[:800]}")
    return call


def failed_result(spec: RunSpec, identifier: str, message: str) -> RunResult:
    """Record of an unmeasurable run; left out of the statistics, shown in the report."""
    return unmeasured_result(
        RunIdentity(
            run_id=identifier,
            task_id=spec.task.id,
            protocol=spec.protocol.value,
            agent=spec.agent,
            variant=spec.variant.value,
            mechanism=spec.variant,
            model=spec.model,
            effort=spec.effort,
            window=spec.window,
            isolation=ISOLATION[spec.agent],
            repetition=spec.repetition,
        ),
        message,
    )


def unmeasured_result(identity: RunIdentity, message: str) -> RunResult:
    """A result record with empty measurement fields, the reason being in the error field."""
    return RunResult(
        schema=RESULT_SCHEMA,
        run_id=identity.run_id,
        task_id=identity.task_id,
        protocol=identity.protocol,
        agent=identity.agent.value,
        variant=identity.variant,
        mechanism=identity.mechanism.value,
        model=identity.model,
        effort=identity.effort,
        window=identity.window,
        effective_window=effective_window(identity.agent, identity.mechanism, identity.window),
        isolation=identity.isolation,
        agent_version="",
        repetition=identity.repetition,
        session_id="",
        success=False,
        steps=0,
        steps_passed=0,
        step_passed=(),
        tests_touched=False,
        agent_exit=-1,
        timed_out=False,
        duration_seconds=0.0,
        provider=None,
        requests=0,
        compactions=0,
        compaction_pre_tokens=(),
        max_context=0,
        mean_context=0.0,
        uncached=0,
        cache_write=0,
        cache_read=0,
        output=0,
        cost_base=0.0,
        error=message,
    )


def ensure_repo(repo: str, ref: str, work_dir: Path) -> Path:
    """Clones the task repository once at a pinned version; runs are replicated from this copy."""
    cache_dir = work_dir / "repos"
    target = cache_dir / f"{Path(repo).stem}@{ref}"
    if not target.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        run_checked(
            ("git", "clone", "--quiet", "--depth", "1", "--branch", ref, repo, str(target)),
            cache_dir,
        )
    return target


def prepare_workspace(task: Task, repo_dir: Path, workspace: Path) -> None:
    """Copies the repository (without git history) and sets up the test environment."""
    shutil.copytree(repo_dir, workspace, ignore=shutil.ignore_patterns(".git"))
    python = workspace / ".venv" / "bin" / "python"
    run_checked(("uv", "venv", str(workspace / ".venv"), "--python", "3.12", "-q"), workspace)
    run_checked(("uv", "pip", "install", "--python", str(python), "-q", *task.packages), workspace)


def apply_mutation(workspace: Path, mutation: Mutation) -> None:
    """Applies a single bug if its anchor occurs exactly once in the file."""
    path = workspace / mutation.path
    source = path.read_text(encoding="utf-8")
    count = source.count(mutation.find)
    if count != 1:
        raise BenchError(f"{mutation.path}: mutation anchor occurs {count} times, expected once")
    path.write_text(source.replace(mutation.find, mutation.replace), encoding="utf-8")


def seal(workspace: Path) -> None:
    """Seals the workspace's current state as a fresh single-commit git history.

    The history is reset at every seal; the injected bug shows up in neither `git diff` nor
    `git log`, while the agent can still use git normally to see its own changes.
    """
    git_dir = workspace / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)
    run_checked(("git", "init", "-q"), workspace)
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(WORKSPACE_EXCLUDES, encoding="utf-8")
    run_checked(("git", "add", "-A"), workspace)
    run_checked(("git", *GIT_IDENTITY, "commit", "-q", "-m", "task"), workspace)


def tests_pass(task: Task, workspace: Path) -> bool:
    """Does the test suite pass?"""
    return run_suite(task, workspace).passed


def run_suite(task: Task, workspace: Path) -> SuiteRun:
    """Runs the test suite in the same allow-listed environment as the agent (the tests run the code
    the agent changed). A timeout (for example an infinite loop) counts as not passing."""
    try:
        completed = subprocess.run(
            list(task.test_command),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=TEST_TIMEOUT_SECONDS,
            env=tool_env(os.environ),
        )
    except subprocess.TimeoutExpired:
        return SuiteRun(passed=False, tail=f"test suite timed out after {TEST_TIMEOUT_SECONDS}s")
    output = completed.stdout + completed.stderr
    return SuiteRun(passed=completed.returncode == 0, tail=output[-TEST_TAIL_CHARS:])


def tool_env(base: Mapping[str, str]) -> dict[str, str]:
    """The environment of the test, git and uv processes: only the allow-listed variables. These
    processes run the agent's code and git configuration; they do not see the session's secret
    variables."""
    return {key: base[key] for key in ENV_ALLOWLIST if key in base}


def touched_test_files(workspace: Path, repo_dir: Path) -> tuple[str, ...]:
    """Test files changed, added or deleted relative to the untouched repository copy."""
    original = test_files(repo_dir)
    current = test_files(workspace)
    changed = {path for path in original.keys() & current.keys() if original[path] != current[path]}
    return tuple(sorted(changed | (original.keys() ^ current.keys())))


def test_files(root: Path) -> dict[str, bytes]:
    """Contents of the test files under the root (git, virtual environment and caches excluded)."""
    return {path: (root / path).read_bytes() for path in walk_files(root) if is_test_path(path)}


def walk_files(root: Path) -> tuple[str, ...]:
    """Relative paths of the files under the root; management directories are skipped."""
    files: list[str] = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in IGNORED_DIRS]
        files.extend(str(Path(directory, name).relative_to(root)) for name in names)
    return tuple(files)


def is_test_path(path: str) -> bool:
    """Is it a test file (tests/ directory, test_*.py, *_test.py, conftest.py)?"""
    parts = Path(path).parts
    name = parts[-1]
    return (
        bool(TEST_DIR_NAMES & set(parts[:-1]))
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def run_checked(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Runs the command; raises an error with its context if it fails."""
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=tool_env(os.environ),
    )
    if completed.returncode != 0:
        raise BenchError(
            f"command failed (exit {completed.returncode}) in {cwd}: {' '.join(command)}\n"
            f"stdout: {completed.stdout[-800:]}\nstderr: {completed.stderr[-800:]}"
        )
    return completed


def start_agent(
    spec: RunSpec, prompt: str, workspace: Path, run_dir: Path, timeout: int, step: int
) -> AgentRun:
    """Starts a new agent session."""
    if spec.agent is Agent.CLAUDE:
        session_id = str(uuid.uuid4())
        session_args = ("--session-id", session_id)
        return claude_call(
            spec, prompt, session_args, session_id, workspace, run_dir, timeout, step
        )
    command = ("codex", "exec", *codex_options(spec, workspace), prompt)
    env = agent_env(spec, run_dir, os.environ)
    process = codex_call(command, workspace, env, run_dir, timeout, step)
    return AgentRun(codex_thread_id(process.stdout), process.exit_code, process.timed_out, None)


def resume_agent(
    spec: RunSpec,
    session_id: str,
    prompt: str,
    workspace: Path,
    run_dir: Path,
    timeout: int,
    step: int,
) -> AgentRun:
    """Resumes the existing session with a new request; context accumulates from earlier steps."""
    if spec.agent is Agent.CLAUDE:
        session_args = ("--resume", session_id)
        return claude_call(
            spec, prompt, session_args, session_id, workspace, run_dir, timeout, step
        )
    command = ("codex", "exec", *codex_options(spec, workspace), "resume", session_id, prompt)
    env = agent_env(spec, run_dir, os.environ)
    process = codex_call(command, workspace, env, run_dir, timeout, step)
    return AgentRun(session_id, process.exit_code, process.timed_out, None)


def codex_call(
    command: Sequence[str],
    workspace: Path,
    env: dict[str, str],
    run_dir: Path,
    timeout: int,
    step: int,
) -> ProcessOutcome:
    """Runs Codex; a turn that failed provider-side (for example a usage limit) is an error."""
    process = run_process(command, workspace, env, timeout, run_dir, step)
    failure = codex_turn_failure(process.stdout)
    if failure is not None:
        raise BenchError(f"codex turn failed at step {step}: {failure}")
    return process


def codex_turn_failure(stdout: str) -> str | None:
    """The turn.failed message in the Codex event stream; None if absent."""
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-JSON warning line
        if isinstance(event, dict) and event.get("type") == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return message if isinstance(message, str) else "turn.failed without a message"
    return None


def claude_call(
    spec: RunSpec,
    prompt: str,
    session_args: tuple[str, str],
    session_id: str,
    workspace: Path,
    run_dir: Path,
    timeout: int,
    step: int,
) -> AgentRun:
    """Starts Claude Code with the user's login but without user settings, plugins and MCP servers
    (claude.ai connectors included)."""
    command = (
        "claude",
        "-p",
        prompt,
        *session_args,
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--output-format",
        "json",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--settings",
        json.dumps(claude_settings(spec)),
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        str(MAX_TURNS),
        "--allowedTools",
        CLAUDE_TOOLS,
        *plugin_args(spec, run_dir),
    )
    env = agent_env(spec, run_dir, os.environ)
    process = run_process(command, workspace, env, timeout, run_dir, step)
    failure = claude_failure(process.stdout, process.timed_out)
    if failure is not None:
        raise BenchError(f"claude call failed at step {step}: {failure}")
    return AgentRun(
        session_id, process.exit_code, process.timed_out, claude_reported_usage(process.stdout)
    )


def claude_failure(stdout: str, timed_out: bool) -> str | None:
    """Provider-side error (usage limit, API error); the time and turn limits are behaviour."""
    if timed_out:
        return None
    try:
        decoded: object = json.loads(stdout)
    except json.JSONDecodeError:
        return f"no JSON result; first bytes: {stdout[:200]!r}"
    records = decoded if isinstance(decoded, list) else [decoded]
    results = [r for r in records if isinstance(r, dict) and r.get("type") == "result"]
    if not results:
        return "no result record in the output"
    last = results[-1]
    if last.get("is_error") is True and last.get("subtype") != "error_max_turns":
        return f"{last.get('subtype')}: {str(last.get('result'))[:300]}"
    return None


def claude_settings(spec: RunSpec) -> dict[str, object]:
    """The variant's Claude Code settings: RTK's hook in the RTK arms, the window in window arms."""
    return merge_settings(
        [
            *([rtk_settings()] if spec.variant in RTK_VARIANTS else []),
            *([governor_env(spec.window)] if spec.variant in WINDOW_VARIANTS else []),
        ]
    )


def rtk_settings() -> dict[str, object]:
    """RTK's Claude Code hook: rewrites Bash commands to their compressing versions."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": RTK_HOOK_COMMAND}],
                }
            ]
        }
    }


def agent_env(spec: RunSpec, run_dir: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The agent process's environment: the allow-listed variables and the arm's own settings.

    In Claude Code the window is given directly as an environment variable in addition to the env
    block in the settings; in the mod arms CimriHook's home is the run's own directory.
    """
    missing = [key for key in REQUIRED_ENV if key not in base]
    if missing:
        raise BenchError(f"the environment lacks {missing}, which {spec.agent.value} needs")
    allowed = {key: base[key] for key in ENV_ALLOWLIST if key in base}
    if spec.agent is Agent.CODEX:
        return allowed
    window = (
        {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(spec.window)}
        if spec.variant in WINDOW_VARIANTS
        else {}
    )
    meter = {"CIMRIHOOK_HOME": str(run_dir / METER_DIR)} if spec.variant in MOD_VARIANTS else {}
    mask = {"CIMRIHOOK_MOD_MASK": "1"} if spec.variant is Variant.MASK else {}
    boundary = (
        {"CIMRIHOOK_MOD_BOUNDARY_TOKENS": str(BOUNDARY_TOKENS)}
        if spec.variant is Variant.BOUNDARY
        else {}
    )
    return allowed | window | meter | mask | boundary


def plugin_args(spec: RunSpec, run_dir: Path) -> tuple[str, ...]:
    """The option that loads the CimriHook mod from the run's own directory in the mod arms."""
    if spec.variant not in MOD_VARIANTS:
        return ()
    return ("--plugin-dir", str(write_mod(run_dir / "mod" / MOD_NAME)))


def claude_reported_usage(stdout: str) -> ReportedUsage | None:
    """Session totals in Claude Code's result record; None without one (for example a timeout).

    total_cost_usd and modelUsage also contain the totals of earlier calls when the session is
    resumed; compaction and helper model calls are included.
    """
    try:
        decoded: object = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    records = decoded if isinstance(decoded, list) else [decoded]
    results = [record for record in records if isinstance(record, dict)]
    last = next((record for record in reversed(results) if record.get("type") == "result"), None)
    if last is None:
        return None
    where = "claude result"
    result = json_object(last, where)
    models = json_object(result.get("modelUsage"), f"{where}.modelUsage")
    usages = [json_object(usage, f"{where}.modelUsage.{name}") for name, usage in models.items()]
    return ReportedUsage(
        cost_usd=float_field(result, "total_cost_usd", where),
        uncached=sum(int_field(usage, "inputTokens", where) for usage in usages),
        cache_write=sum(int_field(usage, "cacheCreationInputTokens", where) for usage in usages),
        cache_read=sum(int_field(usage, "cacheReadInputTokens", where) for usage in usages),
        output=sum(int_field(usage, "outputTokens", where) for usage in usages),
    )


def codex_options(spec: RunSpec, workspace: Path) -> tuple[str, ...]:
    """The variant's Codex options; no user config.toml, and window arms set the threshold."""
    window = (
        ("-c", f"model_auto_compact_token_limit={spec.window}")
        if spec.variant in WINDOW_VARIANTS
        else ()
    )
    return (
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "-C",
        str(workspace),
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-m",
        spec.model,
        "-c",
        f"model_reasoning_effort={spec.effort}",
        *window,
    )


def codex_thread_id(stdout: str) -> str:
    """The session (thread) id in the Codex JSON event stream."""
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-JSON warning line
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                return thread_id
    raise BenchError(f"codex output has no thread.started event; first bytes: {stdout[:300]!r}")


def run_process(
    command: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    run_dir: Path,
    step: int,
) -> ProcessOutcome:
    """Runs the process in its own process group; on a timeout the whole group is terminated."""
    with subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            timed_out = True
    (run_dir / f"agent.{step}.stdout").write_text(stdout, encoding="utf-8")
    (run_dir / f"agent.{step}.stderr").write_text(stderr, encoding="utf-8")
    return ProcessOutcome(process.returncode, timed_out, stdout)


def read_agent_logs(
    agent: Agent,
    session_id: str,
    reported: Sequence[ReportedUsage | None],
    steps: int,
) -> AgentLogs:
    """Measurements from the agent's records; no model requests means an unmeasurable run."""
    if agent is Agent.CLAUDE:
        transcript = claude_transcript(session_id)
        logs = AgentLogs(
            measure(claude_traces(transcript, session_id)),
            claude_provider(reported),
            claude_version(transcript),
        )
    else:
        rollout = codex_rollout(session_id)
        records = load_codex_records(rollout)
        logs = AgentLogs(
            measure((load_codex_trace(rollout),)),
            codex_provider(records, steps),
            records.cli_version,
        )
    if logs.measurement.requests == 0:
        raise BenchError(f"{agent.value} session {session_id} made no model requests")
    return logs


def claude_transcript(session_id: str) -> Path:
    """The session's main transcript."""
    main = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    if len(main) != 1:
        raise BenchError(f"expected one Claude transcript for {session_id}, found {len(main)}")
    return main[0]


def claude_traces(transcript: Path, session_id: str) -> tuple[SessionTrace, ...]:
    """The main transcript and the subagent transcripts."""
    subagents = sorted(
        path
        for path in transcript.parent.glob(f"{session_id}/subagents/**/*.jsonl")
        if path.name != WORKFLOW_JOURNAL
    )
    return tuple(load_claude_trace(path) for path in (transcript, *subagents))


def claude_version(transcript: Path) -> str:
    """The Claude Code version that wrote the transcript."""
    with transcript.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            version = None if entry is None else entry.get("version")
            if isinstance(version, str):
                return version
    raise BenchError(f"{transcript}: no entry carries a Claude Code version")


def claude_provider(reported: Sequence[ReportedUsage | None]) -> ProviderMeasurement | None:
    """The primary cost from Claude Code's cumulative per-step reports.

    If a step gave no report (a process ended by a timeout cannot record its cost), the later
    totals do not include that step either; the cost is then unknown and None is returned.
    """
    complete = [usage for usage in reported if usage is not None]
    if not complete or len(complete) != len(reported):
        return None
    last = complete[-1]
    return ProviderMeasurement(
        cost_by_step=tuple(usage.cost_usd for usage in complete),
        unit=USD,
        price_sheet=CLAUDE_PRICE_SHEET,
        uncached=last.uncached,
        cache_write=last.cache_write,
        cache_read=last.cache_read,
        output=last.output,
    )


def codex_rollout(thread_id: str) -> Path:
    """The Codex rollout record."""
    paths = sorted(CODEX_SESSIONS.rglob(f"rollout-*{thread_id}.jsonl"))
    if len(paths) != 1:
        raise BenchError(f"expected one Codex rollout for {thread_id}, found {len(paths)}")
    return paths[0]


def load_codex_records(path: Path) -> CodexRecords:
    """The token_usage_record entries in the rollout, grouped by task (task_started).

    These records include the compaction request, not the token_count events. If the same
    response id was written more than once, the last usage counts and it is counted in the task
    where it first appeared.
    """
    task_count = 0
    records: dict[str, tuple[int, Usage]] = {}
    version: str | None = None
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            payload = None if entry is None else entry.get("payload")
            if entry is None or not isinstance(payload, dict):
                continue
            kind = entry.get("type")
            if kind == "session_meta" and isinstance(payload.get("cli_version"), str):
                version = str(payload["cli_version"])
            elif kind == "event_msg" and payload.get("type") == "task_started":
                task_count += 1
            elif kind == "token_usage_record":
                if task_count == 0:
                    raise BenchError(f"{path}: token_usage_record before any task_started")
                response_id = text_field(payload, "response_id", f"{path} token_usage_record")
                usage = record_usage(payload, f"{path} {response_id}")
                task = records[response_id][0] if response_id in records else task_count
                records[response_id] = (task, usage)
    if version is None:
        raise BenchError(f"{path}: no session_meta with cli_version")
    if not records:
        raise BenchError(f"{path}: no token_usage_record entries (Codex older than 0.160?)")
    return CodexRecords(
        steps=tuple(
            tuple(usage for task, usage in records.values() if task == index)
            for index in range(1, task_count + 1)
        ),
        cli_version=version,
    )


def record_usage(payload: dict[str, object], where: str) -> Usage:
    """A token_usage_record usage; input_tokens includes the cache reads and writes."""
    usage = json_object(payload.get("usage"), f"{where}.usage")
    total_input = int_field(usage, "input_tokens", where)
    cached = int_field(usage, "cached_input_tokens", where)
    written = usage.get("cache_write_input_tokens")
    write = written if isinstance(written, int) else 0  # older versions do not report writes
    return Usage(
        uncached=total_input - cached - write,
        write_5m=write,
        write_1h=0,
        read=cached,
        output=int_field(usage, "output_tokens", where),
    )


def codex_provider(records: CodexRecords, steps: int) -> ProviderMeasurement:
    """Primary cost from per-response records; a step without model requests is a quota error."""
    if len(records.steps) != steps:
        raise BenchError(f"codex rollout has {len(records.steps)} turns, the run had {steps} steps")
    empty = [index for index, usages in enumerate(records.steps, start=1) if not usages]
    if empty:
        raise BenchError(
            f"codex made no model requests in steps {empty} (usage limit or failed turn)"
        )
    usages = [usage for step in records.steps for usage in step]
    return ProviderMeasurement(
        cost_by_step=tuple(
            itertools.accumulate(
                sum(exact_cost(usage, OPENAI) for usage in step) for step in records.steps
            )
        ),
        unit=BASE_INPUT_TOKENS,
        price_sheet=price_sheet_text(OPENAI),
        uncached=sum(usage.uncached for usage in usages),
        cache_write=sum(usage.write_5m + usage.write_1h for usage in usages),
        cache_read=sum(usage.read for usage in usages),
        output=sum(usage.output for usage in usages),
    )


def price_sheet_text(sheet: PriceSheet) -> str:
    """The price table definition written to the results (base input price = 1)."""
    return (
        f"{sheet.name}: uncached {sheet.uncached}, cached {sheet.read}, write {sheet.write_5m}, "
        f"output {sheet.output} x base input"
    )


def measure(traces: Sequence[SessionTrace]) -> Measurement:
    """Total usage from the request sequences and the cost with each session's own model prices."""
    requests = [usage for trace in traces for usage in trace.requests]
    contexts = [context_of(usage) for usage in requests]
    return Measurement(
        requests=len(requests),
        compactions=sum(len(trace.pre_compact_tokens) for trace in traces),
        compaction_pre_tokens=tuple(
            tokens for trace in traces for tokens in trace.pre_compact_tokens
        ),
        max_context=max(contexts, default=0),
        mean_context=statistics.fmean(contexts) if contexts else 0.0,
        uncached=sum(usage.uncached for usage in requests),
        cache_write=sum(usage.write_5m + usage.write_1h for usage in requests),
        cache_read=sum(usage.read for usage in requests),
        output=sum(usage.output for usage in requests),
        cost_base=sum(
            exact_cost(usage, trace.prices) for trace in traces for usage in trace.requests
        ),
    )


def load_results(results_dir: Path) -> tuple[RunResult, ...]:
    """All run results in a result set."""
    paths = sorted(results_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no run results in {results_dir}")
    return tuple(
        result_from_json(
            json_object(json.loads(path.read_text(encoding="utf-8")), str(path)), str(path)
        )
        for path in paths
    )


def load_run_limits(
    results_dir: Path, results: Sequence[RunResult]
) -> dict[str, tuple[LimitSample, ...]]:
    """The window readings of every measured meter run, by run id; a missing file is an error."""
    readings: dict[str, tuple[LimitSample, ...]] = {}
    for result in results:
        if result.error is not None or Variant(result.mechanism) not in METER_VARIANTS:
            continue
        path = limits_path(results_dir, result.run_id)
        if not path.is_file():
            raise BenchError(f"{path}: the readings of meter run {result.run_id} are missing")
        samples = tuple(session_samples(path))
        if not samples:
            raise BenchError(f"{path}: meter run {result.run_id} has no readings")
        readings[result.run_id] = samples
    return readings


def result_from_json(data: dict[str, object], where: str) -> RunResult:
    """Reads a result file, validating the types; a file in the old schema is an error."""
    schema = data.get("schema")
    if schema != RESULT_SCHEMA:
        raise BenchError(
            f"{where}: result schema {schema!r}, expected {RESULT_SCHEMA}; re-measure the set "
            "from the agents' logs with `cimrihook bench-remeasure --name <set>`"
        )
    return RunResult(
        schema=RESULT_SCHEMA,
        run_id=text_field(data, "run_id", where),
        task_id=text_field(data, "task_id", where),
        protocol=text_field(data, "protocol", where),
        agent=text_field(data, "agent", where),
        variant=text_field(data, "variant", where),
        mechanism=text_field(data, "mechanism", where),
        model=text_field(data, "model", where),
        effort=text_field(data, "effort", where),
        window=int_field(data, "window", where),
        effective_window=optional_int_field(data, "effective_window", where),
        isolation=text_field(data, "isolation", where),
        agent_version=text_field(data, "agent_version", where),
        repetition=int_field(data, "repetition", where),
        session_id=text_field(data, "session_id", where),
        success=bool_field(data, "success", where),
        steps=int_field(data, "steps", where),
        steps_passed=int_field(data, "steps_passed", where),
        step_passed=bool_list(data, "step_passed", where),
        tests_touched=bool_field(data, "tests_touched", where),
        agent_exit=int_field(data, "agent_exit", where),
        timed_out=bool_field(data, "timed_out", where),
        duration_seconds=float_field(data, "duration_seconds", where),
        provider=provider_from_json(data, where),
        requests=int_field(data, "requests", where),
        compactions=int_field(data, "compactions", where),
        compaction_pre_tokens=int_list(data, "compaction_pre_tokens", where),
        max_context=int_field(data, "max_context", where),
        mean_context=float_field(data, "mean_context", where),
        uncached=int_field(data, "uncached", where),
        cache_write=int_field(data, "cache_write", where),
        cache_read=int_field(data, "cache_read", where),
        output=int_field(data, "output", where),
        cost_base=float_field(data, "cost_base", where),
        error=optional_text_field(data, "error", where),
    )


def provider_from_json(data: dict[str, object], where: str) -> ProviderMeasurement | None:
    """The provider measurement in a result file; None if the cost is unknown."""
    if data.get("provider") is None:
        return None
    provider = json_object(data["provider"], f"{where}.provider")
    return ProviderMeasurement(
        cost_by_step=float_list(provider, "cost_by_step", where),
        unit=text_field(provider, "unit", where),
        price_sheet=text_field(provider, "price_sheet", where),
        uncached=int_field(provider, "uncached", where),
        cache_write=int_field(provider, "cache_write", where),
        cache_read=int_field(provider, "cache_read", where),
        output=int_field(provider, "output", where),
    )


def remeasure_results(results_dir: Path, runs_dir: Path) -> tuple[RunResult, ...]:
    """Re-measures recorded results from the agents' records with the current schema and saves them.

    The agents do not run again: the fields of the run itself (success, steps, duration,
    environment) are kept, and the measurements are recomputed from the Claude transcripts,
    Claude's per-step result records (runs_dir/<run_id>/agent.<step>.stdout) and the Codex
    rollouts. The old schema's single treatment arm ("cimrihook") is named after the mechanism
    that was actually on.
    """
    paths = sorted(results_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no run results in {results_dir}")
    results = tuple(
        remeasured_result(
            json_object(json.loads(path.read_text(encoding="utf-8")), str(path)), runs_dir
        )
        for path in paths
    )
    for path, result in zip(paths, results, strict=True):
        path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return results


def remeasured_result(data: dict[str, object], runs_dir: Path) -> RunResult:
    """A single result record, re-measured."""
    where = text_field(data, "run_id", "result")
    legacy = data.get("schema") is None
    agent = Agent(text_field(data, "agent", where))
    variant = text_field(data, "variant", where)
    identity = RunIdentity(
        run_id=where,
        task_id=text_field(data, "task_id", where),
        protocol=text_field(data, "protocol", where),
        agent=agent,
        variant=variant,
        mechanism=legacy_mechanism(agent, variant)
        if legacy
        else Variant(text_field(data, "mechanism", where)),
        model=text_field(data, "model", where),
        effort=text_field(data, "effort", where),
        window=int_field(data, "window", where),
        isolation=LEGACY_ISOLATION[agent] if legacy else text_field(data, "isolation", where),
        repetition=int_field(data, "repetition", where),
    )
    error = optional_text_field(data, "error", where)
    if error is not None:
        return unmeasured_result(identity, error)
    steps = int_field(data, "steps", where)
    steps_passed = int_field(data, "steps_passed", where)
    session_id = text_field(data, "session_id", where)
    reported = claude_reported_files(runs_dir / where, steps) if agent is Agent.CLAUDE else ()
    return measured_result(
        identity,
        RunBehaviour(
            session_id=session_id,
            success=bool_field(data, "success", where),
            steps=steps,
            steps_passed=steps_passed,
            step_passed=legacy_step_passed(steps, steps_passed)
            if legacy
            else bool_list(data, "step_passed", where),
            tests_touched=bool_field(data, "tests_touched", where),
            agent_exit=int_field(data, "agent_exit", where),
            timed_out=bool_field(data, "timed_out", where),
            duration_seconds=float_field(data, "duration_seconds", where),
        ),
        read_agent_logs(agent, session_id, reported, steps),
    )


def legacy_mechanism(agent: Agent, variant: str) -> Variant:
    """The mechanism that was actually on for an arm in the old schema."""
    if variant == Variant.BASELINE.value:
        return Variant.BASELINE
    if variant != LEGACY_VARIANT:
        raise BenchError(f"unknown legacy variant {variant!r}")
    return Variant.COMBINED if agent is Agent.CLAUDE else Variant.GOVERNOR


def legacy_step_passed(steps: int, steps_passed: int) -> tuple[bool, ...]:
    """The old schema keeps only the count; the order is known if all steps passed, else unknown."""
    return (True,) * steps if steps_passed == steps else ()


def claude_reported_files(run_dir: Path, steps: int) -> tuple[ReportedUsage | None, ...]:
    """Claude Code's cumulative reports from the step outputs in the run directory."""
    paths = [run_dir / f"agent.{step}.stdout" for step in range(1, steps + 1)]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise BenchError(f"Claude step outputs are missing, cost reports cannot be read: {missing}")
    return tuple(claude_reported_usage(path.read_text(encoding="utf-8")) for path in paths)


def scenario(result: RunResult) -> str:
    """The task and protocol together as named in the reports."""
    return f"{result.task_id}/{result.protocol}"


def render_bench_report(
    results: Sequence[RunResult],
    run_limits: Mapping[str, Sequence[LimitSample]],
    background: Sequence[LimitSample],
) -> str:
    """Cell summaries, scenario- and agent-level A/B comparisons, and the unmeasurable runs.

    The primary cost is at the provider level (Claude Code: USD, Codex: base input units with the
    price table) and includes the compaction requests; the 'transcript' column does not. The
    agent-level summary combines with equal weight only the scenarios in which both arms have the
    same number of measured runs; with fewer than two measurements in an arm, no confidence
    interval is given. The meter arms' window readings (by run id) and the readings of the other
    recorded sessions add the limit-use section.
    """
    measured = [result for result in results if result.error is None]
    errors = [result for result in results if result.error is not None]
    costed = [result for result in measured if result.provider is not None]
    lines = [
        f"CimriHook bench: {len(results)} runs, {len(errors)} unmeasured, "
        f"{len(measured) - len(costed)} without a provider cost",
        "Primary cost: Claude = total_cost_usd reported by Claude Code (USD); Codex = per-response "
        "usage records priced with the sheet below (base input units). Both include compaction "
        "requests; 'transcript' excludes them.",
        *sorted({f"  price sheet: {provider_of(result).price_sheet}" for result in costed}),
        *version_lines(measured),
        "Cells (geometric mean of costs, medians of the rest):",
        f"  {'agent':<7} {'scenario':<32} {'mechanism':<9} {'n':>2} {'runs ok':>7} {'steps':>7}"
        f" {'cost':>10} {'transcript':>10} {'req':>4} {'max ctx':>8} {'mean ctx':>8}"
        f" {'compact':>7} {'trigger':>8} {'window':>8} {'min':>5}",
        *(cell_line(cell) for cell in group_cells(measured)),
        "A/B per scenario vs baseline: provider cost as a ratio of geometric means with a 95% "
        "Welch t interval on log cost (none when an arm has fewer than two costed runs or no "
        "variation); run success over every measured run, so a run without a provider cost still "
        "counts; steps are descriptive only (the steps of a run are dependent):",
        *scenario_comparisons(measured),
        "A/B per agent and mechanism: cost is the equal-weight mean of balanced scenarios with two "
        "95% intervals (across scenarios, which generalises beyond them, and within these "
        "scenarios); non-inferiority uses the Newcombe interval of the run success difference:",
        *agent_comparisons(measured),
        "Cumulative provider cost at step checkpoints (ratio of geometric means vs baseline):",
        *checkpoint_comparisons(costed),
        "Codex price-sheet sensitivity (pooled ratio over cached x0.1/0.25 and output x4/6/8):",
        *codex_sensitivity(costed),
        *limit_use_lines(measured, run_limits, background),
        *(f"  unmeasured {result.run_id}: {result.error}" for result in errors),
    ]
    return "\n".join(lines)


def version_lines(measured: Sequence[RunResult]) -> list[str]:
    """Agent versions in the measured runs; a warning if one agent has several versions mixed."""
    versions = Counter((result.agent, result.agent_version or "unknown") for result in measured)
    agents = Counter(agent for agent, _ in versions)
    return [
        f"  agent version: {agent} {version} ({count} runs)"
        + (" - several versions are pooled in this set" if agents[agent] > 1 else "")
        for (agent, version), count in sorted(versions.items())
    ]


def provider_of(result: RunResult) -> ProviderMeasurement:
    """Measurement of a run that has a provider cost."""
    if result.provider is None:
        raise BenchError(f"{result.run_id} has no provider cost")
    return result.provider


def final_cost(result: RunResult) -> float:
    """The primary (provider-level) total cost of a run."""
    return provider_of(result).cost_by_step[-1]


def mechanism_rank(mechanism: str) -> int:
    """Order of the arms in the report: baseline, governor, codec, combined."""
    return list(Variant).index(Variant(mechanism))


def group_cells(results: Sequence[RunResult]) -> list[list[RunResult]]:
    """The (agent, scenario, mechanism) cells, in the report's order."""
    keys = sorted(
        {(result.agent, scenario(result), result.mechanism) for result in results},
        key=lambda key: (key[0], key[1], mechanism_rank(key[2])),
    )
    return [
        [result for result in results if (result.agent, scenario(result), result.mechanism) == key]
        for key in keys
    ]


def cell_line(cell: Sequence[RunResult]) -> str:
    """The row of an (agent, scenario, mechanism) cell."""
    first = cell[0]
    costed = [result for result in cell if result.provider is not None]
    cost = (
        format_cost(geometric_mean([final_cost(result) for result in costed]), costed[0])
        if costed
        else "-"
    )
    triggers = [tokens for result in cell for tokens in result.compaction_pre_tokens]
    windows = sorted({result.effective_window for result in cell if result.effective_window})
    return (
        f"  {first.agent:<7} {scenario(first):<32} {first.mechanism:<9} {len(cell):>2}"
        f" {f'{sum(r.success for r in cell)}/{len(cell)}':>7}"
        f" {f'{sum(r.steps_passed for r in cell)}/{sum(r.steps for r in cell)}':>7}"
        f" {cost:>10} {geometric_mean([r.cost_base for r in cell]):>10,.0f}"
        f" {statistics.median(r.requests for r in cell):>4.0f}"
        f" {statistics.median(r.max_context for r in cell):>8,.0f}"
        f" {statistics.median(r.mean_context for r in cell):>8,.0f}"
        f" {sum(r.compactions for r in cell):>7}"
        f" {f'{statistics.median(triggers):,.0f}' if triggers else '-':>8}"
        f" {','.join(str(window) for window in windows) or '-':>8}"
        f" {statistics.median(r.duration_seconds for r in cell) / 60:>5.1f}"
    )


def format_cost(value: float, result: RunResult) -> str:
    """Cost in the run's unit (USD or base input unit)."""
    if provider_of(result).unit == USD:
        return f"${value:,.3f}"
    return f"{value:,.0f}"


def arm(results: Sequence[RunResult], agent: str, label: str, mechanism: str) -> list[RunResult]:
    """The runs of one arm of a scenario."""
    return [
        result
        for result in results
        if result.agent == agent and scenario(result) == label and result.mechanism == mechanism
    ]


def treatments() -> tuple[str, ...]:
    """The arms other than baseline, in the report's order."""
    return tuple(variant.value for variant in Variant if variant is not Variant.BASELINE)


def interval_text(estimate: RatioEstimate) -> str:
    """The bounds of the interval; 'none' if there is no interval."""
    if estimate.low is None or estimate.high is None:
        return "none"
    return f"{estimate.low:.3f}-{estimate.high:.3f}"


def ratio_text(estimate: RatioEstimate) -> str:
    """The ratio and its interval."""
    return f"x{estimate.ratio:.3f} [{interval_text(estimate)}]"


def costed_runs(results: Sequence[RunResult]) -> list[RunResult]:
    """Runs with a recorded provider cost."""
    return [result for result in results if result.provider is not None]


def run_success_difference(
    base: Sequence[RunResult], treated: Sequence[RunResult]
) -> DifferenceEstimate:
    """Run success difference (treatment − baseline) and its Newcombe interval."""
    return rate_difference(
        sum(r.success for r in treated), len(treated), sum(r.success for r in base), len(base)
    )


def success_text(base: Sequence[RunResult], treated: Sequence[RunResult]) -> str:
    """Run success (every step passed, no test file touched), the interval of its difference, and
    step success, which is only descriptive."""
    difference = run_success_difference(base, treated)
    return (
        f"runs ok {sum(r.success for r in treated)}/{len(treated)} vs "
        f"{sum(r.success for r in base)}/{len(base)} (diff {100 * difference.difference:+.1f} pp "
        f"[{100 * difference.low:+.1f}, {100 * difference.high:+.1f}]); steps "
        f"{sum(r.steps_passed for r in treated)}/{sum(r.steps for r in treated)} vs "
        f"{sum(r.steps_passed for r in base)}/{sum(r.steps for r in base)}"
    )


def scenario_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """The cost ratio of each arm to baseline and its success, per scenario.

    The cost is computed only from runs with a provider cost, the success from all measured
    runs: a run whose cost could not be recorded (for example a timeout) is not dropped from the
    success comparison.
    """
    lines: list[str] = []
    for agent, label in sorted({(result.agent, scenario(result)) for result in measured}):
        base = arm(measured, agent, label, Variant.BASELINE.value)
        for mechanism in treatments():
            treated = arm(measured, agent, label, mechanism)
            if not base or not treated:
                continue
            base_costed, treated_costed = costed_runs(base), costed_runs(treated)
            cost = (
                ratio_text(
                    ratio_estimate(
                        [final_cost(r) for r in base_costed],
                        [final_cost(r) for r in treated_costed],
                    )
                )
                if base_costed and treated_costed
                else "none (no costed run in an arm)"
            )
            transcript = ratio_estimate([r.cost_base for r in base], [r.cost_base for r in treated])
            lines.append(
                f"  {agent:<7} {label:<32} {mechanism:<9} runs {len(base)}/{len(treated)} "
                f"(costed {len(base_costed)}/{len(treated_costed)}) cost {cost} transcript "
                f"x{transcript.ratio:.3f} {success_text(base, treated)}"
            )
    return lines


type ArmPair = tuple[list[RunResult], list[RunResult]]


def scenario_pairs(
    costed: Sequence[RunResult], agent: str, mechanism: str
) -> tuple[list[ArmPair], list[str]]:
    """Scenarios with both arms measured: pairs with equal n, and those left out for unequal n."""
    labels = sorted({scenario(r) for r in costed if r.agent == agent and r.mechanism == mechanism})
    pairs = [
        (
            label,
            arm(costed, agent, label, Variant.BASELINE.value),
            arm(costed, agent, label, mechanism),
        )
        for label in labels
    ]
    balanced = [(base, treated) for _, base, treated in pairs if base and len(base) == len(treated)]
    excluded = [label for label, base, treated in pairs if base and len(base) != len(treated)]
    return balanced, excluded


def noninferiority(base_runs: int, treated_runs: int, difference_low: float) -> str:
    """Non-inferiority decision for run success (a drop of at most NONINFERIORITY_MARGIN).

    Failing to show it does not mean the quality dropped; the interval is not narrow enough to
    exclude the margin.
    """
    if min(base_runs, treated_runs) < MIN_RUNS_FOR_VERDICT:
        return f"undecided (fewer than {MIN_RUNS_FOR_VERDICT} runs per arm)"
    if difference_low > -NONINFERIORITY_MARGIN:
        return "shown"
    return f"not shown (lower bound {100 * difference_low:+.1f} pp)"


def agent_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """Per agent and mechanism: the equal-weight cost ratio of the balanced scenarios, and the
    success and non-inferiority over all runs of the scenarios with both arms measured."""
    lines: list[str] = []
    for agent in sorted({result.agent for result in measured}):
        for mechanism in treatments():
            labels = [
                label
                for label in sorted(
                    {scenario(r) for r in measured if r.agent == agent and r.mechanism == mechanism}
                )
                if arm(measured, agent, label, Variant.BASELINE.value)
            ]
            if not labels:
                continue
            base_runs = [
                r for label in labels for r in arm(measured, agent, label, Variant.BASELINE.value)
            ]
            treated_runs = [r for label in labels for r in arm(measured, agent, label, mechanism)]
            balanced, excluded = scenario_pairs(costed_runs(measured), agent, mechanism)
            skipped = f"excluded unbalanced: {', '.join(excluded)}" if excluded else "none excluded"
            uncosted = (
                len(base_runs) + len(treated_runs) - len(costed_runs(base_runs + treated_runs))
            )
            difference = run_success_difference(base_runs, treated_runs)
            lines.append(
                f"  {agent:<7} {mechanism:<9} cost {pooled_text(balanced)} over {len(balanced)} "
                f"scenarios ({skipped}); {success_text(base_runs, treated_runs)}; "
                f"non-inferiority at -{100 * NONINFERIORITY_MARGIN:.0f} pp: "
                f"{noninferiority(len(base_runs), len(treated_runs), difference.low)}"
                + (
                    f"; {uncosted} runs without a provider cost are not in the cost"
                    if uncosted
                    else ""
                )
            )
    return lines


def pooled_text(balanced: Sequence[ArmPair]) -> str:
    """Equal-weight cost ratio of the balanced scenarios and its two intervals."""
    if not balanced:
        return "none (no balanced costed scenario)"
    costs = [
        ([final_cost(r) for r in base], [final_cost(r) for r in treated])
        for base, treated in balanced
    ]
    across = pooled_ratio(
        [math.log(ratio_estimate(base, treated).ratio) for base, treated in costs]
    )
    within = fixed_pooled_ratio(costs)
    return (
        f"x{across.ratio:.3f} [across scenarios {interval_text(across)}; "
        f"these scenarios {interval_text(within)}]"
    )


def checkpoint_comparisons(costed: Sequence[RunResult]) -> list[str]:
    """Cumulative cost ratios of the same runs at the steps' checkpoints."""
    lines: list[str] = []
    for agent, label in sorted({(result.agent, scenario(result)) for result in costed}):
        base = arm(costed, agent, label, Variant.BASELINE.value)
        for mechanism in treatments():
            treated = arm(costed, agent, label, mechanism)
            runs = [*base, *treated]
            points = [
                step
                for step in CHECKPOINT_STEPS
                if base and treated and all(len(provider_of(r).cost_by_step) >= step for r in runs)
            ]
            if not points:
                continue
            parts = [
                f"step {step} x"
                + format(
                    ratio_estimate(
                        [provider_of(r).cost_by_step[step - 1] for r in base],
                        [provider_of(r).cost_by_step[step - 1] for r in treated],
                    ).ratio,
                    ".3f",
                )
                for step in points
            ]
            lines.append(f"  {agent:<7} {label:<32} {mechanism:<9} {'  '.join(parts)}")
    return lines


def sheet_cost(result: RunResult, sheet: PriceSheet) -> float:
    """The cost of the provider's token totals with the given price table."""
    provider = provider_of(result)
    return (
        provider.uncached * sheet.uncached
        + provider.cache_write * sheet.write_5m
        + provider.cache_read * sheet.read
        + provider.output * sheet.output
    )


def codex_sensitivity(costed: Sequence[RunResult]) -> list[str]:
    """Sensitivity of the Codex ratio to the price table assumptions (balanced scenarios)."""
    lines: list[str] = []
    for mechanism in treatments():
        balanced, _ = scenario_pairs(costed, Agent.CODEX.value, mechanism)
        if not balanced:
            continue
        ratios = [
            pooled_ratio(
                [
                    math.log(
                        ratio_estimate(
                            [sheet_cost(r, sheet) for r in base],
                            [sheet_cost(r, sheet) for r in treated],
                        ).ratio
                    )
                    for base, treated in balanced
                ]
            ).ratio
            for sheet in CODEX_SENSITIVITY
        ]
        lines.append(
            f"  codex   {mechanism:<9} x{min(ratios):.3f} to x{max(ratios):.3f} over "
            f"{len(CODEX_SENSITIVITY)} price sheets"
        )
    return lines


def limit_use_lines(
    measured: Sequence[RunResult],
    run_limits: Mapping[str, Sequence[LimitSample]],
    background: Sequence[LimitSample],
) -> list[str]:
    """The points the meter arms' runs moved the subscription's windows, per scenario and window.

    A run's points are the window's percentage at its last reading minus its first, in one window
    period; a run the window reset under is left out of that window. The windows report whole
    percents, so each run is good to about one point, and whatever else ran on the account
    meanwhile moves the window too. The governor arm is set against the meter arm in points, in
    the spend between the same readings and in spend per point (if the account was otherwise idle
    and a list-price dollar fills the window the same in both arms, the points follow the spend),
    and in weights that set the other recorded sessions apart (see `weight_lines`).
    """
    metered = [result for result in measured if result.run_id in run_limits]
    if not metered:
        return []
    points_of = {
        result.run_id: run_points(run_limits[result.run_id], result.run_id) for result in metered
    }
    lines = [
        "Limit use of the meter arms (points a run moved the subscription's windows: the "
        "percentage at its last reading minus its first, in one window period, whole percents):"
    ]
    for agent, label in sorted({(result.agent, scenario(result)) for result in metered}):
        control = arm(metered, agent, label, Variant.METER.value)
        treated = arm(metered, agent, label, Variant.METER_GOVERNOR.value)
        kinds = sorted({p.kind for run in [*control, *treated] for p in points_of[run.run_id]})
        for kind in kinds:
            counted_control = kind_points_of(control, points_of, kind)
            counted_treated = kind_points_of(treated, points_of, kind)
            lines += [
                f"  {agent:<7} {label:<32} {WINDOW_NAMES.get(kind, kind)}",
                use_line(Variant.METER.value, len(control), counted_control),
                use_line(Variant.METER_GOVERNOR.value, len(treated), counted_treated),
                f"    {Variant.METER_GOVERNOR.value} vs {Variant.METER.value}: "
                f"{use_comparison(counted_control, counted_treated)}",
                *weight_lines(kind, control, treated, metered, run_limits, background),
            ]
    lines.append(
        "  Raw points include use outside these runs; the weights set the recorded sessions "
        "apart (--background), but use no session records (claude.ai, sessions without the mod) "
        "still raises them."
    )
    return lines


def weight_lines(
    kind: str,
    control: Sequence[RunResult],
    treated: Sequence[RunResult],
    metered: Sequence[RunResult],
    run_limits: Mapping[str, Sequence[LimitSample]],
    background: Sequence[LimitSample],
) -> list[str]:
    """How many points one list-price dollar moves a window, in each arm and in other sessions.

    The readings of the runs and of the background sessions are merged; between two consecutive
    whole-percent crossings of the window the points are exact, and the spend of each class is
    read off its sessions' cumulative spend (see `cimrihook.weights`). The classes are the two
    arms of this scenario and the other recorded sessions: the background and the runs of other
    scenarios. Only the time of this scenario's runs counts.
    """
    arms = [(Variant.METER.value, control), (Variant.METER_GOVERNOR.value, treated)]
    own_ids = {run.run_id for _, runs in arms for run in runs}
    own = [run_limits[run_id] for run_id in sorted(own_ids)]
    elsewhere = [run_limits[run.run_id] for run in metered if run.run_id not in own_ids]
    moments = [sample.time for samples in own for sample in samples]
    first, last = min(moments), max(moments)
    readings = [sample for samples in [*own, *elsewhere] for sample in samples] + list(background)
    classes = [[spend_curve(run_limits[run.run_id]) for run in runs] for _, runs in arms] + [
        [spend_curve(samples) for samples in [*sessions_of(background), *elsewhere]]
    ]
    spans = crossing_spans(window_crossings(readings, kind), classes, first, last)
    fit = fit_weights(spans, [*(name for name, _ in arms), OTHER_SESSIONS])
    if fit is None:
        return [
            f"    weights: none ({len(spans)} spans between whole-percent crossings are too few "
            "or too alike to tell the arms and the other sessions apart)"
        ]
    meanwhile = sessions_of([sample for sample in background if first <= sample.time <= last])
    lines = [
        f"    weights from {fit.spans} spans between whole-percent crossings "
        f"({fit.degrees} degrees of freedom, residual {fit.residual:.2f} points; "
        f"{len(meanwhile)} other sessions recorded meanwhile): points = weight x list-price spend",
        *(f"      {weight.name:<15} {weight_text(weight)}" for weight in fit.weights),
    ]
    pooled = fit_weights(pooled_spans(spans), [POOLED])
    if pooled is not None:
        lines.append(
            f"      {POOLED:<15} {weight_text(pooled.weights[0])} (one weight for every session)"
        )
    ratio = weight_ratio(fit, Variant.METER_GOVERNOR.value, Variant.METER.value)
    lines.append(
        f"    {Variant.METER_GOVERNOR.value} vs {Variant.METER.value}: "
        f"{weight_comparison(ratio, cost_ratio(control, treated))}"
    )
    uses = run_use_parts(fit, arms, run_limits)
    if uses:
        lines.append(f"    per run (weight x mean list-price spend): {', '.join(uses)}")
    return lines


def cost_ratio(control: Sequence[RunResult], treated: Sequence[RunResult]) -> RatioEstimate | None:
    """Provider cost of the treated arm over the control arm; None if an arm has no costed run."""
    costed_control, costed_treated = costed_runs(control), costed_runs(treated)
    if not costed_control or not costed_treated:
        return None
    return ratio_estimate(
        [final_cost(run) for run in costed_control], [final_cost(run) for run in costed_treated]
    )


def weight_comparison(weights: RatioEstimate | None, cost: RatioEstimate | None) -> str:
    """The weight ratio, the provider cost ratio and their product, the window use per task."""
    parts = [
        "weight none (an arm is missing or its weight is not positive)"
        if weights is None
        else f"weight {ratio_text(weights)}"
    ]
    if cost is not None:
        parts.append(f"provider cost {ratio_text(cost)}")
    if weights is not None and cost is not None:
        parts.append(f"window use x{weights.ratio * cost.ratio:.3f} (weight x cost)")
    return "; ".join(parts)


def run_use_parts(
    fit: WeightFit,
    arms: Sequence[tuple[str, Sequence[RunResult]]],
    run_limits: Mapping[str, Sequence[LimitSample]],
) -> list[str]:
    """Points a run of each arm takes: its weight times the arm's mean list-price spend per run."""
    parts: list[str] = []
    for name, runs in arms:
        weight = weight_of(fit, name)
        if weight is not None and runs:
            spend = statistics.fmean(spend_curve(run_limits[run.run_id]).usd[-1] for run in runs)
            parts.append(f"{name} {weight.weight * spend:.1f} points")
    return parts


def kind_points_of(
    runs: Sequence[RunResult], points_of: Mapping[str, Sequence[RunPoints]], kind: str
) -> list[RunPoints]:
    """The points of the runs that counted for one window kind."""
    return [points for run in runs for points in points_of[run.run_id] if points.kind == kind]


def use_line(mechanism: str, runs: int, counted: Sequence[RunPoints]) -> str:
    """One arm's points, spend between its readings and spend per point for a window."""
    if not counted:
        return f"    {mechanism:<15} 0 of {runs} runs counted"
    points = [run.points for run in counted]
    spend = statistics.fmean(run.usd for run in counted)
    per_point = (
        f"${sum(run.usd for run in counted) / sum(points):,.2f} per point"
        if statistics.fmean(points) >= RUN_MIN_POINTS
        else "too coarse per run"
    )
    return (
        f"    {mechanism:<15} {len(counted)} of {runs} runs  points {statistics.fmean(points):.1f}"
        f" ({min(points):.0f}-{max(points):.0f})  spend ${spend:,.2f}  {per_point}"
    )


def use_comparison(control: Sequence[RunPoints], treated: Sequence[RunPoints]) -> str:
    """Governor over meter in points, spend between the readings and spend per point."""
    if not control or not treated:
        return "none (an arm has no counted run)"
    if min(statistics.fmean(run.points for run in arm_runs) for arm_runs in (control, treated)) < (
        RUN_MIN_POINTS
    ):
        return f"none (under {RUN_MIN_POINTS:.0f} points per run: whole percents are too coarse)"
    if any(run.points <= 0 or run.usd <= 0 for run in [*control, *treated]):
        return "none (a run moved the window by less than a point or spent nothing)"
    points = ratio_estimate([run.points for run in control], [run.points for run in treated])
    spend = ratio_estimate([run.usd for run in control], [run.usd for run in treated])
    per_point = ratio_estimate(
        [run.usd / run.points for run in control], [run.usd / run.points for run in treated]
    )
    return (
        f"points {ratio_text(points)}; spend {ratio_text(spend)}; "
        f"spend per point {ratio_text(per_point)}"
    )


def render_calibration(results: Sequence[RunResult]) -> str:
    """Tests the simulator's policy estimate against the A/B result.

    The records of the baseline runs are replayed with the trigger point actually observed in the
    treatment arm; the cost of a compaction (the later context, the part that stays cached, the
    summary) is measured from the treatment arm's records. This leaves only the simulator's own
    assumptions (the agent's behaviour does not change, the cost accounting) to be tested. The
    estimate and the measurement are geometric mean ratios; the measurement is the provider-level
    primary cost.
    """
    costed = [result for result in results if result.error is None and result.provider is not None]
    lines = [
        "CimriHook calibration: simulated vs measured cost ratio (treatment / baseline)",
        f"  {'agent':<7} {'scenario':<32} {'mechanism':<9} {'trigger':>8} {'post':>7}"
        f" {'cached':>7} {'summary':>7} {'predicted':>9} {'measured':>9} {'error':>9}",
    ]
    for agent, label in sorted({(result.agent, scenario(result)) for result in costed}):
        base = arm(costed, agent, label, Variant.BASELINE.value)
        for mechanism in sorted(variant.value for variant in WINDOW_VARIANTS):
            treated = arm(costed, agent, label, mechanism)
            if base and treated:
                lines.append(calibration_line(agent, label, mechanism, base, treated))
    lines.extend(
        [
            f"  acceptance: |error| <= {100 * CALIBRATION_TOLERANCE:.0f} points of the measured "
            "ratio. The compaction parameters are medians of the very treatment runs being "
            "predicted (in-sample), so 'ok' only checks the cost accounting; when the measured "
            "interval is wider than the tolerance, the check cannot tell a good simulator from a "
            "bad one.",
        ]
    )
    return "\n".join(lines)


def calibration_line(
    agent: str,
    label: str,
    mechanism: str,
    base: Sequence[RunResult],
    treated: Sequence[RunResult],
) -> str:
    """The estimate and measurement row of a scenario."""
    prefix = f"  {agent:<7} {label:<32} {mechanism:<9}"
    treated_traces = [trace for result in treated for trace in run_traces(result)]
    pre = [tokens for trace in treated_traces for tokens in trace.pre_compact_tokens]
    post = [tokens for trace in treated_traces for tokens in trace.post_compact_tokens]
    cached = [tokens for trace in treated_traces for tokens in trace.post_compact_cached]
    summary = [tokens for trace in treated_traces for tokens in trace.summary_tokens]
    if not (pre and post and cached and summary):
        return f"{prefix} no compaction observed in the treatment runs; nothing to calibrate"
    base_traces = [run_traces(result) for result in base]
    model = CostModel(
        # Codex does not price cache writes separately: new input is at the uncached input price.
        write_weight=OPENAI.uncached
        if agent == Agent.CODEX.value
        else average_write_weight(
            total_usage([usage for traces in base_traces for t in traces for usage in t.requests])
        ),
        post_compact_tokens=int(statistics.median(post)),
        post_compact_cached=int(statistics.median(cached)),
        summary_tokens=int(statistics.median(summary)),
        refetch_tokens=0,
        refetch_requests=0,
    )
    trigger = int(statistics.median(pre))
    policy = Policy("treatment trigger", trigger, None)
    predicted = geometric_mean(
        [
            simulated_cost(traces, policy, model) / simulated_cost(traces, OBSERVED, model)
            for traces in base_traces
        ]
    )
    estimate = ratio_estimate([final_cost(r) for r in base], [final_cost(r) for r in treated])
    error = predicted - estimate.ratio
    verdict = "ok" if abs(error) <= CALIBRATION_TOLERANCE else "OFF"
    return (
        f"{prefix} {trigger:>8,} {model.post_compact_tokens:>7,} {model.post_compact_cached:>7,}"
        f" {model.summary_tokens:>7,} {f'x{predicted:.3f}':>9} {f'x{estimate.ratio:.3f}':>9}"
        f" {f'{100 * error:+.1f} pp':>9} {verdict} (measured 95% {interval_text(estimate)})"
    )


def run_traces(result: RunResult) -> tuple[SessionTrace, ...]:
    """The context windows in a run's agent records."""
    if result.agent == Agent.CLAUDE.value:
        return claude_traces(claude_transcript(result.session_id), result.session_id)
    return (load_codex_trace(codex_rollout(result.session_id)),)


def simulated_cost(traces: Sequence[SessionTrace], policy: Policy, model: CostModel) -> float:
    """The cost of the records replayed with a policy (each session with its own prices)."""
    return sum(simulate_trace(trace, policy, model, trace.prices)[0] for trace in traces)


def json_object(value: object, where: str) -> dict[str, object]:
    """Expects a JSON object."""
    if not isinstance(value, dict):
        raise BenchError(f"{where}: expected a JSON object")
    return {str(key): item for key, item in value.items()}


def text_field(data: dict[str, object], key: str, where: str) -> str:
    """Required text field."""
    value = data.get(key)
    if not isinstance(value, str):
        raise BenchError(f"{where}: field {key!r} must be a string")
    return value


def optional_text_field(data: dict[str, object], key: str, where: str) -> str | None:
    """Optional text field; None if absent or null."""
    if data.get(key) is None:
        return None
    return text_field(data, key, where)


def int_field(data: dict[str, object], key: str, where: str) -> int:
    """Required integer field."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError(f"{where}: field {key!r} must be an integer")
    return value


def float_field(data: dict[str, object], key: str, where: str) -> float:
    """Required number field."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BenchError(f"{where}: field {key!r} must be a number")
    return float(value)


def optional_int_field(data: dict[str, object], key: str, where: str) -> int | None:
    """Optional integer field; None if absent or null."""
    if data.get(key) is None:
        return None
    return int_field(data, key, where)


def int_list(data: dict[str, object], key: str, where: str) -> tuple[int, ...]:
    """Integer list field."""
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise BenchError(f"{where}: field {key!r} must be a list of integers")
    return tuple(int(item) for item in value)


def float_list(data: dict[str, object], key: str, where: str) -> tuple[float, ...]:
    """Number list field."""
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int | float) and not isinstance(item, bool) for item in value
    ):
        raise BenchError(f"{where}: field {key!r} must be a list of numbers")
    return tuple(float(item) for item in value)


def bool_list(data: dict[str, object], key: str, where: str) -> tuple[bool, ...]:
    """Boolean list field."""
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, bool) for item in value):
        raise BenchError(f"{where}: field {key!r} must be a list of booleans")
    return tuple(bool(item) for item in value)


def bool_field(data: dict[str, object], key: str, where: str) -> bool:
    """Required boolean field."""
    value = data.get(key)
    if not isinstance(value, bool):
        raise BenchError(f"{where}: field {key!r} must be a boolean")
    return value


def text_list(data: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    """Text list field."""
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BenchError(f"{where}: field {key!r} must be a list of strings")
    return tuple(str(item) for item in value)


def object_list(data: dict[str, object], key: str, where: str) -> tuple[dict[str, object], ...]:
    """Object list field."""
    value = data.get(key)
    if not isinstance(value, list):
        raise BenchError(f"{where}: field {key!r} must be a list of objects")
    return tuple(json_object(item, f"{where}.{key}") for item in value)
