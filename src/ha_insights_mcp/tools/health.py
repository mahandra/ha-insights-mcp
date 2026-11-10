"""Device health tool."""

from collections import Counter
from datetime import timedelta
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from ha_insights_mcp.analytics.health import (
    SEVERITY_ORDER,
    STATELESS_DOMAINS,
    HealthThresholds,
    run_health_checks,
)
from ha_insights_mcp.analytics.history import is_numeric_entity
from ha_insights_mcp.context import AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.schemas import HealthIssueOut, HealthReport, RepairIssueOut
from ha_insights_mcp.snapshot import HomeSnapshot
from ha_insights_mcp.tools.common import READ_ONLY
from ha_insights_mcp.tools.discovery import filter_entities

FLAPPING_LOOKBACK = timedelta(hours=24)
MAX_FLAPPING_ENTITIES = 500


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Check device health", annotations=READ_ONLY)
    async def check_device_health(
        ctx: Context[AppContext],
        area: Annotated[str | None, Field(description="Only this area, by area_id or name")] = None,
        domain: Annotated[str | None, Field(description="Only this domain, e.g. 'sensor'")] = None,
        min_severity: Annotated[
            Literal["error", "warning", "info"],
            Field(description="Hide less severe issues; 'info' also lists unknown states"),
        ] = "warning",
        battery_threshold: Annotated[
            float | None,
            Field(ge=1, le=100, description="Low-battery threshold in percent (default 20)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum issues")] = 50,
    ) -> HealthReport:
        """Find devices that need attention: unavailable (offline) devices, low batteries,
        sensors that stopped reporting (stale), flapping entities (too many state changes
        within an hour over the last 24h), plus problems Home Assistant itself raised
        (Repairs).

        Use it for "is anything broken", "which batteries should I replace", or before
        trusting a sensor's value. Follow up with summarize_history on flapping entities.
        """
        context = app(ctx)
        thresholds = context.thresholds
        if battery_threshold is not None:
            thresholds = HealthThresholds(
                battery_low_percent=battery_threshold,
                stale_hours=thresholds.stale_hours,
                flapping_changes_per_hour=thresholds.flapping_changes_per_hour,
            )
        now = context.now()

        await ctx.report_progress(0, 3, "Reading states and registries")
        with ha_errors_as_tool_errors():
            snapshot = await context.snapshots.get()
        views = filter_entities(snapshot, domain=domain, area=area)

        # Flapping only makes sense for discrete entities; numeric sensors change by design.
        flap_candidates = [
            v.entity_id
            for v in views
            if v.domain not in STATELESS_DOMAINS
            and not is_numeric_entity(
                v.domain, v.state.state, v.state.attributes.get("unit_of_measurement")
            )
        ]
        skipped = []
        history = {}
        await ctx.report_progress(1, 3, f"Reading 24h history of {len(flap_candidates)} entities")
        if len(flap_candidates) > MAX_FLAPPING_ENTITIES:
            skipped.append(
                f"flapping: {len(flap_candidates)} entities, narrow down with area or domain"
            )
        else:
            with ha_errors_as_tool_errors():
                history = await context.client.get_history(
                    flap_candidates, now - FLAPPING_LOOKBACK, now
                )

        await ctx.report_progress(2, 3, "Reading Home Assistant repairs")
        with ha_errors_as_tool_errors():
            repairs = [r for r in await context.client.list_repair_issues() if not r.ignored]

        issues = run_health_checks(views, history, now, thresholds)
        await ctx.report_progress(3, 3, f"Found {len(issues)} issues")

        visible = [i for i in issues if SEVERITY_ORDER[i.severity] <= SEVERITY_ORDER[min_severity]]
        counts = Counter(i.severity for i in issues)
        return HealthReport(
            counts={s: counts.get(s, 0) for s in SEVERITY_ORDER},
            issues=[
                HealthIssueOut(
                    code=i.code,
                    severity=i.severity,
                    entity_id=i.entity_id,
                    name=snapshot.entities[i.entity_id].name,
                    device=_device_name(snapshot, i.entity_id),
                    area=_area_name(snapshot, i.entity_id),
                    detail=i.detail,
                    since=i.since,
                )
                for i in visible[:limit]
            ],
            truncated=len(visible) > limit,
            ha_repairs=[
                RepairIssueOut(
                    domain=r.domain, issue=r.translation_key or r.issue_id, severity=r.severity
                )
                for r in repairs
            ],
            checks_skipped=skipped,
        )


def _device_name(snapshot: HomeSnapshot, entity_id: str) -> str | None:
    device = snapshot.entities[entity_id].device
    return device.display_name if device else None


def _area_name(snapshot: HomeSnapshot, entity_id: str) -> str | None:
    area = snapshot.entities[entity_id].area
    return area.name if area else None
