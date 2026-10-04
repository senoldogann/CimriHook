"""Codex doctor: where a Codex subscription's windows go, read from the rollout records alone.

Codex CLI (and every client of its app-server) writes a `token_count` event after each request
with that request's token counts and the account's window readings at that moment
(`rate_limits`: the used percentage of the 5-hour and the weekly window and their reset times).
Unlike Claude Code, no mod is needed to see how the windows move: the readings and the work that
moved them are in the same file.

The report prices requests in base input units (uncached input = 1, cached input 0.1, output 6,
the OpenAI ratios `simulate` uses): Codex credits and subscription windows have no published
token rates, so these units order the spend, they do not bill it. The window cost is measured
instead: between consecutive whole-percent crossings of a window, the points it moved are
regressed on the input (uncached + 0.1 x cached) and the output tokens of every recorded
session (`cimrihook.weights`). Use outside these rollouts (Codex cloud tasks, another machine)
also moves the windows, so a point then looks cheaper than it is.
"""

import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cimrihook.claude import JsonObject
from cimrihook.codex_config import (
    WHOLE_CONTEXT_SCOPE,
    parse_toml,
    read_config,
    threshold_scope,
    top_level_integer,
)
from cimrihook.doctor import BANDS, TOP_BAND, percent, tokens_text
from cimrihook.errors import ConfigError
from cimrihook.limits import LimitSample, WindowUse
from cimrihook.simulate import OPENAI, rollout_cwd
from cimrihook.transcripts import BENCH_PROJECT_MARKER, SECONDS_PER_DAY, parse_line
from cimrihook.weights import (
    MIN_EXTRA_SPANS,
    ClassWeight,
    Span,
    crossing_spans,
    fit_weights,
    sessions_of,
    spend_curve,
    window_crossings,
)

CODEX_LIMIT: Final = "codex"  # the limit id of the subscription's own windows
WINDOW_KINDS: Final = {300: "five_hour", 10_080: "seven_day"}  # window minutes -> kind
WINDOW_LABELS: Final = {"five_hour": "5-hour window", "seven_day": "weekly window"}
CLASSES: Final = ("input", "output")


@dataclass(frozen=True, slots=True)
class CodexRequest:
    """One request of a rollout with the window readings Codex wrote beside it."""

    time: float  # epoch seconds
    uncached: int
    cached: int
    output: int  # includes the reasoning tokens
    reasoning: int
    readings: tuple[WindowUse, ...]  # empty if the event carried no subscription reading


@dataclass(frozen=True, slots=True)
class CodexRollout:
    """The requests and compactions of one rollout file."""

    session: str
    requests: tuple[CodexRequest, ...]
    compaction_triggers: tuple[int, ...]  # context of the last request before each compaction
    context_window: int | None  # the model's window as Codex reported it


@dataclass(frozen=True, slots=True)
class BandShare:
    """Base input units of the requests whose context falls in one band."""

    label: str
    units: float
    requests: int


@dataclass(frozen=True, slots=True)
class WindowCost:
    """What a point of one window costs, fitted from the crossings."""

    kind: str
    spans: int
    points: float
    input_weight: ClassWeight | None  # points per input token; None without enough spans
    output_weight: ClassWeight | None  # points per output token
    output_share: float | None  # share of the points the output moved
    pooled: ClassWeight | None  # points per base input unit, input and output at list ratios


@dataclass(frozen=True, slots=True)
class PointCost:
    """A window point in tokens, for other programs; a value is None when it is not measured."""

    kind: str
    spans: int
    points: float
    input_tokens: float | None  # input tokens per point, when input and output are told apart
    output_tokens: float | None
    output_share: float | None
    base_units: float | None  # base input units per point at list ratios


@dataclass(frozen=True, slots=True)
class CodexDiagnosis:
    """The report's data."""

    days: int
    sessions: int
    requests: int
    bands: tuple[BandShare, ...]
    uncached: int
    cached: int
    output: int
    reasoning: int
    compactions: int
    median_trigger: int | None
    context_window: int | None
    threshold: int | None  # model_auto_compact_token_limit in config.toml
    threshold_scope: str  # model_auto_compact_token_limit_scope, `total` by default
    windows: tuple[WindowCost, ...]


