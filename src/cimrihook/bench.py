"""Gerçek aboneliklerle A/B değerlendirme düzeneği: aynı görev ve ajan, CimriHook mekanizmaları
açık ve kapalı.

Her çalıştırma yalıtılmış bir çalışma alanında yapılır: görev deposu sabit bir sürümden kopyalanır,
testler için ayrı bir sanal ortam kurulur ve başlangıç durumu git ile mühürlenir. Ajan (Claude
Code ya da Codex CLI) kullanıcının oturum açmış aboneliğiyle etkileşimsiz modda, devralınmamış bir
ortamla çalışır: yalnızca izin listesindeki ortam değişkenleri geçer, Claude Code'a kullanıcı
ayarları ve MCP sunucuları, Codex'e kullanıcının config.toml'u yüklenmez. Başarı: her adımdan sonra
test paketinin geçmesi ve hiçbir test dosyasına dokunulmamış olması.

Protokoller:
- single: tüm hatalar baştan enjekte edilir; ajan tek bir istekte hepsini düzeltir.
- sequential: hatalar aynı oturumda birer birer gelir; ajan her birini konuşmanın devamında
  düzeltir. Gerçek kullanımdaki uzun, birikimli oturumları taklit eder.
- deep: sequential'dan önce ajan hata yokken kütüphanenin tüm kaynak dosyalarını okur (yalnızca
  Claude Code). Bağlam baştan büyür ve okunan kodun çoğu sonraki adımlar için bayatlar; gerçek
  kullanımdaki yüksek bağlamlı (200k üstü) oturumları taklit eder.
- deeper: deep gibi, ama ısınmada test dosyaları da okunur; oturum 400k'nın üstüne çıkar.

Varyantlar (mekanizma ablasyonu):
- baseline: ajanın varsayılan davranışı.
- governor: yalnızca sıkıştırma penceresi (Claude Code: CLAUDE_CODE_AUTO_COMPACT_WINDOW, en az
  100000; Codex: model_auto_compact_token_limit).
- rtk ve rtk-governor: RTK'nın Bash komut çıktısı sıkıştırması (PreToolUse hook'u, `rtk hook
  claude`), tek başına ve pencereyle birlikte (yalnızca Claude Code): RTK var ya da yok, pencere
  var ya da yok.
- mask: pencere ve CimriHook mod'u (`--plugin-dir`), önce maskeleme açık: otomatik sıkıştırmada
  LLM özeti yerine eski araç sonuçları yer tutucuyla değişir (yalnızca Claude Code).
- boundary: pencere ve CimriHook mod'u görev sınırında sıkıştırmayla: bağlam BOUNDARY_TOKENS'ı
  geçtiyse yeni istemden önce sıkıştırılır; pencere görevin içindeki yedektir (yalnızca Claude
  Code).
- codec, combined ve brief: emekliye ayrıldı (araç sonucu yeniden kodlama ve sıkıştırma özeti
  talimatı ürünün parçası değil; kodları `pre-trim` etiketinde). Bu kolların kayıtlı sonuçları
  okunur ve raporlanır, yeni çalıştırma planlanamaz.
- meter ve meter-governor: baseline ve governor gibi davranır, ama CimriHook mod'u yalnızca
  limit ölçeri olarak yüklenir: her turdan sonra aboneliğin 5 saatlik ve haftalık pencerelerinin
  kullanım yüzdesi koşunun yanına `<koşu>.limits.jsonl` olarak yazılır (yalnızca Claude Code,
  abonelikle). Pencere puanı cinsinden A/B için iki kol da meter olmalıdır.

Ölçüm:
- Birincil maliyet sağlayıcı düzeyindedir ve sıkıştırma ile yardımcı çağrıları içerir. Claude Code
  için her adımın sonuç kaydındaki kümülatif total_cost_usd (USD), Codex için rollout'taki yanıt
  başına token_usage_record kayıtları, açık bir fiyat tablosuyla taban girdi fiyatı cinsinden.
- İkincil maliyet (cost_base) yalnızca oturum kayıtlarındaki istekleri (Claude transcript'i, Codex
  token_count olayları) simülatörle aynı ayrıştırıcılarla toplar; sıkıştırma çağrısını içermez.
- Raporda kollar senaryo bazında log maliyetlerin geometrik ortalamasıyla karşılaştırılır; ajan
  düzeyindeki özet yalnızca iki kolu eşit sayıda ölçülmüş senaryoları eşit ağırlıkla birleştirir.
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
# Ajan süreçlerine geçen ortam değişkenleri. Geri kalanı devralınmaz: benchmark bir Claude Code
# oturumundan başlatıldığında o oturumun CLAUDE_CODE_*, ANTHROPIC_* gibi değişkenleri ajanın
# modelini, effort seviyesini, API adresini ve davranışını değiştirir.
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
TEST_TAIL_CHARS: Final = 1_500  # hata iletisine eklenen test çıktısı
SAFE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # görev kimliği ve sürüm
SAFE_PACKAGE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\],<>=!~-]*")
USD: Final = "usd"
BASE_INPUT_TOKENS: Final = "base_input_tokens"
CLAUDE_PRICE_SHEET: Final = "Claude Code total_cost_usd (includes compaction and auxiliary calls)"
CHECKPOINT_STEPS: Final = (5, 10, 20, 40)
NONINFERIORITY_MARGIN: Final = 0.05  # çalıştırma başarısında kabul edilen en büyük düşüş
# Karar çalıştırma düzeyindedir: bir çalıştırmanın adımları birbirine bağlıdır (takılan ajan sonraki
# adımları da kaçırır), adımları bağımsız saymak aralığı yapay olarak daraltır. Hiç başarısızlık
# yokken bile -5 puanı dışlamak kol başına yaklaşık 75 çalıştırma ister; daha azıyla karar çoğu
# zaman "gösterilemedi" olur. Bu sayının altında karar hiç verilmez.
MIN_RUNS_FOR_VERDICT: Final = 5
# Simülatörün kabul edilen tahmin hatası (oran puanı): tahmin ölçülen oranın bu kadar yakınında.
CALIBRATION_TOLERANCE: Final = 0.05
# Codex fiyat tablosu duyarlılığı: önbellekli girdi ve çıktı çarpanlarının makul aralığı.
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
    """Değerlendirilen ajan."""

    CLAUDE = "claude"
    CODEX = "codex"


class Variant(StrEnum):
    """A/B kolu: açık olan CimriHook mekanizmaları."""

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
METER_DIR: Final = "meter"  # CIMRIHOOK_HOME of a mod arm, inside the run directory
BOUNDARY_TOKENS: Final = 100_000  # boundary arm: compact before a prompt above this context
RTK_VARIANTS: Final = frozenset({Variant.RTK, Variant.RTK_GOVERNOR})
RTK_HOOK_COMMAND: Final = "rtk hook claude"  # RTK 0.51'in Claude Code kurulumundaki komut
# Arms of the codec and the summary instruction, which are no longer part of the product. Their
# results from earlier sets are still loaded and reported; new runs cannot be planned.
RETIRED_VARIANTS: Final = frozenset({Variant.CODEC, Variant.COMBINED, Variant.BRIEF})
CODEX_VARIANTS: Final = frozenset({Variant.BASELINE, Variant.GOVERNOR})
# Önceki şemanın tek tedavi kolu: Claude Code'da pencere ve codec, Codex'te yalnızca pencere.
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
    """Görevin ajana veriliş biçimi."""

    SINGLE = "single"
    SEQUENTIAL = "sequential"
    DEEP = "deep"
    DEEPER = "deeper"


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
class SuiteRun:
    """Test paketinin bir çalıştırması."""

    passed: bool
    tail: str  # çıktının sonu, hata iletileri için


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Ajan sürecinin çıkışı."""

    exit_code: int
    timed_out: bool
    stdout: str


