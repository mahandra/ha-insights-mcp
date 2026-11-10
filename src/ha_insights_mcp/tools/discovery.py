"""Discovery tools: find entities, read one entity, list areas.

Tool docstrings and parameter descriptions are prompts for the LLM: they tell it when
to use a tool, what comes back and what to do next. Edit them with the same care as code.
"""

from collections import Counter
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ha_insights_mcp.context import AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.ha.models import ENTITY_ID_PATTERN, EntityState
from ha_insights_mcp.schemas import (
    AreaSummary,
    EntityDetails,
    EntitySummary,
    ListAreasResult,
    SearchEntitiesResult,
)
from ha_insights_mcp.snapshot import EntityView, HomeSnapshot
from ha_insights_mcp.tools.common import READ_ONLY

# Attributes worth showing per domain. Everything else (icons, supported_features bitmasks,
# entity_picture, internal ids, GPS coordinates, ...) is dropped to save tokens and privacy.
COMMON_ATTRIBUTES = ("device_class", "state_class")
DOMAIN_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "light": ("brightness", "color_mode", "color_temp_kelvin", "rgb_color", "effect"),
    "climate": (
        "hvac_action",
        "hvac_modes",
        "current_temperature",
        "temperature",
        "target_temp_low",
        "target_temp_high",
        "current_humidity",
        "humidity",
        "preset_mode",
        "fan_mode",
        "min_temp",
        "max_temp",
    ),
    "cover": ("current_position", "current_tilt_position"),
    "fan": ("percentage", "preset_mode", "oscillating", "direction"),
    "media_player": (
        "volume_level",
        "is_volume_muted",
        "media_title",
        "media_artist",
        "source",
        "app_name",
    ),
    "humidifier": ("humidity", "current_humidity", "mode"),
    "water_heater": ("temperature", "current_temperature", "operation_mode"),
    "vacuum": ("battery_level", "fan_speed", "status"),
    "weather": ("temperature", "humidity", "pressure", "wind_speed", "temperature_unit"),
    "sun": ("elevation", "next_rising", "next_setting"),
    "update": ("installed_version", "latest_version", "in_progress"),
    "input_number": ("min", "max", "step"),
    "number": ("min", "max", "step"),
    "input_select": ("options",),
    "select": ("options",),
}
MAX_LIST_ITEMS = 20


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Search entities", annotations=READ_ONLY)
    async def search_entities(
        ctx: Context[AppContext],
        query: Annotated[
            str,
            Field(
                description=(
                    "Case-insensitive text matched against entity_id and friendly name, "
                    "e.g. 'kitchen', 'temperature', 'light.bed'. All words must match. "
                    "Use an empty string to list everything that matches the filters."
                ),
                max_length=200,
            ),
        ],
        domain: Annotated[
            str | None,
            Field(description="Only this domain, e.g. 'light', 'sensor', 'climate', 'switch'"),
        ] = None,
        area: Annotated[
            str | None,
            Field(description="Only this area, by area_id or name (see list_areas)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum results")] = 25,
    ) -> SearchEntitiesResult:
        """Find Home Assistant entities and their current states.

        Use this first to discover entity_ids, then call get_entity_state for details.
        Returns entity_id, name, state and area for each match, best matches first.
        """
        with ha_errors_as_tool_errors():
            snapshot = await app(ctx).snapshots.get()
        candidates = filter_entities(snapshot, domain=domain, area=area)
        ranked = _rank(candidates, query)
        return SearchEntitiesResult(
            total=len(ranked),
            truncated=len(ranked) > limit,
            entities=[_summary(view) for view in ranked[:limit]],
        )

    @mcp.tool(title="Get entity state", annotations=READ_ONLY)
    async def get_entity_state(
        ctx: Context[AppContext],
        entity_id: Annotated[
            str,
            Field(
                description="Exact entity_id, e.g. 'climate.hvac'. Find it with search_entities.",
                pattern=ENTITY_ID_PATTERN,
                max_length=255,
            ),
        ],
    ) -> EntityDetails:
        """Get the live state of one entity with its area, device and relevant attributes.

        The state is read fresh from Home Assistant. Attributes are filtered to the ones
        meaningful for the entity's domain (e.g. temperatures and modes for climate).
        """
        context = app(ctx)
        with ha_errors_as_tool_errors():
            state = await context.client.get_state(entity_id)
            snapshot = await context.snapshots.get()
        view = snapshot.entities.get(entity_id)
        return EntityDetails(
            entity_id=state.entity_id,
            name=state.friendly_name or state.entity_id,
            domain=state.domain,
            state=state.state,
            unit=state.attributes.get("unit_of_measurement"),
            area=view.area.name if view and view.area else None,
            device=view.device.display_name if view and view.device else None,
            last_changed=state.last_changed,
            attributes=relevant_attributes(state),
        )

    @mcp.tool(title="List areas", annotations=READ_ONLY)
    async def list_areas(ctx: Context[AppContext]) -> ListAreasResult:
        """List all areas (rooms/zones) of the home with how many entities each contains.

        Use area_id or name from here to filter search_entities by area.
        """
        with ha_errors_as_tool_errors():
            snapshot = await app(ctx).snapshots.get()
        counts = Counter(v.area.area_id for v in snapshot.entities.values() if v.area)
        areas = sorted(snapshot.areas.values(), key=lambda a: a.name.casefold())
        return ListAreasResult(
            areas=[
                AreaSummary(area_id=a.area_id, name=a.name, entity_count=counts[a.area_id])
                for a in areas
            ],
            unassigned_entity_count=sum(1 for v in snapshot.entities.values() if v.area is None),
        )


