"""Automation tools: list, inspect, audit."""

from collections import Counter, defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, TypeAdapter, ValidationError

from ha_insights_mcp.analytics.audit import (
    AutomationInfo,
    audit_all,
    extract_references,
    latest_run,
    run_result,
)
from ha_insights_mcp.analytics.health import SEVERITY_ORDER
from ha_insights_mcp.context import AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.ha.models import AutomationTrace
from ha_insights_mcp.schemas import (
    AuditFinding,
    AuditReport,
    AutomationDetails,
    AutomationList,
    AutomationRun,
    AutomationSummary,
)
from ha_insights_mcp.snapshot import HomeSnapshot
from ha_insights_mcp.tools.common import READ_ONLY

AUTOMATION_ID_PATTERN = r"^automation\.[a-z0-9_]+$"
RECENT_RUNS = 5
_datetime = TypeAdapter(datetime)


def _parse_dt(value: Any) -> datetime | None:
    try:
        return _datetime.validate_python(value) if value else None
    except ValidationError:
        return None


async def load_automations(
    context: AppContext, entity_ids: list[str] | None = None, *, with_configs: bool
) -> tuple[HomeSnapshot, list[AutomationInfo]]:
    """Join automation states, (optionally) configs and stored traces."""
    with ha_errors_as_tool_errors():
        snapshot = await context.snapshots.get()
        views = sorted(
            (v for v in snapshot.entities.values() if v.domain == "automation"),
            key=lambda v: v.entity_id,
        )
        if entity_ids is not None:
            views = [v for v in views if v.entity_id in entity_ids]
        configs = (
            await context.client.get_automation_configs([v.entity_id for v in views])
            if with_configs
            else {}
        )
        traces = await context.client.list_automation_traces()

    by_config_id: dict[str, list[AutomationTrace]] = defaultdict(list)
    for trace in traces:
        by_config_id[trace.item_id].append(trace)

    infos = []
    for view in views:
        attrs = view.state.attributes
        config_id = attrs.get("id")
        infos.append(
            AutomationInfo(
                entity_id=view.entity_id,
                name=view.name,
                state=view.state.state,
                config_id=config_id,
                mode=attrs.get("mode"),
                last_triggered=_parse_dt(attrs.get("last_triggered")),
                config=configs.get(view.entity_id),
                traces=sorted(by_config_id.get(config_id, []), key=lambda t: t.started_at)
                if config_id
                else [],
            )
        )
    return snapshot, infos


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="List automations", annotations=READ_ONLY)
    async def list_automations(
        ctx: Context[AppContext],
        state: Annotated[
            Literal["on", "off", "unavailable"] | None,
            Field(description="Only automations in this state; 'unavailable' = broken config"),
        ] = None,
    ) -> AutomationList:
        """List automations with their state, last trigger time and the result of the last run.

        Use get_automation for one automation's triggers/actions and run history, and
        audit_automations to find problems across all of them.
        """
        _, infos = await load_automations(app(ctx), with_configs=False)
        return AutomationList(
            automations=[
                AutomationSummary(
                    entity_id=i.entity_id,
                    name=i.name,
                    state=i.state,
                    mode=i.mode,
                    last_triggered=i.last_triggered,
                    last_run=latest_run(i),
                )
                for i in infos
                if state is None or i.state == state
            ]
        )

    @mcp.tool(title="Get automation", annotations=READ_ONLY)
    async def get_automation(
        ctx: Context[AppContext],
        entity_id: Annotated[
            str,
            Field(
                description="Automation entity_id, e.g. 'automation.night_mode'",
                pattern=AUTOMATION_ID_PATTERN,
                max_length=255,
            ),
        ],
    ) -> AutomationDetails:
        """Show one automation: its triggers, conditions and actions, the entities it
        references, and its most recent runs with errors.

        Use it to explain what an automation does or why it failed.
        """
        _, infos = await load_automations(app(ctx), [entity_id], with_configs=True)
        if not infos:
            raise ToolError(f"Unknown automation {entity_id!r}. Use list_automations.")
        (info,) = infos
        config = info.config or {}
        return AutomationDetails(
            entity_id=info.entity_id,
            name=info.name,
            state=info.state,
            mode=info.mode,
            description=config.get("description") or None,
            last_triggered=info.last_triggered,
            config_available=info.config is not None,
            triggers=_listify(config.get("triggers", config.get("trigger"))),
            conditions=_listify(config.get("conditions", config.get("condition"))),
            actions=_listify(config.get("actions", config.get("action"))),
            referenced_entities=sorted(extract_references(config).entities) if config else [],
            recent_runs=[
                AutomationRun(
                    started_at=t.started_at,
                    result=run_result(t),
                    trigger=t.trigger,
                    error=t.error,
                )
                for t in reversed(info.traces[-RECENT_RUNS:])
            ],
        )

    @mcp.tool(title="Audit automations", annotations=READ_ONLY)
    async def audit_automations(
        ctx: Context[AppContext],
        entity_id: Annotated[
            str | None,
            Field(description="Audit only this automation", pattern=AUTOMATION_ID_PATTERN),
        ] = None,
        min_severity: Annotated[
            Literal["error", "warning", "info"],
            Field(description="Hide less severe findings; 'info' includes disabled/never run"),
        ] = "info",
    ) -> AuditReport:
        """Audit automations for problems: invalid configs, references to missing or
        unavailable entities/devices, failing or dropped runs, and automations that are
        disabled or never/no longer trigger.

        Use it for "which automations are broken" or a periodic cleanup. Follow up with
        get_automation to see the details of a finding.
        """
        context = app(ctx)
        await ctx.report_progress(0, 2, "Loading automations, configs and traces")
        snapshot, infos = await load_automations(
            context, [entity_id] if entity_id else None, with_configs=True
        )
        if entity_id and not infos:
            raise ToolError(f"Unknown automation {entity_id!r}. Use list_automations.")
        await ctx.report_progress(1, 2, f"Auditing {len(infos)} automations")
        findings = audit_all(infos, snapshot, context.now())
        await ctx.report_progress(2, 2, f"Found {len(findings)} findings")

        names = {i.entity_id: i.name for i in infos}
        counts = Counter(f.severity for f in findings)
        return AuditReport(
            automations_checked=len(infos),
            counts={s: counts.get(s, 0) for s in SEVERITY_ORDER},
            findings=[
                AuditFinding(
                    entity_id=f.entity_id,
                    name=names[f.entity_id],
                    code=f.code,
                    severity=f.severity,
                    message=f.message,
                )
                for f in findings
                if SEVERITY_ORDER[f.severity] <= SEVERITY_ORDER[min_severity]
            ],
        )


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
