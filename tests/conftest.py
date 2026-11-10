"""Shared fixtures: realistic (trimmed) Home Assistant payloads and fakes."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import SecretStr

HA_URL = "http://ha.test:8123"
TOKEN = SecretStr("test-token-123")

TS = "2026-11-01T10:00:00.000000+00:00"


def state(entity_id: str, value: str, **attributes: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": attributes,
        "last_changed": TS,
        "last_updated": TS,
        "context": {"id": "01J", "parent_id": None, "user_id": None},
    }


STATES = [
    state(
        "light.kitchen_lights",
        "on",
        friendly_name="Kitchen Lights",
        brightness=180,
        color_mode="hs",
        hs_color=[345, 75],
        supported_color_modes=["hs", "color_temp"],
        supported_features=40,
        icon="mdi:lightbulb",
    ),
    state("light.bed_light", "off", friendly_name="Bed Light", supported_features=40),
    state(
        "sensor.outside_temperature",
        "15.6",
        friendly_name="Outside Temperature",
        unit_of_measurement="°C",
        device_class="temperature",
        state_class="measurement",
    ),
    state(
        "climate.hvac",
        "cool",
        friendly_name="Hvac",
        current_temperature=22,
        temperature=21,
        hvac_modes=["off", "heat", "cool", "auto"],
        min_temp=7,
        max_temp=35,
        fan_mode="on_high",
        fan_modes=["on_low", "on_high", "auto_low", "auto_high", "off"],
        supported_features=441,
    ),
    state("sun.sun", "above_horizon", friendly_name="Sun", elevation=21.3),
]

AREAS = [
    {"area_id": "kitchen", "name": "Kitchen", "floor_id": None, "aliases": [], "icon": None},
    {"area_id": "bedroom", "name": "Bedroom", "floor_id": None, "aliases": [], "icon": None},
    {"area_id": "garage", "name": "Garage", "floor_id": None, "aliases": [], "icon": None},
]

DEVICES = [
    {
        "id": "dev_kitchen",
        "name": "Kitchen Lights",
        "name_by_user": None,
        "area_id": "kitchen",
        "manufacturer": "Philips",
        "model": "Hue",
        "disabled_by": None,
    },
    {
        "id": "dev_hvac",
        "name": "Hvac",
        "name_by_user": "Main HVAC",
        "area_id": "bedroom",
        "manufacturer": "Demo",
        "model": None,
        "disabled_by": None,
    },
]

ENTITY_REGISTRY = [
    {"entity_id": "light.kitchen_lights", "device_id": "dev_kitchen", "area_id": None},
    # Entity-level area overrides the device area.
    {"entity_id": "light.bed_light", "device_id": None, "area_id": "bedroom"},
    {"entity_id": "climate.hvac", "device_id": "dev_hvac", "area_id": None},
    {"entity_id": "sensor.outside_temperature", "device_id": None, "area_id": None},
]

CONFIG = {"version": "2026.10.4", "location_name": "Dev Home", "unit_system": {}}


@dataclass
class FakeError:
    """Return this from a FakeWebSocket handler to make one command fail."""

    code: str
    message: str = ""


class FakeWebSocket:
    """Scripted HA WebSocket: answers auth and commands from a mapping.

    A mapping value is either the result itself or a callable taking the sent command
    (dict) and returning the result or a FakeError.
    """

    def __init__(
        self,
        results: dict[str, Any],
        *,
        auth_ok: bool = True,
        errors: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._results = results
        self._errors = errors or {}
        self._auth_ok = auth_ok
        self._outbox: list[dict[str, Any]] = [{"type": "auth_required", "ha_version": "2026.10.4"}]
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        data = json.loads(message)
        self.sent.append(data)
        if data["type"] == "auth":
            self._outbox.append({"type": "auth_ok" if self._auth_ok else "auth_invalid"})
            return
        command = data["type"]
        # An unrelated message before the answer, to exercise id matching.
        self._outbox.append({"id": 999, "type": "event", "event": {}})
        reply: dict[str, Any] = {"id": data["id"], "type": "result"}
        result = self._results.get(command)
        if callable(result):
            result = result(data)
        if isinstance(result, FakeError):
            reply |= {"success": False, "error": {"code": result.code, "message": result.message}}
        elif command in self._errors:
            reply |= {"success": False, "error": self._errors[command]}
        else:
            reply |= {"success": True, "result": result}
        self._outbox.append(reply)

    async def recv(self) -> str:
        return json.dumps(self._outbox.pop(0))


def fake_ws_factory(ws: FakeWebSocket):
    @asynccontextmanager
    async def connect(url: str):
        ws.url = url
        yield ws

    return connect


REGISTRY_RESULTS = {
    "config/area_registry/list": AREAS,
    "config/device_registry/list": DEVICES,
    "config/entity_registry/list": ENTITY_REGISTRY,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_ws() -> FakeWebSocket:
    return FakeWebSocket(REGISTRY_RESULTS)
