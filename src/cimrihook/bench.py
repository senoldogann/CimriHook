"""Gerçek aboneliklerle A/B değerlendirme düzeneği: aynı görev ve ajan, CimriHook açık ve kapalı.

Her çalıştırma yalıtılmış bir çalışma alanında yapılır: görev deposu sabit bir sürümden kopyalanır,
testler için ayrı bir sanal ortam kurulur ve başlangıç durumu git ile mühürlenir. Ajan (Claude
Code ya da Codex CLI) kullanıcının oturum açmış aboneliğiyle etkileşimsiz modda çalışır. Başarı:
test paketinin geçmesi ve hiçbir test dosyasına dokunulmamış olması. Token kullanımı ajanın kendi
oturum kaydından (Claude transcript'i ve alt ajan kayıtları, Codex rollout'u) simülatörle aynı
ayrıştırıcılarla okunur.

Protokoller:
- single: tüm hatalar baştan enjekte edilir; ajan tek bir istekte hepsini düzeltir.
- sequential: hatalar aynı oturumda birer birer gelir; ajan her birini konuşmanın devamında
  düzeltir. Gerçek kullanımdaki uzun, birikimli oturumları taklit eder.

Varyantlar:
- baseline: ajanın varsayılan davranışı.
- cimrihook: bağlam yöneticisi (sıkıştırma penceresi) ve Claude Code'da codec hook'ları.
"""

import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from cimrihook.errors import BenchError
from cimrihook.settings import governor_env, hook_settings
from cimrihook.simulate import (
    ANTHROPIC,
    OPENAI,
    PriceSheet,
    SessionTrace,
    context_of,
    exact_cost,
    load_claude_trace,
    load_codex_trace,
)

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
BOOTSTRAP_ROUNDS: Final = 2000
BOOTSTRAP_SEED: Final = 7
# Opus 5.5 önbellek okumayı 0.05, Fable 5.1 0.025 çarpanıyla fiyatlar; diğer Claude modelleri 0.1.
CLAUDE_READ_WEIGHTS: Final = (("opus", 0.05), ("fable", 0.025))
FIRST_BUG_PROMPT: Final = (
    "The test suite of this repository fails because of a bug in the library code. Find the bug "
    "and fix it in the library code. Do not modify, add, or delete any test files. The test "
    "environment is ready: run the tests with `.venv/bin/python -m pytest -q`. You are done when "
    "the whole test suite passes."
)
NEXT_BUG_PROMPT: Final = (
    "A new bug has just been introduced in the library code and the test suite fails again. Find "
    "and fix it in the library code. Do not modify, add, or delete any test files. Run the tests "
    "with `.venv/bin/python -m pytest -q`. You are done when the whole test suite passes."
)


class Agent(StrEnum):
    """Değerlendirilen ajan."""

    CLAUDE = "claude"
    CODEX = "codex"


class Variant(StrEnum):
    """A/B kolu."""

    BASELINE = "baseline"
    CIMRIHOOK = "cimrihook"


class Protocol(StrEnum):
    """Görevin ajana veriliş biçimi."""

    SINGLE = "single"
    SEQUENTIAL = "sequential"


@dataclass(frozen=True, slots=True)
class Mutation:
    """Depoya enjekte edilen tek satırlık hata."""

    path: str
    find: str
    replace: str


@dataclass(frozen=True, slots=True)
class Task:
    """Görev tanımı (bench/tasks/*.json)."""

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
    """Tek bir çalıştırmanın tanımı."""

    task: Task
    protocol: Protocol
    agent: Agent
    variant: Variant
    model: str
    effort: str
    window: int
    repetition: int


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Ajan sürecinin çıkışı."""

    exit_code: int
    timed_out: bool
    stdout: str


@dataclass(frozen=True, slots=True)
class AgentRun:
    """Tek bir ajan çağrısının sonucu."""

    session_id: str
    exit_code: int
    timed_out: bool
    reported_usd: float | None  # yalnızca Claude Code API eşdeğeri maliyeti raporlar


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Bir çalıştırmadaki tüm ajan çağrılarının özeti."""

    session_id: str
    exit_code: int  # son çağrının çıkış kodu
    timed_out: bool  # herhangi bir çağrı süreyi aştı mı
    reported_usd: float | None
    duration_seconds: float
    steps: int
    steps_passed: int
    passed: bool  # sonda test paketi geçiyor mu


