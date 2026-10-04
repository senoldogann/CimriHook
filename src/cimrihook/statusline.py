"""Status line: the session's context economics and usage limit, one line per update.

Claude Code gives the status line command the session's state as JSON: the model, the number of
tokens in the context (the last request's input, cache read and cache write together), the
session cost, the 5-hour and 7-day usage limits on a subscription, and its own cache tracking
(prompt_cache: whether it is warm, its lifetime, when it goes cold, how many tokens would be
rewritten then). Claude Code also re-runs the line the moment the cache goes cold. The estimated
cost of the next request is a read of the context if the cache is warm and a rewrite if it is
cold. Usage limit observations are written to the ledger to learn how the plan counts token types.

If the user had a previous status line command, it runs first and every line of its output is
kept. Claude Code blanks the line entirely on a non-zero exit, so CimriHook's own error is shown
inside the line: the user's line is not lost and the error does not go unseen.
"""

import contextlib
import json
import math
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Final

from cimrihook.claude import as_object, require_str
from cimrihook.config import Config
from cimrihook.errors import CimriHookError, HookPayloadError
from cimrihook.ledger import ledger_path, record_new_quota
from cimrihook.model import QuotaSample
from cimrihook.simulate import claude_prices, usd_per_token

LIMIT_LABELS: Final = (("five_hour", "5h"), ("seven_day", "7d"))
CACHE_TTLS: Final = {"5m": 300.0, "1h": 3_600.0}  # prompt_cache.ttl values, seconds
SEPARATOR: Final = " · "
CHAIN_TIMEOUT_SECONDS: Final = 5.0  # time allowed to the user's previous status line command
STATUS_BUSY_TIMEOUT_SECONDS: Final = 0.25  # the status line does not wait long for the ledger lock
ERROR_CHARS: Final = 120  # maximum length of the error text shown in the line
# Payback of a compaction: the context of the first request after it and the summary's output
# (medians of the author's last week; `cimrihook doctor` shows your own values).
POST_COMPACT_TOKENS: Final = 56_000
SUMMARY_OUTPUT_TOKENS: Final = 7_000
PAYBACK_MIN_CONTEXT: Final = 150_000  # not shown for a smaller context
PAYBACK_MAX_REQUESTS: Final = 50  # a longer payback is not shown


@dataclass(frozen=True, slots=True)
class LimitUse:
    """How full a usage limit window is."""

    window: str  # five_hour or seven_day
    used_percentage: float
    resets_at: int  # epoch seconds


@dataclass(frozen=True, slots=True)
class PromptCache:
    """The cache state Claude Code keeps for the session."""

    warm: bool
    ttl_seconds: float
    expires_at: float | None  # epoch seconds
    recache_tokens: int | None  # tokens the next request rewrites if the cache is cold


@dataclass(frozen=True, slots=True)
class StatusInput:
    """The fields used from the status line command's input."""

    session_id: str
    model: str
    context_tokens: int | None  # context tokens; None before the first response and after /compact
    session_usd: float | None
    limits: tuple[LimitUse, ...]
    cache: PromptCache | None  # Claude Code does not give it before the first request


def run_chained_statusline(raw: str, config: Config, now: float, previous_command: str) -> str:
    """Runs the user's previous status line with the same input and adds CimriHook's.

    The previous command (for example another tool's bridge) may use the input itself; it is passed
    the input unchanged.
    """
    return joined(previous_status(raw, previous_command), status_or_error(raw, config, now))


