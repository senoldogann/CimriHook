"""Command line: statusline | guard | doctor | gain | limits | simulate | init | settings |
bench-run | bench-report | bench-remeasure | bench-calibrate."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Final, NoReturn

from cimrihook import __version__
from cimrihook.bench import (
    Agent,
    Protocol,
    Variant,
    load_results,
    load_run_limits,
    load_tasks,
    plan_runs,
    remeasure_results,
    render_bench_report,
    render_calibration,
    run_plan,
    select_tasks,
)
from cimrihook.codex_config import (
    apply_codex_remove,
    apply_codex_window,
    plan_codex_remove,
    plan_codex_window,
    render_codex_plan,
)
from cimrihook.config import load_config
from cimrihook.doctor import diagnose_claude, render_doctor
from cimrihook.errors import BenchError, CimriHookError, ConfigError
from cimrihook.gain import measure_gain, render_gain
from cimrihook.guard import guard_prompt
from cimrihook.install import apply_init, apply_remove, plan_init, plan_remove, render_plan
from cimrihook.limits import limits_dir, read_samples, render_limits, window_rates
from cimrihook.mods import mod_dir, write_mod
from cimrihook.settings import (
    cache_ttl_settings,
    governor_settings,
    guard_settings,
    merge_settings,
    mod_settings,
    statusline_settings,
)
from cimrihook.simulate import (
    CLAUDE_COMPACT_OFFSET,
    CLAUDE_MAX_COMPACT_WINDOW,
    CLAUDE_MIN_COMPACT_WINDOW,
    MEASURED_REFETCH_REQUESTS,
    MEASURED_REFETCH_TOKENS,
    CostOverrides,
    claude_hint,
    codex_hint,
    render_simulation,
    simulate_claude,
    simulate_codex,
)
from cimrihook.statusline import run_chained_statusline, status_or_error

DEFAULT_PROJECTS_DIR: Final = "~/.claude/projects"
DEFAULT_SIMULATE_DAYS: Final = 30
DEFAULT_DOCTOR_DAYS: Final = 7
DEFAULT_SETTINGS_PATH: Final = "~/.claude/settings.json"
DEFAULT_CODEX_CONFIG_PATH: Final = "~/.codex/config.toml"
# The context of the first request after a compaction already includes the re-attached files; in
# real sessions the median re-read beyond that is zero.
DEFAULT_REFETCH_TOKENS: Final = MEASURED_REFETCH_TOKENS
DEFAULT_REFETCH_REQUESTS: Final = MEASURED_REFETCH_REQUESTS
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
PRIVATE_UMASK: Final = 0o077


class CliParser(argparse.ArgumentParser):
    """Raises ConfigError on a bad argument instead of exiting with argparse's code 2.

    The commands run as hooks, and in Claude Code exit code 2 blocks the prompt or the compaction.
    If the command in the settings file and the installed version disagree (for example an unknown
    argument), this would stop every prompt. The subcommand parsers inherit this class.
    """

    def error(self, message: str) -> NoReturn:
        raise ConfigError(f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    """Argument parser with its subcommands."""
    parser = CliParser(prog="cimrihook", description="Context economics for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"cimrihook {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    statusline = commands.add_parser(
        "statusline", help="print the Claude Code status line from its JSON input on stdin"
    )
    statusline.add_argument(
        "--after", help="run this status line command first and add CimriHook's part after it"
    )
    commands.add_parser(
        "guard", help="UserPromptSubmit hook: ask once before re-caching a cold, large session"
    )
    doctor = commands.add_parser(
        "doctor", help="where your Claude Code spend goes and what would change it"
    )
    doctor.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    doctor.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    doctor.add_argument("--days", type=int, default=DEFAULT_DOCTOR_DAYS)
    gain = commands.add_parser(
        "gain", help="the time since your last init vs the same time before it"
    )
    gain.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    gain.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    commands.add_parser(
        "limits", help="what a point of your 5-hour and weekly windows costs in usage"
    )
    simulate = commands.add_parser(
        "simulate", help="replay past sessions under compaction policies and compare their cost"
    )
    simulate.add_argument("--agent", choices=sorted(SIMULATORS), default="claude")
    simulate.add_argument("--logs-dir", help="default: ~/.claude/projects or ~/.codex/sessions")
    simulate.add_argument("--days", type=int, default=DEFAULT_SIMULATE_DAYS)
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
        help="add CimriHook to Claude Code's settings or set Codex's compaction limit "
        "(backup first; --dry-run shows the diff)",
    )
    init.add_argument("--agent", choices=("claude", "codex"), default="claude")
    init.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    init.add_argument("--codex-config", default=DEFAULT_CODEX_CONFIG_PATH)
    init.add_argument("--compact-window", type=int, help="also set the auto-compact window")
    init.add_argument("--cache-ttl", choices=("5m", "1h"), help="main conversation cache lifetime")
    init.add_argument("--subagent-cache-ttl", choices=("5m", "1h"), help="subagent cache lifetime")
    init.add_argument(
        "--mod",
        action="store_true",
        help="also install the mod that compacts idle sessions while their cache is warm",
    )
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
    settings.add_argument("--cache-ttl", choices=("5m", "1h"), help="main conversation cache")
    settings.add_argument("--mod", action="store_true", help="also load the CimriHook mod")
    settings.add_argument(
        "--subagent-cache-ttl", choices=("5m", "1h"), help="subagent cache lifetime"
    )
    bench = commands.add_parser(
        "bench-run", help="A/B runs on real tasks with the logged-in Claude Code and Codex CLI"
    )
    bench.add_argument("--name", required=True, help="result set name (resumable)")
    bench.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    bench.add_argument("--tasks", help="comma-separated task ids (default: all)")
    bench.add_argument(
        "--protocols",
        default="single",
        help="single, sequential, deep and/or deeper (deep and deeper: claude only)",
    )
    bench.add_argument("--agents", default="claude,codex")
    bench.add_argument(
        "--variants",
        default="baseline,governor",
        help="baseline, governor, rtk, rtk-governor, mask, boundary, meter, meter-governor "
        "(all but baseline and governor: claude only)",
    )
    bench.add_argument("--reps", type=int, default=1)
    bench.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    bench.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    bench.add_argument("--effort", default=DEFAULT_EFFORT)
    bench.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW,
        help="compaction window of the arms that set one (claude: at least 100000)",
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


def run_codex_init(args: argparse.Namespace, home: Path, now: float) -> str:
    """Sets Codex's compaction limit or (--remove) takes it out; Codex has no hooks to install."""
    path = Path(str(args.codex_config)).expanduser()
    if args.remove:
        plan = plan_codex_remove(path, home)
    elif args.compact_window is None:
        raise ConfigError(
            "init --agent codex needs --compact-window (see `cimrihook simulate --agent codex`)"
        )
    else:
        plan = plan_codex_window(path, int(args.compact_window), home)
    if args.dry_run:
        return f"{render_codex_plan(plan)}\ndry run: nothing written"
    if args.remove:
        return apply_codex_remove(plan, home, now)
    return apply_codex_window(plan, home, now)


