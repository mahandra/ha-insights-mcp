"""MCP tools, grouped by theme. All tools are read-only."""

from mcp.server.mcpserver import MCPServer

from ha_insights_mcp.tools import automations, discovery, health, history


def register_tools(mcp: MCPServer) -> None:
    for module in (discovery, history, health, automations):
        module.register(mcp)
