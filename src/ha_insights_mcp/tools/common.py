"""Shared bits for tool modules."""

from mcp.types import ToolAnnotations

# Hints for clients: nothing here changes HA, and the set of entities is closed (not the web).
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
