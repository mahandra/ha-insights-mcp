"""MCP server assembly: lifespan, tools and resources."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from ha_insights_mcp import __version__
from ha_insights_mcp.config import Settings
from ha_insights_mcp.context import AppContext
from ha_insights_mcp.ha import HAClient, HAError
from ha_insights_mcp.resources import register_resources
from ha_insights_mcp.snapshot import SnapshotProvider
from ha_insights_mcp.tools import register_tools

logger = logging.getLogger(__name__)

# Sent to the client at initialization; most clients put it into the model's context.
INSTRUCTIONS = """\
Read-only insights into a Home Assistant home.
Typical flow: list_areas or search_entities to discover entity_ids, then get_entity_state
for details. The resource ha://home/summary gives a compact overview of the whole home.
This server cannot change device states.
"""


def create_server(client_factory: Callable[[], HAClient], cache_ttl: float) -> MCPServer:
    client = client_factory()
    app_context = AppContext(client=client, snapshots=SnapshotProvider(client, ttl=cache_ttl))

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppContext]:
        async with client:
            try:
                config = await client.get_config()
                logger.info("Connected to Home Assistant %s at %s", config.version, client.base_url)
            except HAError as exc:
                # Do not fail startup: tools will report the problem to the model.
                logger.warning("Home Assistant check failed: %s", exc)
            yield app_context

    mcp = MCPServer(
        "ha-insights",
        title="HA Insights",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    register_tools(mcp)
    register_resources(mcp, app_context)
    return mcp


def server_from_settings(settings: Settings) -> MCPServer:
    return create_server(
        client_factory=lambda: HAClient(
            settings.ha_url, settings.ha_token, timeout=settings.ha_timeout_seconds
        ),
        cache_ttl=settings.ha_cache_ttl_seconds,
    )
