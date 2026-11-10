"""Server-level contract: what an MCP client sees at connect time."""

import pytest
from mcp import Client

from tests.conftest import mcp_client

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "search_entities",
    "get_entity_state",
    "list_areas",
    "summarize_history",
    "rank_activity",
    "check_device_health",
    "list_automations",
    "get_automation",
    "audit_automations",
}


@pytest.fixture
async def client(ha_api):
    async with mcp_client() as c:
        yield c


async def test_all_tools_are_read_only_with_output_schema(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.output_schema is not None, tool.name
        assert tool.description and len(tool.description) > 40, tool.name


async def test_ctx_is_not_exposed_in_input_schemas(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert "ctx" not in tool.input_schema.get("properties", {}), tool.name