def filter_entities(
    snapshot: HomeSnapshot, *, domain: str | None, area: str | None
) -> list[EntityView]:
    views = list(snapshot.entities.values())
    if domain:
        domain = domain.strip().lower()
        views = [v for v in views if v.domain == domain]
        if not views:
            known = sorted({v.domain for v in snapshot.entities.values()})
            raise ToolError(f"No entities in domain {domain!r}. Known domains: {', '.join(known)}")
    if area:
        found = snapshot.find_area(area)
        if found is None:
            known = ", ".join(sorted(a.name for a in snapshot.areas.values())) or "none"
            raise ToolError(f"Unknown area {area!r}. Known areas: {known}")
        views = [v for v in views if v.area and v.area.area_id == found.area_id]
    return views


def _rank(views: list[EntityView], query: str) -> list[EntityView]:
    """Keep views matching every query word; exact > prefix > substring, then by entity_id."""
    q = query.strip().casefold()
    words = q.split()

    def score(view: EntityView) -> int | None:
        eid, name = view.entity_id.casefold(), view.name.casefold()
        if not all(w in eid or w in name for w in words):
            return None
        if q in (eid, name):
            return 0
        if eid.startswith(q) or name.startswith(q) or eid.split(".", 1)[1].startswith(q):
            return 1
        return 2

    scored = [(s, v) for v in views if (s := score(v)) is not None]
    scored.sort(key=lambda sv: (sv[0], sv[1].entity_id))
    return [v for _, v in scored]


def _summary(view: EntityView) -> EntitySummary:
    return EntitySummary(
        entity_id=view.entity_id,
        name=view.name,
        state=view.state.state,
        area=view.area.name if view.area else None,
    )


def relevant_attributes(state: EntityState) -> dict[str, Any]:
    keys = COMMON_ATTRIBUTES + DOMAIN_ATTRIBUTES.get(state.domain, ())
    result: dict[str, Any] = {}
    for key in keys:
        value = state.attributes.get(key)
        if value is None:
            continue
        if isinstance(value, list) and len(value) > MAX_LIST_ITEMS:
            value = [*value[:MAX_LIST_ITEMS], f"... +{len(value) - MAX_LIST_ITEMS} more"]
        result[key] = value
    return result
