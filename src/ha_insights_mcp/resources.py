"""MCP resources: read-only context the client application can attach for the model."""

from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from ha_insights_mcp.context import AppContext
from ha_insights_mcp.ha import HAError
from ha_insights_mcp.snapshot import HomeSnapshot

HOME_SUMMARY_URI = "ha://home/summary"


def register_resources(mcp: MCPServer, context: AppContext) -> None:
    # Static resources (URI without template variables) cannot receive a Context in SDK v2,
    # so the shared AppContext is captured by closure instead.
    @mcp.resource(HOME_SUMMARY_URI, title="Home summary", mime_type="text/markdown")
    async def home_summary() -> str:
        """Compact overview of the home: areas with entity counts and entities per domain."""
        try:
            config = await context.client.get_config()
            snapshot = await context.snapshots.get()
        except HAError as exc:
            raise ResourceError(str(exc)) from None
        return render_summary(snapshot, config.location_name, config.version)


def render_summary(snapshot: HomeSnapshot, location: str | None, version: str) -> str:
    entities = snapshot.entities.values()
    by_area = Counter(v.area.area_id for v in entities if v.area)
    by_domain = Counter(v.domain for v in entities)
    unavailable = sum(1 for v in entities if v.state.state == "unavailable")

    lines = [
        f"# {location or 'Home'}",
        f"Home Assistant {version}. {len(snapshot.entities)} entities, "
        f"{len(snapshot.areas)} areas, {unavailable} unavailable.",
        "",
        "## Areas (entities)",
    ]
    for area in sorted(snapshot.areas.values(), key=lambda a: a.name.casefold()):
        lines.append(f"- {area.name} (`{area.area_id}`): {by_area[area.area_id]}")
    lines.append(f"- No area: {sum(1 for v in entities if v.area is None)}")
    lines += ["", "## Domains (entities)"]
    lines += [f"- {domain}: {count}" for domain, count in sorted(by_domain.items())]
    return "\n".join(lines) + "\n"
