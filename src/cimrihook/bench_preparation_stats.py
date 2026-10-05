"""Task-cluster inference retaining unsuccessful and unmeasurable preparation attempts."""

import json
import random
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from cimrihook.bench import Agent, int_field, json_object, object_list, text_field
from cimrihook.bench_preparation import (
    SEED,
    Arm,
    Calibration,
    Consumption,
    Episode,
    Job,
    Quality,
    StepResult,
    Study,
    codex_effective_input,
    codex_points,
    codex_units,
    numeric,
)
from cimrihook.errors import BenchError


@dataclass(frozen=True, slots=True)
class Interval:
    """Percentile interval from whole-task resampling, not independent steps."""

    estimate: float
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class TaskDifference:
    """Repeat-averaged episode differences for one task and treatment."""

    task_id: str
    treatment: Arm
    cost_difference: float
    relative_difference: float | None
    acceptance_difference: float


def nullable_text(data: dict[str, object], key: str, where: str) -> str | None:
    """A missing error/cost marker stays absent, not the string 'None'."""
    value = data.get(key)
    if value is None:
        return None
    return text_field(data, key, where)


def boolean(data: dict[str, object], key: str, where: str) -> bool:
    """Acceptance flags cannot be inferred from truthiness of arbitrary JSON."""
    value = data.get(key)
    if not isinstance(value, bool):
        raise BenchError(f"{where}.{key}: expected a boolean")
    return value


def load_episode(path: Path) -> Episode:
    """Read the typed result shape, rejecting corrupt measurement records."""
    where = str(path)
    data = json_object(json.loads(path.read_text(encoding="utf-8")), where)
    j = json_object(data.get("job"), where)
    steps: list[StepResult] = []
    for s in object_list(data, "steps", where):
        consumption: Consumption | None = None
        raw = s.get("consumption")
        if raw is not None:
            c = json_object(raw, where)
            consumption = Consumption(
                None if c.get("usd") is None else numeric(c, "usd", where),
                int_field(c, "uncached", where),
                int_field(c, "cache_write", where),
                int_field(c, "cache_read", where),
                int_field(c, "output", where),
                None if c.get("reasoning") is None else int_field(c, "reasoning", where),
                None if c.get("requests") is None else int_field(c, "requests", where),
                None if c.get("compactions") is None else int_field(c, "compactions", where),
                None if c.get("max_context") is None else int_field(c, "max_context", where),
            )
        q = json_object(s.get("quality"), where)
        raw_targets = s.get("selected_targets")
        if not isinstance(raw_targets, list) or not all(isinstance(t, str) for t in raw_targets):
            raise BenchError(f"{where}: invalid selected targets")
        steps.append(
            StepResult(
                int_field(s, "step", where),
                text_field(s, "session_id", where),
                text_field(s, "started_at", where),
                text_field(s, "finished_at", where),
                numeric(s, "duration_seconds", where),
                int_field(s, "exit_code", where),
                nullable_text(s, "stop_reason", where),
                consumption,
                Quality(*(boolean(q, key, where) for key in Quality.__dataclass_fields__)),
                tuple(str(t) for t in raw_targets),
                int_field(s, "source_bytes", where),
                int_field(s, "packet_bytes", where),
                text_field(s, "artifact_dir", where),
            )
        )
    return Episode(
        Job(
            text_field(j, "task_id", where),
            Agent(text_field(j, "agent", where)),
            Arm(text_field(j, "arm", where)),
            int_field(j, "repetition", where),
            int_field(j, "position", where),
        ),
        tuple(steps),
        int_field(data, "planned_steps", where),
        nullable_text(data, "uninvoked_reason", where),
    )


def episode_cost(episode: Episode, calibration: Calibration) -> float | None:
    """Use whole-episode consumed cost on failures, never convert unknowns to zero."""
    if not episode.steps or any(s.consumption is None for s in episode.steps):
        return None
    last = episode.steps[-1].consumption
    if last is None:
        return None
    return last.usd if episode.job.agent is Agent.CLAUDE else codex_points(last, calibration)[0]