def previous_status(raw: str, previous_command: str) -> str:
    """Output of the previous status line command; an exit code or timeout is added to the text.

    The command runs in its own process group; on timeout everything it started is terminated.
    """
    with subprocess.Popen(
        previous_command,
        shell=True,  # the user's own command from the settings, as Claude Code runs it
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate(raw.encode("utf-8"), timeout=CHAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):  # the group may have ended by itself
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return f"(previous status line timed out after {CHAIN_TIMEOUT_SECONDS:.0f}s)"
    text = stdout.decode("utf-8", errors="replace").rstrip()
    if process.returncode != 0:
        return f"{text} (previous status line exited {process.returncode})".strip()
    return text


def joined(previous: str, ours: str) -> str:
    """The previous output's lines are kept; CimriHook's part is appended to the last line."""
    lines = previous.split("\n") if previous else []
    if not lines:
        return ours
    last = SEPARATOR.join(part for part in (lines[-1], ours) if part)
    return "\n".join([*lines[:-1], last])


def status_or_error(raw: str, config: Config, now: float) -> str:
    """CimriHook's status line; its own error is shown in the line as a short message."""
    try:
        return run_statusline(raw, config, now)
    except CimriHookError as error:
        return f"cimrihook: {str(error)[:ERROR_CHARS]}"


def run_statusline(raw: str, config: Config, now: float) -> str:
    """Produces the status line and writes new usage limit observations to the ledger."""
    status = parse_status_input(raw)
    samples = quota_samples(status, now)
    if samples:
        record_new_quota(ledger_path(config.home), samples, STATUS_BUSY_TIMEOUT_SECONDS)
    return render_status(status, now)


def parse_status_input(raw: str) -> StatusInput:
    """Parses the status line input from stdin; optional fields that are absent are None."""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookPayloadError(
            f"statusline input is not valid JSON ({error.msg}); first bytes: {raw[:120]!r}"
        ) from error
    payload = as_object(decoded, "statusline")
    model = as_object(payload.get("model"), "statusline.model")
    limits = payload.get("rate_limits")
    context = optional_integer(payload.get("context_window"), "total_input_tokens")
    return StatusInput(
        session_id=require_str(payload, "session_id", "statusline"),
        model=require_str(model, "id", "statusline.model"),
        context_tokens=context if context else None,  # 0: no response yet or just compacted
        session_usd=optional_number(payload.get("cost"), "total_cost_usd"),
        limits=tuple(
            use for window, _ in LIMIT_LABELS if (use := limit_use(limits, window)) is not None
        ),
        cache=prompt_cache(payload.get("prompt_cache")),
    )


def prompt_cache(value: object) -> PromptCache | None:
    """The prompt_cache of the input; Claude Code omits it before the first request."""
    if value is None:
        return None
    cache = as_object(value, "statusline.prompt_cache")
    warm = cache.get("warm")
    ttl = cache.get("ttl")
    if not isinstance(warm, bool) or not isinstance(ttl, str) or ttl not in CACHE_TTLS:
        raise HookPayloadError(f"statusline.prompt_cache: unexpected warm {warm!r} or ttl {ttl!r}")
    return PromptCache(
        warm=warm,
        ttl_seconds=CACHE_TTLS[ttl],
        expires_at=optional_number(cache, "expires_at"),
        recache_tokens=optional_integer(cache, "recache_tokens_if_cold"),
    )


def optional_integer(container: object, key: str) -> int | None:
    """An integer field of the dict; None if the dict or the field is missing."""
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def optional_number(container: object, key: str) -> float | None:
    """A numeric field of the dict; None if the dict or the field is missing."""
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def limit_use(limits: object, window: str) -> LimitUse | None:
    """A window in rate_limits; None outside a subscription or when the window has ended."""
    entry = limits.get(window) if isinstance(limits, dict) else None
    used = optional_number(entry, "used_percentage")
    resets_at = optional_integer(entry, "resets_at")
    if used is None or resets_at is None:
        return None
    return LimitUse(window=window, used_percentage=used, resets_at=resets_at)


def quota_samples(status: StatusInput, now: float) -> tuple[QuotaSample, ...]:
    """Ledger record of the usage limit fill levels in the input."""
    return tuple(
        QuotaSample(
            window=use.window,
            resets_at=use.resets_at,
            used_percentage=use.used_percentage,
            taken_at=now,
            session_id=status.session_id,
            model=status.model,
        )
        for use in status.limits
    )


def render_status(status: StatusInput, now: float) -> str:
    """The text of the status line; unknown parts are skipped."""
    context = status.context_tokens
    parts = [] if context is None else [f"{compact_tokens(context)} ctx"]
    if status.cache is not None:
        parts.extend(cache_parts(status.model, context, status.cache, now))
    labels = dict(LIMIT_LABELS)
    parts.extend(f"{labels[use.window]} {use.used_percentage:.0f}%" for use in status.limits)
    if status.session_usd is not None:
        parts.append(f"${status.session_usd:.2f}")
    return SEPARATOR.join(parts)


def cache_parts(model: str, context: int | None, cache: PromptCache, now: float) -> list[str]:
    """Cache warmth and the context cost of the next request (if the price is known).

    With a warm cache the context is read; with a cold cache the tokens Claude Code estimates are
    written again (the write price depends on the cache's lifetime).
    """
    prices = claude_prices(model)
    base = usd_per_token(model)
    left = None if cache.expires_at is None else cache.expires_at - now
    if cache.warm and left is not None and left > 0:
        state = f"cache warm {duration(left)}"
        tokens = context
        weight = prices.read
    else:
        state = "cache cold"
        tokens = cache.recache_tokens
        weight = prices.write_1h if cache.ttl_seconds >= CACHE_TTLS["1h"] else prices.write_5m
    if base is None or tokens is None:
        return [state]
    payback = payback_requests(model, context, cache) if state != "cache cold" else None
    return [
        state,
        f"next ${tokens * weight * base:.2f}",
        *([] if payback is None else [f"compact pays back in {payback} requests"]),
    ]


def payback_requests(model: str, context: int | None, cache: PromptCache) -> int | None:
    """Requests until a /compact now pays for itself; None for a small context or a long payback.

    Cost: the summary request reads the context once and writes the summary at the output price,
    and the context after the compaction is written to the cache again. Gain: every later request
    reads the smaller context.
    """
    if context is None or context < PAYBACK_MIN_CONTEXT:
        return None
    prices = claude_prices(model)
    write = prices.write_1h if cache.ttl_seconds >= CACHE_TTLS["1h"] else prices.write_5m
    cost = (
        context * prices.read + SUMMARY_OUTPUT_TOKENS * prices.output + POST_COMPACT_TOKENS * write
    )
    saving = (context - POST_COMPACT_TOKENS) * prices.read
    requests = math.ceil(cost / saving)
    return requests if requests <= PAYBACK_MAX_REQUESTS else None


def compact_tokens(tokens: int) -> str:
    """Short form of a token count (for example 412k, 1.05M)."""
    return f"{tokens / 1000:.0f}k" if tokens < 1_000_000 else f"{tokens / 1e6:.2f}M"


def duration(seconds: float) -> str:
    """Short form of a duration (for example 38m, 45s)."""
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"
