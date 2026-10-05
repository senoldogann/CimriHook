"""Stable evidence transformations and real-process preparation safety boundaries."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cimrihook.bench import Agent, load_tasks
from cimrihook.bench_preparation import (
    Arm,
    Consumption,
    Episode,
    Quality,
    StepResult,
    codex_points,
    codex_units,
    load_study,
    select_targets,
)
from cimrihook.bench_preparation_run import (
    active_quota_problem,
    allowance_problem,
    block_capacity,
    fit_auto_targets,
    guarded_process,
    quota_problem,
)
from cimrihook.bench_preparation_stats import (
    episode_cost,
    study_report,
    task_bootstrap,
)
from cimrihook.bench_preparation_tasks import full_suite
from cimrihook.bench_preparation_validation import source_overlap
from cimrihook.errors import BenchError
from cimrihook.preparation import SourceExcerpt, parse_target, source_excerpt
from cimrihook.quota import QuotaSnapshot, QuotaWindow

PROJECT = Path(__file__).resolve().parents[1]


def snapshot(weekly: float, observed: str) -> QuotaSnapshot:
    """Frozen quota values wrap a real child process, without a provider probe."""
    return QuotaSnapshot(
        "codex",
        observed,
        True,
        "local-account",
        (QuotaWindow("primary", 20.0, None, 300), QuotaWindow("secondary", weekly, None, 10_080)),
    )


def test_auto_selection_uses_only_evidence_and_inventory() -> None:
    inventory = ("src/itsdangerous/signer.py", "src/itsdangerous/serializer.py")
    output = """File "/tmp/cimrihook-bench/a/workspace/src/itsdangerous/signer.py", line 242
src/itsdangerous/signer.py:242: BadSignature
FAILED tests/test_itsdangerous/test_serializer.py::TestSerializer::test_alt_salt
File "/outside/signer.py", line 14
../src/itsdangerous/signer.py:99: error
"""
    selected = select_targets(
        output, "/tmp/cimrihook-bench/a/workspace", ("src/itsdangerous",), inventory
    )
    assert [(t.path, t.start, t.end) for t in selected.targets] == [
        ("src/itsdangerous/signer.py", 212, 272),
    ]
    assert len(selected.candidates) == 2
    assert any("traversal" in message for message in selected.rejected)
    assert any("weak localization abstained" in message for message in selected.rejected)
    weak = select_targets(
        "FAILED tests/test_signer.py::test_x", "workspace", ("src/itsdangerous",), inventory
    )
    assert weak.targets == () and len(weak.candidates) == 1
    assert weak.candidates[0].rank == 50
    ambiguous = select_targets(
        "FAILED tests/test_signer.py::test_x",
        "workspace",
        ("src",),
        ("src/a/signer.py", "src/b/signer.py"),
    )
    assert ambiguous.targets == ()
    assert any("2 matching sources" in message for message in ambiguous.rejected)
    assert (
        select_targets("no supported failure locations", "workspace", ("src",), inventory).targets
        == ()
    )


def test_overlap_uses_unique_lines_and_keeps_abstention_unmeasured() -> None:
    auto = (
        SourceExcerpt("module.py", "hash", 1, 4, 10, False, ""),
        SourceExcerpt("module.py", "hash", 3, 6, 10, False, ""),
    )
    oracle = (SourceExcerpt("module.py", "hash", 5, 8, 10, False, ""),)
    overlap = source_overlap(auto, oracle)
    assert (
        overlap.auto_unique_lines,
        overlap.oracle_unique_lines,
        overlap.shared_unique_lines,
    ) == (6, 4, 2)
    assert overlap.oracle_coverage == 0.5 and overlap.auto_precision == pytest.approx(1 / 3)
    empty = source_overlap((), oracle)
    assert empty.auto_precision is None and empty.oracle_coverage == 0.0


def test_full_schedule_is_mirrored_and_generic_tasks_are_unchanged() -> None:
    study = load_study(PROJECT / "bench/preparation/full-20261005.json", PROJECT)
    assert not study.execution_approved
    assert len(study.jobs) == 96
    assert sum(len(t.steps) * 16 for t in study.tasks) == 160
    for task in study.tasks:
        for agent in Agent:
            jobs = [j for j in study.jobs if j.task_id == task.task.id and j.agent is agent]
            assert [j.position for j in jobs] == list(range(1, 9))
            for arm in (Arm.ORACLE, Arm.AUTO, Arm.WRONG):
                pair = [j.arm for j in jobs if j.arm in (Arm.CONTROL, arm)]
                assert pair in (
                    [Arm.CONTROL, arm, arm, Arm.CONTROL],
                    [arm, Arm.CONTROL, Arm.CONTROL, arm],
                )
    generic = load_tasks(PROJECT / "bench/tasks")
    assert all(t.id not in {task.task.id for task in study.tasks} for t in generic)


def test_overloaded_symbol_selects_the_implementation(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("""from typing import overload
@overload
def value(x: int) -> int: ...
@overload
def value(x: str) -> str: ...
def value(x):
    return x
