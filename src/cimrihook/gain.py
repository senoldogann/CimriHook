"""Gain: the period before and the period after CimriHook's last install that changed settings.

Like RTK's `gain` report it is read from the user's own records: the time since the last
`cimrihook init` is compared with the period of the same length just before it. Each request is
priced at API list prices from its own usage data, as in doctor; requests that forked sessions
copied count once, and a request without a time counts nowhere because it cannot be assigned to a
period. The workload of the two periods differs: this is not an A/B test but a before-after view.
That is why, next to the total spend, ratios less affected by the amount of work are shown:
spend per request, mean context and the share of spend in large-context requests.

The receipt, by contrast, is matched: the post-install sessions' own requests are re-priced as if
the automatic compactions of that period had never happened. The context a compaction deleted
would have been re-read from the cache on every later request; the summary the first request
after the compaction writes and the re-attached files are not written, the old context would have
been read instead. Compaction calls are not written to the transcript, so they are added to the
real side as an estimate (one read of the context and the summary as output). Claude Code would
still have compacted had the context passed the model's default threshold; the context carried
beyond that is reset. It cannot see the re-reading or the agent's changed behaviour: in A/B runs
this kind of replay came out 0-11 points optimistic.
"""

import math
import sqlite3
import statistics
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cimrihook.doctor import IDLE_HOUR, percent, rewrite_cause, tokens_text
from cimrihook.errors import ConfigError, LedgerError
from cimrihook.install import load_record
from cimrihook.ledger import ledger_path
from cimrihook.scan import TranscriptScan, scan_transcript, token_costs, unique_scans
from cimrihook.simulate import claude_prices, context_of, usd_per_token
from cimrihook.transcripts import SECONDS_PER_DAY, recent_transcripts

LARGE_CONTEXT: Final = 200_000  # requests above this context count as large-context
# Context at which Claude Code would compact 1M-context models without a window (window − 33k).
DEFAULT_COMPACTION_CONTEXT: Final = 967_000
MIN_PERIOD_SECONDS: Final = 3_600.0  # periods shorter than an hour are not compared


@dataclass(frozen=True, slots=True)
class Period:
    """Summary of the requests and spend of one period."""

    start: float
    end: float
    requests: int
    usd: float  # at API list prices
    mean_context: float
    large_context_usd: float  # spend of the requests whose context exceeds LARGE_CONTEXT
    compactions: int
    idle_rewrite_usd: float  # re-writing the cache after an idle gap longer than an hour
    guard_stops: int


@dataclass(frozen=True, slots=True)
class Receipt:
    """Sessions after the install: real cost, and the estimate without the auto-compactions."""

    compactions: int  # automatic compactions of the period
    sessions: int  # sessions in which those compactions happened
    requests_usd: float  # those sessions' requests in the period
    compaction_calls_usd: float  # estimate of the compaction calls (not in the transcript)
    without_compactions_usd: float  # the same requests, re-priced without compactions


@dataclass(frozen=True, slots=True)
class Gain:
    """The periods before and after the install, and the receipt of the later period."""

    installed_at: float
    before: Period
    after: Period
    receipt: Receipt


def measure_gain(projects_dir: Path, home: Path, settings_path: Path, now: float) -> Gain:
    """Compares the time since the last install with an equally long time before it."""
    installed_at = load_record(home, settings_path).installed_at
    if installed_at is None:
        raise ConfigError(
            f"no install time is recorded for {settings_path}; run `cimrihook init` first, gain "
            "compares the time since then with the same time before it"
        )
    length = now - installed_at
    if length < MIN_PERIOD_SECONDS:
        raise ConfigError(
            f"the last init was {length / 60:.0f} minutes ago; gain needs at least an hour after it"
        )
    start = installed_at - length
    files = recent_transcripts(projects_dir, math.ceil((now - start) / SECONDS_PER_DAY), now)
    scans = unique_scans([scan_transcript(path, "subagents" in path.parts) for path in files])
    stops = guard_stop_times(ledger_path(home))
    return Gain(
        installed_at=installed_at,
        before=period(scans, start, installed_at, stops),
        after=period(scans, installed_at, now, stops),
        receipt=receipt(scans, installed_at, now),
    )


def receipt(scans: Sequence[TranscriptScan], start: float, end: float) -> Receipt:
    """Matched counterfactual of the automatic compactions in [start, end)."""
    parts = [session_receipt(scan, start, end) for scan in scans]
    touched = [part for part in parts if part.compactions]
    return Receipt(
        compactions=sum(part.compactions for part in touched),
        sessions=len(touched),
        requests_usd=sum(part.requests_usd for part in touched),
        compaction_calls_usd=sum(part.compaction_calls_usd for part in touched),
        without_compactions_usd=sum(part.without_compactions_usd for part in touched),
    )


def session_receipt(scan: TranscriptScan, start: float, end: float) -> Receipt:
    """One session: each compaction is matched, in order, with the first request after it."""
    boundaries = iter(scan.compactions)
    carried = 0  # tokens the compactions deleted, still in the context in the counterfactual
    compactions = 0
    actual = calls = counterfactual = 0.0
    for request in scan.requests:
        compaction = next(boundaries, None) if request.after_compaction else None
        inside = request.timestamp is not None and start <= request.timestamp < end
        base = usd_per_token(request.model)
        if not inside or base is None:
            carried = 0 if compaction is not None else carried  # outside the window: chain breaks
            continue
        prices = claude_prices(request.model)
        context = context_of(request.usage)
        cost = sum(token_costs(request, base))
        actual += cost
        if compaction is not None and compaction.automatic:
            compactions += 1
            calls += (
                compaction.trigger * prices.read + compaction.summary_tokens * prices.output
            ) * base
            carried += max(0, compaction.trigger - context)
            if context + carried > DEFAULT_COMPACTION_CONTEXT:
                carried = 0  # the default threshold would compact here too
            # Counterfactual: no write after the compaction; the whole context is read from cache.
            counterfactual += (
                (context + carried) * prices.read + request.usage.output * prices.output
            ) * base
            continue
        if context + carried > DEFAULT_COMPACTION_CONTEXT:
            carried = 0
        counterfactual += cost + carried * prices.read * base
    return Receipt(
        compactions=compactions,
        sessions=1 if compactions else 0,
        requests_usd=actual,
        compaction_calls_usd=calls,
        without_compactions_usd=counterfactual,
    )


