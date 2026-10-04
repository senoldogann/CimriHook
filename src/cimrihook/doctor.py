"""Cost anatomy: where the money goes in the user's own Claude Code records.

Every API request re-reads the conversation so far, so the cost of a session grows with the square
of its length. The report prices every real request in the records at API list prices from its own
usage data and splits the cost along the axes where this growth shows: the request's context
size, the token type, large cache rewrites and their likely causes, the static prefix, main
session against subagents, compactions. The effect of the recommended compaction window comes
from the simulator and is labelled as an estimate that no A/B run has confirmed.
"""

import os
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.errors import TranscriptError
from cimrihook.install import MARKER, WINDOW_ENV, env_of, hooks_of, is_ours, load_settings
from cimrihook.lifetime import (
    MAIN,
    LifetimeReplay,
    recommended_lifetime,
    replay_lifetimes,
)
from cimrihook.mods import MOD_NAME, PLUGIN_DIRS_ENV
from cimrihook.prefix import PrefixPart, prefix_dir, prefix_parts, prefix_text, read_records
from cimrihook.scan import (
    Request,
    TranscriptScan,
    recent_requests,
    scan_transcript,
    token_costs,
    unique_scans,
)
from cimrihook.simulate import (
    MEASURED_REFETCH_REQUESTS,
    MEASURED_REFETCH_TOKENS,
    CostOverrides,
    SimulationResult,
    claude_hint,
    context_of,
    dollar_weight,
    load_claude_traces,
    recommendation_text,
    recommended_window,
    simulate_traces,
    total_usage,
    usd_per_token,
    written_of,
)
from cimrihook.tail import FIVE_MINUTES, ONE_HOUR, read_session_tail
from cimrihook.transcripts import (
    SECONDS_PER_DAY,
    average_write_weight,
    bench_transcripts,
    recent_transcripts,
)

BANDS: Final = ((100_000, "up to 100k"), (200_000, "100k-200k"), (400_000, "200k-400k"))
TOP_BAND: Final = "over 400k"
REWRITE_TOKENS: Final = 100_000  # a request writing more input than this is a large rewrite
# First-request context of Claude Code 2.1.288 without plugins, MCP or user settings (A/B bench).
BARE_PREFIX_TOKENS: Final = 16_600
SESSION_START: Final = "session start"
AFTER_COMPACTION: Final = "after compaction"
IDLE_HOUR: Final = "idle over 1 hour (cache expired)"
IDLE_FIVE_MINUTES: Final = "idle 5-60 min on a 5-minute cache"
MODEL_SWITCH: Final = "model switch"
OTHER_BREAK: Final = "other cache break"
REWRITE_CAUSES: Final = (
    IDLE_HOUR,
    IDLE_FIVE_MINUTES,
    SESSION_START,
    AFTER_COMPACTION,
    MODEL_SWITCH,
    OTHER_BREAK,
)


@dataclass(frozen=True, slots=True)
class Share:
    """A line item's name, count (requests or tokens) and USD cost."""

    label: str
    count: int
    usd: float


@dataclass(frozen=True, slots=True)
class Anatomy:
    """The data of the report."""

    transcripts: int
    days: int
    requests: int
    unpriced_requests: int
    unpriced_models: tuple[str, ...]
    total_usd: float
    main_usd: float
    subagent_usd: float
    bands: tuple[Share, ...]  # by the request's context size
    token_types: tuple[Share, ...]  # count: tokens
    rewrites: tuple[Share, ...]  # large rewrites, by cause; cost: the part written
    prefix_main: int | None  # median context of the first request of the sessions
    prefix_subagent: int | None
    compactions: int
    compaction_trigger: int | None  # median
    after_compaction_context: int | None  # context of the first request after a compaction, median
    prefix_excess_usd: float  # estimated cost share from first-request input; not a saving


def band(context: int) -> str:
    """Band of a request's context size."""
    return next((label for limit, label in BANDS if context <= limit), TOP_BAND)


def rewrite_cause(request: Request) -> str | None:
    """Likely cause of a large cache rewrite; None if it is not a large rewrite."""
    if written_of(request.usage) <= REWRITE_TOKENS:
        return None
    if request.first:
        return SESSION_START
    if request.after_compaction:
        return AFTER_COMPACTION
    if request.gap_seconds is not None and request.gap_seconds > ONE_HOUR:
        return IDLE_HOUR
    if (
        request.gap_seconds is not None
        and request.gap_seconds > FIVE_MINUTES
        and request.usage.write_1h == 0
    ):
        return IDLE_FIVE_MINUTES
    if request.model_switch:
        return MODEL_SWITCH
    return OTHER_BREAK


