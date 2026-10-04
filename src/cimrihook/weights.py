"""Window weights: how many points of a subscription window one list-price dollar moves.

The window percentage is a whole number, so the points one run moves are only good to a point,
and anything else on the account moves the same window meanwhile. Between two consecutive
whole-percent crossings, though, the window moved by exactly the levels between them. The time of
a crossing is bracketed by the readings on either side (the readings of every recorded session are
merged, so the brackets are narrow while anything is running). Over the span between two crossings
(bracket midpoints) the spend of each class of sessions is read off its cumulative spend curve,
linear between its readings, and the points of all spans are regressed on the class spends through
the origin:

    points = sum over classes of (weight of the class * spend of the class in the span)

A weight is "points per list-price dollar" of a class: for example the runs with and without the
compaction window, told apart from the other sessions that ran at the same time. Spend that no
session recorded (claude.ai, sessions without the mod) is in no class and raises the weights.
"""

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from cimrihook.limits import WINDOW_NAMES, LimitSample
from cimrihook.stats import RatioEstimate, t_975

MIN_EXTRA_SPANS: Final = 3  # spans beyond the number of classes before weights are stated
ALL_SESSIONS: Final = "all sessions"  # the one class of a ledger fit
SINGULAR_RATIO: Final = 1e-9  # a pivot this small next to the matrix scale is a singular system


@dataclass(frozen=True, slots=True)
class SpendCurve:
    """Cumulative list-price spend of one session at its readings, from its first reading."""

    times: tuple[float, ...]
    usd: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Crossing:
    """A window first showed a whole-percent level between two readings of one period."""

    period: str  # the window's reset time: separates the periods
    level: float
    lower: float  # time of the last reading before the level
    upper: float  # time of the first reading at the level


@dataclass(frozen=True, slots=True)
class Span:
    """The points the window moved between two crossings and the spend of each class meanwhile."""

    start: float
    end: float
    points: float
    spends: tuple[float, ...]  # per class, in the order the classes were given


@dataclass(frozen=True, slots=True)
class ClassWeight:
    """Points per list-price dollar of one class with its 95% interval."""

    name: str
    weight: float
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class WeightFit:
    """Least-squares weights of the classes over the spans."""

    spans: int
    degrees: int  # spans minus classes
    residual: float  # standard deviation of the residuals, in points
    weights: tuple[ClassWeight, ...]
    covariance: tuple[tuple[float, ...], ...]  # of the weights, in the same order


