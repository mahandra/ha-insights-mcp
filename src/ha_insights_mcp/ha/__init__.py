"""Home Assistant client package (no MCP dependencies)."""

from ha_insights_mcp.ha.client import HAClient
from ha_insights_mcp.ha.errors import (
    HAAuthError,
    HAConnectionError,
    HAError,
    HANotFoundError,
    HAResponseError,
    HATimeoutError,
)

__all__ = [
    "HAAuthError",
    "HAClient",
    "HAConnectionError",
    "HAError",
    "HANotFoundError",
    "HAResponseError",
    "HATimeoutError",
]
