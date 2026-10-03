"""Komut satırı: hook | statusline | guard | brief | report | doctor | audit | simulate | init |
settings | bench-run | bench-report | bench-remeasure | bench-calibrate."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Final

from cimrihook.audit import audit_transcripts, render_audit
from cimrihook.bench import (
    Agent,
    Protocol,
    Variant,
    load_results,
    load_tasks,
    plan_runs,
    remeasure_results,
    render_bench_report,
    render_calibration,
    run_plan,
    select_tasks,
)
from cimrihook.config import Config, load_config
from cimrihook.doctor import diagnose_claude, render_doctor
from cimrihook.errors import BenchError, CimriHookError, ConfigError
from cimrihook.guard import compaction_brief, guard_prompt
from cimrihook.hook import ledger_path, run_hook
from cimrihook.install import run_init, run_remove
from cimrihook.ledger import Ledger
from cimrihook.report import render_savings
from cimrihook.settings import (
    brief_settings,
    governor_env,
    guard_settings,
    hook_settings,
    merge_settings,
    statusline_settings,
)
from cimrihook.simulate import (
    CLAUDE_COMPACT_OFFSET,
    CLAUDE_MIN_COMPACT_WINDOW,
    CostOverrides,
    claude_hint,
    codex_hint,
    render_simulation,
    simulate_claude,
    simulate_codex,
)
from cimrihook.statusline import run_chained_statusline, run_statusline

DEFAULT_PROJECTS_DIR: Final = "~/.claude/projects"
DEFAULT_AUDIT_DAYS: Final = 30
DEFAULT_DOCTOR_DAYS: Final = 7
DEFAULT_SETTINGS_PATH: Final = "~/.claude/settings.json"
# Sıkıştırmadan sonraki ilk isteğin bağlamı yeniden eklenen dosyaları zaten içerir; gerçek
# oturumlarda bunun dışında yeniden okuma medyanı sıfırdır.
DEFAULT_REFETCH_TOKENS: Final = 0
DEFAULT_REFETCH_REQUESTS: Final = 0
SIMULATORS: Final = {"claude": simulate_claude, "codex": simulate_codex}
APPLY_HINTS: Final = {"claude": claude_hint, "codex": codex_hint}
DEFAULT_LOGS: Final = {"claude": "~/.claude/projects", "codex": "~/.codex/sessions"}
DEFAULT_TASKS_DIR: Final = "bench/tasks"
DEFAULT_RESULTS_DIR: Final = "bench/results"
DEFAULT_WORK_DIR: Final = "/tmp/cimrihook-bench"
DEFAULT_CLAUDE_MODEL: Final = "claude-opus-5-5[1m]"
DEFAULT_CODEX_MODEL: Final = "gpt-6.1-sol"
DEFAULT_EFFORT: Final = "medium"
DEFAULT_WINDOW: Final = 100_000
DEFAULT_CONCURRENCY: Final = 2
DEFAULT_TIMEOUT_SECONDS: Final = 2_400


def build_parser() -> argparse.ArgumentParser:
    """Alt komutlarıyla argüman ayrıştırıcı."""
    parser = argparse.ArgumentParser(
        prog="cimrihook", description="Context economics for AI coding agents."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook", help="process one Claude Code hook payload from stdin")
    statusline = commands.add_parser(
        "statusline", help="print the Claude Code status line from its JSON input on stdin"
    )
    statusline.add_argument(
        "--after", help="run this status line command first and add CimriHook's part after it"
    )
    commands.add_parser(
        "guard", help="UserPromptSubmit hook: ask once before re-caching a cold, large session"
    )
    commands.add_parser("brief", help="PreCompact hook: ask for a short, structured summary")
    report = commands.add_parser("report", help="show token savings recorded by the live hook")
    report.add_argument("--session", help="limit the report to one Claude Code session id")
    doctor = commands.add_parser(
        "doctor", help="where your Claude Code spend goes and what would change it"
    )
    doctor.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    doctor.add_argument("--days", type=int, default=DEFAULT_DOCTOR_DAYS)
    audit = commands.add_parser(
        "audit", help="replay past Claude Code transcripts and estimate what CimriHook would save"
    )
    audit.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    audit.add_argument("--days", type=int, default=DEFAULT_AUDIT_DAYS)
    simulate = commands.add_parser(
        "simulate", help="replay past sessions under compaction policies and compare their cost"
    )
    simulate.add_argument("--agent", choices=sorted(SIMULATORS), default="claude")
    simulate.add_argument("--logs-dir", help="default: ~/.claude/projects or ~/.codex/sessions")
    simulate.add_argument("--days", type=int, default=DEFAULT_AUDIT_DAYS)
    simulate.add_argument(
        "--summary-tokens",
        type=int,
        help="output tokens of a compaction summary (default: median of real compactions)",
    )
    simulate.add_argument(
        "--refetch-tokens",
        type=int,
        default=DEFAULT_REFETCH_TOKENS,
        help="content re-read after a compaction beyond the next request's context",
    )
    simulate.add_argument("--refetch-requests", type=int, default=DEFAULT_REFETCH_REQUESTS)
    simulate.add_argument(
        "--read-weight",
        type=float,
        help="cache-read multiplier for every model (default: per model; Opus 5.x 0.05, "
        "Fable 5.1 0.025, other models 0.1)",
    )
    simulate.add_argument(
        "--post-compact-tokens",
        type=int,
        help="context of the first request after a compaction, system prompt included "
        "(default: median of real compactions)",
    )
    simulate.add_argument(
        "--post-compact-cached",
        type=int,
        help="part of that context still cached (default: median of real compactions)",
    )
    init = commands.add_parser(
        "init",
        help="add CimriHook to Claude Code's settings (backup first; --dry-run shows the diff)",
    )
    init.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    init.add_argument("--compact-window", type=int, help="also set the auto-compact window")
    init.add_argument("--brief", action="store_true", help="also ask compactions for brevity")
    init.add_argument("--codec", action="store_true", help="also re-encode tool results")
    init.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    init.add_argument("--remove", action="store_true", help="take out only what CimriHook added")
    settings = commands.add_parser(
        "settings",
        help="print the Claude Code settings JSON (cold-prompt guard and status line by default)",
    )
    settings.add_argument(
        "--compact-window",
        type=int,
        help="also set the auto-compact window; Claude Code compacts 33000 tokens below it "
        "(minimum 100000)",
    )
    settings.add_argument(
        "--brief", action="store_true", help="also ask compactions for a short, structured summary"
    )
    settings.add_argument(
        "--codec", action="store_true", help="also re-encode tool results (REF/DELTA/OUTLINE)"
    )
    bench = commands.add_parser(
        "bench-run", help="A/B runs on real tasks with the logged-in Claude Code and Codex CLI"
    )
    bench.add_argument("--name", required=True, help="result set name (resumable)")
    bench.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    bench.add_argument("--tasks", help="comma-separated task ids (default: all)")
    bench.add_argument("--protocols", default="single", help="single and/or sequential")
    bench.add_argument("--agents", default="claude,codex")
    bench.add_argument(
        "--variants",
        default="baseline,governor",
        help="baseline, governor, codec, combined (codec and combined: claude only)",
    )
    bench.add_argument("--reps", type=int, default=1)
    bench.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    bench.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    bench.add_argument("--effort", default=DEFAULT_EFFORT)
    bench.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW,
        help="compaction window of the governor and combined arms (claude: at least 100000)",
    )
    bench.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    bench.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="agent timeout per run (s)"
    )
    bench.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    bench.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    bench_report = commands.add_parser("bench-report", help="summarize an A/B result set")
    bench_report.add_argument("--name", required=True)
    bench_report.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    remeasure = commands.add_parser(
        "bench-remeasure",
        help="re-measure a result set from the agents' logs with the current schema (no runs)",
    )
    remeasure.add_argument("--name", required=True)
    remeasure.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    remeasure.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    calibrate = commands.add_parser(
        "bench-calibrate",
        help="check the simulator's prediction against a measured A/B result set",
    )
    calibrate.add_argument("--name", required=True)
    calibrate.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    return parser


def live_report(config: Config, session: str | None) -> str:
    """Defterdeki canlı kararların raporu."""
    with Ledger(ledger_path(config.home)) as ledger:
        if session is None:
            return render_savings("CimriHook live savings (all sessions)", ledger.savings_all())
        return render_savings(
            f"CimriHook live savings (session {session})", ledger.savings_for_session(session)
        )


def selected_blocks(args: argparse.Namespace) -> list[dict[str, object]]:
    """Seçilen bileşenlerin ayar blokları: koruma ve durum satırı her zaman, diğerleri seçilince."""
    window = optional_int(args.compact_window)
    if window is not None and window < CLAUDE_MIN_COMPACT_WINDOW:
        raise ConfigError(
            f"--compact-window {window} is below {CLAUDE_MIN_COMPACT_WINDOW}; Claude Code would "
            f"raise it to {CLAUDE_MIN_COMPACT_WINDOW} and compact at about "
            f"{CLAUDE_MIN_COMPACT_WINDOW - CLAUDE_COMPACT_OFFSET} tokens"
        )
    python = sys.executable
    return [
        guard_settings(python),
        statusline_settings(python),
        *([brief_settings(python)] if args.brief else []),
        *([hook_settings(python)] if args.codec else []),
        *([] if window is None else [governor_env(window)]),
    ]


def optional_int(value: object) -> int | None:
    """argparse'ın verilmemiş (None) ya da tam sayı değeri."""
    return None if value is None else int(str(value))


