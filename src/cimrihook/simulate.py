"""Context economics simulator: the effect of compaction policies on cost in recorded sessions.

Every API request re-reads the whole context so far, so most of the cost comes from the size of
the context. The simulator takes the real request sequence (the API usage data) from the agent
records and replays compaction policies over the same sequence. Claude Code transcripts and
Codex CLI rollout records are supported; the cost is computed in base input price units with the
price ratios of each session's model.

The cost of a compaction is measured from the real compactions in the records: the summarising
request reads the context once and produces the summary at the output price; the first request
after it carries the new context with the system prompt, tools, summary and re-attached files,
and only the part that is no longer cached is written again. The agent's behaviour is assumed not
to change otherwise; this assumption and the quality effect are tested with an A/B experiment
(`cimrihook bench-calibrate`).
"""

import statistics
from collections import Counter
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from cimrihook.claude import JsonObject
from cimrihook.errors import ConfigError, TranscriptError
from cimrihook.transcripts import (
    SECONDS_PER_DAY,
    Usage,
    average_write_weight,
    estimate_tokens,
    is_bench_location,
    message_usage,
    parse_line,
    recent_transcripts,
)

# Cache cold: the request read less than half of the previous context from the cache. A warm
# request that adds much new content does not count as cold (it reads the whole previous context).
COLD_READ_SHARE: Final = 0.5
# Content the agent reads again after a compaction: the re-reading that explains how far the
# simulator fell below the measured cost in A/B runs, as a median per compaction (0-13k).
MEASURED_REFETCH_TOKENS: Final = 7_000
MEASURED_REFETCH_REQUESTS: Final = 1
# The recommended window is the largest one within this many points of the lowest simulated cost.
# Every compaction reduces detail to a summary; fewer compactions are less risk for the same saving.
RECOMMENDATION_SLACK: Final = 0.01
CLAUDE_MIN_COMPACT_WINDOW: Final = 100_000  # lower bound of autoCompactWindow and the env var
CLAUDE_MAX_COMPACT_WINDOW: Final = 1_000_000  # upper bound of the autoCompactWindow setting
# Claude Code 2.1.288 triggers auto-compaction at a context of window − min(output limit, 20000) −
# 13000 tokens; the output limit of today's models is above 20000.
CLAUDE_COMPACT_OFFSET: Final = 33_000
SYNTHETIC_MODEL: Final = "<synthetic>"  # Claude Code's local messages that never reach the API


@dataclass(frozen=True, slots=True)
class PriceSheet:
    """The provider's price ratios (base input price = 1)."""

    name: str
    read: float
    write_5m: float
    write_1h: float
    uncached: float
    output: float


# Anthropic: cache read 0.1, write 1.25 (5 min) / 2.0 (1 hour), output 5x.
ANTHROPIC: Final = PriceSheet("Anthropic", 0.1, 1.25, 2.0, 1.0, 5.0)
# Opus 5.5 prices cache reads at 0.05, Fable 5.1 and Mythos 5.1 at 0.025; other Claude models
# (Opus 5 and Fable 5 included) at 0.1 (platform.claude.com/docs/en/about-claude/pricing, 2026-10).
CLAUDE_READ_WEIGHTS: Final = (
    ("claude-opus-5-5", 0.05),
    ("claude-fable-5-1", 0.025),
    ("claude-mythos-5-1", 0.025),
)
# OpenAI GPT-5.x/6.x: cached input is discounted 90%; 5.6 and later bill cache writes at 1.25x
# (older models report no writes); output about 6x (GPT-5.5: 6, GPT-5.6-sol: 5).
OPENAI: Final = PriceSheet("OpenAI", 0.1, 1.25, 1.25, 1.0, 6.0)


@dataclass(frozen=True, slots=True)
class SessionTrace:
    """Request sequence of one context window, its model and its real compactions."""

    requests: tuple[Usage, ...]
    model: str  # model that answered most requests; empty if unknown
    prices: PriceSheet  # the model's price ratios
    pre_compact_tokens: tuple[int, ...]  # context sizes that triggered a compaction
    post_compact_tokens: tuple[int, ...]  # context of the first request after a compaction
    post_compact_cached: tuple[int, ...]  # the part of that request read from the cache
    summary_tokens: tuple[int, ...]  # summary output tokens (Claude: estimated from its text)


