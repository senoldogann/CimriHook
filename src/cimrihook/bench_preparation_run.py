"""Serial preparation episodes with native quota stops and retained provider artifacts."""

import json
import logging
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cimrihook.bench import (
    CLAUDE_TOOLS,
    Agent,
    Protocol,
    RunSpec,
    Variant,
    agent_env,
    claude_failure,
    claude_provider,
    claude_reported_usage,
    claude_settings,
    codex_options,
    codex_provider,
    codex_rollout,
    codex_thread_id,
    codex_turn_failure,
    json_object,
    load_codex_records,
    read_agent_logs,
)
from cimrihook.bench_preparation import (
    SOURCE_BUDGET,
    Arm,
    Consumption,
    Episode,
    Job,
    Quality,
    Selection,
    StepResult,
    Study,
    StudyTask,
    codex_points,
    select_targets,
    study_plan_json,
)
from cimrihook.bench_preparation_stats import episode_cost, load_episode
from cimrihook.bench_preparation_tasks import (
    fixture_workspace,
    freeze_fixture,
    full_suite,
    host_quality,
    inject_step,
    pinned_repo,
    protected_test_files,
    source_hashes,
    source_inventory,
    target_source_bytes,
    wrong_source_bytes,
    wrong_targets,
)
from cimrihook.errors import BenchError, PreparationError, TranscriptError
from cimrihook.preparation import (
    SourceTarget,
    build_packet,
    digest,
    parse_target,
    physical_lines,
    read_literal,
    render_packet,
    validate_packet,
)
from cimrihook.quota import (
    QuotaError,
    QuotaSnapshot,
    read_claude_quota,
    read_codex_quota,
)

type QuotaReader = Callable[[Agent], QuotaSnapshot]
POLL_SECONDS: Final = 30.0
LOGGER: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuardedProcess:
    """A guard stop is an outcome, not a discarded exception."""

    stdout: str
    exit_code: int
    reason: str | None
    started_at: str
    finished_at: str
    duration_seconds: float


def read_quota(agent: Agent) -> QuotaSnapshot:
    """Retry the read-only control call once with a warning, never a substitute metric."""
    reader = read_claude_quota if agent is Agent.CLAUDE else read_codex_quota
    try:
        return reader(10.0)
    except QuotaError:
        LOGGER.warning("quota_control_retry", extra={"agent": agent.value, "timeout": 10.0})
        return reader(10.0)


def quota_health_problem(snapshot: QuotaSnapshot, now: datetime) -> str | None:
    """Reject absent, stale or incomplete readings and an exhausted primary window."""
    if not snapshot.available:
        return f"{snapshot.provider}: native quota unavailable"
    stamp = datetime.fromisoformat(snapshot.observed_at)
    if stamp.tzinfo is None or not -5 <= (now - stamp).total_seconds() <= 60:
        return f"{snapshot.provider}: stale or invalid quota timestamp {snapshot.observed_at}"
    weekly = tuple(w for w in snapshot.windows if w.duration_minutes == 10_080)
    primary = tuple(w for w in snapshot.windows if w.duration_minutes == 300)
    if not weekly or not primary:
        return f"{snapshot.provider}: missing weekly or five-hour quota window"
    if any(w.used_percent >= 95 for w in primary):
        return f"{snapshot.provider}: fewer than five primary-window points remain"
    return None


def quota_problem(snapshot: QuotaSnapshot, now: datetime) -> str | None:
    """No new generation at exactly 85% in any reported weekly window."""
    health = quota_health_problem(snapshot, now)
    if health is not None:
        return health
    exceeded = [
        w for w in snapshot.windows if w.duration_minutes == 10_080 and w.used_percent >= 85
    ]
    return (
        f"{snapshot.provider}: weekly launch threshold reached: "
        f"{[(w.id, w.used_percent) for w in exceeded]}"
        if exceeded
        else None
    )


