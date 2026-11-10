"""Pure device-health rules over a home snapshot and recent history.

Each rule yields HealthIssue objects with a stable `code`, so the model (and later the
guarded-actions layer) can group and reason about them.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ha_insights_mcp.analytics.history import to_float
from ha_insights_mcp.ha.models import StatePoint
from ha_insights_mcp.snapshot import EntityView

Severity = Literal["error", "warning", "info"]
SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}

# Domains whose state is normally "unknown" (no persistent state: a button has only a
# "last pressed" timestamp once pressed, tts/stt are services, ...).
STATELESS_DOMAINS = frozenset(
    {
        "ai_task",
        "button",
        "conversation",
        "event",
        "input_button",
        "notify",
        "scene",
        "stt",
        "tts",
        "wake_word",
    }
)


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    battery_low_percent: float = 20
    stale_hours: float = 24
    flapping_changes_per_hour: int = 20


@dataclass(frozen=True, slots=True)
class HealthIssue:
    code: str
    severity: Severity
    entity_id: str
    detail: str
    since: datetime | None = None


def check_unavailable(views: list[EntityView]) -> list[HealthIssue]:
    """Unavailable entities, grouped per device so one dead device is one issue."""
    by_device: dict[str, list[EntityView]] = defaultdict(list)
    issues = []
    for view in views:
        if view.state.state != "unavailable":
            continue
        if view.device is None:
            issues.append(_unavailable_issue([view]))
        else:
            by_device[view.device.id].append(view)
    issues.extend(_unavailable_issue(group) for group in by_device.values())
    return issues


def _unavailable_issue(group: list[EntityView]) -> HealthIssue:
    first = min(group, key=lambda v: v.state.last_changed)
    detail = "unavailable"
    if len(group) > 1:
        detail = f"{len(group)} entities unavailable: " + ", ".join(
            sorted(v.entity_id for v in group)
        )
    return HealthIssue("unavailable", "error", first.entity_id, detail, first.state.last_changed)


def check_unknown(views: list[EntityView]) -> list[HealthIssue]:
    return [
        HealthIssue("unknown_state", "info", v.entity_id, "state is unknown", v.state.last_changed)
        for v in views
        if v.state.state == "unknown" and v.domain not in STATELESS_DOMAINS
    ]


def check_battery(views: list[EntityView], threshold: float) -> list[HealthIssue]:
    issues = []
    for view in views:
        if view.state.attributes.get("device_class") != "battery":
            continue
        if view.domain == "binary_sensor":
            # binary battery sensors: on = low
            if view.state.state == "on":
                issues.append(
                    HealthIssue(
                        "low_battery",
                        "warning",
                        view.entity_id,
                        "battery low",
                        view.state.last_changed,
                    )
                )
            continue
        level = to_float(view.state.state)
        if level is not None and level < threshold:
            issues.append(
                HealthIssue(
                    "low_battery",
                    "error" if level < threshold / 2 else "warning",
                    view.entity_id,
                    f"battery {level:g}%",
                    view.state.last_changed,
                )
            )
    return issues


def check_stale(views: list[EntityView], now: datetime, stale_hours: float) -> list[HealthIssue]:
    """Measurement sensors that have not reported for too long (device silently dead)."""
    limit = now - timedelta(hours=stale_hours)
    issues = []
    for view in views:
        st = view.state
        if st.attributes.get("state_class") != "measurement" or to_float(st.state) is None:
            continue
        reported = st.last_reported or st.last_updated
        if reported < limit:
            hours = (now - reported).total_seconds() / 3600
            issues.append(
                HealthIssue(
                    "stale", "warning", view.entity_id, f"no report for {hours:.0f}h", reported
                )
            )
    return issues


def max_changes_per_hour(points: list[StatePoint]) -> tuple[int, datetime | None]:
    """Largest number of state changes inside any sliding 1-hour window, and its start."""
    ordered = sorted(points, key=lambda p: p.changed_at)
    changes = [
        p.changed_at for prev, p in zip(ordered, ordered[1:], strict=False) if p.state != prev.state
    ]
    best, best_start, lo = 0, None, 0
    for hi, moment in enumerate(changes):
        while moment - changes[lo] > timedelta(hours=1):
            lo += 1
        if hi - lo + 1 > best:
            best, best_start = hi - lo + 1, changes[lo]
    return best, best_start


def check_flapping(
    views: list[EntityView], history: dict[str, list[StatePoint]], threshold: int
) -> list[HealthIssue]:
    issues = []
    for view in views:
        count, started = max_changes_per_hour(history.get(view.entity_id, []))
        if count >= threshold:
            issues.append(
                HealthIssue(
                    "flapping",
                    "warning",
                    view.entity_id,
                    f"{count} state changes within one hour",
                    started,
                )
            )
    return issues


def run_health_checks(
    views: list[EntityView],
    history: dict[str, list[StatePoint]],
    now: datetime,
    thresholds: HealthThresholds,
) -> list[HealthIssue]:
    issues = [
        *check_unavailable(views),
        *check_unknown(views),
        *check_battery(views, thresholds.battery_low_percent),
        *check_stale(views, now, thresholds.stale_hours),
        *check_flapping(views, history, thresholds.flapping_changes_per_hour),
    ]
    return sorted(issues, key=lambda i: (SEVERITY_ORDER[i.severity], i.code, i.entity_id))