@dataclass(frozen=True, slots=True)
class Policy:
    """Compaction policy."""

    name: str
    window: int | None  # compact once the context exceeds this size; None: observed behaviour
    cold_window: int | None  # compact first if the cache is cold and the context exceeds this size


@dataclass(frozen=True, slots=True)
class CostModel:
    """Simulation assumptions for the cost of a compaction (prices come from the session model)."""

    write_weight: float  # average multiplier of newly written input in the simulation
    post_compact_tokens: int  # context of the first request after a compaction
    post_compact_cached: int  # part of that context that stays cached (system prompt and tools)
    summary_tokens: int  # output tokens of the summary
    refetch_tokens: int  # content the agent reads again after a compaction
    refetch_requests: int  # extra requests for that re-reading


@dataclass(frozen=True, slots=True)
class CostOverrides:
    """Assumptions given by the user; None ones are measured from the compactions in the records."""

    post_compact_tokens: int | None
    post_compact_cached: int | None
    summary_tokens: int | None
    refetch_tokens: int
    refetch_requests: int
    read_weight: float | None  # changes the cache read multiplier of all models


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of a policy over all sessions."""

    policy: Policy
    cost: float
    compactions: int
    mean_context: float


@dataclass(frozen=True, slots=True)
class ModelShare:
    """The read multiplier of a model in the simulation and its number of sessions."""

    model: str
    read: float
    sessions: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Data of the simulation report."""

    agent: str
    sessions: int
    requests: int
    days: int
    unit: str  # USD or base input units
    unweighted_sessions: int  # sessions left out because their unit could not be converted
    exact_cost: float  # cost computed exactly from the usage records
    observed_compactions: int
    static_prefix: int  # median context of the sessions' first request (system prompt + tools)
    model: CostModel
    models: tuple[ModelShare, ...]
    outcomes: tuple[Outcome, ...]


type ApplyHint = Callable[[int], str]
type TraceWeight = Callable[[SessionTrace], float | None]  # unit conversion of the session cost

OBSERVED: Final = Policy("observed behaviour", None, None)
POLICIES: Final = (
    OBSERVED,
    Policy("compact above 400k", 400_000, None),
    Policy("compact above 300k", 300_000, None),
    Policy("compact above 200k", 200_000, None),
    Policy("compact above 150k", 150_000, None),
    Policy("compact above 100k", 100_000, None),
    Policy("cold-cache compact above 80k", None, 80_000),
    Policy("above 200k + cold above 80k", 200_000, 80_000),
)


def simulate_claude(
    logs_dir: Path, days: int, now: float, overrides: CostOverrides
) -> SimulationResult:
    """Runs the policies over Claude Code transcripts (subagents included)."""
    traces = load_claude_traces(recent_transcripts(logs_dir, days, now))
    requests = [usage for trace in traces for usage in trace.requests]
    return simulate_traces(
        "Claude Code",
        traces,
        days,
        average_write_weight(total_usage(requests)),
        overrides,
        dollar_weight,
    )


def simulate_codex(
    logs_dir: Path, days: int, now: float, overrides: CostOverrides
) -> SimulationResult:
    """Runs the policies over Codex CLI rollout records."""
    min_mtime = now - days * SECONDS_PER_DAY
    files = sorted(
        path
        for path in logs_dir.rglob("rollout-*.jsonl")
        if path.stat().st_mtime >= min_mtime and not is_bench_location(rollout_cwd(path) or "")
    )
    if not files:
        raise ConfigError(f"no Codex rollouts under {logs_dir} modified in the last {days} days")
    return simulate_traces(
        "Codex CLI",
        [load_codex_trace(path) for path in files],
        days,
        OPENAI.uncached,
        overrides,
        base_unit_weight,
    )