def build_anatomy(scans: Sequence[TranscriptScan], days: int) -> Anatomy:
    """Cost anatomy of the scanned transcripts."""
    requests = [request for scan in scans for request in scan.requests]
    priced = [
        (request, token_costs(request, base))
        for request in requests
        if (base := usd_per_token(request.model)) is not None
    ]
    total = sum(sum(costs) for _, costs in priced)
    labels = (*(label for _, label in BANDS), TOP_BAND)
    causes = [(rewrite_cause(request), costs) for request, costs in priced]
    firsts = [request for request in requests if request.first]
    after = [context_of(request.usage) for request in requests if request.after_compaction]
    triggers = [compaction.trigger for scan in scans for compaction in scan.compactions]
    usages = [request.usage for request, _ in priced]
    return Anatomy(
        transcripts=len(scans),
        days=days,
        requests=len(requests),
        unpriced_requests=len(requests) - len(priced),
        unpriced_models=tuple(
            sorted({request.model for request in requests if usd_per_token(request.model) is None})
        ),
        total_usd=total,
        main_usd=sum(sum(costs) for request, costs in priced if not request.subagent),
        subagent_usd=sum(sum(costs) for request, costs in priced if request.subagent),
        bands=tuple(
            Share(
                label,
                sum(1 for request, _ in priced if band(context_of(request.usage)) == label),
                sum(
                    sum(costs)
                    for request, costs in priced
                    if band(context_of(request.usage)) == label
                ),
            )
            for label in labels
        ),
        token_types=(
            Share(
                "cache read",
                sum(usage.read for usage in usages),
                sum(costs[0] for _, costs in priced),
            ),
            Share(
                "cache write",
                sum(usage.write_5m + usage.write_1h for usage in usages),
                sum(costs[1] for _, costs in priced),
            ),
            Share(
                "uncached input",
                sum(usage.uncached for usage in usages),
                sum(costs[2] for _, costs in priced),
            ),
            Share("output", sum(usage.output for usage in usages), sum(c[3] for _, c in priced)),
        ),
        rewrites=tuple(
            Share(
                cause,
                sum(1 for found, _ in causes if found == cause),
                sum(costs[1] + costs[2] for found, costs in causes if found == cause),
            )
            for cause in REWRITE_CAUSES
        ),
        prefix_main=median_or_none([context_of(r.usage) for r in firsts if not r.subagent]),
        prefix_subagent=median_or_none([context_of(r.usage) for r in firsts if r.subagent]),
        compactions=len(triggers),
        compaction_trigger=median_or_none(triggers),
        after_compaction_context=median_or_none(after),
        prefix_excess_usd=sum(prefix_excess(scan) for scan in scans),
    )


def prefix_excess(scan: TranscriptScan) -> float:
    """Estimated input cost of the first-request input above the historical bare baseline.

    The first request also holds the user's messages. Each request's read, write and uncached cost
    is shared by that amount's ratio to the context; the result measures neither the real prefix
    nor a removable saving. A session whose first request falls outside the period gets no cost.
    """
    first = next((request for request in scan.requests if request.first), None)
    if first is None or context_of(first.usage) <= BARE_PREFIX_TOKENS:
        return 0.0
    excess = context_of(first.usage) - BARE_PREFIX_TOKENS
    return sum(
        sum(costs[:3]) * min(excess / context_of(request.usage), 1.0)
        for request in scan.requests
        if (base := usd_per_token(request.model)) is not None
        and (costs := token_costs(request, base))
        and context_of(request.usage) > 0
    )


def median_or_none(values: Sequence[int]) -> int | None:
    """Median of the values; None if there are none."""
    return int(statistics.median(values)) if values else None


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """The data of the doctor report."""

    anatomy: Anatomy
    simulation: SimulationResult | None  # unmeasurable if the records hold no real compaction
    guard_check: str
    bench_transcripts: int  # CimriHook A/B run transcripts that were left out
    setup: str  # window, guard and status line in the settings file
    lifetimes: tuple[LifetimeReplay, LifetimeReplay]  # main sessions and subagents
    prefix_parts: tuple[PrefixPart, ...]  # median tokens and session count of each category
    prefix_sessions: int