def period(
    scans: Sequence[TranscriptScan], start: float, end: float, stops: Sequence[float]
) -> Period:
    """Requests, compactions and guard stops in [start, end)."""
    requests = [
        request
        for scan in scans
        for request in scan.requests
        if request.timestamp is not None and start <= request.timestamp < end
    ]
    priced = [
        (request, token_costs(request, base))
        for request in requests
        if (base := usd_per_token(request.model)) is not None
    ]
    contexts = [context_of(request.usage) for request in requests]
    return Period(
        start=start,
        end=end,
        requests=len(requests),
        usd=sum(sum(costs) for _, costs in priced),
        mean_context=statistics.fmean(contexts) if contexts else 0.0,
        large_context_usd=sum(
            sum(costs) for request, costs in priced if context_of(request.usage) > LARGE_CONTEXT
        ),
        compactions=sum(
            1
            for scan in scans
            for compaction in scan.compactions
            if compaction.timestamp is not None and start <= compaction.timestamp < end
        ),
        idle_rewrite_usd=sum(
            costs[1] + costs[2] for request, costs in priced if rewrite_cause(request) == IDLE_HOUR
        ),
        guard_stops=sum(1 for stopped in stops if start <= stopped < end),
    )


def guard_stop_times(ledger: Path) -> tuple[float, ...]:
    """Times of the prompts the cold-prompt guard stopped; empty if there is no ledger or table."""
    if not ledger.exists():
        return ()
    try:
        with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=2.0)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'guard_blocks'"
            ).fetchone()
            if table is None:
                return ()
            rows = db.execute("SELECT blocked_at FROM guard_blocks").fetchall()
    except sqlite3.Error as error:
        raise LedgerError(f"cannot read guard stops from {ledger}: {error}") from error
    return tuple(float(row[0]) for row in rows)


def render_gain(gain: Gain) -> str:
    """The text of the report."""
    before, after = gain.before, gain.after
    since = datetime.fromtimestamp(gain.installed_at).strftime("%Y-%m-%d %H:%M")
    days = (after.end - after.start) / SECONDS_PER_DAY
    rows = [
        ("API requests", f"{before.requests:,}", f"{after.requests:,}", ""),
        ("spend at list prices", f"${before.usd:,.0f}", f"${after.usd:,.0f}", ""),
        (
            "spend per request",
            f"${per_request(before):.3f}",
            f"${per_request(after):.3f}",
            change(per_request(before), per_request(after)),
        ),
        (
            "mean context",
            tokens_text(round(before.mean_context)),
            tokens_text(round(after.mean_context)),
            change(before.mean_context, after.mean_context),
        ),
        (
            f"spend in requests over {LARGE_CONTEXT // 1000}k",
            percent(before.large_context_usd, before.usd),
            percent(after.large_context_usd, after.usd),
            "",
        ),
        ("compactions", f"{before.compactions:,}", f"{after.compactions:,}", ""),
        (
            "re-cache after 1 h idle",
            f"${before.idle_rewrite_usd:,.0f}",
            f"${after.idle_rewrite_usd:,.0f}",
            "",
        ),
        ("prompts stopped by the guard", f"{before.guard_stops:,}", f"{after.guard_stops:,}", ""),
    ]
    return "\n".join(
        [
            f"CimriHook gain: the {days:.1f} days since your last `cimrihook init` ({since}) vs "
            "the same time before it",
            f"  {'':<30}{'before':>12}{'after':>12}{'change':>10}",
            *(f"  {label:<30}{old:>12}{new:>12}{delta:>10}" for label, old, new, delta in rows),
            "Workloads differ between the two periods, so this is a before/after view, not an A/B "
            "test; per-request spend and mean context depend least on how much you worked.",
            *receipt_lines(gain.receipt),
        ]
    )


def receipt_lines(receipt: Receipt) -> list[str]:
    """Text of the matched receipt."""
    if not receipt.compactions:
        return ["Receipt: no automatic compaction since the last init, so nothing to compare yet."]
    actual = receipt.requests_usd + receipt.compaction_calls_usd
    saved = receipt.without_compactions_usd - actual
    return [
        f"Receipt for the {receipt.sessions:,} sessions with automatic compactions since then "
        f"({receipt.compactions:,} compactions): they cost ${actual:,.2f} (requests "
        f"${receipt.requests_usd:,.2f} + compaction calls about "
        f"${receipt.compaction_calls_usd:,.2f}). The same requests without those compactions: "
        f"about ${receipt.without_compactions_usd:,.2f}, so the window saved about ${saved:,.2f} "
        f"({change(receipt.without_compactions_usd, actual)}). This replay keeps the removed "
        "context and re-reads it on every later request; in A/B runs such replays were 0-11 points "
        "optimistic.",
    ]


def per_request(summary: Period) -> float:
    """List-price spend per request; 0 if there are no requests."""
    return summary.usd / summary.requests if summary.requests else 0.0


def change(before: float, after: float) -> str:
    """Relative change; '-' if the earlier value is zero."""
    return f"{100 * (after - before) / before:+.0f}%" if before > 0 else "-"
