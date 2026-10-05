"""Immutable preparation study definitions and evidence-only target selection."""

import json
import math
import random
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from cimrihook.bench import (
    Agent,
    Mutation,
    Task,
    int_field,
    json_object,
    load_task,
    object_list,
    text_field,
    text_list,
)
from cimrihook.errors import BenchError
from cimrihook.preparation import SourceTarget

STUDY_KIND: Final = "preparation-ab"
SOURCE_BUDGET: Final = 24_000
SEED: Final = 20_261_005
TRACE_FRAME: Final = re.compile(r'File "([^"\n]+\.py)", line (\d+)')
PYTEST_FRAME: Final = re.compile(r"(?m)(?:^|\s)([^\s:\n]+\.py):(\d+)(?=[:\s])")
TEST_NODE: Final = re.compile(r"(?<![\w/])((?:tests?/)[^\s:]+\.py)(?=::)")


class Arm(StrEnum):
    """The four target policies, without changing the provider governor."""

    CONTROL = "control"
    ORACLE = "oracle"
    AUTO = "auto"
    WRONG = "wrong"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A supported source location and the public evidence that selected it."""

    path: str
    line: int
    rank: int
    evidence: str


@dataclass(frozen=True, slots=True)
class Selection:
    """Empty targets are an observed lack of evidence, not an error fallback."""

    targets: tuple[SourceTarget, ...]
    candidates: tuple[Candidate, ...]
    rejected: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StudyStep:
    """Host-only mutations and target policies for one invocation."""

    prompt: str
    mutations: tuple[Mutation, ...]
    oracle: tuple[str, ...]
    wrong: tuple[str, ...]
    wrong_offsets: tuple[tuple[int, int], ...]
    public_check: str
    hidden_check: str
    reference: str


@dataclass(frozen=True, slots=True)
class StudyTask:
    """A pinned source task with private host acceptance and sequential steps."""

    task: Task
    commit: str
    roots: tuple[str, ...]
    steps: tuple[StudyStep, ...]


@dataclass(frozen=True, slots=True)
class Job:
    """One scheduled episode, with reflected repeat placement."""

    task_id: str
    agent: Agent
    arm: Arm
    repetition: int
    position: int

    @property
    def id(self) -> str:
        """A stable filename identity independent of agent outcomes."""
        return f"{self.task_id}.{self.agent.value}.{self.arm.value}.r{self.repetition}"


@dataclass(frozen=True, slots=True)
class Calibration:
    """Frozen account fit; the weekly coefficient is a labeled pooled proxy."""

    observed_at: str
    input_weight: float
    output_weight: float
    weekly_weight: float
    weekly_low: float
    weekly_high: float


@dataclass(frozen=True, slots=True)
class Allowance:
    """Cumulative launch allowances, not exact provider billing ceilings."""

    claude_usd: float
    codex_five_hour: float
    codex_weekly_proxy: float
    reserve_claude_call: float
    reserve_codex_five_hour_call: float
    reserve_codex_weekly_call: float


@dataclass(frozen=True, slots=True)
class Study:
    """The committed manifest must match before any provider generation starts."""

    stage: str
    design_commit: str
    execution_approved: bool
    task_paths: tuple[str, ...]
    tasks: tuple[StudyTask, ...]
    jobs: tuple[Job, ...]
    claude_model: str
    codex_model: str
    claude_version: str
    codex_version: str
    effort: str
    window: int
    timeout: int
    claude_call_limit: float
    calibration: Calibration
    allowance: Allowance


@dataclass(frozen=True, slots=True)
class Quality:
    """All four acceptance dimensions are retained even for failed attempts."""

    public_passed: bool
    tests_unchanged: bool
    hidden_passed: bool
    reference_match: bool

    @property
    def accepted(self) -> bool:
        """An agent declaration is not an acceptance dimension."""
        return all(asdict(self).values())


@dataclass(frozen=True, slots=True)
class Consumption:
    """Whole-session counters; reasoning is a subset diagnostic of output."""

    usd: float | None
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    reasoning: int | None
    requests: int | None
    compactions: int | None
    max_context: int | None


@dataclass(frozen=True, slots=True)
class StepResult:
    """A generation attempt, including cost on non-successful exits."""

    step: int
    session_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    exit_code: int
    stop_reason: str | None
    consumption: Consumption | None
    quality: Quality
    selected_targets: tuple[str, ...]
    source_bytes: int
    packet_bytes: int
    artifact_dir: str


@dataclass(frozen=True, slots=True)
class Episode:
    """Uninvoked downstream steps consume nothing; the failed episode is retained."""

    job: Job
    steps: tuple[StepResult, ...]
    planned_steps: int
    uninvoked_reason: str | None

    @property
    def accepted(self) -> bool:
        """Every scheduled step must complete and pass all host checks."""
        return len(self.steps) == self.planned_steps and all(
            s.quality.accepted and s.stop_reason is None and s.exit_code == 0 for s in self.steps
        )


def relative_evidence_path(raw: str, workspace: str) -> str | None:
    """Normalize inside-workspace frames; external/traversal paths are rejected."""
    value = raw.replace("\\", "/")
    prefix = workspace.rstrip("/") + "/"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    if value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


def source_path(path: str, roots: Sequence[str], inventory: Sequence[str]) -> bool:
    """Inventory and declared production roots are the selector's only source inputs."""
    return path in inventory and any(path.startswith(root.rstrip("/") + "/") for root in roots)