def diagnose_claude(
    projects_dir: Path, settings_path: Path, home: Path, days: int, now: float
) -> Diagnosis:
    """Cost anatomy of the last `days` days, a simulation of window policies and a guard self-check.

    The anatomy counts only requests in the window; requests and compactions that forked or resumed
    sessions copied are counted once. The simulation replays the sessions active in the window as a
    whole. If the records hold no real compaction, the cost of a compaction cannot be measured: no
    simulation is made and the report says so.
    """
    files = recent_transcripts(projects_dir, days, now)
    scans = recent_requests(
        unique_scans([scan_transcript(path, "subagents" in path.parts) for path in files]),
        now - days * SECONDS_PER_DAY,
    )
    anatomy = build_anatomy(scans, days)
    guard = guard_check(files)
    bench = bench_transcripts(projects_dir, days, now)
    setup = setup_line(settings_path)
    lifetimes = replay_lifetimes(scans)
    records = read_records(prefix_dir(home), now - days * SECONDS_PER_DAY)
    parts, recorded = tuple(prefix_parts(records)), len(records)
    if anatomy.compactions == 0:
        return Diagnosis(anatomy, None, guard, bench, setup, lifetimes, parts, recorded)
    traces = load_claude_traces(files)
    requests = [usage for trace in traces for usage in trace.requests]
    simulation = simulate_traces(
        "Claude Code",
        traces,
        days,
        average_write_weight(total_usage(requests)),
        CostOverrides(None, None, None, MEASURED_REFETCH_TOKENS, MEASURED_REFETCH_REQUESTS, None),
        dollar_weight,
    )
    return Diagnosis(anatomy, simulation, guard, bench, setup, lifetimes, parts, recorded)


def setup_line(settings_path: Path) -> str:
    """What CimriHook has enabled in the settings file: compaction window, guard, status line."""
    settings = load_settings(settings_path)
    override = env_of(settings).get(WINDOW_ENV)
    window = settings.get("autoCompactWindow")
    if override is not None:
        compaction = f"{WINDOW_ENV}={override} (overrides every window setting)"
    elif isinstance(window, int) and not isinstance(window, bool):
        compaction = (
            f"autoCompactWindow {window} (compacts at about {tokens_text(window - 33_000)} on "
            "models with a larger context window)"
        )
    else:
        compaction = "Claude Code's default (about 33k below each model's context window)"
    guard = any(
        f"{MARKER}guard" in command
        for entry in hooks_of(settings).get("UserPromptSubmit", [])
        for command in hook_commands(entry)
    )
    status = is_ours(settings.get("statusLine"))
    plugin_dirs = str(env_of(settings).get(PLUGIN_DIRS_ENV, ""))
    mod = any(Path(part).name == MOD_NAME for part in plugin_dirs.split(os.pathsep) if part)
    return (
        f"Setup ({settings_path}): compaction window {compaction}; cold-prompt guard "
        f"{'on' if guard else 'off'}; status line {'on' if status else 'off'}; idle-compaction "
        f"mod {'on' if mod else 'off'}"
    )


def hook_commands(entry: object) -> list[str]:
    """The command texts in a hook group."""
    handlers = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(handlers, list):
        return []
    return [
        str(handler.get("command"))
        for handler in handlers
        if isinstance(handler, dict) and isinstance(handler.get("command"), str)
    ]


def guard_check(files: Sequence[Path]) -> str:
    """Whether the cold-prompt guard can read the cache state of the newest main session: if Claude
    Code changes the record format, the guard either errors or is silently disabled."""
    mains = [path for path in files if "subagents" not in path.parts]
    if not mains:
        return "Guard check: no main session transcript in these days"
    newest = mains[-1]
    try:
        tail = read_session_tail(str(newest))
    except TranscriptError as error:
        return f"Guard check: FAILS on the newest session: {error}"
    if tail is None:
        return (
            f"Guard check: the newest session ({newest.stem}) has no cache write in its last 4 MiB "
            "or was just compacted, so the guard stays quiet there"
        )
    cache = "1-hour" if tail.ttl_seconds >= ONE_HOUR else "5-minute"
    return (
        f"Guard check: reads the newest session's cache state ({cache} cache, "
        f"{tokens_text(tail.context_tokens)} tokens of context)"
    )


