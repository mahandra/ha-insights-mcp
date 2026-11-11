"""Guarded action tools. Registered only when ACTIONS_ENABLED is true.

preview_action is read-only and can be auto-approved by the client; execute_action is a
separate tool with destructive hints, so clients ask for permission on it independently.
"""

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, StringConstraints

from ha_insights_mcp.actions.effects import describe, expect, is_met
from ha_insights_mcp.actions.guard import Assessment, assess
from ha_insights_mcp.actions.policy import SERVICE_PATTERN, ActionRequest
from ha_insights_mcp.context import ActionsContext, AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.ha.models import ENTITY_ID_PATTERN
from ha_insights_mcp.schemas import ActionCheckOut, ActionPreview, Bounds, PlannedChange
from ha_insights_mcp.tools.common import READ_ONLY

ServiceArg = Annotated[
    str,
    Field(
        pattern=SERVICE_PATTERN,
        description="'domain.service', e.g. 'light.turn_on'. See resource ha://actions/policy",
    ),
]
EntityIdsArg = Annotated[
    list[Annotated[str, StringConstraints(pattern=ENTITY_ID_PATTERN)]],
    Field(min_length=1, max_length=50, description="Explicit target entity_ids (no areas)"),
]
DataArg = Annotated[
    dict[str, str | int | float | bool],
    Field(description="Service data, e.g. {'brightness_pct': 40}. Only policy-allowed fields"),
]


def actions_of(ctx: Context[AppContext]) -> ActionsContext:
    actions = app(ctx).actions
    if actions is None:  # tools are registered only with actions enabled
        raise ToolError("Guarded actions are disabled on this server")
    return actions


def can_confirm(ctx: Context[AppContext]) -> bool:
    """True if the client can show a form to the human (form elicitation)."""
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    # A bare `elicitation: {}` predates modes and means form support.
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def request_of(service: str, entity_ids: list[str], data: dict) -> ActionRequest:
    return ActionRequest(service, list(dict.fromkeys(entity_ids)), dict(data))


async def assess_request(ctx: Context[AppContext], request: ActionRequest) -> Assessment:
    with ha_errors_as_tool_errors():
        return await assess(app(ctx).client, actions_of(ctx).policy, request)


def planned_changes(assessment: Assessment) -> list[PlannedChange]:
    changes = []
    for entity_id in assessment.request.entity_ids:
        current = assessment.states.get(entity_id)
        if current is None:
            continue
        expectation = expect(assessment.request.service, assessment.request.data, current)
        changes.append(
            PlannedChange(
                entity_id=entity_id,
                name=current.friendly_name or entity_id,
                current_state=current.state,
                expected=describe(expectation, current),
                already_satisfied=expectation is not None and is_met(expectation, current),
            )
        )
    return changes


def _finite(value: float) -> float | None:
    return value if abs(value) != float("inf") else None


def register(mcp: MCPServer) -> None:
    @mcp.tool(title="Preview action", annotations=READ_ONLY)
    async def preview_action(
        ctx: Context[AppContext],
        service: ServiceArg,
        entity_ids: EntityIdsArg,
        data: DataArg | None = None,
    ) -> ActionPreview:
        """Check a device action against the action policy without performing it.

        Returns every policy check, the risk level, whether a human must confirm, the
        expected change per entity and the allowed value ranges. Call this before
        execute_action and fix any failed check first. Changes nothing.
        """
        assessment = await assess_request(ctx, request_of(service, entity_ids, data or {}))
        decision = assessment.decision
        confirmable = can_confirm(ctx)
        if not decision.allowed:
            next_step = "Not allowed: fix the failed checks, or tell the user it is not permitted."
        elif decision.needs_confirmation and not confirmable:
            next_step = (
                f"Risk {decision.risk}: needs a human confirmation, but this client cannot "
                "show one. execute_action will refuse; tell the user to do it in Home Assistant."
            )
        elif decision.needs_confirmation:
            next_step = (
                f"Risk {decision.risk}: call execute_action with a reason; the user will be "
                "asked to confirm."
            )
        else:
            next_step = "Allowed: call execute_action with the same arguments and a reason."
        return ActionPreview(
            allowed=decision.allowed,
            risk=decision.risk,
            needs_confirmation=decision.needs_confirmation,
            confirmation_available=confirmable,
            checks=[
                ActionCheckOut(check=c.name, ok=c.ok, detail=c.detail) for c in decision.checks
            ],
            changes=planned_changes(assessment),
            allowed_ranges={
                name: Bounds(min=_finite(low), max=_finite(high))
                for name, (low, high) in decision.effective_bounds.items()
            },
            next_step=next_step,
        )
