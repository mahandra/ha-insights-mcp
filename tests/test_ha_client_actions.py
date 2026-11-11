"""HA client write path (call_service over WebSocket) and service schemas."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from ha_insights_mcp.ha import (
    HAClient,
    HAConnectionError,
    HANotFoundError,
    HAResponseError,
    HATimeoutError,
)
from tests.conftest import HA_URL, TOKEN, FakeError, FakeWebSocket, fake_ws_factory

pytestmark = pytest.mark.anyio

OK = {"context": {"id": "01CTX", "parent_id": None, "user_id": "u1"}}


def client_with(result) -> tuple[HAClient, FakeWebSocket]:
    ws = FakeWebSocket({"call_service": result})
    return HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)), ws


async def test_call_service_sends_targets_and_data() -> None:
    client, ws = client_with(OK)
    async with client:
        context_id = await client.call_service(
            "light", "turn_on", ["light.bed_light"], {"brightness_pct": 40}
        )
    assert context_id == "01CTX"
    command = ws.sent[1]
    assert command["type"] == "call_service"
    assert (command["domain"], command["service"]) == ("light", "turn_on")
    assert command["service_data"] == {"brightness_pct": 40}
    assert command["target"] == {"entity_id": ["light.bed_light"]}


@pytest.mark.parametrize(
    ("domain", "service", "ids", "data"),
    [
        ("light", "turn_on/../x", ["light.a"], {}),
        ("Light", "turn_on", ["light.a"], {}),
        ("light", "turn_on", [], {}),
        ("light", "turn_on", ["all"], {}),
        # Targets in data would be merged by HA and widen the action.
        ("light", "turn_on", ["light.a"], {"entity_id": "all"}),
        ("light", "turn_on", ["light.a"], {"area_id": "kitchen"}),
    ],
)
async def test_call_service_validates_before_sending(domain, service, ids, data) -> None:
    client, ws = client_with(OK)
    async with client:
        with pytest.raises(ValueError):
            await client.call_service(domain, service, ids, data)
    assert ws.sent == []


async def test_call_service_invalid_data_shows_ha_message() -> None:
    client, _ = client_with(FakeError("invalid_format", "value must be at most 100"))
    async with client:
        with pytest.raises(HAResponseError, match="at most 100"):
            await client.call_service("light", "turn_on", ["light.a"], {"brightness_pct": 400})


async def test_call_service_unknown_service() -> None:
    client, _ = client_with(FakeError("not_found", "Service light.blink not found."))
    async with client:
        with pytest.raises(HANotFoundError, match=r"light\.blink"):
            await client.call_service("light", "blink", ["light.a"], {})


async def test_call_service_timeout_is_ambiguous() -> None:
    @asynccontextmanager
    async def hanging(url: str):
        await asyncio.sleep(10)
        yield

    client = HAClient(HA_URL, TOKEN, timeout=0.05, ws_connect_factory=hanging)
    async with client:
        with pytest.raises(HATimeoutError, match="may or may not have been applied"):
            await client.call_service("lock", "unlock", ["lock.front_door"], {})


async def test_call_service_connection_error() -> None:
    @asynccontextmanager
    async def refused(url: str):
        raise ConnectionRefusedError
        yield

    client = HAClient(HA_URL, TOKEN, ws_connect_factory=refused)
    async with client:
        with pytest.raises(HAConnectionError):
            await client.call_service("light", "turn_on", ["light.a"], {})


async def test_get_services() -> None:
    services = {"light": {"turn_on": {"fields": {}}}}
    ws = FakeWebSocket({"get_services": services})
    async with HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)) as client:
        assert await client.get_services() == services
    assert ws.sent[1]["type"] == "get_services"