def diagnose_codex(sessions_dir: Path, config_path: Path, days: int, now: float) -> CodexDiagnosis:
    """Reads the rollouts modified in the last `days` days and the configured threshold."""
    rollouts = recent_rollouts(sessions_dir, days, now)
    requests = [request for rollout in rollouts for request in rollout.requests]
    triggers = [tokens for rollout in rollouts for tokens in rollout.compaction_triggers]
    windows = [rollout.context_window for rollout in rollouts if rollout.context_window]
    config = parse_toml(read_config(config_path), config_path)
    return CodexDiagnosis(
        days=days,
        sessions=len([rollout for rollout in rollouts if rollout.requests]),
        requests=len(requests),
        bands=band_shares(requests),
        uncached=sum(request.uncached for request in requests),
        cached=sum(request.cached for request in requests),
        output=sum(request.output for request in requests),
        reasoning=sum(request.reasoning for request in requests),
        compactions=len(triggers),
        median_trigger=int(statistics.median(triggers)) if triggers else None,
        context_window=int(statistics.median(windows)) if windows else None,
        threshold=top_level_integer(config, config_path),
        threshold_scope=threshold_scope(config, config_path),
        windows=tuple(window_cost(rollouts, kind) for kind in WINDOW_KINDS.values()),
    )


def recent_rollouts(sessions_dir: Path, days: int, now: float) -> list[CodexRollout]:
    """Rollouts modified in the window, CimriHook's A/B runs left out.

    A forked or resumed rollout copies the requests of the one it came from; a request (same time
    and counts) is kept only in the first file it is seen in, so it is counted once.
    """
    min_mtime = now - days * SECONDS_PER_DAY
    files = sorted(
        path
        for path in sessions_dir.rglob("rollout-*.jsonl")
        if path.stat().st_mtime >= min_mtime
        and BENCH_PROJECT_MARKER not in (rollout_cwd(path) or "")
    )
    if not files:
        raise ConfigError(
            f"no Codex rollouts under {sessions_dir} modified in the last {days} days"
        )
    seen: set[tuple[float, int, int, int]] = set()
    unique: list[CodexRollout] = []
    for rollout in (read_rollout(path) for path in files):
        keys = [(r.time, r.uncached, r.cached, r.output) for r in rollout.requests]
        kept = tuple(r for r, key in zip(rollout.requests, keys, strict=True) if key not in seen)
        seen.update(keys)
        unique.append(
            CodexRollout(rollout.session, kept, rollout.compaction_triggers, rollout.context_window)
        )
    return unique


def read_rollout(path: Path) -> CodexRollout:
    """The token_count events (one per request) and the compactions of a rollout."""
    requests: list[CodexRequest] = []
    triggers: list[int] = []
    context_window: int | None = None
    last_total: int | None = None
    with path.open("rb") as handle:
        for number, raw_line in enumerate(handle, start=1):
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "compacted" and requests:
                triggers.append(requests[-1].uncached + requests[-1].cached)
                continue
            payload = entry.get("payload")
            if entry.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            info = payload.get("info")
            if payload.get("type") != "token_count" or not isinstance(info, dict):
                continue
            last, total = info.get("last_token_usage"), info.get("total_token_usage")
            if not isinstance(last, dict) or not isinstance(total, dict):
                continue
            if total.get("total_tokens") == last_total:
                continue  # the same cumulative total: no new request
            last_total = integer(total, "total_tokens", f"{path}:{number}")
            window = info.get("model_context_window")
            context_window = window if isinstance(window, int) else context_window
            requests.append(
                codex_request(last, payload.get("rate_limits"), entry, f"{path}:{number}")
            )
    return CodexRollout(path.stem, tuple(requests), tuple(triggers), context_window)


def codex_request(usage: JsonObject, limits: object, entry: JsonObject, where: str) -> CodexRequest:
    """One request from a token_count event; input_tokens includes the cached tokens."""
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        raise ConfigError(f"{where}: a token_count event without a timestamp")
    tokens = integer(usage, "input_tokens", where)
    cached = integer(usage, "cached_input_tokens", where)
    reasoning = usage.get("reasoning_output_tokens")
    return CodexRequest(
        time=datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp(),
        uncached=tokens - cached,
        cached=cached,
        output=integer(usage, "output_tokens", where),
        reasoning=reasoning if isinstance(reasoning, int) else 0,
        readings=window_readings(limits, where),
    )


def window_readings(limits: object, where: str) -> tuple[WindowUse, ...]:
    """The subscription windows of a rate_limits record; none for another limit or no record."""
    if not isinstance(limits, dict) or limits.get("limit_id") not in (CODEX_LIMIT, None):
        return ()
    readings: list[WindowUse] = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict) or window.get("window_minutes") not in WINDOW_KINDS:
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, int | float):
            raise ConfigError(f"{where}: rate_limits.{name} without a numeric used_percent")
        kind = WINDOW_KINDS[int(str(window["window_minutes"]))]
        readings.append(WindowUse(kind, float(used), str(window.get("resets_at"))))
    return tuple(readings)


