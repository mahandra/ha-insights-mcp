from datetime import UTC, datetime, timedelta

import pytest

from ha_insights_mcp.analytics.history import (
    is_numeric_entity,
    statistics_period,
    summarize_discrete,
    summarize_numeric_from_history,
    summarize_numeric_from_statistics,
    to_float,
)
from ha_insights_mcp.ha.models import StatBucket, StatePoint

START = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=10)


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


def p(hours: float, state: str) -> StatePoint:
    return StatePoint(state=state, changed_at=at(hours))


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("state", "expected"), [("21.5", 21.5), ("-3", -3.0), ("on", None), ("nan", None), ("", None)]
)
def test_to_float(state: str, expected: float | None) -> None:
    assert to_float(state) == expected


def test_is_numeric_entity() -> None:
    assert is_numeric_entity("sensor", "21.5")
    assert not is_numeric_entity("sensor", "unavailable")
    assert not is_numeric_entity("light", "on")
    # A number outside numeric domains counts only with a unit (geo_location distance).
    assert not is_numeric_entity("event", "3")
    assert is_numeric_entity("geo_location", "12.3", unit="km")


def test_statistics_period() -> None:
    assert statistics_period(timedelta(hours=24)) == "5minute"
    assert statistics_period(timedelta(hours=25)) == "hour"


# ----------------------------------------------------------------- discrete


def test_discrete_time_in_state_and_changes() -> None:
    # Start state off, on at 2h, off at 6h, on at 9h -> off 2+3=5h, on 4+1=5h.
    points = [p(0, "off"), p(2, "on"), p(6, "off"), p(9, "on")]
    summary = summarize_discrete(points, START, END)
    assert summary.changes == 3
    assert summary.time_in_state_pct == {"off": 50.0, "on": 50.0}
    assert [c.state for c in summary.recent_changes] == ["on", "off", "on"]


def test_discrete_same_state_repeats_are_not_changes() -> None:
    summary = summarize_discrete([p(0, "on"), p(1, "on"), p(5, "off")], START, END)
    assert summary.changes == 1
    assert summary.time_in_state_pct == {"on": 50.0, "off": 50.0}


def test_discrete_entity_created_mid_window_counts_only_covered_time() -> None:
    summary = summarize_discrete([p(8, "on"), p(9, "off")], START, END)
    assert summary.time_in_state_pct == {"on": 50.0, "off": 50.0}
    assert summary.changes == 1


def test_discrete_recent_is_capped() -> None:
    points = [p(i / 10, "on" if i % 2 else "off") for i in range(50)]
    summary = summarize_discrete(points, START, END, recent=10)
    assert summary.changes == 49
    assert len(summary.recent_changes) == 10
    assert summary.recent_changes[-1].changed_at == at(4.9)


def test_discrete_empty() -> None:
    summary = summarize_discrete([], START, END)
    assert (summary.changes, summary.time_in_state_pct, summary.recent_changes) == (0, {}, [])


# ------------------------------------------------------------------ numeric


def test_numeric_from_history_is_time_weighted() -> None:
    # 10 for 9 hours, 20 for 1 hour: plain average of points would say 15.
    points = [p(0, "10"), p(9, "20")]
    summary = summarize_numeric_from_history(points, START, END, buckets=2)
    assert (summary.min, summary.mean, summary.max) == (10.0, 11.0, 20.0)
    assert [(b.start, b.mean) for b in summary.buckets] == [(at(0), 10.0), (at(5), 12.0)]
    assert summary.source == "history"


def test_numeric_from_history_tracks_missing_time() -> None:
    points = [p(0, "10"), p(5, "unavailable"), p(7.5, "12")]
    summary = summarize_numeric_from_history(points, START, END, buckets=1)
    assert summary.missing_pct == 25.0
    # (10 * 5h + 12 * 2.5h) / 7.5h: unavailable time is excluded from the mean.
    assert summary.mean == 10.67


def test_numeric_from_statistics_merges_buckets() -> None:
    stats = [
        StatBucket(start=at(h), end=at(h + 1), min=h, mean=h + 0.5, max=h + 1) for h in range(10)
    ]
    summary = summarize_numeric_from_statistics(stats, START, END, buckets=2)
    assert [(b.start, b.min, b.mean, b.max) for b in summary.buckets] == [
        (at(0), 0.0, 2.5, 5.0),
        (at(5), 5.0, 7.5, 10.0),
    ]
    assert (summary.min, summary.mean, summary.max) == (0.0, 5.0, 10.0)
    assert summary.source == "statistics"


def test_numeric_from_statistics_never_more_buckets_than_data() -> None:
    stats = [StatBucket(start=at(0), end=at(1), min=1, mean=1, max=1)]
    assert len(summarize_numeric_from_statistics(stats, START, END, buckets=12).buckets) == 1