def percent(part: float, whole: float) -> str:
    """Percentage text."""
    return f"{100 * part / whole:.0f}%" if whole > 0 else "-"


def tokens_text(tokens: int | None) -> str:
    """Short text of a token count (for example 34.6k, 1.25M, 7.31B)."""
    if tokens is None:
        return "-"
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k"
    return f"{tokens / 1e6:.2f}M" if tokens < 1_000_000_000 else f"{tokens / 1e9:.2f}B"


def render_doctor(diagnosis: Diagnosis) -> str:
    """The text of the report."""
    anatomy = diagnosis.anatomy
    simulation = diagnosis.simulation
    total = anatomy.total_usd
    unpriced = (
        f"; {anatomy.unpriced_requests:,} requests of {', '.join(anatomy.unpriced_models)} "
        "have no list price and are left out"
        if anatomy.unpriced_requests
        else ""
    )
    rewritten = sum(share.usd for share in anatomy.rewrites)
    lines = [
        f"CimriHook doctor (Claude Code, last {anatomy.days} days): {anatomy.transcripts:,} "
        f"transcripts, {anatomy.requests:,} API requests, ${total:,.0f} at API list prices"
        f"{unpriced}"
        + (
            f"; {diagnosis.bench_transcripts:,} transcripts of CimriHook's A/B runs left out"
            if diagnosis.bench_transcripts
            else ""
        ),
        bottom_line(anatomy, simulation),
        f"  main sessions ${anatomy.main_usd:,.0f} ({percent(anatomy.main_usd, total)}), "
        f"subagents ${anatomy.subagent_usd:,.0f} ({percent(anatomy.subagent_usd, total)})",
        "Spend by the context size of the request (every request re-reads the whole conversation):",
        *(
            f"  {share.label:<12} ${share.usd:>10,.0f} {percent(share.usd, total):>5}"
            f"  {share.count:>8,} requests"
            for share in anatomy.bands
        ),
        "Spend by token type:",
        *(
            f"  {share.label:<15} ${share.usd:>10,.0f} {percent(share.usd, total):>5}"
            f"  {tokens_text(share.count):>9} tokens"
            for share in anatomy.token_types
        ),
        f"Cache rewrites over {REWRITE_TOKENS // 1000}k tokens in one request: "
        f"${rewritten:,.0f} ({percent(rewritten, total)}) re-caching context that was already "
        "paid for:",
        *(
            f"  {share.label:<36} {share.count:>5} req  ${share.usd:>8,.0f}"
            for share in anatomy.rewrites
            if share.count
        ),
        f"First-request input (prefix and initial messages): main "
        f"{tokens_text(anatomy.prefix_main)}, subagent {tokens_text(anatomy.prefix_subagent)} "
        f"tokens; historical bare Claude Code baseline {tokens_text(BARE_PREFIX_TOKENS)} "
        "(version and environment dependent)",
        *prefix_lines(diagnosis),
        f"Compactions: {anatomy.compactions:,}; median trigger "
        f"{tokens_text(anatomy.compaction_trigger)} tokens, next request "
        f"{tokens_text(anatomy.after_compaction_context)} tokens",
        "Cache lifetime (each request re-priced with your own pauses; cache input only):",
        *(lifetime_line(replay) for replay in diagnosis.lifetimes if replay.requests),
        *recommendations(anatomy, simulation),
        *lifetime_recommendations(diagnosis.lifetimes),
        diagnosis.setup,
        diagnosis.guard_check,
    ]
    return "\n".join(lines)


def bottom_line(anatomy: Anatomy, simulation: SimulationResult | None) -> str:
    """The report's one number: how much of the spend was avoidable (an estimate) and how."""
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    found = None if simulation is None else recommended_window(simulation)
    chosen = None if found is None else found[0]
    window = None if chosen is None else chosen.policy.window
    if simulation is None or chosen is None or window is None:
        return (
            f"Bottom line: ${idle.usd:,.0f} went to re-caching sessions after an hour idle; no "
            "compaction window lowers the simulated cost of these logs"
        )
    saving = simulation.outcomes[0].cost - chosen.cost
    share = saving / simulation.outcomes[0].cost
    return (
        f"Bottom line: about ${saving:,.0f} ({100 * share:.0f}%) was avoidable by compacting "
        f"above {tokens_text(window)} (simulated), plus ${idle.usd:,.0f} of re-caching after an "
        f"hour idle; apply with {claude_hint(window)}"
    )


