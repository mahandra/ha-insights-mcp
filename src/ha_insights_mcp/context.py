"""Objects shared by all MCP handlers for the lifetime of the server."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from ha_insights_mcp.ha import HAClient, HAError, HANotFoundError
from ha_insights_mcp.snapshot import SnapshotProvider


@dataclass(frozen=True, slots=True)
class AppContext:
    client: HAClient
    snapshots: SnapshotProvider
    # Injectable for deterministic tests of time-window analytics.
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


@contextmanager
def ha_errors_as_tool_errors() -> Iterator[None]:
    """Turn HA client errors into ToolError so the model reads an actionable message.

    Any other exception still surfaces as a generic tool failure, without internals.
    """
    try:
        yield
    except HANotFoundError as exc:
        raise ToolError(f"{exc}. Use search_entities to find valid entity_ids.") from None
    except HAError as exc:
        raise ToolError(str(exc)) from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
