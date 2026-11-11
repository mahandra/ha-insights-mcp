"""Read-only MCP tools, grouped by theme. Guarded actions (tools.actions) are registered
separately, only when enabled."""

from mcp.server.mcpserver import MCPServer

from ha_insights_mcp.tools import automations, discovery, health, history


def register_tools(mcp: MCPServer) -> None:
    for module in (discovery, history, health, automations):
        module.register(mcp)
