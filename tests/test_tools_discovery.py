"""End-to-end discovery tool tests: in-memory MCP Client -> server -> real HAClient -> mocked HA."""

import httpx
import pytest
from mcp import Client

from tests.conftest import STATES, TOKEN, call, error_text, mcp_client

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(ha_api):
    async with mcp_client() as c:
        yield c


# ------------------------------------------------------------ search_entities


async def test_search_by_text(client: Client) -> None:
    out = await call(client, "search_entities", query="kitchen")
    assert out == {
        "total": 1,
        "truncated": False,
        "entities": [
            {
                "entity_id": "light.kitchen_lights",
                "name": "Kitchen Lights",
                "state": "on",
                "area": "Kitchen",
            }
        ],
    }


async def test_search_all_words_must_match(client: Client) -> None:
    out = await call(client, "search_entities", query="outside temp")
    assert [e["entity_id"] for e in out["entities"]] == ["sensor.outside_temperature"]


async def test_search_ranks_prefix_before_substring(client: Client) -> None:
    out = await call(client, "search_entities", query="bed")
    # "Bed Light" (name prefix) before nothing else; kitchen/hvac do not match.
    assert [e["entity_id"] for e in out["entities"]] == ["light.bed_light"]
    out = await call(client, "search_entities", query="light")
    assert [e["entity_id"] for e in out["entities"]] == ["light.bed_light", "light.kitchen_lights"]


async def test_search_by_domain_and_area_name(client: Client) -> None:
    out = await call(client, "search_entities", query="", domain="light", area="bedroom")
    assert [e["entity_id"] for e in out["entities"]] == ["light.bed_light"]
    # Area inherited from the device.
    out = await call(client, "search_entities", query="", area="Bedroom")
    assert {e["entity_id"] for e in out["entities"]} == {"light.bed_light", "climate.hvac"}


async def test_search_truncation(client: Client) -> None:
    out = await call(client, "search_entities", query="", limit=2)
    assert out["total"] == len(STATES)
    assert out["truncated"] is True
    assert len(out["entities"]) == 2


async def test_search_unknown_area_lists_known_ones(client: Client) -> None:
    result = await client.call_tool("search_entities", {"query": "", "area": "attic"})
    assert "Known areas: Bedroom, Garage, Kitchen" in error_text(result)


async def test_search_unknown_domain_lists_known_ones(client: Client) -> None:
    result = await client.call_tool("search_entities", {"query": "", "domain": "lamp"})
    assert "Known domains: climate, light, sensor, sun" in error_text(result)


async def test_search_rejects_limit_out_of_range(client: Client) -> None:
    result = await client.call_tool("search_entities", {"query": "", "limit": 1000})
    assert result.is_error


# ----------------------------------------------------------- get_entity_state


async def test_get_entity_state_filters_attributes(client: Client) -> None:
    out = await call(client, "get_entity_state", entity_id="climate.hvac")
    assert out["state"] == "cool"
    assert out["area"] == "Bedroom"
    assert out["device"] == "Main HVAC"
    assert out["attributes"] == {
        "hvac_modes": ["off", "heat", "cool", "auto"],
        "current_temperature": 22,
        "temperature": 21,
        "fan_mode": "on_high",
        "min_temp": 7,
        "max_temp": 35,
    }
    # Noise is dropped.
    assert "supported_features" not in out["attributes"]
    assert "fan_modes" not in out["attributes"]


async def test_get_entity_state_sensor_unit(client: Client) -> None:
    out = await call(client, "get_entity_state", entity_id="sensor.outside_temperature")
    assert out["unit"] == "°C"
    assert out["attributes"] == {"device_class": "temperature", "state_class": "measurement"}
    assert out["area"] is None


async def test_get_entity_state_not_found_hints_search(client: Client) -> None:
    result = await client.call_tool("get_entity_state", {"entity_id": "light.nope"})
    text = error_text(result)
    assert "not found" in text and "search_entities" in text


@pytest.mark.parametrize("bad", ["../api/config", "Light.X", "light"])
async def test_get_entity_state_rejects_malformed_id(client: Client, bad: str) -> None:
    result = await client.call_tool("get_entity_state", {"entity_id": bad})
    assert result.is_error


# ----------------------------------------------------------------- list_areas


async def test_list_areas_counts(client: Client) -> None:
    out = await call(client, "list_areas")
    assert out == {
        "areas": [
            {"area_id": "bedroom", "name": "Bedroom", "entity_count": 2},
            {"area_id": "garage", "name": "Garage", "entity_count": 0},
            {"area_id": "kitchen", "name": "Kitchen", "entity_count": 1},
        ],
        "unassigned_entity_count": 2,
    }


# --------------------------------------------------------------- HA failures


async def test_ha_auth_failure_reaches_model_without_token(client: Client, ha_api) -> None:
    ha_api.get("/api/states").respond(401)
    result = await client.call_tool("list_areas", {})
    text = error_text(result)
    assert "rejected the access token" in text
    assert TOKEN.get_secret_value() not in text


async def test_ha_down_reaches_model(client: Client, ha_api) -> None:
    ha_api.get("/api/states").mock(side_effect=httpx.ConnectError("refused"))
    result = await client.call_tool("search_entities", {"query": "x"})
    assert "Cannot connect to Home Assistant" in error_text(result)


# ------------------------------------------------------------------ resources


async def test_home_summary_resource_is_listed(client: Client) -> None:
    resources = (await client.list_resources()).resources
    assert [(str(r.uri), r.mime_type) for r in resources] == [
        ("ha://home/summary", "text/markdown")
    ]


async def test_home_summary_resource_content(client: Client) -> None:
    result = await client.read_resource("ha://home/summary")
    text = result.contents[0].text
    assert text == (
        "# Dev Home\n"
        "Home Assistant 2026.10.4. 5 entities, 3 areas, 0 unavailable.\n"
        "\n"
        "## Areas (entities)\n"
        "- Bedroom (`bedroom`): 2\n"
        "- Garage (`garage`): 0\n"
        "- Kitchen (`kitchen`): 1\n"
        "- No area: 2\n"
        "\n"
        "## Domains (entities)\n"
        "- climate: 1\n"
        "- light: 2\n"
        "- sensor: 1\n"
        "- sun: 1\n"
    )