def active_quota_problem(snapshot: QuotaSnapshot, now: datetime) -> str | None:
    """Terminate the active process only when weekly use exceeds 85%."""
    health = quota_health_problem(snapshot, now)
    if health is not None:
        return health
    exceeded = [w for w in snapshot.windows if w.duration_minutes == 10_080 and w.used_percent > 85]
    return (
        f"{snapshot.provider}: weekly hard stop exceeded: "
        f"{[(w.id, w.used_percent) for w in exceeded]}"
        if exceeded
        else None
    )


def quota_reset_problem(before: QuotaSnapshot, after: QuotaSnapshot) -> str | None:
    """A reset change beyond timestamp rounding interrupts the approved block."""
    previous = {window.id: window for window in before.windows}
    for window in after.windows:
        prior = previous.get(window.id)
        if prior is None or prior.resets_at is None or window.resets_at is None:
            continue
        delta = datetime.fromisoformat(window.resets_at) - datetime.fromisoformat(prior.resets_at)
        if abs(delta.total_seconds()) > 60:
            return f"{after.provider}: {window.id} reset boundary changed during generation"
    return None


def append_quota(path: Path, snapshot: QuotaSnapshot, phase: str) -> None:
    """Preserve account observations without exposing account fingerprints in reports."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": phase, **asdict(snapshot)}, allow_nan=False) + "\n")


def terminate_group(process: subprocess.Popen[str]) -> None:
    """Stop the child and its tool subprocesses; races with normal exit are explicit."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                raise