def selected_blocks(args: argparse.Namespace, home: Path) -> list[dict[str, object]]:
    """Settings blocks of the selected parts: guard and status line always, the rest when asked."""
    window = optional_int(args.compact_window)
    if window is not None and not CLAUDE_MIN_COMPACT_WINDOW <= window <= CLAUDE_MAX_COMPACT_WINDOW:
        raise ConfigError(
            f"--compact-window {window} is outside Claude Code's autoCompactWindow range "
            f"{CLAUDE_MIN_COMPACT_WINDOW}-{CLAUDE_MAX_COMPACT_WINDOW}; the earliest compaction is "
            f"at about {CLAUDE_MIN_COMPACT_WINDOW - CLAUDE_COMPACT_OFFSET} tokens"
        )
    python = sys.executable
    return [
        guard_settings(python),
        statusline_settings(python),
        *([] if window is None else [governor_settings(window)]),
        *(
            []
            if args.cache_ttl is None and args.subagent_cache_ttl is None
            else [cache_ttl_settings(args.cache_ttl, args.subagent_cache_ttl)]
        ),
        *([mod_settings(str(mod_dir(home)))] if args.mod else []),
    ]


def optional_int(value: object) -> int | None:
    """An argparse value that is either not given (None) or an integer."""
    return None if value is None else int(str(value))