def simulate_traces(
    agent: str,
    traces: Sequence[SessionTrace],
    days: int,
    write_weight: float,
    overrides: CostOverrides,
    weight: TraceWeight,
) -> SimulationResult:
    """Provider-independent core: replays all policies over the same request sequences.

    The cost of each session is computed in base input units with its own model's price ratios and
    converted to the common unit (Claude: USD) with `weight`, so a token of an expensive model
    weighs more than one of a cheap model. Sessions whose unit cannot be converted stay out of the
    simulation and are counted.
    """
    active = [trace for trace in traces if trace.requests]
    weighted = [(trace, w) for trace in active if (w := weight(trace)) is not None]
    used = [trace for trace, _ in weighted]
    model = CostModel(
        write_weight=write_weight,
        post_compact_tokens=observed_median(
            [tokens for trace in used for tokens in trace.post_compact_tokens],
            overrides.post_compact_tokens,
            "--post-compact-tokens",
        ),
        post_compact_cached=observed_median(
            [tokens for trace in used for tokens in trace.post_compact_cached],
            overrides.post_compact_cached,
            "--post-compact-cached",
        ),
        summary_tokens=observed_median(
            [tokens for trace in used for tokens in trace.summary_tokens],
            overrides.summary_tokens,
            "--summary-tokens",
        ),
        refetch_tokens=overrides.refetch_tokens,
        refetch_requests=overrides.refetch_requests,
    )
    shares = Counter(
        (trace.model, trace_prices(trace, overrides.read_weight).read) for trace in used
    )
    return SimulationResult(
        agent=agent,
        sessions=len(used),
        requests=sum(len(trace.requests) for trace in used),
        days=days,
        unit="USD" if weight is dollar_weight else "base input units",
        unweighted_sessions=len(active) - len(used),
        exact_cost=sum(
            w * exact_cost(usage, trace_prices(trace, overrides.read_weight))
            for trace, w in weighted
            for usage in trace.requests
        ),
        observed_compactions=sum(len(trace.pre_compact_tokens) for trace in used),
        static_prefix=int(statistics.median(context_of(trace.requests[0]) for trace in used)),
        model=model,
        models=tuple(ModelShare(name, read, count) for (name, read), count in shares.most_common()),
        outcomes=tuple(
            run_policy(weighted, policy, model, overrides.read_weight) for policy in POLICIES
        ),
    )


def observed_median(observed: Sequence[int], override: int | None, flag: str) -> int:
    """The user's value if given, else the median of the real compactions in the records."""
    if override is not None:
        return override
    if not observed:
        raise ConfigError(f"no real compactions found in the logs; pass {flag} explicitly")
    return int(statistics.median(observed))


# API list prices: base input, USD per million tokens (2026-10, platform.claude.com prices).
# Cache and output multipliers come from claude_prices; models not in the list are not priced and
# are counted separately in the report.
USD_PER_MTOK: Final = (
    ("claude-fable", 10.0),
    ("claude-mythos", 10.0),
    ("claude-opus-5-5", 4.0),
    ("claude-opus-5", 5.0),
    ("claude-opus-4-1", 15.0),
    ("claude-opus-4-2025", 15.0),  # Opus 4 (claude-opus-4-20250514)
    ("claude-opus-4", 5.0),
    ("claude-sonnet-5", 2.0),
    ("claude-sonnet-4", 3.0),
    ("claude-haiku-4-5", 1.0),
)


def usd_per_token(model: str) -> float | None:
    """A model's base input list price (USD per token); None for a model not in the list."""
    for marker, usd in USD_PER_MTOK:
        if marker in model.lower():
            return usd / 1e6
    return None


def dollar_weight(trace: SessionTrace) -> float | None:
    """USD per base input unit of a session; None for a model not in the list (it cannot be weighed
    in dollars, stays out of the simulation and is counted in the report)."""
    return usd_per_token(trace.model)


def base_unit_weight(trace: SessionTrace) -> float | None:
    """A provider without a price list (Codex): base input units, every session weighted equally."""
    return 1.0


def claude_prices(model: str) -> PriceSheet:
    """Anthropic price ratios with a Claude model's cache read multiplier."""
    for marker, read in CLAUDE_READ_WEIGHTS:
        if marker in model.lower():
            return replace(ANTHROPIC, read=read)
    return ANTHROPIC


def trace_prices(trace: SessionTrace, read_weight: float | None) -> PriceSheet:
    """The session's price ratios; with the read multiplier if one is given."""
    return trace.prices if read_weight is None else replace(trace.prices, read=read_weight)


