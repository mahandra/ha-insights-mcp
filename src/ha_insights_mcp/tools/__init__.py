"""MCP tools, grouped by theme. All tools are read-only."""

from mcp.server.mcpserver import MCPServer

from ha_insights_mcp.tools import discovery, history


def register_tools(mcp: MCPServer) -> None:
    for module in (discovery, history):
        module.register(mcp)