@dataclass(frozen=True, slots=True)
class Measurement:
    """Oturum kayıtlarından ölçülen token kullanımı."""

    requests: int
    compactions: int
    max_context: int
    mean_context: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    cost_base: float  # taban girdi fiyatı cinsinden, sağlayıcının fiyat oranlarıyla


@dataclass(frozen=True, slots=True)
class RunResult:
    """Bir çalıştırmanın kalıcı sonucu (bench/results/<ad>/<run_id>.json)."""

    run_id: str
    task_id: str
    protocol: str
    agent: str
    variant: str
    model: str
    effort: str
    window: int
    repetition: int
    session_id: str
    success: bool
    steps: int
    steps_passed: int
    tests_touched: bool
    agent_exit: int
    timed_out: bool
    duration_seconds: float
    reported_usd: float | None
    requests: int
    compactions: int
    max_context: int
    mean_context: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    cost_base: float
    error: str | None  # ölçülemeyen çalıştırmanın nedeni


def load_tasks(tasks_dir: Path) -> tuple[Task, ...]:
    """Görev tanımlarını yükler."""
    paths = sorted(tasks_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no task definitions (*.json) in {tasks_dir}")
    return tuple(load_task(path) for path in paths)


def select_tasks(tasks: Sequence[Task], task_ids: Sequence[str]) -> tuple[Task, ...]:
    """Yalnızca istenen görevler; bilinmeyen kimlik hatadır."""
    known = {task.id for task in tasks}
    unknown = sorted(set(task_ids) - known)
    if unknown:
        raise BenchError(f"unknown task ids {unknown}; available: {sorted(known)}")
    return tuple(task for task in tasks if task.id in task_ids)


def load_task(path: Path) -> Task:
    """Tek görev tanımını doğrulayarak okur."""
    where = str(path)
    data = json_object(json.loads(path.read_text(encoding="utf-8")), where)
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
    """Çalıştırma matrisi; aynı görev ve ajanın kolları art arda sıralanır."""
    return tuple(
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


def run_id(spec: RunSpec) -> str:
    """Çalıştırmanın kararlı kimliği."""
    return (
        f"{spec.task.id}.{spec.protocol.value}.{spec.agent.value}.{spec.variant.value}"
        f".r{spec.repetition}"
    )


def run_plan(
    specs: Sequence[RunSpec], results_dir: Path, work_dir: Path, concurrency: int, timeout: int
) -> tuple[RunResult, ...]:
    """Sonucu olmayan çalıştırmaları paralel yürütür; her sonuç bitince diske yazılır."""
    results_dir.mkdir(parents=True, exist_ok=True)
    pending = [spec for spec in specs if needs_run(result_path(results_dir, run_id(spec)))]
    repos = {
        (spec.task.repo, spec.task.ref): ensure_repo(spec.task.repo, spec.task.ref, work_dir)
        for spec in pending
    }
    finished: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                execute_run, spec, repos[(spec.task.repo, spec.task.ref)], work_dir, timeout
            )
            for spec in pending
        ]
        for future in as_completed(futures):
            result = future.result()
            result_path(results_dir, result.run_id).write_text(
                json.dumps(asdict(result), indent=2), encoding="utf-8"
            )
            print(json.dumps(progress_fields(result)), flush=True)
            finished.append(result)
    return tuple(finished)


def result_path(results_dir: Path, identifier: str) -> Path:
    """Çalıştırma sonucunun dosyası."""
    return results_dir / f"{identifier}.json"


def needs_run(path: Path) -> bool:
    """Sonucu yok ya da ölçülemeden bitmiş (ör. kullanım limiti) çalıştırmalar yeniden denenir."""
    if not path.exists():
        return True
    data = json_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    return data.get("error") is not None


def progress_fields(result: RunResult) -> dict[str, object]:
    """İlerleme satırının yapılandırılmış alanları."""
    return {
        "event": "run_finished",
        "run_id": result.run_id,
        "success": result.success,
        "steps_passed": f"{result.steps_passed}/{result.steps}",
        "cost_base": round(result.cost_base),
        "reported_usd": result.reported_usd,
        "requests": result.requests,
        "compactions": result.compactions,
        "max_context": result.max_context,
        "minutes": round(result.duration_seconds / 60, 1),
        "error": result.error,
    }