def load_claude_traces(paths: Sequence[Path]) -> list[SessionTrace]:
    """Transcripts in the given order; messages (same message id) that a forked or resumed session
    copied from the earlier file count only in the file where they were first seen."""
    seen: set[str] = set()
    traces: list[SessionTrace] = []
    for path in paths:
        trace, ids = read_claude_trace(path, frozenset(seen))
        traces.append(trace)
        seen.update(ids)
    return traces


def load_claude_trace(path: Path) -> SessionTrace:
    """One Claude Code transcript, not deduplicated against other files."""
    return read_claude_trace(path, frozenset())[0]


def read_claude_trace(path: Path, skip: Set[str]) -> tuple[SessionTrace, frozenset[str]]:
    """A Claude Code transcript: the last usage per message id, the model and the real compactions;
    message ids in `skip` are skipped. Returns the file's message ids with them.

    The context after a compaction is not the postTokens of compact_boundary but the context of the
    first real request after it: postTokens leaves out the system prompt, tools and re-attached
    files. The summary's output tokens are estimated from the summary text (the summarising request
    is not written to the transcript; with thinking tokens the real output is larger).
    """
    order: list[str] = []
    usages: dict[str, Usage] = {}
    models: Counter[str] = Counter()
    pre: list[int] = []
    summaries: list[int] = []
    after: list[str] = []  # id of the first real request after each compaction
    ids: set[str] = set()  # ids of all real requests in the file (skipped ones included)
    awaiting = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                pre.append(compact_metadata_tokens(entry, "preTokens", str(path)))
                awaiting = True
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if entry.get("isCompactSummary") is True:
                summaries.append(estimate_tokens(content_text(message.get("content"))))
                continue
            message_id = message.get("id")
            model = message.get("model")
            usage = message_usage(message)
            if (
                entry.get("type") != "assistant"
                or not isinstance(message_id, str)
                or not isinstance(model, str)
                or model == SYNTHETIC_MODEL  # local message, never sent to the API; context 0
                or usage is None
                or not is_api_usage(usage)
            ):
                continue
            ids.add(message_id)
            if message_id in skip:
                continue
            if message_id not in usages:
                order.append(message_id)
                models[model] += 1
                if awaiting and context_of(usage) > 0:
                    after.append(message_id)
                    awaiting = False
            usages[message_id] = usage  # the last line of the same id is the valid one
    name = models.most_common(1)[0][0] if models else ""
    first_after = [usages[message_id] for message_id in after]
    trace = SessionTrace(
        requests=tuple(usages[message_id] for message_id in order),
        model=name,
        prices=claude_prices(name),
        pre_compact_tokens=tuple(pre),
        post_compact_tokens=tuple(context_of(usage) for usage in first_after),
        post_compact_cached=tuple(usage.read for usage in first_after),
        summary_tokens=tuple(summaries),
    )
    return trace, frozenset(ids)


def content_text(content: object) -> str:
    """The text of a message's content (text blocks joined)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def rollout_cwd(path: Path) -> str | None:
    """Working directory of a Codex rollout (session_meta); None without a record. It leaves out
    CimriHook's own A/B runs."""
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            payload = None if entry is None else entry.get("payload")
            if (
                entry is not None
                and entry.get("type") == "session_meta"
                and isinstance(payload, dict)
            ):
                cwd = payload.get("cwd")
                return cwd if isinstance(cwd, str) else None
    return None


