"""Shared fixtures: realistic (trimmed) Home Assistant payloads and fakes."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
import respx
from mcp import Client
from pydantic import SecretStr

from ha_insights_mcp.ha import HAClient
from ha_insights_mcp.server import create_server

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
        self.url: str | None = None
        self._errors = errors or {}
        self._auth_ok = auth_ok
        self.sent: list[dict[str, Any]] = []
        self.connections = 0
        self.new_connection()

    def new_connection(self) -> None:
        self.connections += 1
        self._outbox: list[dict[str, Any]] = [{"type": "auth_required", "ha_version": "2026.10.4"}]

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
        if ws.url is not None:
            ws.new_connection()
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


@pytest.fixture
def ha_api():
    """Mocked HA REST API with STATES; tests can override routes."""
    with respx.mock(base_url=HA_URL, assert_all_called=False) as mock:
        mock.get("/api/config").respond(json=CONFIG)
        mock.get("/api/states").respond(json=STATES)
        for st in STATES:
            mock.get(f"/api/states/{st['entity_id']}").respond(json=st)
        mock.get(url__regex=r"/api/states/.+").respond(404, json={"message": "Entity not found."})
        yield mock


@asynccontextmanager
async def mcp_client(
    ws_results: dict[str, Any] | None = None, now: Callable[[], datetime] | None = None
) -> AsyncIterator[Client]:
    """In-memory MCP client -> server -> real HAClient -> fake WebSocket (+ respx REST)."""
    ws = FakeWebSocket({**REGISTRY_RESULTS, **(ws_results or {})})
    server = create_server(
        client_factory=lambda: HAClient(
            HA_URL, TOKEN, timeout=2, ws_connect_factory=fake_ws_factory(ws)
        ),
        cache_ttl=10,
        now=now,
    )
    async with Client(server) as client:
        yield client


async def call(client: Client, tool: str, **args: Any) -> Any:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


def error_text(result: Any) -> str:
    assert result.is_error
    return result.content[0].text