def split_csv(raw: str) -> tuple[str, ...]:
    """Comma-separated values."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_agents(raw: str) -> tuple[Agent, ...]:
    """List of agents (claude, codex)."""
    allowed = {agent.value for agent in Agent}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--agents must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Agent(value) for value in values)


def parse_protocols(raw: str) -> tuple[Protocol, ...]:
    """List of protocols (single, sequential, deep, deeper)."""
    allowed = {protocol.value for protocol in Protocol}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--protocols must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Protocol(value) for value in values)


def parse_variants(raw: str) -> tuple[Variant, ...]:
    """List of A/B arms (see `Variant`)."""
    allowed = {variant.value for variant in Variant}
    values = split_csv(raw)
    unknown = [value for value in values if value not in allowed]
    if unknown or not values:
        raise BenchError(f"--variants must list some of {sorted(allowed)}, got {raw!r}")
    return tuple(Variant(value) for value in values)


def bench_run(args: argparse.Namespace) -> None:
    """Builds the A/B matrix and runs the missing runs."""
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
    """CLI entry point; CimriHook errors end with a one-line message and exit code 1.

    Files it creates (ledger, install record, backups) are open to the user only.
    """
    os.umask(PRIVATE_UMASK)
    try:
        args = build_parser().parse_args()
        command: str = args.command
        config = load_config(os.environ)
        if command == "statusline" and args.after is not None:
            raw = sys.stdin.read()
            sys.stdout.write(run_chained_statusline(raw, config, time.time(), str(args.after)))
        elif command == "statusline":
            sys.stdout.write(status_or_error(sys.stdin.read(), config, time.time()))
        elif command == "guard":
            sys.stdout.write(guard_prompt(sys.stdin.read(), config, time.time()))
        elif command == "doctor":
            print(
                render_doctor(
                    diagnose_claude(
                        Path(str(args.projects_dir)).expanduser(),
                        Path(str(args.settings)).expanduser(),
                        int(args.days),
                        time.time(),
                    )
                )
            )
        elif command == "gain":
            print(
                render_gain(
                    measure_gain(
                        Path(str(args.projects_dir)).expanduser(),
                        config.home,
                        Path(str(args.settings)).expanduser(),
                        time.time(),
                    )
                )
            )
        elif command == "limits":
            samples = read_samples(limits_dir(config.home))
            print(render_limits(samples, window_rates(samples)))
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
        elif command == "init" and args.agent == "codex":
            print(run_codex_init(args, config.home, time.time()))
        elif command == "init":
            path = Path(str(args.settings)).expanduser()
            plan = (
                plan_remove(path, config.home)
                if args.remove
                else plan_init(path, selected_blocks(args, config.home), config.home)
            )
            if args.dry_run:
                print(f"{render_plan(plan)}\ndry run: nothing written")
            elif args.remove:
                print(apply_remove(plan, config.home, time.time()))
            else:
                if args.mod:
                    write_mod(mod_dir(config.home))
                print(apply_init(plan, config.home, time.time()))
        elif command == "bench-run":
            bench_run(args)
        elif command == "bench-report":
            results_dir = Path(str(args.results_dir)) / str(args.name)
            results = load_results(results_dir)
            print(render_bench_report(results, load_run_limits(results_dir, results)))
        elif command == "bench-remeasure":
            name = str(args.name)
            results_dir = Path(str(args.results_dir)) / name
            results = remeasure_results(results_dir, Path(str(args.work_dir)) / name)
            print(render_bench_report(results, load_run_limits(results_dir, results)))
        elif command == "bench-calibrate":
            results_dir = Path(str(args.results_dir)) / str(args.name)
            print(render_calibration(load_results(results_dir)))
        elif command == "settings":
            print(json.dumps(merge_settings(selected_blocks(args, config.home)), indent=2))
        else:
            raise ConfigError(f"unhandled command {command!r}")
    except CimriHookError as error:
        print(f"cimrihook: {error}", file=sys.stderr)
        raise SystemExit(1) from error
