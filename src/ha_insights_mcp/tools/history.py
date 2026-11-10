"""History analytics tools. Aggregation happens here, not in the LLM's context."""

from datetime import datetime, timedelta
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ha_insights_mcp.analytics.history import (
    count_changes,
    is_numeric_entity,
    statistics_period,
    summarize_discrete,
    summarize_numeric_from_history,
    summarize_numeric_from_statistics,
)
from ha_insights_mcp.context import AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.ha.models import ENTITY_ID_PATTERN, EntityState
from ha_insights_mcp.schemas import (
    ActivityEntry,
    ActivityResult,
    DiscreteHistory,
    HistoryBucket,
    HistorySummary,
    NumericHistory,
    StateChange,
)
from ha_insights_mcp.tools.common import READ_ONLY
from ha_insights_mcp.tools.discovery import filter_entities

MAX_HOURS = 168  # HA's recorder keeps 10 days by default; stay well inside it.
MAX_ACTIVITY_ENTITIES = 300

Hours = Annotated[
    int, Field(ge=1, le=MAX_HOURS, description="Look-back window in hours (max 168 = 7 days)")
]


def _window_end(context: AppContext) -> datetime:
    # Whole minutes keep bucket boundaries readable and cheap in tokens.
    return context.now().replace(second=0, microsecond=0)


def _is_numeric(state: EntityState) -> bool:
    return is_numeric_entity(state.domain, state.state, state.attributes.get("unit_of_measurement"))


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Summarize history", annotations=READ_ONLY)
    async def summarize_history(
        ctx: Context[AppContext],
        entity_id: Annotated[
            str,
            Field(
                description="Exact entity_id, e.g. 'sensor.outside_temperature'",
                pattern=ENTITY_ID_PATTERN,
                max_length=255,
            ),
        ],
        hours: Hours = 24,
        buckets: Annotated[
            int, Field(ge=1, le=48, description="Number of time buckets for numeric entities")
        ] = 12,
    ) -> HistorySummary:
        """Summarize how one entity behaved over a time window, computed server-side.

        Numeric entities (sensors, numbers): overall min/mean/max plus a bucketed series.
        Other entities (lights, switches, binary sensors, modes): number of changes, share
        of time in each state and the latest changes. Use it for questions like "how warm was
        it overnight", "how long was the light on today", "is this sensor jumping around".
        """
        context = app(ctx)
        end = _window_end(context)
        start = end - timedelta(hours=hours)
        with ha_errors_as_tool_errors():
            state = await context.client.get_state(entity_id)
            snapshot = await context.snapshots.get()
            numeric = _is_numeric(state)
            stats = (
                await context.client.get_statistics(
                    [entity_id], start, end, statistics_period(end - start)
                )
                if numeric
                else {}
            )
            points = (
                None
                if stats.get(entity_id)
                else (await context.client.get_history([entity_id], start, end)).get(entity_id, [])
            )

        view = snapshot.entities.get(entity_id)
        summary = HistorySummary(
            entity_id=entity_id,
            name=view.name if view else state.friendly_name or entity_id,
            unit=state.attributes.get("unit_of_measurement"),
            current_state=state.state,
            window_hours=hours,
            kind="numeric" if numeric else "discrete",
            numeric=None,
            discrete=None,
        )
        if numeric:
            result = (
                summarize_numeric_from_statistics(stats[entity_id], start, end, buckets)
                if points is None
                else summarize_numeric_from_history(points, start, end, buckets)
            )
            summary.numeric = NumericHistory(
                min=result.min,
                mean=result.mean,
                max=result.max,
                missing_pct=result.missing_pct,
                buckets=[
                    HistoryBucket(start=b.start, min=b.min, mean=b.mean, max=b.max)
                    for b in result.buckets
                ],
                source=result.source,
            )
        else:
            result = summarize_discrete(points or [], start, end)
            summary.discrete = DiscreteHistory(
                changes=result.changes,
                time_in_state_pct=result.time_in_state_pct,
                recent_changes=[
                    StateChange(at=p.changed_at, state=p.state) for p in result.recent_changes
                ],
            )
        return summary

    @mcp.tool(title="Rank activity", annotations=READ_ONLY)
    async def rank_activity(
        ctx: Context[AppContext],
        hours: Hours = 24,
        domain: Annotated[
            str | None, Field(description="Only this domain, e.g. 'light', 'binary_sensor'")
        ] = None,
        area: Annotated[str | None, Field(description="Only this area, by area_id or name")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum results")] = 10,
    ) -> ActivityResult:
        """Rank entities by how often their state changed in a time window.

        Answers "what was used the most", "which sensors are the busiest" and helps spot
        flapping devices. Numeric measurements (temperature, power, distance, ...) change
        constantly and are excluded unless you pass a domain.
        """
        context = app(ctx)
        end = _window_end(context)
        start = end - timedelta(hours=hours)
        with ha_errors_as_tool_errors():
            snapshot = await context.snapshots.get()
        candidates = filter_entities(snapshot, domain=domain, area=area)
        exclude_numeric = domain is None
        if exclude_numeric:
            candidates = [v for v in candidates if not _is_numeric(v.state)]
        if len(candidates) > MAX_ACTIVITY_ENTITIES:
            raise ToolError(
                f"{len(candidates)} entities match; narrow down with domain or area "
                f"(limit {MAX_ACTIVITY_ENTITIES})"
            )
        with ha_errors_as_tool_errors():
            history = await context.client.get_history(
                [v.entity_id for v in candidates], start, end
            )

        ranked = sorted(
            ((count_changes(history.get(v.entity_id, []), start, end), v) for v in candidates),
            key=lambda cv: (-cv[0], cv[1].entity_id),
        )
        return ActivityResult(
            window_hours=hours,
            considered=len(candidates),
            numeric_sensors_excluded=exclude_numeric,
            entities=[
                ActivityEntry(
                    entity_id=v.entity_id,
                    name=v.name,
                    area=v.area.name if v.area else None,
                    changes=changes,
                    last_changed=v.state.last_changed,
                )
                for changes, v in ranked[:limit]
                if changes > 0
            ],
        )
