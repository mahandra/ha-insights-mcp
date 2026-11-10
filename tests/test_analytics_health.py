from datetime import UTC, datetime, timedelta
from typing import Any

from ha_insights_mcp.analytics.health import (
    HealthThresholds,
    check_battery,
    check_flapping,
    check_stale,
    check_unavailable,
    check_unknown,
    max_changes_per_hour,
    run_health_checks,
)
from ha_insights_mcp.ha.models import Device, EntityState, StatePoint
from ha_insights_mcp.snapshot import EntityView

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def view(
    entity_id: str,
    state: str,
    *,
    device: str | None = None,
    changed: datetime = NOW,
    reported: datetime | None = None,
    **attributes: Any,
) -> EntityView:
    return EntityView(
        state=EntityState(
            entity_id=entity_id,
            state=state,
            attributes=attributes,
            last_changed=changed,
            last_updated=changed,
            last_reported=reported,
        ),
        area=None,
        device=Device(id=device, name=device) if device else None,
    )


def test_unavailable_grouped_by_device() -> None:
    views = [
        view("sensor.a_temp", "unavailable", device="dev_a", changed=NOW - timedelta(hours=1)),
        view("sensor.a_hum", "unavailable", device="dev_a", changed=NOW - timedelta(hours=3)),
        view("light.b", "unavailable", device="dev_b"),
        view("sensor.orphan", "unavailable"),
        view("sensor.ok", "21"),
    ]
    issues = {i.entity_id: i for i in check_unavailable(views)}
    assert set(issues) == {"sensor.a_hum", "light.b", "sensor.orphan"}
    grouped = issues["sensor.a_hum"]  # earliest of the device's entities
    assert grouped.detail == "2 entities unavailable: sensor.a_hum, sensor.a_temp"
    assert grouped.since == NOW - timedelta(hours=3)
    assert grouped.severity == "error"


def test_unknown_skips_stateless_domains() -> None:
    views = [view("button.push", "unknown"), view("sensor.x", "unknown"), view("tts.g", "unknown")]
    assert [(i.entity_id, i.severity) for i in check_unknown(views)] == [("sensor.x", "info")]


def test_battery_levels() -> None:
    views = [
        view("sensor.b1", "8", device_class="battery"),
        view("sensor.b2", "15", device_class="battery"),
        view("sensor.b3", "80", device_class="battery"),
        view("sensor.b4", "unavailable", device_class="battery"),
        view("binary_sensor.b5", "on", device_class="battery"),
        view("binary_sensor.b6", "off", device_class="battery"),
        view("sensor.not_battery", "5", device_class="humidity"),
    ]
    issues = {i.entity_id: (i.severity, i.detail) for i in check_battery(views, threshold=20)}
    assert issues == {
        "sensor.b1": ("error", "battery 8%"),  # below half the threshold
        "sensor.b2": ("warning", "battery 15%"),
        "binary_sensor.b5": ("warning", "battery low"),
    }


def test_stale_uses_last_reported() -> None:
    old = NOW - timedelta(hours=30)
    views = [
        # Value unchanged for 30h but reported recently: healthy.
        view("sensor.steady", "21", changed=old, reported=NOW, state_class="measurement"),
        # Silent for 30h: stale.
        view("sensor.silent", "21", changed=old, reported=old, state_class="measurement"),
        # Old HA without last_reported falls back to last_updated.
        view("sensor.legacy", "21", changed=old, state_class="measurement"),
        # Not a measurement: ignored.
        view("sensor.energy", "100", changed=old, state_class="total_increasing"),
    ]
    issues = check_stale(views, NOW, stale_hours=24)
    assert [(i.entity_id, i.detail) for i in issues] == [
        ("sensor.silent", "no report for 30h"),
        ("sensor.legacy", "no report for 30h"),
    ]


def changes(*minutes: float, start: str = "off") -> list[StatePoint]:
    states = [start]
    for _ in minutes:
        states.append("on" if states[-1] == "off" else "off")
    times = [
        NOW - timedelta(hours=24),
        *(NOW - timedelta(hours=24) + timedelta(minutes=m) for m in minutes),
    ]
    return [StatePoint(state=s, changed_at=t) for s, t in zip(states, times, strict=True)]


def test_max_changes_per_hour_sliding_window() -> None:
    # 3 changes in the first hour, then a burst of 5 within 10 minutes later on.
    points = changes(10, 20, 30, 300, 302, 304, 306, 310)
    count, start = max_changes_per_hour(points)
    assert count == 5
    assert start == NOW - timedelta(hours=24) + timedelta(minutes=300)


def test_max_changes_per_hour_ignores_repeated_states() -> None:
    points = [StatePoint(state="on", changed_at=NOW - timedelta(minutes=m)) for m in range(10)]
    assert max_changes_per_hour(points) == (0, None)


def test_flapping_threshold() -> None:
    history = {
        "binary_sensor.burst": changes(*range(0, 40, 1)),  # 40 changes in 40 minutes
        "binary_sensor.calm": changes(0, 120, 240),
    }
    views = [view("binary_sensor.burst", "off"), view("binary_sensor.calm", "off")]
    issues = check_flapping(views, history, threshold=20)
    assert [(i.entity_id, i.detail) for i in issues] == [
        ("binary_sensor.burst", "40 state changes within one hour")
    ]


def test_run_health_checks_sorted_by_severity() -> None:
    views = [
        view("sensor.x", "unknown"),
        view("sensor.b", "15", device_class="battery"),
        view("light.dead", "unavailable"),
    ]
    issues = run_health_checks(views, {}, NOW, HealthThresholds())
    assert [(i.severity, i.code) for i in issues] == [
        ("error", "unavailable"),
        ("warning", "low_battery"),
        ("info", "unknown_state"),
    ]