def quantile(values: Sequence[float], fraction: float) -> float:
    """Linearly interpolated empirical quantile with explicit empty input errors."""
    if not values:
        raise BenchError("cannot take a quantile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def task_bootstrap(values: Sequence[float], draws: int, seed: int) -> Interval:
    """Resample one value per task; repeated steps never increase cluster n."""
    if len(values) < 2 or draws < 2:
        raise BenchError("task bootstrap needs at least two tasks and two draws")
    rng = random.Random(seed)
    samples = [statistics.fmean(rng.choices(values, k=len(values))) for _ in range(draws)]
    return Interval(statistics.fmean(values), quantile(samples, 0.025), quantile(samples, 0.975))


def task_differences(
    episodes: Sequence[Episode], study: Study, agent: Agent, arm: Arm
) -> tuple[TaskDifference, ...]:
    """Require every scheduled episode and measurable cost, including failures."""
    by_job = {episode.job: episode for episode in episodes}
    if len(by_job) != len(episodes):
        raise BenchError("duplicate preparation result identity")
    if any(job not in by_job for job in study.jobs):
        raise BenchError("planned study incomplete; primary CI unavailable")
    differences: list[TaskDifference] = []
    for task in study.tasks:
        control = [
            e
            for e in episodes
            if e.job.task_id == task.task.id and e.job.agent is agent and e.job.arm is Arm.CONTROL
        ]
        treatment = [
            e
            for e in episodes
            if e.job.task_id == task.task.id and e.job.agent is agent and e.job.arm is arm
        ]
        costs_c = [episode_cost(e, study.calibration) for e in control]
        costs_a = [episode_cost(e, study.calibration) for e in treatment]
        if not costs_c or not costs_a or any(c is None for c in (*costs_c, *costs_a)):
            raise BenchError(
                f"{task.task.id}/{agent}: missing paired measurement; primary CI unavailable"
            )
        mean_c = statistics.fmean(c for c in costs_c if c is not None)
        mean_a = statistics.fmean(c for c in costs_a if c is not None)
        differences.append(
            TaskDifference(
                task.task.id,
                arm,
                mean_a - mean_c,
                None if mean_c == 0 else (mean_a - mean_c) / mean_c,
                statistics.fmean(e.accepted for e in treatment)
                - statistics.fmean(e.accepted for e in control),
            )
        )
    return tuple(differences)


def study_report(episodes: Sequence[Episode], study: Study) -> dict[str, object]:
    """Pilot and incomplete studies expose all attempts without claiming efficacy CIs."""
    scheduled = set(study.jobs)
    if any(e.job not in scheduled for e in episodes):
        raise BenchError("result includes an episode outside the registered schedule")
    if len({e.job for e in episodes}) != len(episodes):
        raise BenchError("duplicate preparation result identity")
    rows: list[dict[str, object]] = []
    for episode in episodes:
        consumed = episode.steps[-1].consumption if episode.steps else None
        cost = episode_cost(episode, study.calibration)
        row: dict[str, object] = {
            "id": episode.job.id,
            "task_id": episode.job.task_id,
            "agent": episode.job.agent.value,
            "arm": episode.job.arm.value,
            "repetition": episode.job.repetition,
            "accepted": episode.accepted,
            "attempted_steps": len(episode.steps),
            "planned_steps": episode.planned_steps,
            "primary_cost": cost,
            "primary_unit": "provider_reported_usd"
            if episode.job.agent is Agent.CLAUDE
            else "predicted_five_hour_points",
            "uninvoked_reason": episode.uninvoked_reason,
            "steps": [asdict(s) for s in episode.steps],
        }
        if episode.job.agent is Agent.CODEX and consumed is not None:
            _, weekly = codex_points(consumed, study.calibration)
            base = codex_units(consumed, 6.0)
            pooled_base = codex_effective_input(consumed) + 6 * consumed.output
            row.update(
                {
                    "units_6": base,
                    "units_8": codex_units(consumed, 8.0),
                    "units_20": codex_units(consumed, 20.0),
                    "weekly_pooled_proxy": weekly,
                    "weekly_coefficient_interval": [
                        pooled_base * study.calibration.weekly_low,
                        pooled_base * study.calibration.weekly_high,
                    ],
                }
            )
        rows.append(row)
    complete = len(episodes) == len(study.jobs)
    measured = all(episode_cost(e, study.calibration) is not None for e in episodes)
    comparisons: list[dict[str, object]] = []
    if complete and measured:
        for agent in Agent:
            for arm in (Arm.ORACLE, Arm.AUTO, Arm.WRONG):
                differences = task_differences(episodes, study, agent, arm)
                comparison: dict[str, object] = {
                    "agent": agent.value,
                    "treatment": arm.value,
                    "task_differences": [asdict(d) for d in differences],
                    "task_n": len(differences),
                }
                if study.stage == "full":
                    comparison["cost_ci"] = asdict(
                        task_bootstrap([d.cost_difference for d in differences], 10_000, SEED)
                    )
                    comparison["acceptance_ci"] = asdict(
                        task_bootstrap([d.acceptance_difference for d in differences], 10_000, SEED)
                    )
                    relatives = [d.relative_difference for d in differences]
                    comparison["relative_cost_ci"] = (
                        None
                        if any(v is None for v in relatives)
                        else asdict(
                            task_bootstrap([v for v in relatives if v is not None], 10_000, SEED)
                        )
                    )
                comparisons.append(comparison)
    totals: dict[str, object] = {}
    for agent in Agent:
        group = [e for e in episodes if e.job.agent is agent]
        known = [episode_cost(e, study.calibration) for e in group]
        missing = any(c is None for c in known)
        total = None if missing else sum(c for c in known if c is not None)
        totals[agent.value] = {
            "attempts": len(group),
            "accepted": sum(e.accepted for e in group),
            "cost_unknown": missing,
            "known_cost_subtotal": sum(c for c in known if c is not None),
            "total_primary_cost": total,
            "accepted_episodes_per_cost": None
            if total is None or total == 0
            else sum(e.accepted for e in group) / total,
        }
    return {
        "kind": "preparation-ab",
        "stage": study.stage,
        "task_n": len(study.tasks),
        "planned_episodes": len(study.jobs),
        "attempted_episodes": len(episodes),
        "complete": complete,
        "all_costs_measured": measured,
        "primary_ci_status": "pilot_no_efficacy_ci"
        if study.stage == "pilot"
        else "available"
        if complete and measured
        else "unavailable_incomplete_or_unknown_cost",
        "quota_attribution": (
            "account-wide observations, not attributable while concurrent work is unexcluded"
        ),
        "calibration": asdict(study.calibration),
        "totals": totals,
        "comparisons": comparisons,
        "episodes": rows,
    }


def report_directory(results: Path, study: Study) -> str:
    """Read every retained episode, not the historical generic error-filtered results."""
    episodes = tuple(load_episode(path) for path in sorted(results.glob("*.episode.json")))
    return json.dumps(study_report(episodes, study), indent=2, allow_nan=False)


def calibration_directory(results: Path) -> str:
    """Report measured compaction diagnostics without treating replay as preparation evidence."""
    episodes = tuple(load_episode(path) for path in sorted(results.glob("*.episode.json")))
    rows: list[dict[str, object]] = []
    for episode in episodes:
        last = episode.steps[-1].consumption if episode.steps else None
        rows.append(
            {
                "id": episode.job.id,
                "requests": None if last is None else last.requests,
                "compactions": None if last is None else last.compactions,
                "max_context": None if last is None else last.max_context,
                "status": "unknown_native_telemetry"
                if last is None or last.compactions is None
                else "no_compaction_observed"
                if last.compactions == 0
                else "compaction_observed",
            }
        )
    return json.dumps(
        {
            "kind": "preparation-compaction-diagnostic",
            "preparation_savings_inferred": False,
            "episodes": rows,
            "message": "Native counters only; no causal preparation estimate from simulation.",
        },
        indent=2,
        allow_nan=False,
    )