""")
    excerpt = source_excerpt(tmp_path, parse_target("module.py::value"))
    assert excerpt.text == "def value(x):\n    return x\n"


def test_quota_thresholds_and_staleness_are_exact() -> None:
    now = datetime.now(UTC)
    at_limit = snapshot(85.0, now.isoformat())
    assert quota_problem(at_limit, now) is not None
    assert active_quota_problem(at_limit, now) is None
    assert active_quota_problem(snapshot(85.01, now.isoformat()), now) is not None
    assert quota_problem(replace(at_limit, available=False), now) is not None
    assert quota_problem(snapshot(20.0, "2020-01-01T00:00:00+00:00"), now) is not None


def test_guard_kills_the_real_process_group_and_retains_logs(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    marker = tmp_path / "child-survived"
    child = (
        "import time;from pathlib import Path;time.sleep(1);Path("
        + repr(str(marker))
        + ").write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]);print('started',flush=True);time.sleep(10)"
    )
    observed = datetime.now(UTC).isoformat()
    readings = iter((snapshot(40.0, observed), snapshot(86.0, observed), snapshot(86.0, observed)))

    def reader(agent: Agent) -> QuotaSnapshot:
        if agent is Agent.CODEX:
            return snapshot(40.0, observed)
        return replace(next(readings), provider="claude")

    result = guarded_process(
        (sys.executable, "-c", parent),
        "",
        tmp_path,
        os.environ,
        artifact,
        tuple(Agent),
        5.0,
        0.15,
        reader,
    )
    assert result.reason is not None and "weekly" in result.reason
    assert result.exit_code != 0
    assert "started" in (artifact / "agent.stdout").read_text()
    time.sleep(1.05)
    assert not marker.exists()
    rows = [json.loads(row) for row in (artifact / "quota.jsonl").read_text().splitlines()]
    assert {row["provider"] for row in rows} == {"claude", "codex"}
    assert any(
        row["provider"] == "claude"
        and any(window["used_percent"] > 85 for window in row["windows"])
        for row in rows
    )


def test_auto_budget_narrowing_is_recorded_and_repeatable(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    source = tmp_path / "module.py"
    source.write_text("# " + "x" * 2000 + "\n" + ("VALUE=1\n" * 59))
    subprocess.run(("git", "-C", str(tmp_path), "add", "module.py"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ),
        check=True,
    )
    targets = (parse_target("module.py:1:60"),)
    result = fit_auto_targets(tmp_path, "request", targets, 1000)
    assert result == fit_auto_targets(tmp_path, "request", targets, 1000)
    fitted, notes = result
    assert fitted and notes and fitted[0].start != 1


def test_block_preflight_archives_forecast_without_generation(tmp_path: Path) -> None:
    study = load_study(PROJECT / "bench/preparation/pilot-20261005.json", PROJECT)
    observed = datetime.now(UTC).isoformat()

    def reader(agent: Agent) -> QuotaSnapshot:
        return replace(snapshot(64.0, observed), provider=agent.value)

    artifact = tmp_path / "block-quota.jsonl"
    block_capacity(study, study.jobs, study.tasks[0], artifact, reader)
    forecast = json.loads(artifact.with_suffix(".forecast.json").read_text())
    assert forecast["codex_calls"] == 4
    assert forecast["codex_five_hour_reserved"] == pytest.approx(3.531846824)
    assert forecast["claude_quota_forecast"] is None
    assert len(artifact.read_text().splitlines()) == 2


def test_host_tests_do_not_reuse_same_size_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "value.py"
    source.write_text("VALUE=1\n")
    subprocess.run((sys.executable, "-c", "import value"), cwd=tmp_path, check=True)
    stamp = source.stat().st_mtime_ns
    source.write_text("VALUE=2\n")
    os.utime(source, ns=(stamp, stamp))
    result = full_suite(tmp_path, (sys.executable, "-c", "import value;assert value.VALUE==2"), 5)
    assert result.passed


def step_result(consumed: Consumption | None, quality: Quality, reason: str | None) -> StepResult:
    """Stable serialized measurements exercise failure retention and unknown costs."""
    return StepResult(
        1, "session", "start", "end", 1.0, 0, reason, consumed, quality, (), 0, 0, "artifact"
    )


def test_failures_remain_in_pilot_and_unknown_cost_stops_next_calls() -> None:
    study = load_study(PROJECT / "bench/preparation/pilot-20261005.json", PROJECT)
    measured = Consumption(0.1, 1000, 0, 100, 30, 10, 2, 0, 1000)
    failed = Quality(False, True, False, False)
    passed = Quality(True, True, True, True)
    episodes = tuple(
        Episode(
            job, (step_result(measured, failed if job.arm is Arm.WRONG else passed, None),), 1, None
        )
        for job in study.jobs
    )
    report = study_report(episodes, study)
    assert report["attempted_episodes"] == 8
    assert report["primary_ci_status"] == "pilot_no_efficacy_ci"
    rows = report["episodes"]
    assert isinstance(rows, list) and len(rows) == 8
    assert episode_cost(episodes[3], study.calibration) == 0.1
    unknown = replace(episodes[0], steps=(step_result(None, failed, "measurement failed"),))
    assert episode_cost(unknown, study.calibration) is None
    assert allowance_problem(study, (unknown,), study.jobs[1]) is not None
    with_unknown = study_report((unknown, *episodes[1:]), study)
    assert with_unknown["comparisons"] == []
    assert with_unknown["all_costs_measured"] is False


def test_bootstrap_clusters_and_frozen_cost_weights() -> None:
    values = (-2.0, 0.0, 1.0, 3.0, -1.0, 2.0)
    assert task_bootstrap(values, 10_000, 20261005) == task_bootstrap(values, 10_000, 20261005)
    with pytest.raises(BenchError, match="at least two"):
        task_bootstrap((1.0,), 10_000, 20261005)
    study = load_study(PROJECT / "bench/preparation/pilot-20261005.json", PROJECT)
    control = Consumption(None, 17625, 0, 112128, 760, 100, 5, 0, 100000)
    assert codex_points(control, study.calibration) == pytest.approx((0.441480853, 0.075502518))
    assert [codex_units(control, weight) for weight in (6, 8, 20)] == [33397.8, 34917.8, 44037.8]


def test_cli_plan_only_needs_no_provider_executable(tmp_path: Path) -> None:
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(tmp_path), PYTHONPATH=str(PROJECT / "src"))
    command = (
        sys.executable,
        "-m",
        "cimrihook",
        "bench-run",
        "--name",
        "local-plan",
        "--preparation-study",
        "bench/preparation/pilot-20261005.json",
        "--plan-only",
    )
    result = subprocess.run(
        command, cwd=PROJECT, env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["episodes"] == 8 and plan["generation_invocations"] == 8
    conflict = subprocess.run(
        (*command, "--variants", "governor"),
        cwd=PROJECT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert conflict.returncode == 1 and "conflicts" in conflict.stderr