def prefix_lines(diagnosis: Diagnosis) -> list[str]:
    """What the prefix above bare cost and, when the mod recorded sessions, what it is made of."""
    anatomy = diagnosis.anatomy
    lines = []
    if anatomy.prefix_excess_usd > 0:
        lines.append(
            f"  estimated input-cost allocation above that baseline: "
            f"${anatomy.prefix_excess_usd:,.0f} "
            f"({percent(anatomy.prefix_excess_usd, anatomy.total_usd)} of the spend); "
            "includes initial messages and is not measured recoverable savings"
        )
    if diagnosis.prefix_parts:
        lines.append(
            f"  active categories ({diagnosis.prefix_sessions} recorded sessions; median when "
            f"present, as /context counts them): {prefix_text(list(diagnosis.prefix_parts))}"
        )
    return lines


def lifetime_line(replay: LifetimeReplay) -> str:
    """A group's cost under both lifetimes and the replay's error margin."""
    error = replay.replay_error()
    now = replay.current or "no cache writes"
    base = replay.replayed(replay.current) if replay.current is not None else 0.0
    parts = [
        f"{lifetime} ${replay.replayed(lifetime):,.0f}"
        + (
            f" ({100 * (replay.replayed(lifetime) - base) / base:+.0f}%)"
            if base > 0 and lifetime != replay.current
            else ""
        )
        for lifetime in ("5m", "1h")
    ]
    check = "" if error is None else f"; replay check {100 * error:+.1f}%"
    return f"  {replay.bucket:<14} {now:>3} now: {', '.join(parts)}{check}"


def lifetime_recommendations(lifetimes: Sequence[LifetimeReplay]) -> list[str]:
    """Cache lifetime recommendations: only if the other lifetime is cheaper beyond the error."""
    lines: list[str] = []
    for replay in lifetimes:
        better = recommended_lifetime(replay)
        if better is None or replay.current is None:
            continue
        flag = "--cache-ttl" if replay.bucket == MAIN else "--subagent-cache-ttl"
        saving = 1 - replay.replayed(better) / replay.replayed(replay.current)
        lines.append(
            f"  cache lifetime: {replay.bucket} would spend {100 * saving:.0f}% less on cache "
            f"input with {better} caches. Apply with `cimrihook init {flag} {better}`"
        )
    return lines


def recommendations(anatomy: Anatomy, simulation: SimulationResult | None) -> list[str]:
    """Recommended changes and their estimated effects; estimates are labelled as unconfirmed."""
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    lines = ["What would change it (estimates):"]
    if simulation is None:
        lines.append(
            "  compaction window: no real compaction in these logs, so its cost cannot be "
            "measured; run `cimrihook simulate` with --post-compact-tokens, "
            "--post-compact-cached and --summary-tokens"
        )
    else:
        found = recommended_window(simulation)
        window = None if found is None else found[0].policy.window
        if found is None or window is None:
            lines.append("  compaction window: no window lowers the simulated cost of these logs")
        else:
            chosen, cheapest = found
            lines.append(
                "  compact earlier: "
                f"{recommendation_text(chosen, cheapest, simulation.outcomes[0].cost)} "
                f"(simulation with {MEASURED_REFETCH_TOKENS // 1000}k tokens re-read after each "
                "compaction; in the A/B runs so far the simulator was 0-11 points optimistic, see "
                f"`cimrihook bench-calibrate`). Apply with {claude_hint(window)}"
            )
    if idle.count:
        lines.append(
            f"  ask before cold sends: ${idle.usd:,.0f} went to re-caching contexts after more "
            "than an hour idle; compacting first would re-cache a much smaller context"
        )
    if anatomy.prefix_main is not None and anatomy.prefix_main > BARE_PREFIX_TOKENS:
        lines.append(
            f"  inspect /context for optional active tools or instructions: first-request input "
            f"is {tokens_text(anatomy.prefix_main - BARE_PREFIX_TOKENS)} tokens above the "
            "historical baseline, including initial messages. Measure a matched task comparison "
            "before claiming savings from a smaller profile"
        )
    return lines
