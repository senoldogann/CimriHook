"""Cache lifetime advisor: 5-minute and 1-hour caches under the user's own pauses.

Claude Code writes the cache with a 1-hour lifetime for the main conversation on a subscription
and a 5-minute lifetime with an API key and for subagents; `promptCacheTtl` and
`subagentPromptCacheTtl` change that. A 1-hour write costs 2x and a 5-minute write 1.25x, but if
the gap between two requests outlasts the lifetime, the next request writes the whole context
again. Which one is cheaper depends on the pauses and on the size of the context.

Each request is re-priced for both lifetimes: the context the previous request put into the cache
(its input and its response) is read if the gap did not outlast the lifetime and written again if
it did; the rest of the input is written. The same replay with the observed lifetime gives the
recorded cost; the difference is the method's error margin, because other cache breaks, such as a
tool or model change, are not visible here. A recommendation is made only when the other lifetime
is cheaper by more than this error margin.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cimrihook.scan import Request, TranscriptScan, token_costs
from cimrihook.simulate import claude_prices, context_of, usd_per_token

FIVE_MINUTES: Final = "5m"
ONE_HOUR: Final = "1h"
LIFETIME_SECONDS: Final = {FIVE_MINUTES: 300.0, ONE_HOUR: 3_600.0}
MAIN: Final = "main sessions"
SUBAGENTS: Final = "subagents"
# Smallest difference a recommendation must clear: above the replay's error at the observed
# lifetime and at least this much.
MIN_GAIN: Final = 0.05


@dataclass(frozen=True, slots=True)
class LifetimeReplay:
    """Cache input cost of a group of requests: as recorded and replayed with both lifetimes."""

    bucket: str  # main sessions or subagents
    requests: int
    current: str | None  # dominant lifetime in the records; None if there is no cache write
    observed_usd: float  # read, write and uncached input (output is the same for both lifetimes)
    five_minutes_usd: float
    one_hour_usd: float

    def replayed(self, lifetime: str) -> float:
        """Cost replayed with the given lifetime."""
        return self.five_minutes_usd if lifetime == FIVE_MINUTES else self.one_hour_usd

    def replay_error(self) -> float | None:
        """Relative difference of the replay with the observed lifetime from the recorded cost."""
        if self.current is None or self.observed_usd <= 0:
            return None
        return (self.replayed(self.current) - self.observed_usd) / self.observed_usd


def replay_lifetimes(scans: Sequence[TranscriptScan]) -> tuple[LifetimeReplay, LifetimeReplay]:
    """Re-pricing with both lifetimes for the main sessions and the subagents."""
    return (
        bucket_replay(MAIN, [scan for scan in scans if not is_subagent(scan)]),
        bucket_replay(SUBAGENTS, [scan for scan in scans if is_subagent(scan)]),
    )


def is_subagent(scan: TranscriptScan) -> bool:
    """Is the transcript a subagent's (its requests are marked as a subagent)?"""
    return any(request.subagent for request in scan.requests)


def bucket_replay(bucket: str, scans: Sequence[TranscriptScan]) -> LifetimeReplay:
    """Re-prices every transcript of a group in order and sums them."""
    priced = [
        (request, cached, base)
        for scan in scans
        for request, cached, base in priced_requests(scan.requests)
    ]
    five_written = sum(request.usage.write_5m for request, _, _ in priced)
    hour_written = sum(request.usage.write_1h for request, _, _ in priced)
    current = (
        None
        if five_written + hour_written == 0
        else (ONE_HOUR if hour_written > five_written else FIVE_MINUTES)
    )
    return LifetimeReplay(
        bucket=bucket,
        requests=len(priced),
        current=current,
        observed_usd=sum(sum(token_costs(request, base)[:3]) for request, _, base in priced),
        five_minutes_usd=sum(
            replayed_cost(request, cached, base, FIVE_MINUTES) for request, cached, base in priced
        ),
        one_hour_usd=sum(
            replayed_cost(request, cached, base, ONE_HOUR) for request, cached, base in priced
        ),
    )


def priced_requests(requests: Sequence[Request]) -> list[tuple[Request, int, float]]:
    """Requests with a known price, the previous request's cached context and USD per base token.

    A request with an unknown price breaks the chain: the next request's previous context counts as
    unknown.
    """
    result: list[tuple[Request, int, float]] = []
    previous: int | None = None
    for request in requests:
        base = usd_per_token(request.model)
        if base is None:
            previous = None
            continue
        result.append((request, 0 if previous is None or request.first else previous, base))
        previous = context_of(request.usage) + request.usage.output
    return result


def replayed_cost(request: Request, cached_before: int, base: float, lifetime: str) -> float:
    """The input cost of the request had the cache been written with this lifetime (USD)."""
    prices = claude_prices(request.model)
    context = context_of(request.usage)
    warm = request.gap_seconds is not None and request.gap_seconds <= LIFETIME_SECONDS[lifetime]
    cached = min(cached_before, context) if warm else 0
    write = prices.write_5m if lifetime == FIVE_MINUTES else prices.write_1h
    return (cached * prices.read + (context - cached) * write) * base


def recommended_lifetime(replay: LifetimeReplay) -> str | None:
    """Recommended lifetime: the other one if it beats the error and MIN_GAIN, else None."""
    error = replay.replay_error()
    if replay.current is None or error is None:
        return None
    other = ONE_HOUR if replay.current == FIVE_MINUTES else FIVE_MINUTES
    now, then = replay.replayed(replay.current), replay.replayed(other)
    gain = (now - then) / now if now > 0 else 0.0
    return other if gain > max(MIN_GAIN, abs(error)) else None