@dataclass(frozen=True, slots=True)
class ReportedUsage:
    """Claude Code'un sonuç kaydındaki oturum toplamları (tüm modeller, sürdürmelerde kümülatif)."""

    cost_usd: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int


@dataclass(frozen=True, slots=True)
class AgentRun:
    """Tek bir ajan çağrısının sonucu."""

    session_id: str
    exit_code: int
    timed_out: bool
    reported: ReportedUsage | None  # yalnızca Claude Code raporlar; süre aşımında yoktur


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Bir çalıştırmadaki tüm ajan çağrılarının özeti."""

    session_id: str
    exit_code: int  # son çağrının çıkış kodu
    timed_out: bool  # herhangi bir çağrı süreyi aştı mı
    reported: tuple[ReportedUsage | None, ...]  # adım başına Claude Code raporu
    duration_seconds: float
    step_passed: tuple[bool, ...]  # her adımdan sonra test paketi geçti mi
    passed: bool  # sonda test paketi geçiyor mu


@dataclass(frozen=True, slots=True)
class Measurement:
    """Oturum kayıtlarındaki isteklerden ölçülen token kullanımı (sıkıştırma çağrısı hariç)."""

    requests: int
    compactions: int
    compaction_pre_tokens: tuple[int, ...]  # sıkıştırmaları tetikleyen bağlam boyutları
    max_context: int
    mean_context: float
    uncached: int
    cache_write: int
    cache_read: int
    output: int
    cost_base: float  # taban girdi fiyatı cinsinden, sağlayıcının fiyat oranlarıyla


@dataclass(frozen=True, slots=True)
class ProviderMeasurement:
    """Sağlayıcı düzeyinde ölçüm: sıkıştırma ve yardımcı çağrılar dahil birincil maliyet."""

    cost_by_step: tuple[float, ...]  # adım sonlarındaki kümülatif maliyet; son eleman toplamdır
    unit: str  # USD ya da BASE_INPUT_TOKENS
    price_sheet: str
    uncached: int
    cache_write: int
    cache_read: int
    output: int


@dataclass(frozen=True, slots=True)
class CodexRecords:
    """Codex rollout'undaki yanıt başına kullanım kayıtları, görev (adım) sırasıyla."""

    steps: tuple[tuple[Usage, ...], ...]
    cli_version: str