def split_csv(raw: str) -> tuple[str, ...]:
    """Virgülle ayrılmış değerler."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_agents(raw: str) -> tuple[Agent, ...]:
    """Ajan listesi (claude, codex)."""
    allowed = {agent.value for agent in Agent}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--agents must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Agent(value) for value in values)


def parse_protocols(raw: str) -> tuple[Protocol, ...]:
    """Protokol listesi (single, sequential)."""
    allowed = {protocol.value for protocol in Protocol}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--protocols must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Protocol(value) for value in values)


def parse_variants(raw: str) -> tuple[Variant, ...]:
    """Varyant listesi (baseline, cimrihook)."""
    allowed = {variant.value for variant in Variant}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--variants must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Variant(value) for value in values)


def bench_run(args: argparse.Namespace) -> None:
    """A/B matrisini kurar ve eksik çalıştırmaları yürütür."""
    name = str(args.name)
    all_tasks = load_tasks(Path(str(args.tasks_dir)))
    tasks = all_tasks if args.tasks is None else select_tasks(all_tasks, split_csv(str(args.tasks)))
    specs = plan_runs(
        tasks,
        parse_protocols(str(args.protocols)),
        parse_agents(str(args.agents)),
        parse_variants(str(args.variants)),
        int(args.reps),
        str(args.claude_model),
        str(args.codex_model),
        str(args.effort),
        int(args.window),
    )
    run_plan(
        specs,
        Path(str(args.results_dir)) / name,
        Path(str(args.work_dir)) / name,
        int(args.concurrency),
        int(args.timeout),
    )


def main() -> None:
    """CLI giriş noktası; CimriHook hataları tek satırlık mesaj ve çıkış kodu 1 ile biter."""
    args = build_parser().parse_args()
    command: str = args.command
    try:
        config = load_config(os.environ)
        if command == "hook":
            sys.stdout.write(run_hook(sys.stdin.read(), config))
        elif command == "statusline" and args.after is not None:
            raw = sys.stdin.read()
            sys.stdout.write(run_chained_statusline(raw, config, time.time(), str(args.after)))
        elif command == "statusline":
            sys.stdout.write(run_statusline(sys.stdin.read(), config, time.time()))
        elif command == "guard":
            sys.stdout.write(guard_prompt(sys.stdin.read(), config, time.time()))
        elif command == "brief":
            sys.stdout.write(compaction_brief(sys.stdin.read()))
        elif command == "report":
            session: str | None = args.session
            print(live_report(config, session))
        elif command == "doctor":
            anatomy, simulation = diagnose_claude(
                Path(str(args.projects_dir)).expanduser(), int(args.days), time.time()
            )
            print(render_doctor(anatomy, simulation))
        elif command == "audit":
            projects_dir: str = args.projects_dir
            days: int = args.days
            result = audit_transcripts(
                Path(projects_dir).expanduser(), days, config.codec, time.time()
            )
            print(render_audit(result))
        elif command == "simulate":
            agent: str = args.agent
            logs_dir = DEFAULT_LOGS[agent] if args.logs_dir is None else str(args.logs_dir)
            simulation = SIMULATORS[agent](
                Path(logs_dir).expanduser(),
                int(args.days),
                time.time(),
                CostOverrides(
                    post_compact_tokens=optional_int(args.post_compact_tokens),
                    post_compact_cached=optional_int(args.post_compact_cached),
                    summary_tokens=optional_int(args.summary_tokens),
                    refetch_tokens=int(args.refetch_tokens),
                    refetch_requests=int(args.refetch_requests),
                    read_weight=None if args.read_weight is None else float(args.read_weight),
                ),
            )
            print(render_simulation(simulation, APPLY_HINTS[agent]))
        elif command == "init":
            path = Path(str(args.settings)).expanduser()
            if args.remove:
                print(run_remove(path, config.home, bool(args.dry_run), time.time()))
            else:
                blocks = selected_blocks(args)
                print(run_init(path, blocks, config.home, bool(args.dry_run), time.time()))
        elif command == "bench-run":
            bench_run(args)
        elif command == "bench-report":
            results_dir = Path(str(args.results_dir)) / str(args.name)
            print(render_bench_report(load_results(results_dir)))
        elif command == "bench-remeasure":
            name = str(args.name)
            results = remeasure_results(
                Path(str(args.results_dir)) / name, Path(str(args.work_dir)) / name
            )
            print(render_bench_report(results))
        elif command == "bench-calibrate":
            results_dir = Path(str(args.results_dir)) / str(args.name)
            print(render_calibration(load_results(results_dir)))
        else:
            print(json.dumps(merge_settings(selected_blocks(args)), indent=2))
    except CimriHookError as error:
        print(f"cimrihook: {error}", file=sys.stderr)
        raise SystemExit(1) from error
