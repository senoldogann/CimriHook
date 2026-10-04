"""Command line: quota | closure-probe | statusline | guard | doctor | gain | limits |
simulate | init | settings |
bench-run | bench-report | bench-remeasure | bench-calibrate."""

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn

from cimrihook import __version__
from cimrihook.bench import (
    Agent,
    Protocol,
    RunResult,
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
from cimrihook.closure_probe import run_probe
from cimrihook.codex_config import (
    apply_codex_remove,
    apply_codex_window,
    plan_codex_remove,
    plan_codex_window,
    render_codex_plan,
)
from cimrihook.codex_doctor import (
    WINDOW_KINDS,
    codex_limits_json,
    diagnose_codex,
    recent_rollouts,
    render_codex_doctor,
    render_codex_limits,
    window_cost,
)
from cimrihook.config import load_config
from cimrihook.doctor import diagnose_claude, render_doctor
from cimrihook.errors import BenchError, CimriHookError, ConfigError
from cimrihook.gain import gain_json, measure_gain, render_gain
from cimrihook.guard import guard_prompt
from cimrihook.install import apply_init, apply_remove, plan_init, plan_remove, render_plan
from cimrihook.limits import LimitSample, limits_dir, read_samples
from cimrihook.mods import mod_dir, write_mod
from cimrihook.preparation import (
    DEFAULT_PACKET_BYTES,
    build_packet,
    packet_json,
    parse_target,
    read_literal,
    render_packet,
)
from cimrihook.preparation_report import diagnose_preparation, render_report, report_json
from cimrihook.quota import quota_json, read_claude_quota, read_codex_quota
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
from cimrihook.weights import render_limits

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
BACKGROUND_HELP: Final = (
    "directory of the other sessions' limit readings (`limits/*.jsonl` files), which the "
    "meter arms' weights set apart; default: the mod's ledger in CIMRIHOOK_HOME, if any"
)
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
        "doctor", help="where your Claude Code or Codex spend goes and what would change it"
    )
    doctor.add_argument("--agent", choices=("claude", "codex"), default="claude")
    doctor.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    doctor.add_argument("--sessions-dir", default=DEFAULT_LOGS["codex"])
    doctor.add_argument("--codex-config", default=DEFAULT_CODEX_CONFIG_PATH)
    doctor.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    doctor.add_argument("--days", type=int, default=DEFAULT_DOCTOR_DAYS)
    gain = commands.add_parser(
        "gain", help="the time since your last init vs the same time before it"
    )
    gain.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    gain.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    gain.add_argument("--json", action="store_true", help="print the measurement as JSON")
    limits = commands.add_parser(
        "limits", help="what a point of your 5-hour and weekly windows costs in usage"
    )
    limits.add_argument("--agent", choices=("claude", "codex"), default="claude")
    limits.add_argument("--sessions-dir", default=DEFAULT_LOGS["codex"])
    limits.add_argument("--days", type=int, default=DEFAULT_DOCTOR_DAYS)
    limits.add_argument("--json", action="store_true", help="print the Codex point cost as JSON")
    quota = commands.add_parser(
        "quota", help="read current account-wide subscription windows without a model request"
    )
    quota.add_argument("--agent", choices=("claude", "codex"), required=True)
    quota.add_argument("--timeout", type=float, default=25.0)
    preparation = commands.add_parser(
        "preparation-report", help="count local research opportunities without model calls"
    )
    preparation.add_argument("--agent", choices=("claude", "codex"), default="claude")
    preparation.add_argument("--logs-dir", type=Path)
    preparation.add_argument("--days", type=int, default=DEFAULT_DOCTOR_DAYS)
    preparation.add_argument("--json", action="store_true")
    prepare = commands.add_parser(
        "prepare", help="assemble literal task context from explicit source targets, locally"
    )
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--request-file", type=Path, required=True)
    prepare.add_argument(
        "--source",
        action="append",
        required=True,
        help="relative file, file:start:end, or file::Python.symbol (repeatable)",
    )
    prepare.add_argument("--evidence-file", action="append", default=[])
    prepare.add_argument("--max-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    prepare.add_argument("--json", action="store_true")
    closure = commands.add_parser(
        "closure-probe", help="run an isolated verified task closure pilot"
    )
    closure.add_argument("--output", type=Path, required=True)
    closure.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
    closure.add_argument("--effort", choices=("low", "medium", "high"), default=DEFAULT_EFFORT)
    closure.add_argument("--timeout", type=int, default=300)
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
        help="baseline, governor, targeted-governor, prepared-governor, rtk, rtk-governor, "
        "mask, boundary, meter, meter-governor (preparation arms support both agents)",
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
    bench_report.add_argument("--background", default=None, help=BACKGROUND_HELP)
    remeasure = commands.add_parser(
        "bench-remeasure",
        help="re-measure a result set from the agents' logs with the current schema (no runs)",
    )
    remeasure.add_argument("--name", required=True)
    remeasure.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    remeasure.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    remeasure.add_argument("--background", default=None, help=BACKGROUND_HELP)
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


def optional_text(value: object) -> str | None:
    """An argparse value that is either not given (None) or a string."""
    return None if value is None else str(value)


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


def background_samples(requested: str | None, home: Path) -> list[LimitSample]:
    """The readings of the sessions outside the meter runs.

    The directory asked for, which must exist; else the mod's own ledger, where no ledger means
    that no other session was recorded.
    """
    if requested is not None:
        return read_samples(Path(requested).expanduser())
    ledger = limits_dir(home)
    return read_samples(ledger) if ledger.is_dir() else []


def bench_report_text(
    results: Sequence[RunResult], results_dir: Path, requested: str | None, home: Path
) -> str:
    """The report of a result set; the meter arms' readings are read from beside the results."""
    run_limits = load_run_limits(results_dir, results)
    background = background_samples(requested, home) if run_limits else []
    return render_bench_report(results, run_limits, background)


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
        elif command == "doctor" and args.agent == "codex":
            print(
                render_codex_doctor(
                    diagnose_codex(
                        Path(str(args.sessions_dir)).expanduser(),
                        Path(str(args.codex_config)).expanduser(),
                        int(args.days),
                        time.time(),
                    )
                )
            )
        elif command == "doctor":
            print(
                render_doctor(
                    diagnose_claude(
                        Path(str(args.projects_dir)).expanduser(),
                        Path(str(args.settings)).expanduser(),
                        config.home,
                        int(args.days),
                        time.time(),
                    )
                )
            )
        elif command == "gain":
            measured = measure_gain(
                Path(str(args.projects_dir)).expanduser(),
                config.home,
                Path(str(args.settings)).expanduser(),
                time.time(),
            )
            print(gain_json(measured) if args.json else render_gain(measured))
        elif command == "limits" and args.agent == "codex":
            rollouts = recent_rollouts(
                Path(str(args.sessions_dir)).expanduser(), int(args.days), time.time()
            )
            costs = [window_cost(rollouts, kind) for kind in WINDOW_KINDS.values()]
            print(codex_limits_json(costs) if args.json else render_codex_limits(costs))
        elif command == "limits" and args.json:
            raise ConfigError("limits --json reads Codex rollouts only: add --agent codex")
        elif command == "limits":
            samples = read_samples(limits_dir(config.home))
            print(render_limits(samples))
        elif command == "quota":
            snapshot = (
                read_claude_quota(float(args.timeout))
                if args.agent == "claude"
                else read_codex_quota(float(args.timeout))
            )
            print(quota_json(snapshot))
        elif command == "preparation-report":
            root = args.logs_dir or Path(DEFAULT_LOGS[str(args.agent)])
            report = diagnose_preparation(
                str(args.agent), root.expanduser(), int(args.days), time.time()
            )
            print(report_json(report) if args.json else render_report(report))
        elif command == "prepare":
            packet = build_packet(
                args.root.resolve(),
                read_literal(args.request_file),
                tuple(parse_target(str(raw)) for raw in args.source),
                tuple(str(path) for path in args.evidence_file),
            )
            sys.stdout.write(
                packet_json(packet, int(args.max_bytes))
                if args.json
                else render_packet(packet, int(args.max_bytes))
            )
        elif command == "closure-probe":
            print(
                json.dumps(
                    run_probe(args.output, str(args.model), str(args.effort), int(args.timeout)),
                    indent=2,
                )
            )
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
            print(
                bench_report_text(results, results_dir, optional_text(args.background), config.home)
            )
        elif command == "bench-remeasure":
            name = str(args.name)
            results_dir = Path(str(args.results_dir)) / name
            results = remeasure_results(results_dir, Path(str(args.work_dir)) / name)
            print(
                bench_report_text(results, results_dir, optional_text(args.background), config.home)
            )
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