def guarded_process(
    command: Sequence[str],
    prompt: str,
    workspace: Path,
    env: Mapping[str, str],
    artifact: Path,
    agents: Sequence[Agent],
    timeout: float,
    poll_seconds: float,
    quota_reader: QuotaReader,
) -> GuardedProcess:
    """Poll quota while the child runs and archive output after any stop, without retrying it."""
    before: dict[Agent, QuotaSnapshot] = {}
    for agent in agents:
        snapshot = quota_reader(agent)
        before[agent] = snapshot
        append_quota(artifact / "quota.jsonl", snapshot, "before")
        problem = quota_problem(snapshot, datetime.now(UTC))
        if problem is not None:
            raise BenchError(problem)
    started = datetime.now(UTC).isoformat()
    start = time.monotonic()
    reason: str | None = None
    with subprocess.Popen(
        list(command),
        cwd=workspace,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        pending_input: str | None = prompt
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                reason = f"generation timeout after {timeout}s"
                terminate_group(process)
                stdout, stderr = process.communicate()
                break
            try:
                stdout, stderr = process.communicate(
                    input=pending_input, timeout=min(poll_seconds, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                for agent in agents:
                    try:
                        snapshot = quota_reader(agent)
                    except QuotaError as error:
                        reason = f"quota monitoring failed: {error}"
                        break
                    append_quota(artifact / "quota.jsonl", snapshot, "during")
                    reason = active_quota_problem(
                        snapshot, datetime.now(UTC)
                    ) or quota_reset_problem(before[agent], snapshot)
                    if snapshot.account_fingerprint != before[agent].account_fingerprint:
                        reason = "provider account changed during generation"
                    if reason is not None:
                        break
                if reason is not None:
                    terminate_group(process)
                    stdout, stderr = process.communicate()
                    break
        exit_code = process.returncode
    (artifact / "agent.stdout").write_text(stdout, encoding="utf-8")
    (artifact / "agent.stderr").write_text(stderr, encoding="utf-8")
    for agent in agents:
        try:
            after = quota_reader(agent)
            append_quota(artifact / "quota.jsonl", after, "after")
            after_problem = quota_problem(after, datetime.now(UTC)) or quota_reset_problem(
                before[agent], after
            )
            if after.account_fingerprint != before[agent].account_fingerprint:
                after_problem = "provider account changed at completion"
            reason = reason or after_problem
        except QuotaError as error:
            reason = reason or f"post-run quota failed: {error}"
    outcome = GuardedProcess(
        stdout, exit_code, reason, started, datetime.now(UTC).isoformat(), time.monotonic() - start
    )
    (artifact / "process.json").write_text(
        json.dumps(asdict(outcome) | {"stdout": "agent.stdout"}, indent=2) + "\n", encoding="utf-8"
    )
    return outcome


def bounded_auto_targets(
    workspace: Path, targets: Sequence[SourceTarget]
) -> tuple[SourceTarget, ...]:
    """Clamp evidence ranges to current source length, without inventing another location."""
    result: list[SourceTarget] = []
    for target in targets:
        lines = physical_lines(read_literal(workspace / target.path))
        if target.start is None or target.end is None or target.start > len(lines):
            raise PreparationError(
                f"{target.path}: selected range is inconsistent with current source"
            )
        result.append(replace(target, end=min(target.end, len(lines))))
    return tuple(result)


def target_label(target: SourceTarget) -> str:
    """A stable target string for artifacts and coverage auditing."""
    if target.symbol is not None:
        return f"{target.path}::{target.symbol}"
    if target.start is None or target.end is None:
        return target.path
    return f"{target.path}:{target.start}:{target.end}"


def fit_auto_targets(
    workspace: Path, request: str, targets: Sequence[SourceTarget], budget: int
) -> tuple[tuple[SourceTarget, ...], tuple[str, ...]]:
    """Narrow the lowest-ranked range around its midpoint, then exclude it if necessary."""
    fitted = bounded_auto_targets(workspace, targets)
    notes: list[str] = []
    while fitted:
        packet = build_packet(workspace, request, fitted, ())
        size = len(render_packet(packet, 1_000_000_000).encode("utf-8"))
        if size <= budget + len(request.encode("utf-8")):
            break
        target = fitted[-1]
        if target.start is None or target.end is None:
            raise PreparationError(f"auto target is not a bounded range: {target}")
        if target.start == target.end:
            notes.append(f"budget excluded {target_label(target)}")
            fitted = fitted[:-1]
        else:
            width = (target.end - target.start + 1) // 2
            start = (target.start + target.end - width + 1) // 2
            narrowed = replace(target, start=start, end=start + width - 1)
            notes.append(f"budget narrowed {target_label(target)} to {target_label(narrowed)}")
            fitted = (*fitted[:-1], narrowed)
    return fitted, tuple(notes)


def packet_prompt(
    task: StudyTask,
    index: int,
    arm: Arm,
    workspace: Path,
    evidence: str,
    artifact: Path,
) -> tuple[str, tuple[str, ...], int, int]:
    """Initial evidence is shared; later failure evidence is from the edited episode state."""
    step = task.steps[index]
    common = step.prompt + "\n\nHost-observed public failing-test output:\n" + evidence
    selected: Selection | None = None
    if arm is Arm.CONTROL:
        targets: tuple[SourceTarget, ...] = ()
    elif arm is Arm.AUTO:
        selected = select_targets(
            evidence, str(workspace), task.roots, source_inventory(task, workspace)
        )
        targets, notes = fit_auto_targets(workspace, step.prompt, selected.targets, SOURCE_BUDGET)
        selected = replace(selected, targets=targets, rejected=(*selected.rejected, *notes))
        (artifact / "selection.json").write_text(
            json.dumps(asdict(selected), indent=2) + "\n", encoding="utf-8"
        )
    else:
        targets = (
            tuple(parse_target(t) for t in step.oracle)
            if arm is Arm.ORACLE
            else wrong_targets(step, workspace)
        )
    labels = tuple(target_label(t) for t in targets)
    (artifact / "evidence.txt").write_text(evidence, encoding="utf-8")
    if not targets:
        (artifact / "prompt.txt").write_text(common, encoding="utf-8")
        return common, labels, 0, 0
    packet = build_packet(workspace, step.prompt, targets, ())
    rendered = render_packet(packet, SOURCE_BUDGET + len(step.prompt.encode("utf-8")))
    body = rendered[len(step.prompt) :]
    validate_packet(workspace, packet)
    (artifact / "packet.json").write_text(
        json.dumps(asdict(packet), indent=2) + "\n", encoding="utf-8"
    )
    prompt = common + body
    (artifact / "prompt.txt").write_text(prompt, encoding="utf-8")
    return (
        prompt,
        labels,
        sum(len(s.text.encode("utf-8")) for s in packet.sources),
        len(body.encode("utf-8")),
    )


def provider_command(
    study: Study,
    task: StudyTask,
    job: Job,
    workspace: Path,
    artifact: Path,
    session: str,
    index: int,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Common per-provider tools/config across all arms; never write installed settings."""
    command: tuple[str, ...]
    model = study.claude_model if job.agent is Agent.CLAUDE else study.codex_model
    spec = RunSpec(
        task.task,
        Protocol.SEQUENTIAL,
        job.agent,
        Variant.GOVERNOR,
        model,
        study.effort,
        study.window,
        job.repetition,
    )
    env = agent_env(spec, artifact, os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if job.agent is Agent.CLAUDE:
        env.update({"ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_AUTO_CONNECT_IDE": "0"})
        command = (
            "claude",
            "-p",
            "--session-id" if index == 0 else "--resume",
            session,
            "--model",
            model,
            "--effort",
            study.effort,
            "--output-format",
            "json",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            json.dumps(claude_settings(spec) | {"disableAllHooks": True}),
            "--permission-mode",
            "acceptEdits",
            "--max-turns",
            "12",
            "--max-budget-usd",
            str(study.claude_call_limit),
            "--tools",
            CLAUDE_TOOLS,
            "--allowedTools",
            CLAUDE_TOOLS,
        )
    else:
        options = (
            *codex_options(spec, workspace),
            "-c",
            'model_auto_compact_token_limit_scope="total"',
        )
        command = (
            ("codex", "exec", *options, "-")
            if index == 0
            else ("codex", "exec", *options, "resume", session, "-")
        )
    return command, env


def codex_reasoning(path: Path) -> int | None:
    """Deduplicate per-response reasoning, preserving missing counters as unknown."""
    records: dict[str, int | None] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = json_object(json.loads(raw), str(path))
        if entry.get("type") != "token_usage_record":
            continue
        payload = json_object(entry.get("payload"), str(path))
        usage = json_object(payload.get("usage"), str(path))
        identity = payload.get("response_id")
        if not isinstance(identity, str):
            raise BenchError(f"{path}: invalid reasoning response ID")
        value = usage.get("reasoning_output_tokens")
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise BenchError(f"{path}: invalid reasoning token count")
        records[identity] = value if isinstance(value, int) else None
    return (
        None
        if not records or any(v is None for v in records.values())
        else sum(v for v in records.values() if v is not None)
    )


def measured_consumption(
    agent: Agent, session: str, outputs: Sequence[str], artifact: Path
) -> Consumption | None:
    """Provider counters on failed calls are retained; unknown cost stops later generation."""
    reported = (
        tuple(claude_reported_usage(output) for output in outputs) if agent is Agent.CLAUDE else ()
    )
    try:
        provider = (
            claude_provider(reported)
            if agent is Agent.CLAUDE
            else codex_provider(load_codex_records(codex_rollout(session)), len(outputs))
        )
    except BenchError as error:
        (artifact / "measurement.error.txt").write_text(str(error), encoding="utf-8")
        return None
    if provider is None:
        (artifact / "measurement.error.txt").write_text(
            "Provider report missing; transcript proxy is not billed USD.\n", encoding="utf-8"
        )
        return None
    p = provider
    try:
        logs = read_agent_logs(agent, session, reported, len(outputs))
    except (BenchError, TranscriptError) as error:
        (artifact / "measurement.error.txt").write_text(str(error), encoding="utf-8")
        return Consumption(
            p.cost_by_step[-1] if agent is Agent.CLAUDE else None,
            p.uncached,
            p.cache_write,
            p.cache_read,
            p.output,
            None,
            None,
            None,
            None,
        )
    m = logs.measurement
    (artifact / "measurement.json").write_text(
        json.dumps(
            {"agent_version": logs.version, "provider": asdict(p), "secondary": asdict(m)}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    reasoning = codex_reasoning(codex_rollout(session)) if agent is Agent.CODEX else None
    return Consumption(
        p.cost_by_step[-1] if agent is Agent.CLAUDE else None,
        p.uncached,
        p.cache_write,
        p.cache_read,
        p.output,
        reasoning,
        m.requests,
        m.compactions,
        m.max_context,
    )


def generation_failure(agent: Agent, outcome: GuardedProcess) -> str | None:
    """A ceiling hit remains failed even if a source edit happened to pass tests."""
    if outcome.reason is not None:
        return outcome.reason
    if agent is Agent.CODEX:
        return codex_turn_failure(outcome.stdout) or (
            f"agent exit {outcome.exit_code}" if outcome.exit_code else None
        )
    failure = claude_failure(outcome.stdout, False)
    if failure is not None:
        return failure
    decoded: object = json.loads(outcome.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    for record in records:
        if (
            isinstance(record, dict)
            and record.get("type") == "result"
            and record.get("is_error") is True
        ):
            return str(record.get("subtype"))
    return f"agent exit {outcome.exit_code}" if outcome.exit_code else None


def save_episode(results: Path, episode: Episode) -> None:
    """Persist the complete attempt after every step, including downstream cancellation."""
    (results / f"{episode.job.id}.episode.json").write_text(
        json.dumps(asdict(episode), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def allowance_problem(study: Study, episodes: Sequence[Episode], job: Job) -> str | None:
    """Reserve the next invocation before launch; incomplete unknown spend blocks all jobs."""
    if any(episode_cost(e, study.calibration) is None for e in episodes):
        return "unknown provider cost; no further generation authorized"
    claude = [episode_cost(e, study.calibration) for e in episodes if e.job.agent is Agent.CLAUDE]
    dollars = sum(c for c in claude if c is not None)
    codex = [e.steps[-1].consumption for e in episodes if e.job.agent is Agent.CODEX and e.steps]
    predicted = [codex_points(c, study.calibration) for c in codex if c is not None]
    if (
        dollars >= study.allowance.claude_usd
        or sum(p[0] for p in predicted) >= study.allowance.codex_five_hour
        or sum(p[1] for p in predicted) >= study.allowance.codex_weekly_proxy
    ):
        return "cumulative study allowance exhausted; all queued providers stopped"
    if job.agent is Agent.CLAUDE:
        costs = [
            episode_cost(e, study.calibration) for e in episodes if e.job.agent is Agent.CLAUDE
        ]
        spent = sum(c for c in costs if c is not None)
        if spent + study.allowance.reserve_claude_call > study.allowance.claude_usd:
            return (
                f"Claude cumulative launch allowance exhausted: spent {spent}, "
                f"allowance {study.allowance.claude_usd}"
            )
    else:
        counters = [
            e.steps[-1].consumption for e in episodes if e.job.agent is Agent.CODEX and e.steps
        ]
        points = [codex_points(c, study.calibration) for c in counters if c is not None]
        five = sum(p[0] for p in points)
        weekly = sum(p[1] for p in points)
        if (
            five + study.allowance.reserve_codex_five_hour_call > study.allowance.codex_five_hour
            or weekly + study.allowance.reserve_codex_weekly_call
            > study.allowance.codex_weekly_proxy
        ):
            return (
                f"Codex cumulative launch allowance exhausted: five-hour {five}, "
                f"weekly proxy {weekly}"
            )
    return None


def episode_stop_problem(episode: Episode) -> str | None:
    """Keep known-cost call ceilings as failed samples; halt on telemetry or provider errors."""
    for step in episode.steps:
        if step.consumption is None:
            return "provider cost unknown"
        reason = step.stop_reason
        if reason is None or reason in ("error_max_turns", "error_max_budget_usd"):
            continue
        if reason.startswith("generation timeout after "):
            continue
        return reason
    return None


def run_episode(
    study: Study,
    task: StudyTask,
    job: Job,
    work: Path,
    results: Path,
    initial_evidence: str,
    prior: Sequence[Episode],
) -> Episode:
    """Sequential state carries within one episode; every other job starts a new workspace."""
    directory = work / job.id
    workspace = directory / "workspace"
    if directory.exists():
        raise BenchError(f"attempt already exists: {directory}; no automatic retries")
    directory.mkdir()
    repo = pinned_repo(task, work)
    fixture_workspace(task, repo, workspace)
    (directory / "python-version.txt").write_text(
        subprocess.check_output((str(workspace / ".venv/bin/python"), "--version"), text=True),
        encoding="utf-8",
    )
    (directory / "packages.txt").write_text(
        subprocess.check_output(
            ("uv", "pip", "freeze", "--python", str(workspace / ".venv/bin/python")), text=True
        ),
        encoding="utf-8",
    )
    steps: list[StepResult] = []
    outputs: list[str] = []
    session = str(uuid.uuid4()) if job.agent is Agent.CLAUDE else ""
    uninvoked: str | None = None
    for index, step in enumerate(task.steps):
        artifact = directory / f"step-{index + 1}"
        artifact.mkdir()
        inject_step(task, index, workspace)
        freeze_fixture(workspace)
        baseline = protected_test_files(workspace)
        before_hashes = source_hashes(task, workspace)
        if index == 0:
            evidence = initial_evidence
        else:
            failure = full_suite(workspace, task.task.test_command, 120)
            if failure.passed:
                raise BenchError(f"{job.id}: next mutation did not fail its public suite")
            evidence = failure.output.replace(str(workspace) + "/", "")
        current = Episode(job, tuple(steps), len(task.steps), None)
        problem = allowance_problem(study, (*prior, *((current,) if steps else ())), job)
        if problem is not None:
            uninvoked = problem
            break
        prompt, targets, source_bytes, packet_bytes = packet_prompt(
            task, index, job.arm, workspace, evidence, artifact
        )
        oracle_size = target_source_bytes(workspace, step.oracle)
        wrong_size = wrong_source_bytes(step, workspace)
        if not 0.8 * oracle_size <= wrong_size <= 1.2 * oracle_size:
            raise BenchError(
                f"{job.id}: wrong source size {wrong_size} outside ±20% of oracle {oracle_size}"
            )
        command, env = provider_command(study, task, job, workspace, artifact, session, index)
        (artifact / "invocation.json").write_text(
            json.dumps(
                {
                    "command": command,
                    "environment_keys": sorted(env),
                    "source_hashes_before": before_hashes,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        outcome = guarded_process(
            command,
            prompt,
            workspace,
            env,
            artifact,
            tuple(Agent),
            float(study.timeout),
            POLL_SECONDS,
            read_quota,
        )
        outputs.append(outcome.stdout)
        if job.agent is Agent.CODEX and not session:
            try:
                session = codex_thread_id(outcome.stdout)
            except BenchError as error:
                (artifact / "session.error.txt").write_text(str(error), encoding="utf-8")
        consumed = measured_consumption(job.agent, session, outputs, artifact) if session else None
        reason = generation_failure(job.agent, outcome)
        if consumed is None:
            reason = reason or "provider cost unknown"
        elif (artifact / "measurement.error.txt").exists():
            reason = reason or "native telemetry incomplete; see measurement.error.txt"
        provisional = StepResult(
            index + 1,
            session,
            outcome.started_at,
            outcome.finished_at,
            outcome.duration_seconds,
            outcome.exit_code,
            reason or "host acceptance pending",
            consumed,
            Quality(False, False, False, False),
            targets,
            source_bytes,
            packet_bytes,
            str(artifact),
        )
        save_episode(
            results,
            Episode(job, (*steps, provisional), len(task.steps), "host acceptance pending"),
        )
        quality = host_quality(task, index, workspace, repo, baseline, artifact)
        (artifact / "source_hashes.after.json").write_text(
            json.dumps(source_hashes(task, workspace), indent=2) + "\n", encoding="utf-8"
        )
        steps.append(
            StepResult(
                index + 1,
                session,
                outcome.started_at,
                outcome.finished_at,
                outcome.duration_seconds,
                outcome.exit_code,
                reason,
                consumed,
                quality,
                targets,
                source_bytes,
                packet_bytes,
                str(artifact),
            )
        )
        current = Episode(job, tuple(steps), len(task.steps), None)
        save_episode(results, current)
        print(
            json.dumps(
                {
                    "event": "preparation_step_finished",
                    "id": job.id,
                    "step": index + 1,
                    "accepted": current.accepted,
                    "quality": asdict(quality),
                    "primary_cost": episode_cost(current, study.calibration),
                    "reason": reason,
                }
            ),
            flush=True,
        )
        if reason is not None or not quality.accepted:
            uninvoked = reason or "earlier step failed host acceptance"
            break
    episode = Episode(job, tuple(steps), len(task.steps), uninvoked)
    save_episode(results, episode)
    return episode


def verify_versions(study: Study) -> None:
    """No model availability probe; enforce installed CLI versions without generation."""
    for command, expected in (("claude", study.claude_version), ("codex", study.codex_version)):
        observed = subprocess.check_output((command, "--version"), text=True).strip()
        if expected not in observed.split():
            raise BenchError(f"{command}: registered version {expected}, installed {observed}")


def block_capacity(study: Study, jobs: Sequence[Job], task: StudyTask, artifact: Path) -> None:
    """Reserve a complete remaining task block before its first generation."""
    calls = sum(len(task.steps) for job in jobs if job.agent is Agent.CODEX)
    five = 2 * calls * study.allowance.reserve_codex_five_hour_call
    weekly = (
        2
        * calls
        * study.allowance.reserve_codex_weekly_call
        * study.calibration.weekly_high
        / study.calibration.weekly_weight
    )
    observations: list[QuotaSnapshot] = []
    for agent in Agent:
        snapshot = read_quota(agent)
        observations.append(snapshot)
        append_quota(artifact, snapshot, "block_forecast")
        problem = quota_problem(snapshot, datetime.now(UTC))
        if problem is not None:
            raise BenchError(problem)
        if agent is Agent.CODEX:
            for window in snapshot.windows:
                forecast = five if window.duration_minutes == 300 else weekly
                threshold = 95 if window.duration_minutes == 300 else 85
                if (
                    window.duration_minutes in (300, 10_080)
                    and window.used_percent + forecast >= threshold
                ):
                    raise BenchError(
                        f"{agent}: task block forecast plus reserve {forecast:.4f} points "
                        f"does not fit {window.id} at {window.used_percent}%"
                    )
    artifact.with_suffix("forecast.json").write_text(
        json.dumps(
            {
                "task_id": task.task.id,
                "codex_calls": calls,
                "codex_five_hour_reserved": five,
                "codex_weekly_upper_proxy_reserved": weekly,
                "claude_quota_forecast": None,
                "claude_forecast_status": "USD cannot identify subscription points",
                "observations": [asdict(s) for s in observations],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def execute_study(
    study: Study, manifest: Path, root: Path, results: Path, work: Path, eligibility_dir: Path
) -> None:
    """A pilot execution ends at eight scheduled attempts or a guard, never starts full work."""
    if not study.execution_approved:
        raise BenchError("study execution is not explicitly approved; use --plan-only")
    if "cimrihook-bench" not in str(work) or not work.is_absolute():
        raise BenchError("execution work dir must contain the lowercase cimrihook-bench marker")
    if results.exists() or work.exists():
        raise BenchError("study directory already exists; automatic resume/retry is prohibited")
    verify_versions(study)
    tasks = {task.task.id: task for task in study.tasks}
    initial: dict[str, str] = {}
    for task in study.tasks:
        report_path = eligibility_dir / f"{task.task.id}.json"
        report = json_object(json.loads(report_path.read_text(encoding="utf-8")), str(report_path))
        if (
            report.get("eligible") is not True
            or report.get("commit") != task.commit
            or report.get("task_sha256") != digest(json.dumps(asdict(task), sort_keys=True))
        ):
            raise BenchError(f"{task.task.id}: local eligibility report is absent or stale")
        initial[task.task.id] = (
            eligibility_dir / "eligibility" / task.task.id / "step-1/public.before.txt"
        ).read_text(encoding="utf-8")
    results.mkdir(parents=True)
    work.mkdir(parents=True)
    (results / "study.json").write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    implementation = (
        "src/cimrihook/bench_preparation.py",
        "src/cimrihook/bench_preparation_run.py",
        "src/cimrihook/bench_preparation_stats.py",
        "src/cimrihook/bench_preparation_tasks.py",
        "src/cimrihook/preparation.py",
        "src/cimrihook/bench.py",
        "src/cimrihook/cli.py",
    )
    (results / "implementation.json").write_text(
        json.dumps(
            {
                "commit": subprocess.check_output(
                    ("git", "rev-parse", "HEAD"), cwd=root, text=True
                ).strip(),
                "source_hashes": {
                    p: digest((root / p).read_text(encoding="utf-8")) for p in implementation
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (results / "plan.json").write_text(study_plan_json(study) + "\n", encoding="utf-8")
    (results / "task_hashes.json").write_text(
        json.dumps(
            {p: digest((root / p).read_text(encoding="utf-8")) for p in study.task_paths}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    episodes: list[Episode] = []
    stop: str | None = None
    blocks: set[str] = set()
    for job in study.jobs:
        stop = allowance_problem(study, episodes, job)
        if stop is not None:
            break
        try:
            if job.task_id not in blocks:
                block_capacity(
                    study,
                    tuple(j for j in study.jobs if j.task_id == job.task_id),
                    tasks[job.task_id],
                    results / f"{job.task_id}.block-quota.jsonl",
                )
                blocks.add(job.task_id)
            episode = run_episode(
                study, tasks[job.task_id], job, work, results, initial[job.task_id], episodes
            )
        except (BenchError, PreparationError, QuotaError) as error:
            stop = f"protocol or quota stop at {job.id}: {error}"
            retained = results / f"{job.id}.episode.json"
            if retained.exists():
                episode = replace(load_episode(retained), uninvoked_reason=stop)
                save_episode(results, episode)
                episodes.append(episode)
            break
        episodes.append(episode)
        problem = episode_stop_problem(episode)
        if problem is not None:
            stop = problem
            break
    (results / "completion.json").write_text(
        json.dumps(
            {
                "planned": len(study.jobs),
                "retained": len(episodes),
                "stop_reason": stop,
                "complete": len(episodes) == len(study.jobs),
                "unstarted_jobs": [j.id for j in study.jobs if j not in {e.job for e in episodes}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "event": "preparation_study_stopped",
                "stage": study.stage,
                "retained": len(episodes),
                "stop_reason": stop,
            }
        ),
        flush=True,
    )