def load_codex_trace(path: Path) -> SessionTrace:
    """The request sequence (token_count) and the compactions (compacted) of a Codex rollout record.

    The size that triggered a compaction is the context of the last request before the compacted
    record, the size after it that of the first request after it. token_count events do not include
    the compaction request itself; the summary's output tokens are read from that request's
    token_usage_record.
    """
    requests: list[Usage] = []
    post: list[Usage] = []
    pre: list[int] = []
    models: Counter[str] = Counter()
    outputs: dict[str, int] = {}  # response id -> output tokens (token_usage_record)
    compaction_ids: list[str] = []
    last_total: int | None = None
    after_compaction = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            payload = entry.get("payload")
            kind = entry.get("type")
            if kind == "turn_context" and isinstance(payload, dict):
                if isinstance(payload.get("model"), str):
                    models[str(payload["model"])] += 1
                continue
            if kind == "token_usage_record" and isinstance(payload, dict):
                output = record_output(payload)
                if output is not None:
                    outputs[output[0]] = output[1]
                continue
            if kind == "compacted":
                if isinstance(payload, dict) and isinstance(
                    payload.get("compaction_response_id"), str
                ):
                    compaction_ids.append(str(payload["compaction_response_id"]))
                if requests:
                    pre.append(context_of(requests[-1]))
                after_compaction = True
                continue
            request = codex_request(entry)
            if request is None or request[1] == last_total:
                continue  # same cumulative total: no new request
            usage, last_total = request
            if after_compaction:
                post.append(usage)
                after_compaction = False
            requests.append(usage)
    return SessionTrace(
        requests=tuple(requests),
        model=models.most_common(1)[0][0] if models else "",
        prices=OPENAI,
        pre_compact_tokens=tuple(pre),
        post_compact_tokens=tuple(context_of(usage) for usage in post),
        post_compact_cached=tuple(usage.read for usage in post),
        summary_tokens=tuple(outputs[key] for key in compaction_ids if key in outputs),
    )


def record_output(payload: JsonObject) -> tuple[str, int] | None:
    """The (response id, output tokens) pair of a token_usage_record; None without the fields."""
    response_id = payload.get("response_id")
    usage = payload.get("usage")
    if not isinstance(response_id, str) or not isinstance(usage, dict):
        return None
    output = usage.get("output_tokens")
    return (response_id, output) if isinstance(output, int) else None


def codex_request(entry: JsonObject) -> tuple[Usage, int] | None:
    """From a token_count event (the last request's usage, the cumulative total)."""
    payload = entry.get("payload")
    if entry.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if payload.get("type") != "token_count" or not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    if not isinstance(last, dict) or not isinstance(total, dict):
        return None
    tokens = last.get("input_tokens")
    cached = last.get("cached_input_tokens")
    written = last.get("cache_write_input_tokens")
    output = last.get("output_tokens")
    cumulative = total.get("total_tokens")
    if not (
        isinstance(tokens, int)
        and isinstance(cached, int)
        and isinstance(output, int)
        and isinstance(cumulative, int)
    ):
        return None
    # input_tokens includes the cache tokens read and written; older records have no write field.
    write = written if isinstance(written, int) else 0
    usage = Usage(
        uncached=tokens - cached - write, write_5m=write, write_1h=0, read=cached, output=output
    )
    return usage, cumulative


def compact_metadata_tokens(entry: JsonObject, key: str, where: str) -> int:
    """Context size on a compact_boundary line (preTokens: the trigger, postTokens: the one after).

    If the value is missing the compaction would silently go uncounted; that is an unknown record
    format: an error.
    """
    metadata = entry.get("compactMetadata")
    tokens = metadata.get(key) if isinstance(metadata, dict) else None
    if not isinstance(tokens, int) or tokens <= 0:
        raise TranscriptError(
            f"{where}: compact_boundary without a positive compactMetadata.{key} ({metadata!r})"
        )
    return tokens


def total_usage(requests: Sequence[Usage]) -> Usage:
    """The total usage of the requests."""
    return Usage(
        uncached=sum(usage.uncached for usage in requests),
        write_5m=sum(usage.write_5m for usage in requests),
        write_1h=sum(usage.write_1h for usage in requests),
        read=sum(usage.read for usage in requests),
        output=sum(usage.output for usage in requests),
    )


def context_of(usage: Usage) -> int:
    """The total input sent to the model in a request."""
    return usage.uncached + usage.write_5m + usage.write_1h + usage.read


def is_api_usage(usage: Usage) -> bool:
    """Is the usage from an API response? Lines rewritten into the transcript after a plugin's
    compaction carry the same message id with zero usage; they do not count as requests."""
    return context_of(usage) + usage.output > 0


def written_of(usage: Usage) -> int:
    """Input of a request that was not read from the cache (newly written or uncached)."""
    return usage.uncached + usage.write_5m + usage.write_1h


def is_cold(usage: Usage, previous_context: int) -> bool:
    """Has the cache gone cold: did the request read under half of the previous context from cache?

    A session's first request has no previous context; it writes everything anyway.
    """
    return previous_context > 0 and usage.read < COLD_READ_SHARE * previous_context


