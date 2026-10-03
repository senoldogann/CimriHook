"""Komut satırı: hook | report | audit | simulate | settings | bench-run | bench-report."""

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
    render_bench_report,
    run_plan,
    select_tasks,
)
from cimrihook.config import Config, load_config
from cimrihook.errors import BenchError, CimriHookError
from cimrihook.hook import ledger_path, run_hook
from cimrihook.ledger import Ledger
from cimrihook.report import render_savings
from cimrihook.settings import governor_env, hook_settings
from cimrihook.simulate import (
    claude_hint,
    codex_hint,
    render_simulation,
    simulate_claude,
    simulate_codex,
)

DEFAULT_PROJECTS_DIR: Final = "~/.claude/projects"
DEFAULT_AUDIT_DAYS: Final = 30
DEFAULT_SUMMARY_TOKENS: Final = 12_000
DEFAULT_REFETCH_TOKENS: Final = 20_000
DEFAULT_REFETCH_REQUESTS: Final = 3
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
    report = commands.add_parser("report", help="show token savings recorded by the live hook")
    report.add_argument("--session", help="limit the report to one Claude Code session id")
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
    simulate.add_argument("--summary-tokens", type=int, default=DEFAULT_SUMMARY_TOKENS)
    simulate.add_argument("--refetch-tokens", type=int, default=DEFAULT_REFETCH_TOKENS)
    simulate.add_argument("--refetch-requests", type=int, default=DEFAULT_REFETCH_REQUESTS)
    simulate.add_argument(
        "--read-weight",
        type=float,
        help="cache-read price multiplier (default 0.1; Opus 5.5: 0.05, Fable 5.1: 0.025)",
    )
    simulate.add_argument(
        "--post-compact-tokens",
        type=int,
        help="context size after a compaction (default: median of real compactions)",
    )
    settings = commands.add_parser("settings", help="print the Claude Code settings JSON")
    settings.add_argument(
        "--compact-window",
        type=int,
        help="also cap the context: auto-compact once it reaches this many tokens",
    )
    bench = commands.add_parser(
        "bench-run", help="A/B runs on real tasks with the logged-in Claude Code and Codex CLI"
    )
    bench.add_argument("--name", required=True, help="result set name (resumable)")
    bench.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    bench.add_argument("--tasks", help="comma-separated task ids (default: all)")
    bench.add_argument("--protocols", default="single", help="single and/or sequential")
    bench.add_argument("--agents", default="claude,codex")
    bench.add_argument("--variants", default="baseline,cimrihook")
    bench.add_argument("--reps", type=int, default=1)
    bench.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    bench.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    bench.add_argument("--effort", default=DEFAULT_EFFORT)
    bench.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW, help="compaction window of the cimrihook arm"
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
    return parser


def live_report(config: Config, session: str | None) -> str:
    """Defterdeki canlı kararların raporu."""
    with Ledger(ledger_path(config.home)) as ledger:
        if session is None:
            return render_savings("CimriHook live savings (all sessions)", ledger.savings_all())
        return render_savings(
            f"CimriHook live savings (session {session})", ledger.savings_for_session(session)
        )


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
        elif command == "report":
            session: str | None = args.session
            print(live_report(config, session))
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
                int(args.summary_tokens),
                int(args.refetch_tokens),
                int(args.refetch_requests),
                None if args.post_compact_tokens is None else int(args.post_compact_tokens),
                None if args.read_weight is None else float(args.read_weight),
            )
            print(render_simulation(simulation, APPLY_HINTS[agent]))
        elif command == "bench-run":
            bench_run(args)
        elif command == "bench-report":
            results_dir = Path(str(args.results_dir)) / str(args.name)
            print(render_bench_report(load_results(results_dir)))
        else:
            window: int | None = args.compact_window
            extra = {} if window is None else governor_env(window)
            print(json.dumps(hook_settings(sys.executable) | extra, indent=2))
    except CimriHookError as error:
        print(f"cimrihook: {error}", file=sys.stderr)
        raise SystemExit(1) from error