def select_targets(
    output: str, workspace: str, roots: Sequence[str], inventory: Sequence[str]
) -> Selection:
    """Select from failed traceback paths/lines and unambiguous failing test module names."""
    candidates: list[Candidate] = []
    rejected: list[str] = []
    test_paths: set[str] = set()
    frames = (*TRACE_FRAME.findall(output), *PYTEST_FRAME.findall(output))
    for raw, number in frames:
        path = relative_evidence_path(raw, workspace)
        if path is None:
            rejected.append(f"external or traversal frame: {raw}")
        elif source_path(path, roots, inventory) and int(number) > 0:
            candidates.append(Candidate(path, int(number), 100, f"frame:{raw}:{number}"))
        elif Path(path).name.startswith("test_"):
            test_paths.add(path)
        else:
            rejected.append(f"unsupported frame: {raw}:{number}")
    test_paths.update(TEST_NODE.findall(output))
    for path in sorted(test_paths):
        name = Path(path).name
        if not name.startswith("test_"):
            continue
        matches = sorted(
            item
            for item in inventory
            if Path(item).name == name[5:] and source_path(item, roots, inventory)
        )
        if len(matches) == 1:
            candidates.append(Candidate(matches[0], 1, 50, f"test-module:{path}"))
        else:
            rejected.append(f"test-module:{path}: {len(matches)} matching sources")
    ranked = sorted(candidates, key=lambda c: (-c.rank, c.path, c.line, c.evidence))
    unique: dict[tuple[str, int], Candidate] = {}
    for candidate in ranked:
        unique.setdefault((candidate.path, candidate.line), candidate)
    ordered = tuple(unique.values())
    targets = tuple(
        SourceTarget(c.path, max(1, c.line - 30), c.line + 30 if c.rank == 100 else 60, None)
        for c in ordered[:4]
    )
    rejected.extend(f"candidate limit:{c.path}:{c.line}" for c in ordered[4:])
    return Selection(targets, ordered, tuple(sorted(set(rejected))))