def exact_cost(usage: Usage, prices: PriceSheet) -> float:
    """Exact cost from the usage record (in base input price units)."""
    return (
        usage.uncached * prices.uncached
        + usage.write_5m * prices.write_5m
        + usage.write_1h * prices.write_1h
        + usage.read * prices.read
        + usage.output * prices.output
    )


def should_compact(policy: Policy, context: int, cold: bool, model: CostModel) -> bool:
    """Does the policy want a compaction before this request?"""
    if context <= model.post_compact_tokens + model.refetch_tokens:
        return False  # a compaction would not shrink the context
    if policy.window is not None and context > policy.window:
        return True
    return cold and policy.cold_window is not None and context > policy.cold_window


def simulate_trace(
    trace: SessionTrace, policy: Policy, model: CostModel, prices: PriceSheet
) -> tuple[float, int, int]:
    """Replays a session with a policy: (cost, number of compactions, sum of contexts).

    Newly written input: the whole context if the cache is cold, the part beyond the prefix that
    stays cached after a simulated compaction, and for other requests as much as the real request
    wrote.
    """
    cost = 0.0
    compactions = 0
    context_sum = 0
    simulated = 0
    previous_actual = 0
    for index, usage in enumerate(trace.requests):
        actual = context_of(usage)
        growth = actual - previous_actual
        cold = is_cold(usage, previous_actual)
        previous_actual = actual
        if index == 0:
            simulated = actual
        elif growth < 0:
            simulated = min(simulated, actual)  # the real session shrank here too
        else:
            simulated += growth
        written = min(written_of(usage), simulated)
        if should_compact(policy, simulated, cold, model):
            read_weight = prices.uncached if cold else prices.read
            cost += simulated * read_weight + model.summary_tokens * prices.output
            simulated = model.post_compact_tokens + model.refetch_tokens
            cost += model.refetch_requests * simulated * prices.read
            compactions += 1
            written = simulated - min(model.post_compact_cached, simulated)
        if cold:
            written = simulated
        cost += (
            written * model.write_weight
            + (simulated - written) * prices.read
            + usage.output * prices.output
        )
        context_sum += simulated
    return cost, compactions, context_sum


def run_policy(
    weighted: Sequence[tuple[SessionTrace, float]],
    policy: Policy,
    model: CostModel,
    read_weight: float | None,
) -> Outcome:
    """Runs the policy in each session with its own prices and sums in the common unit."""
    results = [
        (w, simulate_trace(trace, policy, model, trace_prices(trace, read_weight)))
        for trace, w in weighted
    ]
    requests = sum(len(trace.requests) for trace, _ in weighted)
    return Outcome(
        policy=policy,
        cost=sum(w * cost for w, (cost, _, _) in results),
        compactions=sum(count for _, (_, count, _) in results),
        mean_context=sum(total for _, (_, _, total) in results) / requests if requests else 0.0,
    )


def claude_hint(window: int) -> str:
    """Claude Code setting: compaction triggers at window − 33000 of context; lower bound 100000.

    So compaction before 67000 tokens is not possible with the documented setting.
    """
    setting = max(window + CLAUDE_COMPACT_OFFSET, CLAUDE_MIN_COMPACT_WINDOW)
    return f"`cimrihook init --compact-window {setting}`"


def codex_hint(window: int) -> str:
    """Codex applies the compaction threshold to the total context."""
    return f"`cimrihook init --agent codex --compact-window {window}`"


