import httpx
import pytest
import respx

from ha_insights_mcp.ha import (
    HAAuthError,
    HAClient,
    HAConnectionError,
    HANotFoundError,
    HAResponseError,
    HATimeoutError,
)
from tests.conftest import (
    CONFIG,
    HA_URL,
    REGISTRY_RESULTS,
    STATES,
    TOKEN,
    FakeWebSocket,
    fake_ws_factory,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(fake_ws: FakeWebSocket):
    async with HAClient(HA_URL, TOKEN, timeout=2, ws_connect_factory=fake_ws_factory(fake_ws)) as c:
        yield c


# ---------------------------------------------------------------- REST


@respx.mock
async def test_get_states_parses_and_sends_bearer_token(client: HAClient) -> None:
    route = respx.get(f"{HA_URL}/api/states").respond(json=STATES)

    states = await client.get_states()

    assert [s.entity_id for s in states][:2] == ["light.kitchen_lights", "light.bed_light"]
    assert states[0].domain == "light"
    assert states[0].friendly_name == "Kitchen Lights"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token-123"


@respx.mock
async def test_get_config(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/config").respond(json=CONFIG)
    config = await client.get_config()
    assert config.version == "2026.10.4"


@respx.mock
async def test_get_state_found(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states/climate.hvac").respond(json=STATES[3])
    st = await client.get_state("climate.hvac")
    assert st.state == "cool"


@respx.mock
async def test_get_state_not_found(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states/light.nope").respond(404, json={"message": "Entity not found."})
    with pytest.raises(HANotFoundError, match="light.nope"):
        await client.get_state("light.nope")


@pytest.mark.parametrize(
    "bad", ["", "light", "light.", "../api/config", "light.x/../../y", "LIGHT.X"]
)
async def test_get_state_rejects_malformed_entity_id(client: HAClient, bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid entity_id"):
        await client.get_state(bad)


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_does_not_leak_token(client: HAClient, status: int) -> None:
    respx.get(f"{HA_URL}/api/states").respond(status)
    with pytest.raises(HAAuthError, match="HA_TOKEN") as exc_info:
        await client.get_states()
    assert TOKEN.get_secret_value() not in str(exc_info.value)


@respx.mock
async def test_connection_refused(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(HAConnectionError, match="Cannot connect.*HA_URL"):
        await client.get_states()


@respx.mock
async def test_timeout(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(HATimeoutError, match="within 2s"):
        await client.get_states()


@respx.mock
async def test_server_error(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states").respond(500, text="boom")
    with pytest.raises(HAResponseError, match="HTTP 500"):
        await client.get_states()


@respx.mock
async def test_invalid_json(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states").respond(200, text="<html>proxy page</html>")
    with pytest.raises(HAResponseError, match="invalid JSON"):
        await client.get_states()


@respx.mock
async def test_unexpected_payload_shape(client: HAClient) -> None:
    respx.get(f"{HA_URL}/api/states").respond(json=[{"entity_id": "light.x"}])
    with pytest.raises(HAResponseError, match="Unexpected state payload"):
        await client.get_states()


# ----------------------------------------------------------- WebSocket


async def test_get_registries(client: HAClient, fake_ws: FakeWebSocket) -> None:
    registries = await client.get_registries()

    assert fake_ws.url == "ws://ha.test:8123/api/websocket"
    assert fake_ws.sent[0] == {"type": "auth", "access_token": "test-token-123"}
    assert [m["type"] for m in fake_ws.sent[1:]] == list(REGISTRY_RESULTS)
    assert [a.area_id for a in registries.areas] == ["kitchen", "bedroom", "garage"]
    assert registries.devices[1].display_name == "Main HVAC"
    assert registries.entities[1].area_id == "bedroom"


async def test_ws_url_uses_wss_for_https(fake_ws: FakeWebSocket) -> None:
    async with HAClient(
        "https://ha.example.com", TOKEN, ws_connect_factory=fake_ws_factory(fake_ws)
    ) as c:
        await c.get_registries()
    assert fake_ws.url == "wss://ha.example.com/api/websocket"


async def test_ws_auth_invalid() -> None:
    ws = FakeWebSocket(REGISTRY_RESULTS, auth_ok=False)
    async with HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)) as c:
        with pytest.raises(HAAuthError, match="HA_TOKEN"):
            await c.get_registries()


async def test_ws_non_admin_token() -> None:
    ws = FakeWebSocket(
        REGISTRY_RESULTS,
        errors={"config/area_registry/list": {"code": "unauthorized", "message": "Unauthorized"}},
    )
    async with HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)) as c:
        with pytest.raises(HAAuthError, match="admin"):
            await c.get_registries()


async def test_ws_command_failure() -> None:
    ws = FakeWebSocket(
        REGISTRY_RESULTS,
        errors={"config/device_registry/list": {"code": "unknown_command", "message": "Unknown"}},
    )
    async with HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)) as c:
        with pytest.raises(HAResponseError, match="unknown_command"):
            await c.get_registries()


async def test_ws_connection_refused() -> None:
    def refusing(url: str):
        raise ConnectionRefusedError(111, "Connection refused")

    async with HAClient(HA_URL, TOKEN, ws_connect_factory=refusing) as c:
        with pytest.raises(HAConnectionError, match="Cannot connect"):
            await c.get_registries()