def load_study_task(path: Path) -> StudyTask:
    """Parse a study companion without letting host metadata enter auto selection."""
    data = json_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    if data.get("kind") != STUDY_KIND:
        raise BenchError(f"{path}: expected kind={STUDY_KIND}")
    steps = tuple(
        StudyStep(
            text_field(step, "prompt", str(path)),
            tuple(
                Mutation(
                    text_field(m, "path", str(path)),
                    text_field(m, "find", str(path)),
                    text_field(m, "replace", str(path)),
                )
                for m in object_list(step, "mutations", str(path))
            ),
            text_list(step, "oracle", str(path)),
            text_list(step, "wrong", str(path)),
            tuple(
                (int_field(item, "first", str(path)), int_field(item, "last", str(path)))
                for item in object_list(step, "wrong_offsets", str(path))
            ),
            text_field(step, "public_check", str(path)),
            text_field(step, "hidden_check", str(path)),
            text_field(step, "reference", str(path)),
        )
        for step in object_list(data, "study_steps", str(path))
    )
    commit = text_field(data, "commit", str(path))
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not steps:
        raise BenchError(f"{path}: invalid pinned commit or empty steps")
    task = load_task(path)
    for step in steps:
        if len(step.wrong) != len(step.wrong_offsets):
            raise BenchError(f"{path}: every wrong target needs a frozen relative range")
        for mutation in step.mutations:
            relative = PurePosixPath(mutation.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise BenchError(f"{path}: unsafe step mutation {mutation.path}")
    return StudyTask(task, commit, text_list(data, "source_roots", str(path)), steps)


def schedule(tasks: Sequence[StudyTask], stage: str) -> tuple[Job, ...]:
    """Serial mirrored episodes; never reverse an already palindromic full sequence."""
    arms = tuple(Arm)
    if stage == "pilot":
        if len(tasks) != 1 or len(tasks[0].steps) != 1:
            raise BenchError("pilot requires exactly one single-step task")
        return tuple(
            Job(tasks[0].task.id, agent, arm, 1, index + 1)
            for agent in Agent
            for index, arm in enumerate(arms if agent is Agent.CLAUDE else arms[::-1])
        )
    if stage != "full" or len(tasks) != 6:
        raise BenchError("full study requires six tasks; unknown study stages are rejected")
    indexed = list(enumerate(tasks))
    random.Random(SEED).shuffle(indexed)
    jobs: list[Job] = []
    for task_index, task in indexed:
        rotation = task_index % 4
        base = arms[rotation:] + arms[:rotation]
        agents = tuple(Agent) if task_index % 2 == 0 else tuple(Agent)[::-1]
        for agent in agents:
            first = base if agent is Agent.CLAUDE else base[::-1]
            jobs.extend(
                Job(task.task.id, agent, arm, 1 if i < 4 else 2, i + 1)
                for i, arm in enumerate(first + first[::-1])
            )
    return tuple(jobs)


def numeric(data: dict[str, object], key: str, where: str) -> float:
    """Finite nonnegative JSON numbers, without accepting booleans or a zero fallback."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BenchError(f"{where}.{key}: expected a number")
    if not math.isfinite(value) or value < 0:
        raise BenchError(f"{where}.{key}: expected a finite nonnegative number")
    return float(value)


def load_study(path: Path, root: Path) -> Study:
    """Require exact protocol configuration and regenerate the prespecified schedule."""
    where = str(path)
    data = json_object(json.loads(path.read_text(encoding="utf-8")), where)
    if data.get("kind") != STUDY_KIND or data.get("schema_version") != 1:
        raise BenchError(f"{path}: unsupported preparation study schema")
    approved = data.get("execution_approved")
    if not isinstance(approved, bool):
        raise BenchError(f"{path}: missing execution approval field")
    paths = text_list(data, "task_paths", where)
    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
        raise BenchError(f"{path}: task paths must stay inside the project")
    tasks = tuple(load_study_task(root / p) for p in paths)
    if len({task.task.id for task in tasks}) != len(tasks):
        raise BenchError(f"{path}: duplicate task IDs")
    fit = json_object(data.get("calibration"), f"{where}.calibration")
    budget = json_object(data.get("allowance"), f"{where}.allowance")
    stage = text_field(data, "stage", where)
    study = Study(
        stage,
        text_field(data, "design_commit", where),
        approved,
        paths,
        tasks,
        schedule(tasks, stage),
        text_field(data, "claude_model", where),
        text_field(data, "codex_model", where),
        text_field(data, "claude_version", where),
        text_field(data, "codex_version", where),
        text_field(data, "effort", where),
        int_field(data, "window", where),
        int_field(data, "timeout", where),
        numeric(data, "claude_call_limit", where),
        Calibration(
            text_field(fit, "observed_at", where),
            numeric(fit, "input_weight", where),
            numeric(fit, "output_weight", where),
            numeric(fit, "weekly_weight", where),
            numeric(fit, "weekly_low", where),
            numeric(fit, "weekly_high", where),
        ),
        Allowance(*(numeric(budget, name, where) for name in Allowance.__dataclass_fields__)),
    )
    if study.window != 183_000 or study.effort != "medium" or study.timeout != 300:
        raise BenchError(f"{path}: configuration differs from the registered protocol")
    if stage == "pilot" and (
        tasks[0].task.id != "id-salt-rotation" or study.claude_call_limit != 0.5
    ):
        raise BenchError(f"{path}: pilot task or call ceiling differs from registration")
    return study


def codex_units(consumption: Consumption, output_weight: float) -> float:
    """Fixed API-equivalent units with explicit output sensitivity."""
    return (
        consumption.uncached
        + 1.25 * consumption.cache_write
        + 0.1 * consumption.cache_read
        + output_weight * consumption.output
    )


def codex_effective_input(consumption: Consumption) -> float:
    """Doctor's uncached class includes cache writes; match that fitted definition exactly."""
    return consumption.uncached + consumption.cache_write + 0.1 * consumption.cache_read


def codex_points(consumption: Consumption, calibration: Calibration) -> tuple[float, float]:
    """Measured five-hour prediction and separately labeled weekly pooled proxy."""
    effective_input = codex_effective_input(consumption)
    return (
        effective_input * calibration.input_weight + consumption.output * calibration.output_weight,
        (effective_input + 6 * consumption.output) * calibration.weekly_weight,
    )


def study_plan_json(study: Study) -> str:
    """A schedule and call budget without preparing workspaces or starting providers."""
    by_id = {task.task.id: task for task in study.tasks}
    return json.dumps(
        {
            "kind": STUDY_KIND,
            "stage": study.stage,
            "execution_approved": study.execution_approved,
            "design_commit": study.design_commit,
            "episodes": len(study.jobs),
            "generation_invocations": sum(len(by_id[j.task_id].steps) for j in study.jobs),
            "schedule": [asdict(job) | {"id": job.id} for job in study.jobs],
            "allowance": asdict(study.allowance),
            "calibration": asdict(study.calibration),
        },
        indent=2,
        allow_nan=False,
    )