def integer(record: JsonObject, key: str, where: str) -> int:
    """A required integer field of a usage record."""
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: token usage without an integer {key} ({value!r})")
    return value


def units(request: CodexRequest) -> float:
    """Base input units of a request at the OpenAI ratios."""
    return (
        request.uncached * OPENAI.uncached
        + request.cached * OPENAI.read
        + request.output * OPENAI.output
    )


def input_units(request: CodexRequest) -> float:
    """Input of a request with cached tokens at their discount, the window fit's input class."""
    return request.uncached + request.cached * OPENAI.read


def band_shares(requests: Sequence[CodexRequest]) -> tuple[BandShare, ...]:
    """Base input units by the context size of the request."""
    labels = (*(label for _, label in BANDS), TOP_BAND)
    by_label = {
        label: [r for r in requests if band_of(r.uncached + r.cached) == label] for label in labels
    }
    return tuple(
        BandShare(label, sum(units(r) for r in members), len(members))
        for label, members in by_label.items()
    )


def band_of(context: int) -> str:
    """The band of a context size."""
    return next((label for limit, label in BANDS if context <= limit), TOP_BAND)


def window_cost(rollouts: Sequence[CodexRollout], kind: str) -> WindowCost:
    """Points of one window per input and per output token, fitted over the crossings."""
    inputs = [sample for rollout in rollouts for sample in samples(rollout, input_units)]
    outputs = [sample for rollout in rollouts for sample in samples(rollout, output_tokens)]
    if not inputs:
        return WindowCost(kind, 0, 0.0, None, None, None, None)
    ordered = sorted(inputs, key=lambda sample: sample.time)
    spans = crossing_spans(
        window_crossings(ordered, kind),
        [
            [spend_curve(group) for group in sessions_of(inputs)],
            [spend_curve(group) for group in sessions_of(outputs)],
        ],
        ordered[0].time,
        ordered[-1].time,
    )
    fit = fit_weights(spans, CLASSES)
    points = sum(span.points for span in spans)
    pooled_fit = fit_weights(pooled_units(spans), ["base units"])
    pooled = pooled_fit.weights[0] if pooled_fit is not None else None
    if fit is None or len(fit.weights) != len(CLASSES):
        return WindowCost(kind, len(spans), points, None, None, None, pooled)
    input_weight, output_weight = fit.weights
    return WindowCost(
        kind,
        len(spans),
        points,
        input_weight,
        output_weight,
        output_share(spans, input_weight, output_weight),
        pooled,
    )


def pooled_units(spans: Sequence[Span]) -> list[Span]:
    """The spans with input and output added into one class of base input units."""
    return [
        Span(span.start, span.end, span.points, (span.spends[0] + OPENAI.output * span.spends[1],))
        for span in spans
    ]


def output_tokens(request: CodexRequest) -> float:
    """Output tokens of a request, the window fit's output class."""
    return float(request.output)


def samples(rollout: CodexRollout, amount: Callable[[CodexRequest], float]) -> list[LimitSample]:
    """A rollout as the readings `cimrihook.weights` reads: cumulative amount and windows.

    Only requests with a subscription reading count: a rollout of another provider's model
    (an Ollama or gateway model) carries none and moves no Codex window.
    """
    total = 0.0
    readings: list[LimitSample] = []
    for request in rollout.requests:
        if not request.readings:
            continue
        total += amount(request)
        readings.append(LimitSample(rollout.session, request.time, total, request.readings))
    return readings


def output_share(spans: Sequence[Span], input_weight: ClassWeight, output: ClassWeight) -> float:
    """Share of the fitted points the output moved over all spans."""
    input_points = input_weight.weight * sum(span.spends[0] for span in spans)
    output_points = output.weight * sum(span.spends[1] for span in spans)
    return output_points / (input_points + output_points)


def render_codex_doctor(diagnosis: CodexDiagnosis) -> str:
    """The text of `cimrihook doctor --agent codex`."""
    total = sum(band.units for band in diagnosis.bands)
    input_total = diagnosis.uncached + diagnosis.cached
    lines = [
        f"CimriHook doctor (Codex, last {diagnosis.days} days): {diagnosis.sessions} rollouts, "
        f"{diagnosis.requests:,} requests, {tokens_text(round(total))} base input units "
        "(uncached input 1, cached 0.1, output 6: an order of the spend, not a bill)",
        "Units by the context size of the request (every request re-sends the conversation):",
        *(
            f"  {band.label:<12} {percent(band.units, total):>4} {band.requests:>9,} requests"
            for band in diagnosis.bands
        ),
        f"Input {tokens_text(input_total)} tokens, {percent(diagnosis.cached, input_total)} "
        f"served from the cache; output {tokens_text(diagnosis.output)} tokens, of which "
        f"reasoning {percent(diagnosis.reasoning, diagnosis.output)}",
        compaction_line(diagnosis),
        "What a point of your Codex windows costs (fitted from the readings Codex records):",
        *(f"  {line}" for line in window_lines(diagnosis.windows)),
    ]
    return "\n".join(lines)


