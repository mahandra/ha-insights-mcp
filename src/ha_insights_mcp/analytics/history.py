"""Pure history analytics: no I/O, no MCP. Inputs are HA client models, outputs dataclasses.

The point of this module is to aggregate on the server, so the LLM gets conclusions
("on 37% of the time, 14 switches") instead of thousands of raw points.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from ha_insights_mcp.ha.models import StatBucket, StatePoint

NUMERIC_DOMAINS = frozenset({"sensor", "number", "input_number"})
MISSING_STATES = frozenset({"unavailable", "unknown"})


def to_float(state: str) -> float | None:
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def is_numeric_entity(domain: str, state: str, unit: str | None = None) -> bool:
    """Numeric = the state is a number and it is a measurement (numeric domain or has a unit).

    The unit check catches e.g. geo_location (distance in km) outside the sensor domain.
    """
    return to_float(state) is not None and (domain in NUMERIC_DOMAINS or unit is not None)


@dataclass(frozen=True, slots=True)
class Bucket:
    start: datetime
    min: float | None
    mean: float | None
    max: float | None


@dataclass(frozen=True, slots=True)
class NumericSummary:
    min: float | None
    mean: float | None
    max: float | None
    missing_pct: float  # share of the covered time spent unavailable/unknown (history only)
    buckets: list[Bucket]
    source: str  # "statistics" | "history"


@dataclass(frozen=True, slots=True)
class DiscreteSummary:
    changes: int
    time_in_state_pct: dict[str, float]
    recent_changes: list[StatePoint]


def _segments(
    points: list[StatePoint], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Turn points into [from, to) segments clipped to the window. Time before the first
    point (entity did not exist yet) is not covered."""
    ordered = sorted(points, key=lambda p: p.changed_at)
    segments = []
    for i, point in enumerate(ordered):
        seg_start = max(point.changed_at, start)
        seg_end = min(ordered[i + 1].changed_at if i + 1 < len(ordered) else end, end)
        if seg_end > seg_start:
            segments.append((seg_start, seg_end, point.state))
    return segments


def summarize_discrete(
    points: list[StatePoint], start: datetime, end: datetime, recent: int = 10
) -> DiscreteSummary:
    segments = _segments(points, start, end)
    durations: dict[str, float] = defaultdict(float)
    for seg_start, seg_end, state in segments:
        durations[state] += (seg_end - seg_start).total_seconds()
    covered = sum(durations.values())

    # A change is a point whose state differs from the previous one. The first point is
    # the state at window start, not a change.
    ordered = sorted(points, key=lambda p: p.changed_at)
    changes = [
        p
        for prev, p in zip(ordered, ordered[1:], strict=False)
        if p.state != prev.state and start <= p.changed_at <= end
    ]
    return DiscreteSummary(
        changes=len(changes),
        time_in_state_pct={
            state: round(100 * seconds / covered, 1)
            for state, seconds in sorted(durations.items(), key=lambda kv: -kv[1])
        }
        if covered
        else {},
        recent_changes=changes[-recent:] if recent else [],
    )


def count_changes(points: list[StatePoint], start: datetime, end: datetime) -> int:
    return summarize_discrete(points, start, end, recent=0).changes


def _bucket_edges(start: datetime, end: datetime, n: int) -> list[datetime]:
    step = (end - start) / n
    return [start + step * i for i in range(n + 1)]


def summarize_numeric_from_history(
    points: list[StatePoint], start: datetime, end: datetime, buckets: int
) -> NumericSummary:
    """Time-weighted aggregates from raw states (for sensors without statistics)."""
    edges = _bucket_edges(start, end, buckets)
    segments = _segments(points, start, end)

    def aggregate(lo: datetime, hi: datetime) -> tuple[float | None, ...]:
        weighted, seconds, values = 0.0, 0.0, []
        for seg_start, seg_end, state in segments:
            overlap = (min(seg_end, hi) - max(seg_start, lo)).total_seconds()
            value = to_float(state)
            if overlap <= 0 or value is None:
                continue
            weighted += value * overlap
            seconds += overlap
            values.append(value)
        if not values:
            return None, None, None
        return min(values), weighted / seconds, max(values)

    result = [
        Bucket(lo, *_rounded(aggregate(lo, hi))) for lo, hi in zip(edges, edges[1:], strict=False)
    ]
    total_min, total_mean, total_max = _rounded(aggregate(start, end))
    covered = sum((e - s).total_seconds() for s, e, _ in segments)
    missing = sum((e - s).total_seconds() for s, e, st in segments if st in MISSING_STATES)
    return NumericSummary(
        min=total_min,
        mean=total_mean,
        max=total_max,
        missing_pct=round(100 * missing / covered, 1) if covered else 0.0,
        buckets=result,
        source="history",
    )


def summarize_numeric_from_statistics(
    stats: list[StatBucket], start: datetime, end: datetime, buckets: int
) -> NumericSummary:
    """Merge HA's fixed-size statistics buckets into `buckets` evenly sized ones."""
    n = max(1, min(buckets, len(stats)))
    edges = _bucket_edges(start, end, n)
    groups: list[list[StatBucket]] = [[] for _ in range(n)]
    for stat in stats:
        if not start <= stat.start < end:
            continue
        index = min(int((stat.start - start) / (end - start) * n), n - 1)
        groups[index].append(stat)

    def merge(items: list[StatBucket]) -> tuple[float | None, ...]:
        mins = [s.min for s in items if s.min is not None]
        means = [s.mean for s in items if s.mean is not None]
        maxs = [s.max for s in items if s.max is not None]
        return (
            min(mins) if mins else None,
            sum(means) / len(means) if means else None,
            max(maxs) if maxs else None,
        )

    in_window = [s for g in groups for s in g]
    total = _rounded(merge(in_window))
    return NumericSummary(
        min=total[0],
        mean=total[1],
        max=total[2],
        missing_pct=0.0,
        buckets=[Bucket(edges[i], *_rounded(merge(g))) for i, g in enumerate(groups)],
        source="statistics",
    )


def statistics_period(window: timedelta) -> str:
    # Short-term (5-minute) statistics are kept ~10 days; hourly ones forever.
    return "5minute" if window <= timedelta(hours=24) else "hour"


def _rounded(values: tuple[float | None, ...]) -> tuple[float | None, ...]:
    return tuple(None if v is None else round(v, 2) for v in values)