def execute_run(spec: RunSpec, repo_dir: Path, work_dir: Path, timeout: int) -> RunResult:
    """Görevi protokolüne göre çalıştırır; testleri ve token kullanımını ölçer."""
    identifier = run_id(spec)
    run_dir = work_dir / identifier
    if run_dir.exists():
        shutil.rmtree(run_dir)  # yarıda kalmış önceki denemenin artığı
    workspace = run_dir / "workspace"
    try:
        if spec.protocol is Protocol.SINGLE:
            outcome = run_single(spec, repo_dir, workspace, run_dir, timeout)
        else:
            outcome = run_sequential(spec, repo_dir, workspace, run_dir, timeout)
        touched = touched_test_files(workspace, repo_dir)
        measurement = measure_run(spec, outcome.session_id)
        if measurement.requests == 0:
            raise BenchError(f"{identifier}: the agent made no model requests")
    except BenchError as error:
        return failed_result(spec, identifier, str(error))
    return RunResult(
        run_id=identifier,
        task_id=spec.task.id,
        protocol=spec.protocol.value,
        agent=spec.agent.value,
        variant=spec.variant.value,
        model=spec.model,
        effort=spec.effort,
        window=spec.window,
        repetition=spec.repetition,
        session_id=outcome.session_id,
        success=outcome.passed and outcome.steps_passed == outcome.steps and not touched,
        steps=outcome.steps,
        steps_passed=outcome.steps_passed,
        tests_touched=bool(touched),
        agent_exit=outcome.exit_code,
        timed_out=outcome.timed_out,
        duration_seconds=outcome.duration_seconds,
        reported_usd=outcome.reported_usd,
        requests=measurement.requests,
        compactions=measurement.compactions,
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
    """Tüm hatalar baştan enjekte edilir; ajan tek istekte düzeltir."""
    prepare_workspace(spec.task, repo_dir, workspace)
    for mutation in spec.task.mutations:
        apply_mutation(workspace, mutation)
    if tests_pass(spec.task, workspace):
        raise BenchError(f"{spec.task.id}: test suite passes after the mutations; they are inert")
    seal(workspace)
    started = time.monotonic()
    call = start_agent(spec, spec.task.prompt, workspace, run_dir, timeout, 1)
    duration = time.monotonic() - started
    passed = tests_pass(spec.task, workspace)
    return SessionOutcome(
        session_id=call.session_id,
        exit_code=call.exit_code,
        timed_out=call.timed_out,
        reported_usd=call.reported_usd,
        duration_seconds=duration,
        steps=1,
        steps_passed=int(passed),
        passed=passed,
    )


def run_sequential(
    spec: RunSpec, repo_dir: Path, workspace: Path, run_dir: Path, timeout: int
) -> SessionOutcome:
    """Hatalar aynı oturumda birer birer gelir; bağlam gerçek kullanımdaki gibi birikir."""
    prepare_workspace(spec.task, repo_dir, workspace)
    if not tests_pass(spec.task, workspace):
        raise BenchError(f"{spec.task.id}: test suite fails before any mutation at {spec.task.ref}")
    started = time.monotonic()
    calls: list[AgentRun] = []
    steps_passed = 0
    for step, mutation in enumerate(spec.task.mutations, start=1):
        apply_mutation(workspace, mutation)
        if tests_pass(spec.task, workspace):
            raise BenchError(f"{spec.task.id}: step {step} mutation is inert after earlier fixes")
        seal(workspace)  # yeni hata git geçmişinde görünmesin
        if not calls:
            calls.append(start_agent(spec, FIRST_BUG_PROMPT, workspace, run_dir, timeout, step))
        else:
            session_id = calls[0].session_id
            calls.append(
                resume_agent(spec, session_id, NEXT_BUG_PROMPT, workspace, run_dir, timeout, step)
            )
        steps_passed += int(tests_pass(spec.task, workspace))
    return SessionOutcome(
        session_id=calls[0].session_id,
        exit_code=calls[-1].exit_code,
        timed_out=any(call.timed_out for call in calls),
        # Claude Code her sürdürmede oturumun o ana kadarki birikimli maliyetini raporlar.
        reported_usd=calls[-1].reported_usd,
        duration_seconds=time.monotonic() - started,
        steps=len(calls),
        steps_passed=steps_passed,
        passed=tests_pass(spec.task, workspace),
    )


def failed_result(spec: RunSpec, identifier: str, message: str) -> RunResult:
    """Ölçülemeyen çalıştırmanın kaydı; istatistiklerden çıkarılır, raporda görünür."""
    return RunResult(
        run_id=identifier,
        task_id=spec.task.id,
        protocol=spec.protocol.value,
        agent=spec.agent.value,
        variant=spec.variant.value,
        model=spec.model,
        effort=spec.effort,
        window=spec.window,
        repetition=spec.repetition,
        session_id="",
        success=False,
        steps=0,
        steps_passed=0,
        tests_touched=False,
        agent_exit=-1,
        timed_out=False,
        duration_seconds=0.0,
        reported_usd=None,
        requests=0,
        compactions=0,
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
    """Görev deposunu sabit sürümde bir kez klonlar; çalıştırmalar bu kopyadan çoğaltılır."""
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
    """Depoyu (git geçmişi olmadan) kopyalar ve test ortamını kurar."""
    shutil.copytree(repo_dir, workspace, ignore=shutil.ignore_patterns(".git"))
    python = workspace / ".venv" / "bin" / "python"
    run_checked(("uv", "venv", str(workspace / ".venv"), "--python", "3.12", "-q"), workspace)
    run_checked(("uv", "pip", "install", "--python", str(python), "-q", *task.packages), workspace)


def apply_mutation(workspace: Path, mutation: Mutation) -> None:
    """Tek bir hatayı, çapası dosyada tam bir kez geçiyorsa uygular."""
    path = workspace / mutation.path
    source = path.read_text(encoding="utf-8")
    count = source.count(mutation.find)
    if count != 1:
        raise BenchError(f"{mutation.path}: mutation anchor occurs {count} times, expected once")
    path.write_text(source.replace(mutation.find, mutation.replace), encoding="utf-8")


def seal(workspace: Path) -> None:
    """Çalışma alanının o anki durumunu tek commit'lik yeni bir git geçmişi olarak mühürler.

    Geçmiş her mühürde sıfırlanır; enjekte edilen hata ne `git diff` ne `git log` ile görünür,
    ajan ise git'i kendi değişikliklerini görmek için normal biçimde kullanabilir.
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
    """Test paketi geçiyor mu? Süre aşımı (ör. sonsuz döngü) geçmemek demektir."""
    try:
        completed = subprocess.run(
            list(task.test_command),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def touched_test_files(workspace: Path, repo_dir: Path) -> tuple[str, ...]:
    """El değmemiş depo kopyasına göre değişen, eklenen ya da silinen test dosyaları."""
    original = test_files(repo_dir)
    current = test_files(workspace)
    changed = {path for path in original.keys() & current.keys() if original[path] != current[path]}
    return tuple(sorted(changed | (original.keys() ^ current.keys())))


def test_files(root: Path) -> dict[str, bytes]:
    """Kök altındaki test dosyalarının içerikleri (git, sanal ortam ve önbellekler hariç)."""
    return {path: (root / path).read_bytes() for path in walk_files(root) if is_test_path(path)}


def walk_files(root: Path) -> tuple[str, ...]:
    """Kök altındaki dosyaların göreli yolları; yönetim dizinleri atlanır."""
    files: list[str] = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in IGNORED_DIRS]
        files.extend(str(Path(directory, name).relative_to(root)) for name in names)
    return tuple(files)


def is_test_path(path: str) -> bool:
    """Test dosyası mı (tests/ dizini, test_*.py, *_test.py, conftest.py)?"""
    parts = Path(path).parts
    name = parts[-1]
    return (
        bool(TEST_DIR_NAMES & set(parts[:-1]))
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def run_checked(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Komutu çalıştırır; başarısızsa bağlamıyla birlikte hata yükseltir."""
    completed = subprocess.run(list(command), cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise BenchError(
            f"command failed (exit {completed.returncode}) in {cwd}: {' '.join(command)}\n"
            f"stdout: {completed.stdout[-800:]}\nstderr: {completed.stderr[-800:]}"
        )
    return completed


def start_agent(
    spec: RunSpec, prompt: str, workspace: Path, run_dir: Path, timeout: int, step: int
) -> AgentRun:
    """Yeni bir ajan oturumu başlatır."""
    if spec.agent is Agent.CLAUDE:
        session_id = str(uuid.uuid4())
        session_args = ("--session-id", session_id)
        return claude_call(
            spec, prompt, session_args, session_id, workspace, run_dir, timeout, step
        )
    command = ("codex", "exec", *codex_options(spec, workspace), prompt)
    process = codex_call(command, workspace, run_dir, timeout, step)
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
    """Var olan oturumu yeni istekle sürdürür; bağlam önceki adımlardan birikir."""
    if spec.agent is Agent.CLAUDE:
        session_args = ("--resume", session_id)
        return claude_call(
            spec, prompt, session_args, session_id, workspace, run_dir, timeout, step
        )
    command = ("codex", "exec", *codex_options(spec, workspace), "resume", session_id, prompt)
    process = codex_call(command, workspace, run_dir, timeout, step)
    return AgentRun(session_id, process.exit_code, process.timed_out, None)


def codex_call(
    command: Sequence[str], workspace: Path, run_dir: Path, timeout: int, step: int
) -> ProcessOutcome:
    """Codex'i çalıştırır; sağlayıcı kaynaklı başarısız tur (ör. kullanım limiti) hatadır."""
    process = run_process(command, workspace, dict(os.environ), timeout, run_dir, step)
    failure = codex_turn_failure(process.stdout)
    if failure is not None:
        raise BenchError(f"codex turn failed at step {step}: {failure}")
    return process


def codex_turn_failure(stdout: str) -> str | None:
    """Codex olay akışındaki turn.failed iletisi; yoksa None."""
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue  # JSON olmayan uyarı satırı
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
    """Claude Code'u kullanıcının oturumuyla, kullanıcı ayarları ve eklentileri olmadan başlatır."""
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
        "--settings",
        json.dumps(claude_settings(spec)),
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        str(MAX_TURNS),
        "--allowedTools",
        CLAUDE_TOOLS,
    )
    process = run_process(command, workspace, claude_env(spec, run_dir), timeout, run_dir, step)
    failure = claude_failure(process.stdout, process.timed_out)
    if failure is not None:
        raise BenchError(f"claude call failed at step {step}: {failure}")
    return AgentRun(
        session_id, process.exit_code, process.timed_out, claude_reported_usd(process.stdout)
    )


def claude_failure(stdout: str, timed_out: bool) -> str | None:
    """Sağlayıcı kaynaklı hata (kullanım limiti, API hatası); süre ve tur sınırı davranıştır."""
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
    """Varyantın Claude Code ayarları: CimriHook kolunda hook'lar ve sıkıştırma penceresi."""
    if spec.variant is Variant.BASELINE:
        return {}
    return hook_settings(sys.executable) | governor_env(spec.window)


def claude_env(spec: RunSpec, run_dir: Path) -> dict[str, str]:
    """Varyantın ortamı; pencere doğrulanmış yol olan ortam değişkeniyle de verilir."""
    if spec.variant is Variant.BASELINE:
        return dict(os.environ)
    return dict(os.environ) | {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(spec.window),
        "CIMRIHOOK_HOME": str(run_dir / "ledger"),
    }


def claude_reported_usd(stdout: str) -> float | None:
    """Claude Code'un sonuç kaydındaki API eşdeğeri maliyet; sonuç yoksa (ör. süre aşımı) None."""
    try:
        decoded: object = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    records = decoded if isinstance(decoded, list) else [decoded]
    for record in reversed(records):
        if isinstance(record, dict) and record.get("type") == "result":
            cost = record.get("total_cost_usd")
            return float(cost) if isinstance(cost, int | float) else None
    return None


def codex_options(spec: RunSpec, workspace: Path) -> tuple[str, ...]:
    """Varyantın Codex seçenekleri; CimriHook kolunda otomatik sıkıştırma eşiği verilir."""
    window = (
        ()
        if spec.variant is Variant.BASELINE
        else ("-c", f"model_auto_compact_token_limit={spec.window}")
    )
    return (
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "--sandbox",
        "workspace-write",
        "-m",
        spec.model,
        "-c",
        f"model_reasoning_effort={spec.effort}",
        *window,
    )


def codex_thread_id(stdout: str) -> str:
    """Codex JSON olay akışındaki oturum (thread) kimliği."""
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue  # JSON olmayan uyarı satırı
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
    """Süreci kendi süreç grubunda çalıştırır; süre aşımında tüm grubu sonlandırır."""
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


def measure_run(spec: RunSpec, session_id: str) -> Measurement:
    """Ajanın oturum kayıtlarından token kullanımını ölçer."""
    if spec.agent is Agent.CLAUDE:
        return measure(claude_traces(session_id), claude_prices(spec.model))
    return measure(codex_traces(session_id), OPENAI)


def claude_traces(session_id: str) -> tuple[SessionTrace, ...]:
    """Ana transcript ve alt ajan transcript'leri."""
    main = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    if len(main) != 1:
        raise BenchError(f"expected one Claude transcript for {session_id}, found {len(main)}")
    subagents = sorted(
        path
        for path in CLAUDE_PROJECTS.glob(f"*/{session_id}/subagents/**/*.jsonl")
        if path.name != WORKFLOW_JOURNAL
    )
    return tuple(load_claude_trace(path) for path in (*main, *subagents))


def codex_traces(thread_id: str) -> tuple[SessionTrace, ...]:
    """Codex rollout kaydı."""
    paths = sorted(CODEX_SESSIONS.rglob(f"rollout-*{thread_id}.jsonl"))
    if len(paths) != 1:
        raise BenchError(f"expected one Codex rollout for {thread_id}, found {len(paths)}")
    return (load_codex_trace(paths[0]),)


def claude_prices(model: str) -> PriceSheet:
    """Modelin önbellek okuma çarpanıyla Anthropic fiyat oranları."""
    for marker, read in CLAUDE_READ_WEIGHTS:
        if marker in model.lower():
            return replace(ANTHROPIC, read=read)
    return ANTHROPIC


def measure(traces: Sequence[SessionTrace], prices: PriceSheet) -> Measurement:
    """İstek dizilerinden toplam kullanım ve maliyet."""
    requests = [usage for trace in traces for usage in trace.requests]
    contexts = [context_of(usage) for usage in requests]
    return Measurement(
        requests=len(requests),
        compactions=sum(len(trace.post_compact_tokens) for trace in traces),
        max_context=max(contexts, default=0),
        mean_context=statistics.fmean(contexts) if contexts else 0.0,
        uncached=sum(usage.uncached for usage in requests),
        cache_write=sum(usage.write_5m + usage.write_1h for usage in requests),
        cache_read=sum(usage.read for usage in requests),
        output=sum(usage.output for usage in requests),
        cost_base=sum(exact_cost(usage, prices) for usage in requests),
    )


def load_results(results_dir: Path) -> tuple[RunResult, ...]:
    """Bir sonuç kümesindeki tüm çalıştırma sonuçları."""
    paths = sorted(results_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no run results in {results_dir}")
    return tuple(
        result_from_json(json_object(json.loads(path.read_text(encoding="utf-8")), str(path)))
        for path in paths
    )


def result_from_json(data: dict[str, object]) -> RunResult:
    """Sonuç dosyasını tipleri doğrulayarak okur."""
    where = "result"
    return RunResult(
        run_id=text_field(data, "run_id", where),
        task_id=text_field(data, "task_id", where),
        protocol=text_field(data, "protocol", where),
        agent=text_field(data, "agent", where),
        variant=text_field(data, "variant", where),
        model=text_field(data, "model", where),
        effort=text_field(data, "effort", where),
        window=int_field(data, "window", where),
        repetition=int_field(data, "repetition", where),
        session_id=text_field(data, "session_id", where),
        success=bool_field(data, "success", where),
        steps=int_field(data, "steps", where),
        steps_passed=int_field(data, "steps_passed", where),
        tests_touched=bool_field(data, "tests_touched", where),
        agent_exit=int_field(data, "agent_exit", where),
        timed_out=bool_field(data, "timed_out", where),
        duration_seconds=float_field(data, "duration_seconds", where),
        reported_usd=optional_float_field(data, "reported_usd", where),
        requests=int_field(data, "requests", where),
        compactions=int_field(data, "compactions", where),
        max_context=int_field(data, "max_context", where),
        mean_context=float_field(data, "mean_context", where),
        uncached=int_field(data, "uncached", where),
        cache_write=int_field(data, "cache_write", where),
        cache_read=int_field(data, "cache_read", where),
        output=int_field(data, "output", where),
        cost_base=float_field(data, "cost_base", where),
        error=optional_text_field(data, "error", where),
    )


def scenario(result: RunResult) -> str:
    """Raporlarda görev ve protokolün birlikte adı."""
    return f"{result.task_id}/{result.protocol}"


def render_bench_report(results: Sequence[RunResult]) -> str:
    """Hücre özetleri, senaryo bazında A/B oranları ve ajan bazında toplam oran (bootstrap)."""
    measured = [result for result in results if result.error is None]
    errors = [result for result in results if result.error is not None]
    lines = [
        f"CimriHook bench: {len(results)} runs, {len(errors)} unmeasured",
        f"  {'agent':<7} {'scenario':<30} {'variant':<10} {'n':>2} {'pass':>6} {'steps':>7}"
        f" {'cost(base)':>12} {'usd':>7} {'req':>5} {'max ctx':>9} {'compact':>8} {'min':>6}",
    ]
    for agent, label, variant in sorted({(r.agent, scenario(r), r.variant) for r in measured}):
        cell = [r for r in measured if (r.agent, scenario(r), r.variant) == (agent, label, variant)]
        lines.append(cell_line(agent, label, variant, cell))
    lines.append("A/B per scenario (cimrihook vs baseline, median cost ratio):")
    lines.extend(scenario_comparisons(measured))
    lines.append("A/B per agent (sum of costs, 95% bootstrap interval):")
    lines.extend(agent_comparisons(measured))
    lines.extend(f"  unmeasured {result.run_id}: {result.error}" for result in errors)
    return "\n".join(lines)


def cell_line(agent: str, label: str, variant: str, cell: Sequence[RunResult]) -> str:
    """Bir (ajan, senaryo, varyant) hücresinin satırı (medyanlar)."""
    usd = [r.reported_usd for r in cell if r.reported_usd is not None]
    usd_text = f"{statistics.median(usd):.2f}" if usd else "-"
    passed = sum(1 for r in cell if r.success)
    steps = f"{sum(r.steps_passed for r in cell)}/{sum(r.steps for r in cell)}"
    return (
        f"  {agent:<7} {label:<30} {variant:<10} {len(cell):>2} {f'{passed}/{len(cell)}':>6}"
        f" {steps:>7} {statistics.median(r.cost_base for r in cell):>12,.0f} {usd_text:>7}"
        f" {statistics.median(r.requests for r in cell):>5.0f}"
        f" {statistics.median(r.max_context for r in cell):>9,.0f}"
        f" {sum(r.compactions for r in cell):>8}"
        f" {statistics.median(r.duration_seconds for r in cell) / 60:>6.1f}"
    )


def scenario_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """Senaryo bazında maliyet oranı ve başarı karşılaştırması."""
    lines: list[str] = []
    for agent, label in sorted({(r.agent, scenario(r)) for r in measured}):
        base = arm(measured, agent, label, Variant.BASELINE)
        treated = arm(measured, agent, label, Variant.CIMRIHOOK)
        if not base or not treated:
            continue
        ratio = statistics.median(r.cost_base for r in treated) / statistics.median(
            r.cost_base for r in base
        )
        lines.append(
            f"  {agent:<7} {label:<30} cost x{ratio:.2f}  pass "
            f"{sum(r.success for r in base)}/{len(base)} -> "
            f"{sum(r.success for r in treated)}/{len(treated)}"
        )
    return lines


def agent_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """Ajan bazında, her iki kolu da ölçülmüş senaryolar üzerinden toplam maliyet oranı."""
    lines: list[str] = []
    for agent in sorted({r.agent for r in measured}):
        labels = sorted(
            label
            for label in {scenario(r) for r in measured if r.agent == agent}
            if arm(measured, agent, label, Variant.BASELINE)
            and arm(measured, agent, label, Variant.CIMRIHOOK)
        )
        if not labels:
            continue
        base_arms = [arm(measured, agent, label, Variant.BASELINE) for label in labels]
        treated_arms = [arm(measured, agent, label, Variant.CIMRIHOOK) for label in labels]
        base = [[r.cost_base for r in runs] for runs in base_arms]
        treated = [[r.cost_base for r in runs] for runs in treated_arms]
        ratio = sum(map(sum, treated)) / sum(map(sum, base))
        low, high = bootstrap_ratio(base, treated, BOOTSTRAP_ROUNDS, BOOTSTRAP_SEED)
        base_pass = sum(r.success for runs in base_arms for r in runs)
        treated_pass = sum(r.success for runs in treated_arms for r in runs)
        lines.append(
            f"  {agent:<7} cost x{ratio:.2f} (95% interval {low:.2f}-{high:.2f}) over "
            f"{len(labels)} scenarios; pass {base_pass}/{sum(map(len, base_arms))} -> "
            f"{treated_pass}/{sum(map(len, treated_arms))}"
        )
    return lines


def arm(measured: Sequence[RunResult], agent: str, label: str, variant: Variant) -> list[RunResult]:
    """Bir senaryonun bir koluna ait ölçülmüş çalıştırmalar."""
    return [
        r for r in measured if r.agent == agent and scenario(r) == label and r.variant == variant
    ]


def bootstrap_ratio(
    base: Sequence[Sequence[float]], treated: Sequence[Sequence[float]], rounds: int, seed: int
) -> tuple[float, float]:
    """Hücre içi yeniden örneklemeyle toplam maliyet oranının %95 yüzdelik aralığı."""
    rng = random.Random(seed)
    ratios = sorted(
        sum(sum(rng.choice(cell) for _ in cell) for cell in treated)
        / sum(sum(rng.choice(cell) for _ in cell) for cell in base)
        for _ in range(rounds)
    )
    return ratios[int(0.025 * rounds)], ratios[int(0.975 * rounds) - 1]


def json_object(value: object, where: str) -> dict[str, object]:
    """JSON nesnesi bekler."""
    if not isinstance(value, dict):
        raise BenchError(f"{where}: expected a JSON object")
    return {str(key): item for key, item in value.items()}


def text_field(data: dict[str, object], key: str, where: str) -> str:
    """Zorunlu metin alanı."""
    value = data.get(key)
    if not isinstance(value, str):
        raise BenchError(f"{where}: field {key!r} must be a string")
    return value


def optional_text_field(data: dict[str, object], key: str, where: str) -> str | None:
    """Boş olabilen metin alanı."""
    if data.get(key) is None:
        return None
    return text_field(data, key, where)


def int_field(data: dict[str, object], key: str, where: str) -> int:
    """Zorunlu tam sayı alanı."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError(f"{where}: field {key!r} must be an integer")
    return value


def float_field(data: dict[str, object], key: str, where: str) -> float:
    """Zorunlu sayı alanı."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BenchError(f"{where}: field {key!r} must be a number")
    return float(value)


def optional_float_field(data: dict[str, object], key: str, where: str) -> float | None:
    """Boş olabilen sayı alanı."""
    if data.get(key) is None:
        return None
    return float_field(data, key, where)


def bool_field(data: dict[str, object], key: str, where: str) -> bool:
    """Zorunlu mantıksal alan."""
    value = data.get(key)
    if not isinstance(value, bool):
        raise BenchError(f"{where}: field {key!r} must be a boolean")
    return value


def text_list(data: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    """Metin listesi alanı."""
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BenchError(f"{where}: field {key!r} must be a list of strings")
    return tuple(str(item) for item in value)


def object_list(data: dict[str, object], key: str, where: str) -> tuple[dict[str, object], ...]:
    """Nesne listesi alanı."""
    value = data.get(key)
    if not isinstance(value, list):
        raise BenchError(f"{where}: field {key!r} must be a list of objects")
    return tuple(json_object(item, f"{where}.{key}") for item in value)
