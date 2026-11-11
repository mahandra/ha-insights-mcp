"""What a service call should do to an entity, and whether it did.

Pure. Used by preview (current -> expected) and by execute (verify the state after the
call: devices can ignore commands, jam or still be moving).
"""

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from ha_insights_mcp.ha.models import EntityState

Verification = Literal["confirmed", "pending", "not_confirmed", "not_checked"]

# States that mean "on its way": the call worked, the device has not finished yet.
TRANSITIONAL_STATES = frozenset({"opening", "closing", "locking", "unlocking"})

_FIXED_STATE: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
}
_TOGGLED = {"on": "off", "off": "on"}
# (service, data field) -> attribute that should equal the field value afterwards.
_ATTRIBUTE_TARGETS: dict[tuple[str, str], str] = {
    ("climate.set_temperature", "temperature"): "temperature",
    ("cover.set_cover_position", "position"): "current_position",
    ("fan.set_percentage", "percentage"): "percentage",
}
# (service, data field) whose value is the expected state.
_STATE_FROM_FIELD: dict[str, str] = {"climate.set_hvac_mode": "hvac_mode"}


@dataclass(frozen=True, slots=True)
class Expectation:
    state: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


def expect(service: str, data: dict[str, Any], current: EntityState) -> Expectation | None:
    """The observable result of `service` on `current`; None if we cannot tell."""
    name = service.partition(".")[2]
    state: str | None = _FIXED_STATE.get(name)
    if name == "toggle":
        state = _TOGGLED.get(current.state)
    if service in _STATE_FROM_FIELD:
        value = data.get(_STATE_FROM_FIELD[service])
        state = str(value) if value is not None else None
    attributes = {
        attr: data[key]
        for (svc, key), attr in _ATTRIBUTE_TARGETS.items()
        if svc == service and key in data
    }
    if state is None and not attributes:
        return None
    return Expectation(state=state, attributes=attributes)


def _same(actual: Any, wanted: Any) -> bool:
    if isinstance(wanted, int | float) and isinstance(actual, int | float):
        return math.isclose(actual, wanted, abs_tol=0.01)
    return actual == wanted


def is_met(expectation: Expectation, state: EntityState) -> bool:
    if expectation.state is not None and state.state != expectation.state:
        return False
    return all(_same(state.attributes.get(k), v) for k, v in expectation.attributes.items())


def verify(expectation: Expectation | None, after: EntityState | None) -> Verification:
    if expectation is None or after is None:
        return "not_checked"
    if is_met(expectation, after):
        return "confirmed"
    if after.state in TRANSITIONAL_STATES and after.state != expectation.state:
        return "pending"
    return "not_confirmed"


def describe(expectation: Expectation | None, current: EntityState) -> str | None:
    """Human-readable planned change, e.g. 'state off -> on, temperature 21 -> 24'."""
    if expectation is None:
        return None
    parts = []
    if expectation.state is not None:
        parts.append(f"state {current.state} -> {expectation.state}")
    for attr, value in expectation.attributes.items():
        parts.append(f"{attr} {current.attributes.get(attr)} -> {value}")
    return ", ".join(parts)