def render_simulation(result: SimulationResult, hint: ApplyHint) -> str:
    """The text of the simulation report."""
    model = result.model
    baseline = result.outcomes[0].cost
    calibration = 100 * (baseline - result.exact_cost) / result.exact_cost
    prices = ", ".join(
        f"{share.model or 'unknown'} read {share.read} ({share.sessions} sessions)"
        for share in result.models
    )
    written = (
        "observed cache-write mix"
        if result.agent == "Claude Code"
        else "uncached input; Codex does not price cache writes"
    )
    lines = [
        f"CimriHook simulate ({result.agent}): {result.sessions} sessions active in the last "
        f"{result.days} days (replayed whole), {result.requests:,} requests; static prefix "
        f"(median first request) {result.static_prefix:,} tokens",
        f"Prices (base input = 1, per session model; sessions are summed in {result.unit}"
        + (
            f", {result.unweighted_sessions} sessions of models without a list price left out"
            if result.unweighted_sessions
            else ""
        )
        + f"): {prices}; new input written at {model.write_weight:.2f} ({written})",
        f"Compaction (medians of {result.observed_compactions} real compactions unless given): "
        f"summary {model.summary_tokens:,} output tokens (estimated from the summary text; the "
        f"real output is larger), next request carries {model.post_compact_tokens:,} context "
        f"tokens of which {model.post_compact_cached:,} stay cached, + "
        f"{model.refetch_tokens:,} re-read in {model.refetch_requests} extra requests",
        f"Exact cost from usage logs: {amount(result.exact_cost, result.unit)}; model replay of "
        f"observed "
        f"behaviour: {amount(baseline, result.unit)} (replay check {calibration:+.1f}%; this "
        "checks the "
        "cost accounting, not the policy predictions: see `cimrihook bench-calibrate`)",
        f"  {'policy':<30}{cost_header(result.unit):>10}{'vs observed':>13}{'compactions':>13}"
        f"{'mean context':>14}",
    ]
    lines.extend(
        f"  {outcome.policy.name:<30}{amount(outcome.cost, result.unit):>10}"
        f"{100 * (outcome.cost - baseline) / baseline:>12.1f}%{outcome.compactions:>13,}"
        f"{outcome.mean_context:>14,.0f}"
        for outcome in result.outcomes
    )
    return "\n".join([*lines, *recommendation(result, hint)])


def amount(cost: float, unit: str) -> str:
    """Short text of the cost in its unit: dollars or billions of base input units."""
    return f"${cost:,.0f}" if unit == "USD" else f"{cost / 1e9:.3f}B"


def cost_header(unit: str) -> str:
    """The cost column header of the policy table."""
    return "cost ($)" if unit == "USD" else "cost (B)"


def recommended_window(result: SimulationResult) -> tuple[Outcome, Outcome] | None:
    """The (recommended, cheapest) window policy; None if no window lowers the cost.

    The recommended one is the largest window within RECOMMENDATION_SLACK points of the cheapest
    cost. Policies with a cold-cache condition are not candidates because no setting can apply them.
    """
    baseline = result.outcomes[0].cost
    windows = [
        outcome
        for outcome in result.outcomes
        if outcome.policy.window is not None and outcome.policy.cold_window is None
    ]
    if not windows:
        return None
    cheapest = min(windows, key=lambda outcome: outcome.cost)
    if cheapest.cost >= baseline:
        return None
    near = [o for o in windows if o.cost <= cheapest.cost + RECOMMENDATION_SLACK * baseline]
    return max(near, key=lambda outcome: outcome.policy.window or 0), cheapest


def recommendation_text(chosen: Outcome, cheapest: Outcome, baseline: float) -> str:
    """The effect of the recommended window; if it differs from the cheapest, why it was chosen."""
    effect = (
        f"{100 * (chosen.cost - baseline) / baseline:+.1f}%, {chosen.compactions:,} compactions"
    )
    if chosen is cheapest:
        return f"'{chosen.policy.name}' ({effect})"
    return (
        f"'{chosen.policy.name}' ({effect}); the cheapest, '{cheapest.policy.name}' "
        f"({100 * (cheapest.cost - baseline) / baseline:+.1f}%, {cheapest.compactions:,} "
        f"compactions), saves at most {100 * RECOMMENDATION_SLACK:.0f} point more with more "
        "compactions, and each compaction loses detail"
    )


def recommendation(result: SimulationResult, hint: ApplyHint) -> list[str]:
    """The recommended window policy and how to apply it."""
    found = recommended_window(result)
    if found is None:
        return ["No compaction window lowers the simulated cost of these logs; keep the default."]
    chosen, cheapest = found
    window = chosen.policy.window
    if window is None:
        raise ValueError(f"recommended policy {chosen.policy.name!r} has no window")
    return [
        f"Recommended window: {recommendation_text(chosen, cheapest, result.outcomes[0].cost)}. "
        f"Apply it with {hint(window)} and confirm task quality with an A/B run first.",
    ]