def sessions_of(samples: Sequence[LimitSample]) -> list[list[LimitSample]]:
    """The measurements grouped by session."""
    groups: dict[str, list[LimitSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.session].append(sample)
    return list(groups.values())


def spend_curve(samples: Sequence[LimitSample]) -> SpendCurve:
    """Spend since the first reading of one session, which must have at least one.

    A session's spend before its first reading is left out: a resumed session or a mod enabled
    mid-session already carries spend that never filled the window during the readings.
    """
    ordered = sorted(samples, key=lambda sample: sample.time)
    return SpendCurve(
        tuple(s.time for s in ordered), tuple(s.usd - ordered[0].usd for s in ordered)
    )


def spend_at(curve: SpendCurve, moment: float) -> float:
    """The spend up to a moment: linear between readings, nothing before the first."""
    if moment < curve.times[0]:
        return 0.0
    index = bisect_right(curve.times, moment) - 1
    if index == len(curve.times) - 1:
        return curve.usd[-1]
    share = (moment - curve.times[index]) / (curve.times[index + 1] - curve.times[index])
    return curve.usd[index] + share * (curve.usd[index + 1] - curve.usd[index])


def window_crossings(samples: Sequence[LimitSample], kind: str) -> list[Crossing]:
    """When each whole-percent level of a window was first seen, bracketed by readings."""
    readings = sorted(
        (sample.time, window.percent, window.resets_at)
        for sample in samples
        for window in sample.windows
        if window.kind == kind
    )
    crossings: list[Crossing] = []
    for period in sorted({resets_at for _, _, resets_at in readings}):
        series = [(time, percent) for time, percent, resets_at in readings if resets_at == period]
        top = series[0][1]
        for (before, _), (time, percent) in pairwise(series):
            if percent > top:
                crossings.append(Crossing(period, percent, before, time))
                top = percent
    return crossings


def crossing_spans(
    crossings: Sequence[Crossing],
    classes: Sequence[Sequence[SpendCurve]],
    first: float,
    last: float,
) -> list[Span]:
    """The points and the spend of each class between consecutive crossings of one period.

    Only spans inside [first, last] count, so that the sessions of other days do not enter.
    """
    spans: list[Span] = []
    for before, after in pairwise(crossings):
        start, end = (before.lower + before.upper) / 2, (after.lower + after.upper) / 2
        if before.period == after.period and first <= start < end <= last:
            spends = tuple(
                sum(spend_at(curve, end) - spend_at(curve, start) for curve in curves)
                for curves in classes
            )
            spans.append(Span(start, end, after.level - before.level, spends))
    return spans


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Inner product of two vectors of the same length."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def invert(matrix: Sequence[Sequence[float]]) -> list[list[float]] | None:
    """Inverse by Gauss-Jordan elimination with partial pivoting; None for a singular matrix."""
    size = len(matrix)
    scale = max(abs(value) for row in matrix for value in row)
    rows = [[*row, *(1.0 if i == j else 0.0 for j in range(size))] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda index: abs(rows[index][column]))
        if abs(rows[pivot][column]) < SINGULAR_RATIO * scale:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        rows[column] = [value / rows[column][column] for value in rows[column]]
        for other in range(size):
            if other != column:
                factor = rows[other][column]
                rows[other] = [
                    value - factor * pivot_value
                    for value, pivot_value in zip(rows[other], rows[column], strict=True)
                ]
    return [row[size:] for row in rows]


def fit_weights(spans: Sequence[Span], names: Sequence[str]) -> WeightFit | None:
    """Least squares through the origin.

    None if there are too few spans for the classes or their spends move together, so that the
    weights cannot be told apart. A class with no spend in any span is left out of the fit.
    """
    active = [i for i in range(len(names)) if any(span.spends[i] > 0 for span in spans)]
    if not active or len(spans) < len(active) + MIN_EXTRA_SPANS:
        return None
    columns = [[span.spends[i] for span in spans] for i in active]
    points = [span.points for span in spans]
    inverse = invert([[dot(a, b) for b in columns] for a in columns])
    if inverse is None:
        return None
    weights = [dot(row, [dot(column, points) for column in columns]) for row in inverse]
    fitted = [
        sum(weight * column[n] for weight, column in zip(weights, columns, strict=True))
        for n in range(len(spans))
    ]
    degrees = len(spans) - len(active)
    variance = sum((p - f) ** 2 for p, f in zip(points, fitted, strict=True)) / degrees
    covariance = tuple(tuple(variance * value for value in row) for row in inverse)
    quantile = t_975(degrees)
    return WeightFit(
        spans=len(spans),
        degrees=degrees,
        residual=math.sqrt(variance),
        weights=tuple(
            ClassWeight(
                names[i],
                weights[k],
                weights[k] - quantile * math.sqrt(covariance[k][k]),
                weights[k] + quantile * math.sqrt(covariance[k][k]),
            )
            for k, i in enumerate(active)
        ),
        covariance=covariance,
    )


def pooled_spans(spans: Sequence[Span]) -> list[Span]:
    """The spans with the spend of all classes added into one class."""
    return [Span(span.start, span.end, span.points, (sum(span.spends),)) for span in spans]


def weight_of(fit: WeightFit, name: str) -> ClassWeight | None:
    """The weight of a class; None if it was left out of the fit."""
    return next((weight for weight in fit.weights if weight.name == name), None)


def weight_ratio(fit: WeightFit, numerator: str, denominator: str) -> RatioEstimate | None:
    """Ratio of two class weights with a delta-method t interval on the log scale.

    None if a class is not in the fit or a weight is not positive.
    """
    names = [weight.name for weight in fit.weights]
    if numerator not in names or denominator not in names:
        return None
    top, bottom = names.index(numerator), names.index(denominator)
    a, b = fit.weights[top].weight, fit.weights[bottom].weight
    if a <= 0 or b <= 0:
        return None
    variance = (
        fit.covariance[top][top] / a**2
        + fit.covariance[bottom][bottom] / b**2
        - 2 * fit.covariance[top][bottom] / (a * b)
    )
    margin = t_975(fit.degrees) * math.sqrt(max(variance, 0.0))
    return RatioEstimate(a / b, a / b * math.exp(-margin), a / b * math.exp(margin))


def weight_text(weight: ClassWeight) -> str:
    """A weight as points per list-price dollar and as dollars per point, with the 95% interval.

    Dollars per point have no upper bound when the interval of the weight reaches zero.
    """
    points = f"{weight.weight:.3f} points per $ [95% {weight.low:.3f}-{weight.high:.3f}]"
    if weight.low <= 0:
        return points
    return (
        f"{points} = ${1 / weight.weight:,.2f} per point "
        f"[95% ${1 / weight.high:,.2f}-${1 / weight.low:,.2f}]"
    )


def ledger_spans(samples: Sequence[LimitSample], kind: str) -> list[Span]:
    """The spans of a window over the whole ledger, with the spend of all sessions as one class."""
    curves = [spend_curve(group) for group in sessions_of(samples)]
    moments = [sample.time for sample in samples]
    return crossing_spans(window_crossings(samples, kind), [curves], min(moments), max(moments))


def point_cost_text(weight: ClassWeight) -> str:
    """What one point of a window costs, in list-price dollars, with the 95% interval."""
    if weight.weight <= 0:
        return "no recorded spend has moved this window yet"
    cost = f"1 point is about ${1 / weight.weight:,.2f} of usage at API list prices"
    if weight.low <= 0:
        return f"{cost} (the 95% interval has no upper bound)"
    return f"{cost} [95% ${1 / weight.high:,.2f}-${1 / weight.low:,.2f}]"


def render_limits(samples: Sequence[LimitSample]) -> str:
    """Output of `cimrihook limits`: the cost of a window point from the ledger's own readings."""
    sessions = len({sample.session for sample in samples})
    lines = [
        f"CimriHook limits: {len(samples):,} measurements from {sessions:,} sessions "
        "(list-price spend of each session against your subscription windows)"
    ]
    kinds = sorted({window.kind for sample in samples for window in sample.windows})
    if not kinds:
        lines.append("  no window reading yet")
    for kind in kinds:
        spans = ledger_spans(samples, kind)
        fit = fit_weights(spans, [ALL_SESSIONS])
        measured = (
            f"{len(spans)} spans between whole-percent crossings, "
            f"{sum(span.points for span in spans):.0f} points, "
            f"${sum(sum(span.spends) for span in spans):,.2f} measured"
        )
        cost = (
            point_cost_text(fit.weights[0])
            if fit is not None
            else f"needs at least {1 + MIN_EXTRA_SPANS} spans to estimate"
        )
        lines.append(f"  {WINDOW_NAMES.get(kind, kind)}: {cost} ({measured})")
    lines.append(
        "  Use outside these sessions (claude.ai, sessions without the mod, A/B runs) also "
        "fills the windows; with such use a point looks cheaper than it is."
    )
    return "\n".join(lines)