def compaction_line(diagnosis: CodexDiagnosis) -> str:
    """The compactions and the threshold that set them."""
    threshold = (
        f"model_auto_compact_token_limit {diagnosis.threshold:,}"
        if diagnosis.threshold is not None
        else "no model_auto_compact_token_limit in config.toml (Codex's own default)"
    )
    window = (
        f"; model window {tokens_text(diagnosis.context_window)}"
        if diagnosis.context_window
        else ""
    )
    trigger = (
        f", median trigger {tokens_text(diagnosis.median_trigger)}"
        if diagnosis.median_trigger is not None
        else ""
    )
    if diagnosis.threshold_scope != WHOLE_CONTEXT_SCOPE:
        return (
            f"Compactions: {diagnosis.compactions}{trigger}; {threshold}{window}, counted with "
            f"scope {diagnosis.threshold_scope} (after the stable prompt prefix). "
            "`cimrihook simulate --agent codex` replays the whole context, so its thresholds do "
            "not apply to this scope."
        )
    return (
        f"Compactions: {diagnosis.compactions}{trigger}; {threshold}{window}. "
        "`cimrihook simulate --agent codex` replays other thresholds."
    )


def window_lines(windows: Sequence[WindowCost]) -> list[str]:
    """One line per window, then how to read them."""
    return [
        *(window_line(window) for window in windows),
        "Other use of the account (Codex cloud tasks, other machines) also moves the windows; "
        "with such use a point looks cheaper than it is.",
    ]


def window_line(window: WindowCost) -> str:
    """What a point of one window costs in input and output tokens, with 95% intervals."""
    label = WINDOW_LABELS[window.kind]
    if window.input_weight is None or window.output_weight is None:
        return (
            f"{label}: needs at least {len(CLASSES) + MIN_EXTRA_SPANS} spans between whole-percent "
            f"crossings to estimate ({window.spans} so far)"
        )
    measured = f"{window.spans} spans, {window.points:.0f} points"
    if window.input_weight.low <= 0 or window.output_weight.low <= 0:
        pooled = (
            f"; at list ratios 1 point is about {per_point(window.pooled)} base input units"
            if window.pooled is not None and window.pooled.low > 0
            else ""
        )
        return (
            f"{label}: input and output cannot be told apart in these readings yet{pooled} "
            f"({measured})"
        )
    share = f"{window.output_share:.0%}" if window.output_share is not None else "-"
    ratio = window.output_weight.weight / window.input_weight.weight
    return (
        f"{label}: 1 point is about {per_point(window.input_weight)} input tokens "
        f"or {per_point(window.output_weight)} output tokens; an output token counts about "
        f"{ratio:.0f}x an uncached input token, and output moved {share} of the points "
        f"({measured})"
    )


def per_point(weight: ClassWeight) -> str:
    """Tokens per point with the 95% interval."""
    return (
        f"{tokens_text(round(1 / weight.weight))} "
        f"[95% {tokens_text(round(1 / weight.high))}-{tokens_text(round(1 / weight.low))}]"
    )


def render_codex_limits(windows: Sequence[WindowCost]) -> str:
    """The text of `cimrihook limits --agent codex`."""
    return "\n".join(
        ["CimriHook limits (Codex): what a point of your windows costs, from the rollouts"]
        + [f"  {line}" for line in window_lines(windows)]
    )


def point_cost(window: WindowCost) -> PointCost:
    """The tokens per point a report states: only weights whose 95% interval excludes zero."""
    separated = (
        window.input_weight is not None
        and window.output_weight is not None
        and window.input_weight.low > 0
        and window.output_weight.low > 0
    )
    return PointCost(
        kind=window.kind,
        spans=window.spans,
        points=window.points,
        input_tokens=1 / window.input_weight.weight
        if separated and window.input_weight is not None
        else None,
        output_tokens=1 / window.output_weight.weight
        if separated and window.output_weight is not None
        else None,
        output_share=window.output_share if separated else None,
        base_units=1 / window.pooled.weight
        if window.pooled is not None and window.pooled.low > 0
        else None,
    )


def codex_limits_json(windows: Sequence[WindowCost]) -> str:
    """`cimrihook limits --agent codex --json`, for the macOS menu bar panel."""
    return json.dumps([asdict(point_cost(window)) for window in windows], indent=2, allow_nan=False)
