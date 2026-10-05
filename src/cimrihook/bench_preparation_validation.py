"""Host-only selector replay and oracle overlap, without provider generation."""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cimrihook.bench_preparation import AUTO_MIN_RANK, Arm, StudyTask, load_study, select_targets
from cimrihook.bench_preparation_run import packet_prompt
from cimrihook.bench_preparation_tasks import (
    fixture_workspace,
    freeze_fixture,
    full_suite,
    inject_step,
    pinned_repo,
    restore_gold,
    source_inventory,
)
from cimrihook.errors import BenchError
from cimrihook.preparation import SourceExcerpt, digest, parse_target, source_excerpt


@dataclass(frozen=True, slots=True, order=True)
class SourceLine:
    """A unique physical line; repeated overlapping ranges do not inflate coverage."""

    path: str
    line: int


@dataclass(frozen=True, slots=True)
class Overlap:
    """Oracle-span coverage is location coverage, not a claim of causal correctness."""

    auto_unique_lines: int
    oracle_unique_lines: int
    shared_unique_lines: int
    oracle_coverage: float
    auto_precision: float | None
    oracle_targets_hit: int
    oracle_target_count: int


@dataclass(frozen=True, slots=True)
class LocalSelection:
    """Local source observations carry no measured agent cost or quality verdict."""

    task_id: str
    step: int
    packet_emitted: bool
    selected_targets: tuple[str, ...]
    production_candidates: int
    weak_module_candidates: int
    rejected: tuple[str, ...]
    auto_source_bytes: int
    auto_packet_bytes: int
    oracle_source_bytes: int
    oracle_packet_bytes: int
    common_evidence_bytes: int
    common_evidence_sha256: str
    empty_auto_equals_control: bool | None
    overlap: Overlap
    gold_public_passed: bool


def excerpt_lines(excerpts: tuple[SourceExcerpt, ...]) -> frozenset[SourceLine]:
    """Union line identities, including zero-source abstention."""
    return frozenset(
        SourceLine(excerpt.path, line)
        for excerpt in excerpts
        for line in range(excerpt.start, excerpt.end + 1)
    )


def source_overlap(auto: tuple[SourceExcerpt, ...], oracle: tuple[SourceExcerpt, ...]) -> Overlap:
    """Measure unique-line intersection after selection, without informing the selector."""
    selected = excerpt_lines(auto)
    expected = excerpt_lines(oracle)
    shared = selected & expected
    if not expected:
        raise BenchError("local oracle has no source lines")
    return Overlap(
        len(selected),
        len(expected),
        len(shared),
        len(shared) / len(expected),
        None if not selected else len(shared) / len(selected),
        sum(bool(excerpt_lines((excerpt,)) & selected) for excerpt in oracle),
        len(oracle),
    )


def validate_task(task: StudyTask, work: Path) -> tuple[LocalSelection, ...]:
    """Gold transitions supply repeatable dependent states; no agent edit is simulated."""
    repo = pinned_repo(task, work)
    directory = work / task.task.id
    workspace = directory / "workspace"
    fixture_workspace(task, repo, workspace)
    baseline = full_suite(workspace, task.task.test_command, 120)
    if not baseline.passed:
        raise BenchError(f"{task.task.id}: original public suite failed")
    rows: list[LocalSelection] = []
    for index in range(len(task.steps)):
        artifact = directory / f"step-{index + 1}"
        artifact.mkdir()
        inject_step(task, index, workspace)
        failing = full_suite(workspace, task.task.test_command, 120)
        if failing.passed or failing.timed_out:
            raise BenchError(f"{task.task.id}/{index + 1}: no finite public failure")
        evidence = failing.output.replace(str(workspace) + "/", "")
        freeze_fixture(workspace)
        selection = select_targets(
            evidence, str(workspace), task.roots, source_inventory(task, workspace)
        )
        prompts: dict[Arm, tuple[str, tuple[str, ...], int, int]] = {}
        for arm in (Arm.AUTO, Arm.CONTROL, Arm.ORACLE):
            destination = artifact / arm.value
            destination.mkdir()
            prompts[arm] = packet_prompt(task, index, arm, workspace, evidence, destination)
        auto_prompt, targets, source_bytes, packet_bytes = prompts[Arm.AUTO]
        oracle_prompt, oracle_targets, oracle_bytes, oracle_packet_bytes = prompts[Arm.ORACLE]
        control_prompt = prompts[Arm.CONTROL][0]
        empty_equal = auto_prompt == control_prompt if not targets else None
        if not targets and (not empty_equal or source_bytes != 0 or packet_bytes != 0):
            raise BenchError(f"{task.task.id}/{index + 1}: empty auto differs from control")
        auto_sources = tuple(source_excerpt(workspace, parse_target(target)) for target in targets)
        oracle_sources = tuple(
            source_excerpt(workspace, parse_target(target)) for target in oracle_targets
        )
        overlap = source_overlap(auto_sources, oracle_sources)
        if not oracle_prompt:
            raise BenchError(f"{task.task.id}/{index + 1}: oracle prompt is empty")
        restore_gold(task, index, workspace, repo)
        gold = full_suite(workspace, task.task.test_command, 120)
        (artifact / "gold.public.txt").write_text(gold.output, encoding="utf-8")
        if not gold.passed:
            raise BenchError(f"{task.task.id}/{index + 1}: gold public suite failed")
        row = LocalSelection(
            task.task.id,
            index + 1,
            bool(targets),
            targets,
            sum(candidate.rank >= AUTO_MIN_RANK for candidate in selection.candidates),
            sum(candidate.rank < AUTO_MIN_RANK for candidate in selection.candidates),
            selection.rejected,
            source_bytes,
            packet_bytes,
            oracle_bytes,
            oracle_packet_bytes,
            len(evidence.encode("utf-8")),
            digest(evidence),
            empty_equal,
            overlap,
            gold.passed,
        )
        (artifact / "selection-validation.json").write_text(
            json.dumps(asdict(row), indent=2) + "\n", encoding="utf-8"
        )
        rows.append(row)
        print(json.dumps(asdict(row)), flush=True)
    return tuple(rows)


def main() -> None:
    """Only local fixture/test commands run; execution approval is never consumed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    study = load_study(args.study, Path.cwd())
    work = args.work_dir.resolve()
    if "cimrihook-bench" not in str(work):
        raise BenchError("local replay must use the lowercase benchmark cwd marker")
    work.mkdir(parents=True, exist_ok=False)
    rows = tuple(row for task in study.tasks for row in validate_task(task, work))
    report: dict[str, object] = {
        "kind": "preparation-selector-v2-local-validation",
        "model_invocations": 0,
        "minimum_source_rank": AUTO_MIN_RANK,
        "task_n": len(study.tasks),
        "step_n": len(rows),
        "packets_emitted": sum(row.packet_emitted for row in rows),
        "abstentions": sum(not row.packet_emitted for row in rows),
        "state_basis": "frozen host gold transitions, not observed agent edits",
        "steps": [asdict(row) for row in rows],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