@dataclass(frozen=True, slots=True)
class AgentLogs:
    """Bir çalıştırmanın ajan kayıtlarından okunan ölçümler."""

    measurement: Measurement
    provider: ProviderMeasurement | None  # bir adımın sağlayıcı toplamı yoksa None
    version: str


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Bir çalıştırmanın ne olduğu: görev, ajan, kol ve koşullar."""

    run_id: str
    task_id: str
    protocol: str
    agent: Agent
    variant: str  # çalıştırıldığı adıyla kol
    mechanism: Variant  # gerçekte açık olan mekanizma
    model: str
    effort: str
    window: int
    isolation: str
    repetition: int


@dataclass(frozen=True, slots=True)
class RunBehaviour:
    """Ajanın çalıştırmadaki davranışı ve görev sonucu."""

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
    """Bir çalıştırmanın kalıcı sonucu (bench/results/<ad>/<run_id>.json)."""

    schema: int
    run_id: str
    task_id: str
    protocol: str
    agent: str
    variant: str  # çalıştırıldığı adıyla kol (eski şemada "cimrihook")
    mechanism: str  # gerçekte açık olan mekanizma: baseline, governor, codec ya da combined
    model: str
    effort: str
    window: int  # istenen sıkıştırma penceresi
    effective_window: int | None  # ajanın uyguladığı pencere; kol pencere ayarlamıyorsa None
    isolation: str  # ajan sürecinin ortamı ve yüklenen yapılandırma
    agent_version: str
    repetition: int
    session_id: str
    success: bool
    steps: int
    steps_passed: int
    step_passed: tuple[bool, ...]  # adım başına sonuç; eski şemada bilinmiyorsa boş
    tests_touched: bool
    agent_exit: int
    timed_out: bool
    duration_seconds: float
    provider: ProviderMeasurement | None  # birincil maliyet; bilinmiyorsa None
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
    """Tek görev tanımını doğrulayarak okur.

    Görev tanımı komut satırlarına ve yollara girer (git clone, uv pip install, çalıştırma dizini);
    seçenek gibi ya da dizin dışına çıkan değerler hatadır.
    """
    where = str(path)
    data = json_object(json.loads(path.read_text(encoding="utf-8")), where)
    task = parse_task(data, where)
    problems = task_problems(task)
    if problems:
        raise BenchError(f"{where}: {'; '.join(problems)}")
    return task


def task_problems(task: Task) -> list[str]:
    """Görev tanımındaki güvensiz değerler."""
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
    """Görev alanlarını tipleriyle okur."""
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
    """Çalıştırma matrisi; aynı görev ve ajanın kolları art arda sıralanır.

    Ajanın uygulayamayacağı kollar ve pencereler hatadır; sessizce farklı bir koşul ölçülmez.
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
    """Ajanın bu kolu ya da pencereyi uygulayamamasının nedeni; uygulayabiliyorsa None."""
    if spec.variant in RETIRED_VARIANTS:
        return (
            f"variant {spec.variant.value!r} is retired: its mechanism was removed from CimriHook "
            "(the code is at the git tag pre-trim); its recorded results can still be reported"
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
    """Ajanın gerçekte uyguladığı sıkıştırma penceresi; kol pencere ayarlamıyorsa None.

    Claude Code 2.1.288 CLAUDE_CODE_AUTO_COMPACT_WINDOW'u en az 100000'e yükseltir ve otomatik
    sıkıştırmayı pencere − 20000 (çıktı payı) − 13000 token bağlamda tetikler.
    """
    if mechanism not in WINDOW_VARIANTS:
        return None
    if agent is Agent.CLAUDE:
        return max(window, CLAUDE_MIN_COMPACT_WINDOW)
    return window


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
    """Çalıştırmayı yürütür ve sonucunu hemen yazar: başka bir çalıştırmanın beklenmedik hatası
    bitmiş sonuçları kaybettirmez."""
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
    """Çalıştırma sonucunun dosyası."""
    return results_dir / f"{identifier}.json"


def needs_run(path: Path, spec: RunSpec) -> bool:
    """Sonucu yok ya da ölçülemeden bitmiş (ör. kullanım limiti) çalıştırmalar yeniden denenir.

    Var olan sonuç kolu etkileyen başka koşullarla (model, effort, pencere) ölçülmüşse aynı sette
    karıştırılmaz: hata.
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
    """İlerleme satırının yapılandırılmış alanları."""
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
    """Çalıştırmanın kimliği, ajan davranışı ve kayıtlardan ölçümler tek sonuç kaydında."""
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
    """Tüm hatalar baştan enjekte edilir; ajan tek istekte düzeltir."""
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
    """Hatalar aynı oturumda birer birer gelir; bağlam gerçek kullanımdaki gibi birikir.

    deep protokolünde oturum, ilk hatadan önce kütüphanenin okunduğu ısınma çağrısıyla başlar.
    Isınma bir adım sayılmaz; maliyeti Claude Code'un kümülatif raporu sayesinde ilk adımın
    toplamına girer.
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
        seal(workspace)  # yeni hata git geçmişinde görünmesin
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
    """deep protokolünün ısınma çağrısı: ajan hata yokken kütüphaneyi okur, hiçbir dosyayı
    değiştirmez. Süre aşımında maliyeti kaydedilemediği için çalıştırma ölçülemez."""
    seal(workspace)
    call = start_agent(spec, WARMUP_PROMPTS[spec.protocol.value], workspace, run_dir, timeout, 0)
    if call.timed_out:
        raise BenchError(f"{spec.task.id}: the warm-up call timed out after {timeout}s")
    changed = run_checked(("git", "status", "--porcelain"), workspace).stdout.strip()
    if changed:
        raise BenchError(f"{spec.task.id}: the warm-up call changed files:\n{changed[:800]}")
    return call


def failed_result(spec: RunSpec, identifier: str, message: str) -> RunResult:
    """Ölçülemeyen çalıştırmanın kaydı; istatistiklerden çıkarılır, raporda görünür."""
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
    """Ölçüm alanları boş, nedeni error alanında olan sonuç kaydı."""
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
    """Test paketi geçiyor mu?"""
    return run_suite(task, workspace).passed


def run_suite(task: Task, workspace: Path) -> SuiteRun:
    """Test paketini ajanınkiyle aynı izin listesindeki ortamla çalıştırır (testler ajanın
    değiştirdiği kodu çalıştırır). Süre aşımı (ör. sonsuz döngü) geçmemek demektir."""
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
    """Test, git ve uv süreçlerinin ortamı: yalnızca izin listesindeki değişkenler. Bu süreçler
    ajanın yazdığı kodu ve git yapılandırmasını çalıştırır; oturumun gizli değişkenlerini görmez."""
    return {key: base[key] for key in ENV_ALLOWLIST if key in base}


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
    """Yeni bir ajan oturumu başlatır."""
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
    """Var olan oturumu yeni istekle sürdürür; bağlam önceki adımlardan birikir."""
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
    """Codex'i çalıştırır; sağlayıcı kaynaklı başarısız tur (ör. kullanım limiti) hatadır."""
    process = run_process(command, workspace, env, timeout, run_dir, step)
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
    """Claude Code'u kullanıcının oturumuyla; kullanıcı ayarları, eklentileri ve MCP sunucuları
    (claude.ai bağlayıcıları dahil) olmadan başlatır."""
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
    """Varyantın Claude Code ayarları: RTK kollarında RTK hook'u, pencere kollarında pencere."""
    return merge_settings(
        [
            *([rtk_settings()] if spec.variant in RTK_VARIANTS else []),
            *([governor_env(spec.window)] if spec.variant in WINDOW_VARIANTS else []),
        ]
    )


def rtk_settings() -> dict[str, object]:
    """RTK'nın Claude Code hook'u: Bash komutlarını sıkıştıran sürümleriyle yeniden yazar."""
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
    """Ajan sürecinin ortamı: izin listesindeki değişkenler ve kolun kendi ayarları.

    Claude Code'da pencere, ayarlardaki env bloğuna ek olarak doğrudan ortam değişkeniyle de
    verilir; mod kollarında CimriHook'un ev dizini çalıştırmanın kendi dizinindedir.
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
    """Mod kollarında CimriHook mod'unu çalıştırmanın kendi dizininden yükleyen seçenek."""
    if spec.variant not in MOD_VARIANTS:
        return ()
    return ("--plugin-dir", str(write_mod(run_dir / "mod" / MOD_NAME)))


def claude_reported_usage(stdout: str) -> ReportedUsage | None:
    """Claude Code'un sonuç kaydındaki oturum toplamları; sonuç yoksa (ör. süre aşımı) None.

    total_cost_usd ve modelUsage, oturum sürdürüldüğünde önceki çağrıların toplamını da içerir;
    sıkıştırma ve yardımcı model çağrıları dahildir.
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
    """Varyantın Codex seçenekleri; kullanıcı config.toml'u yüklenmez, pencere kolu eşik verir."""
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


def read_agent_logs(
    agent: Agent,
    session_id: str,
    reported: Sequence[ReportedUsage | None],
    steps: int,
) -> AgentLogs:
    """Ajanın oturum kayıtlarından ölçümler; model isteği yoksa çalıştırma ölçülemez."""
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
    """Oturumun ana transcript'i."""
    main = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    if len(main) != 1:
        raise BenchError(f"expected one Claude transcript for {session_id}, found {len(main)}")
    return main[0]


def claude_traces(transcript: Path, session_id: str) -> tuple[SessionTrace, ...]:
    """Ana transcript ve alt ajan transcript'leri."""
    subagents = sorted(
        path
        for path in transcript.parent.glob(f"{session_id}/subagents/**/*.jsonl")
        if path.name != WORKFLOW_JOURNAL
    )
    return tuple(load_claude_trace(path) for path in (transcript, *subagents))


def claude_version(transcript: Path) -> str:
    """Transcript'i yazan Claude Code sürümü."""
    with transcript.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            version = None if entry is None else entry.get("version")
            if isinstance(version, str):
                return version
    raise BenchError(f"{transcript}: no entry carries a Claude Code version")


def claude_provider(reported: Sequence[ReportedUsage | None]) -> ProviderMeasurement | None:
    """Claude Code'un adım başına kümülatif raporlarından birincil maliyet.

    Bir adım rapor vermediyse (süre aşımıyla sonlandırılan süreç maliyetini kaydedemez) sonraki
    toplamlar da o adımı içermez; bu durumda maliyet bilinmez ve None döner.
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
    """Codex rollout kaydı."""
    paths = sorted(CODEX_SESSIONS.rglob(f"rollout-*{thread_id}.jsonl"))
    if len(paths) != 1:
        raise BenchError(f"expected one Codex rollout for {thread_id}, found {len(paths)}")
    return paths[0]


def load_codex_records(path: Path) -> CodexRecords:
    """Rollout'taki token_usage_record kayıtları, görevlere (task_started) göre gruplanmış.

    Bu kayıtlar sıkıştırma isteğini de içerir; token_count olayları içermez. Aynı yanıt kimliği
    birden çok kez yazıldıysa son kullanım geçerlidir ve ilk göründüğü görevde sayılır.
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
    """token_usage_record kullanımı; input_tokens önbellekten okunan ve yazılan girdiyi içerir."""
    usage = json_object(payload.get("usage"), f"{where}.usage")
    total_input = int_field(usage, "input_tokens", where)
    cached = int_field(usage, "cached_input_tokens", where)
    written = usage.get("cache_write_input_tokens")
    write = written if isinstance(written, int) else 0  # eski sürümler yazım alanını raporlamaz
    return Usage(
        uncached=total_input - cached - write,
        write_5m=write,
        write_1h=0,
        read=cached,
        output=int_field(usage, "output_tokens", where),
    )


def codex_provider(records: CodexRecords, steps: int) -> ProviderMeasurement:
    """Yanıt başına kayıtlardan birincil maliyet; model isteği olmayan adım kota hatasıdır."""
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
    """Fiyat tablosunun sonuçlara yazılan tanımı (taban girdi fiyatı = 1)."""
    return (
        f"{sheet.name}: uncached {sheet.uncached}, cached {sheet.read}, write {sheet.write_5m}, "
        f"output {sheet.output} x base input"
    )


def measure(traces: Sequence[SessionTrace]) -> Measurement:
    """İstek dizilerinden toplam kullanım ve her oturumun kendi modelinin fiyatlarıyla maliyet."""
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
    """Bir sonuç kümesindeki tüm çalıştırma sonuçları."""
    paths = sorted(results_dir.glob("*.json"))
    if not paths:
        raise BenchError(f"no run results in {results_dir}")
    return tuple(
        result_from_json(
            json_object(json.loads(path.read_text(encoding="utf-8")), str(path)), str(path)
        )
        for path in paths
    )


def result_from_json(data: dict[str, object], where: str) -> RunResult:
    """Sonuç dosyasını tipleri doğrulayarak okur; eski şemadaki dosya hatadır."""
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
    """Sonuç dosyasındaki sağlayıcı ölçümü; maliyet bilinmiyorsa None."""
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
    """Kayıtlı sonuçları ajanların kayıtlarından güncel şemayla yeniden ölçüp dosyalara yazar.

    Ajanlar yeniden çalışmaz: çalıştırma anına ait alanlar (başarı, adımlar, süre, ortam) korunur,
    ölçümler Claude transcript'lerinden, Claude'un adım başına sonuç kayıtlarından
    (runs_dir/<run_id>/agent.<adım>.stdout) ve Codex rollout'larından yeniden hesaplanır. Eski
    şemanın tek tedavi kolu ("cimrihook") gerçekte açık olan mekanizmayla adlandırılır.
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
    """Tek sonuç kaydının yeniden ölçülmüş hali."""
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
    """Eski şemadaki kolun gerçekte açık olan mekanizması."""
    if variant == Variant.BASELINE.value:
        return Variant.BASELINE
    if variant != LEGACY_VARIANT:
        raise BenchError(f"unknown legacy variant {variant!r}")
    return Variant.COMBINED if agent is Agent.CLAUDE else Variant.GOVERNOR


def legacy_step_passed(steps: int, steps_passed: int) -> tuple[bool, ...]:
    """Eski şema yalnızca sayıyı tutar; tüm adımlar geçtiyse sıra bellidir, değilse bilinmez."""
    return (True,) * steps if steps_passed == steps else ()


def claude_reported_files(run_dir: Path, steps: int) -> tuple[ReportedUsage | None, ...]:
    """Çalıştırma dizinindeki adım çıktılarından Claude Code'un kümülatif raporları."""
    paths = [run_dir / f"agent.{step}.stdout" for step in range(1, steps + 1)]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise BenchError(f"Claude step outputs are missing, cost reports cannot be read: {missing}")
    return tuple(claude_reported_usage(path.read_text(encoding="utf-8")) for path in paths)


def scenario(result: RunResult) -> str:
    """Raporlarda görev ve protokolün birlikte adı."""
    return f"{result.task_id}/{result.protocol}"


def render_bench_report(results: Sequence[RunResult]) -> str:
    """Hücre özetleri, senaryo ve ajan düzeyinde A/B karşılaştırmaları, ölçülemeyen çalıştırmalar.

    Birincil maliyet sağlayıcı düzeyindedir (Claude Code: USD, Codex: fiyat tablosuyla taban girdi
    birimi) ve sıkıştırma isteklerini içerir; 'transcript' sütunu içermez. Ajan düzeyindeki özet
    yalnızca iki kolu eşit sayıda ölçülmüş senaryoları eşit ağırlıkla birleştirir; bir kolda
    ikiden az ölçüm varsa güven aralığı verilmez.
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
        *(f"  unmeasured {result.run_id}: {result.error}" for result in errors),
    ]
    return "\n".join(lines)


def version_lines(measured: Sequence[RunResult]) -> list[str]:
    """Ölçülmüş çalıştırmalardaki ajan sürümleri; bir ajanın birden çok sürümü karışmışsa uyarı."""
    versions = Counter((result.agent, result.agent_version or "unknown") for result in measured)
    agents = Counter(agent for agent, _ in versions)
    return [
        f"  agent version: {agent} {version} ({count} runs)"
        + (" - several versions are pooled in this set" if agents[agent] > 1 else "")
        for (agent, version), count in sorted(versions.items())
    ]


def provider_of(result: RunResult) -> ProviderMeasurement:
    """Sağlayıcı maliyeti olan çalıştırmanın ölçümü."""
    if result.provider is None:
        raise BenchError(f"{result.run_id} has no provider cost")
    return result.provider


def final_cost(result: RunResult) -> float:
    """Çalıştırmanın birincil (sağlayıcı düzeyindeki) toplam maliyeti."""
    return provider_of(result).cost_by_step[-1]


def mechanism_rank(mechanism: str) -> int:
    """Raporda kolların sırası: baseline, governor, codec, combined."""
    return list(Variant).index(Variant(mechanism))


def group_cells(results: Sequence[RunResult]) -> list[list[RunResult]]:
    """(ajan, senaryo, mekanizma) hücreleri, raporun sırasıyla."""
    keys = sorted(
        {(result.agent, scenario(result), result.mechanism) for result in results},
        key=lambda key: (key[0], key[1], mechanism_rank(key[2])),
    )
    return [
        [result for result in results if (result.agent, scenario(result), result.mechanism) == key]
        for key in keys
    ]


def cell_line(cell: Sequence[RunResult]) -> str:
    """Bir (ajan, senaryo, mekanizma) hücresinin satırı."""
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
    """Maliyet, çalıştırmanın birimiyle (USD ya da taban girdi birimi)."""
    if provider_of(result).unit == USD:
        return f"${value:,.3f}"
    return f"{value:,.0f}"


def arm(results: Sequence[RunResult], agent: str, label: str, mechanism: str) -> list[RunResult]:
    """Bir senaryonun bir koluna ait çalıştırmalar."""
    return [
        result
        for result in results
        if result.agent == agent and scenario(result) == label and result.mechanism == mechanism
    ]


def treatments() -> tuple[str, ...]:
    """Baseline dışındaki kollar, raporun sırasıyla."""
    return tuple(variant.value for variant in Variant if variant is not Variant.BASELINE)


def interval_text(estimate: RatioEstimate) -> str:
    """Aralığın sınırları; aralık yoksa 'none'."""
    if estimate.low is None or estimate.high is None:
        return "none"
    return f"{estimate.low:.3f}-{estimate.high:.3f}"


def ratio_text(estimate: RatioEstimate) -> str:
    """Oran ve aralığı."""
    return f"x{estimate.ratio:.3f} [{interval_text(estimate)}]"


def costed_runs(results: Sequence[RunResult]) -> list[RunResult]:
    """Sağlayıcı maliyeti kaydedilmiş çalıştırmalar."""
    return [result for result in results if result.provider is not None]


def run_success_difference(
    base: Sequence[RunResult], treated: Sequence[RunResult]
) -> DifferenceEstimate:
    """Çalıştırma başarısı farkı (tedavi − baseline) ve Newcombe aralığı."""
    return rate_difference(
        sum(r.success for r in treated), len(treated), sum(r.success for r in base), len(base)
    )


def success_text(base: Sequence[RunResult], treated: Sequence[RunResult]) -> str:
    """Çalıştırma başarısı (her adım geçti, test dosyasına dokunulmadı), farkının aralığı ve
    yalnızca betimleyici adım başarısı."""
    difference = run_success_difference(base, treated)
    return (
        f"runs ok {sum(r.success for r in treated)}/{len(treated)} vs "
        f"{sum(r.success for r in base)}/{len(base)} (diff {100 * difference.difference:+.1f} pp "
        f"[{100 * difference.low:+.1f}, {100 * difference.high:+.1f}]); steps "
        f"{sum(r.steps_passed for r in treated)}/{sum(r.steps for r in treated)} vs "
        f"{sum(r.steps_passed for r in base)}/{sum(r.steps for r in base)}"
    )


def scenario_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """Senaryo bazında her kolun baseline'a göre maliyet oranı ve başarısı.

    Maliyet yalnızca sağlayıcı maliyeti olan çalıştırmalardan, başarı ölçülmüş tüm
    çalıştırmalardan hesaplanır: maliyeti kaydedilemeyen (ör. süre aşımı) çalıştırma başarı
    karşılaştırmasından düşmez.
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
    """İki kolu ölçülmüş senaryolar: eşit n'li çiftler ve n'i farklı olduğu için dışarıdakiler."""
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
    """Çalıştırma başarısı için non-inferiority kararı (en fazla NONINFERIORITY_MARGIN düşüş).

    Gösterilememesi kalitenin düştüğü anlamına gelmez; aralık marjı dışlayacak kadar dar değildir.
    """
    if min(base_runs, treated_runs) < MIN_RUNS_FOR_VERDICT:
        return f"undecided (fewer than {MIN_RUNS_FOR_VERDICT} runs per arm)"
    if difference_low > -NONINFERIORITY_MARGIN:
        return "shown"
    return f"not shown (lower bound {100 * difference_low:+.1f} pp)"


def agent_comparisons(measured: Sequence[RunResult]) -> list[str]:
    """Ajan ve mekanizma düzeyinde: dengeli senaryoların eşit ağırlıklı maliyet oranı ve iki kolu
    da ölçülmüş senaryoların tüm çalıştırmalarıyla başarı ve non-inferiority."""
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
    """Dengeli senaryoların eşit ağırlıklı maliyet oranı ve iki aralığı."""
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
    """Aynı çalıştırmaların adım kontrol noktalarındaki kümülatif maliyet oranları."""
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
    """Sağlayıcı token toplamlarının verilen fiyat tablosuyla maliyeti."""
    provider = provider_of(result)
    return (
        provider.uncached * sheet.uncached
        + provider.cache_write * sheet.write_5m
        + provider.cache_read * sheet.read
        + provider.output * sheet.output
    )


def codex_sensitivity(costed: Sequence[RunResult]) -> list[str]:
    """Codex oranının fiyat tablosu varsayımlarına duyarlılığı (dengeli senaryolar)."""
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


def render_calibration(results: Sequence[RunResult]) -> str:
    """Simülatörün politika tahminini A/B sonucuyla sınar.

    Baseline çalıştırmalarının kayıtları, tedavi kolunda gerçekten gözlenen tetik noktasıyla
    yeniden oynatılır; sıkıştırmanın bedeli (sonraki bağlam, önbellekte kalan kısmı, özet)
    tedavi kolunun kayıtlarından ölçülür. Böylece yalnızca simülatörün kendi varsayımları
    (ajan davranışı değişmez, maliyet muhasebesi) sınanır. Tahmin ve ölçüm geometrik ortalama
    oranlarıdır; ölçüm sağlayıcı düzeyindeki birincil maliyettir.
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
    """Bir senaryonun tahmin ve ölçüm satırı."""
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
        # Codex önbelleğe yazımı ayrıca fiyatlamaz: yeni girdi önbelleksiz girdi fiyatındadır.
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
    """Çalıştırmanın ajan kayıtlarındaki bağlam pencereleri."""
    if result.agent == Agent.CLAUDE.value:
        return claude_traces(claude_transcript(result.session_id), result.session_id)
    return (load_codex_trace(codex_rollout(result.session_id)),)


def simulated_cost(traces: Sequence[SessionTrace], policy: Policy, model: CostModel) -> float:
    """Kayıtların bir politikayla yeniden oynatılmış maliyeti (her oturum kendi fiyatlarıyla)."""
    return sum(simulate_trace(trace, policy, model, trace.prices)[0] for trace in traces)


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


def optional_int_field(data: dict[str, object], key: str, where: str) -> int | None:
    """Boş olabilen tam sayı alanı."""
    if data.get(key) is None:
        return None
    return int_field(data, key, where)


def int_list(data: dict[str, object], key: str, where: str) -> tuple[int, ...]:
    """Tam sayı listesi alanı."""
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise BenchError(f"{where}: field {key!r} must be a list of integers")
    return tuple(int(item) for item in value)


def float_list(data: dict[str, object], key: str, where: str) -> tuple[float, ...]:
    """Sayı listesi alanı."""
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int | float) and not isinstance(item, bool) for item in value
    ):
        raise BenchError(f"{where}: field {key!r} must be a list of numbers")
    return tuple(float(item) for item in value)


def bool_list(data: dict[str, object], key: str, where: str) -> tuple[bool, ...]:
    """Mantıksal değer listesi alanı."""
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, bool) for item in value):
        raise BenchError(f"{where}: field {key!r} must be a list of booleans")
    return tuple(bool(item) for item in value)


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
