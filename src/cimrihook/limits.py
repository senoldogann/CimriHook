"""Limit meter: the rate of the subscription's 5-hour and weekly windows in list-price dollars.

Pro and Max subscriptions have no bill; usage fills a 5-hour and a weekly window, shown as a
percentage. Anthropic does not publish how much a token type (cache read, write, output) counts
in a window, but Claude Code's own interface says that switching the model or the effort
"re-reads everything so far, which adds to your usage": what fills the window are the token
flows CimriHook counts at list prices.

On every measurement (after every turn, and when a window advances by a point) the CimriHook mod
appends the session's list-price spend so far and the windows' percentages to
`limits/<session>.jsonl`. `cimrihook.weights` combines the records of all sessions into the rate
"one point of the window is this many dollars of usage". claude.ai chats, sessions without the
mod and A/B runs also fill the window but are not recorded; with such use the rate shows a point
as worth fewer dollars than it is.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard

from cimrihook.errors import ConfigError

LIMITS_DIR: Final = "limits"
WINDOW_NAMES: Final = {"five_hour": "5-hour window", "seven_day": "weekly window"}


@dataclass(frozen=True, slots=True)
class WindowUse:
    """The used percentage of one window in one measurement."""

    kind: str  # five_hour, seven_day or a gateway's spend_limit
    percent: float
    resets_at: str  # separates the periods; the same reset time is the same period


@dataclass(frozen=True, slots=True)
class LimitSample:
    """One measurement the mod makes in a session."""

    session: str
    time: float  # epoch seconds
    usd: float  # the session's list-price spend up to that moment
    windows: tuple[WindowUse, ...]


@dataclass(frozen=True, slots=True)
class RunPoints:
    """Points one run moved a window and the list-price spend between the same two readings."""

    kind: str
    points: float
    usd: float


def limits_dir(home: Path) -> Path:
    """Directory of the mod's measurement files."""
    return home / LIMITS_DIR


def read_samples(directory: Path) -> list[LimitSample]:
    """The measurements of all sessions, ordered by time."""
    if not directory.is_dir():
        raise ConfigError(
            f"no limit samples in {directory}: the CimriHook mod writes them on a subscription "
            "once it is enabled (`cimrihook init --mod`)"
        )
    samples = [
        sample for path in sorted(directory.glob("*.jsonl")) for sample in session_samples(path)
    ]
    return sorted(samples, key=lambda sample: sample.time)


def session_samples(path: Path) -> list[LimitSample]:
    """Measurements of one session file; a corrupt line is an error naming file and line."""
    return [
        parse_sample(path.stem, line, f"{path}:{number}")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip()
    ]


def parse_sample(session: str, line: str, where: str) -> LimitSample:
    """One line the mod writes: `{"t": ms, "usd": ..., "limits": [{kind, percentUsed, ...}]}`."""
    try:
        raw: object = json.loads(line)
    except ValueError as error:
        raise ConfigError(f"{where}: unreadable limit sample ({error})") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a limit sample must be a JSON object, got {line[:80]!r}")
    stamp, usd, limits = raw.get("t"), raw.get("usd"), raw.get("limits")
    if not is_number(stamp) or not is_number(usd) or not isinstance(limits, list):
        raise ConfigError(f"{where}: a limit sample needs numeric t and usd and a limits list")
    return LimitSample(
        session=session,
        time=float(stamp) / 1000,
        usd=float(usd),
        windows=tuple(parse_window(window, where) for window in limits),
    )


def parse_window(window: object, where: str) -> WindowUse:
    """Use of one window: kind, percentage and reset time."""
    if not isinstance(window, dict):
        raise ConfigError(f"{where}: a window must be a JSON object")
    kind, percent, resets_at = window.get("kind"), window.get("percentUsed"), window.get("resetsAt")
    if not isinstance(kind, str) or not is_number(percent):
        raise ConfigError(f"{where}: a window needs a kind and a numeric percentUsed")
    return WindowUse(kind, float(percent), resets_at if isinstance(resets_at, str) else "")


def is_number(value: object) -> TypeGuard[int | float]:
    """Is the value a JSON number (bool excluded)?"""
    return isinstance(value, int | float) and not isinstance(value, bool)


def run_points(samples: Sequence[LimitSample], where: str) -> list[RunPoints]:
    """Points each window kind moved between the first and the last reading of one run.

    A kind whose reset time changed during the run (the window reset under it) is left out: the
    readings of two periods are not one count. The windows report whole percents, so the points of
    a run are good to about one point.
    """
    ordered = sorted(samples, key=lambda sample: sample.time)
    kinds = sorted({window.kind for sample in ordered for window in sample.windows})
    counted = [kind_points(ordered, kind, where) for kind in kinds]
    return [points for points in counted if points is not None]


def kind_points(ordered: Sequence[LimitSample], kind: str, where: str) -> RunPoints | None:
    """Points and spend of one window kind in a run; None if its reset time changed."""
    readings = [
        (sample, window) for sample in ordered for window in sample.windows if window.kind == kind
    ]
    if len({window.resets_at for _, window in readings}) != 1:
        return None
    (first_sample, first_window), (last_sample, last_window) = readings[0], readings[-1]
    points = last_window.percent - first_window.percent
    usd = last_sample.usd - first_sample.usd
    if points < 0 or usd < 0:
        raise ConfigError(
            f"{where}: the {kind} readings go backwards within one window period "
            f"(points {points}, spend ${usd:.4f})"
        )
    return RunPoints(kind, points, usd)
