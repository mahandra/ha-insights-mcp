"""Guarded action tools. Registered only when ACTIONS_ENABLED is true.

preview_action is read-only and can be auto-approved by the client; execute_action is a
separate tool with destructive hints, so clients ask for permission on it independently.
"""

from typing import Annotated, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, StringConstraints

from ha_insights_mcp.actions.audit_log import AuditEntry, Confirmation, Outcome
from ha_insights_mcp.actions.effects import describe, expect, is_met
from ha_insights_mcp.actions.executor import execute, record_refusal
from ha_insights_mcp.actions.guard import Assessment, assess
from ha_insights_mcp.actions.policy import SERVICE_PATTERN, ActionRequest
from ha_insights_mcp.auth import SCOPE_ACTIONS, current_actor, has_scope
from ha_insights_mcp.context import ActionsContext, AppContext, app, ha_errors_as_tool_errors
from ha_insights_mcp.ha.models import ENTITY_ID_PATTERN
from ha_insights_mcp.schemas import (
    ActionCheckOut,
    ActionLog,
    ActionLogEntry,
    ActionPreview,
    ActionResult,
    Bounds,
    OutcomeOut,
    PlannedChange,
    TargetResult,
)
from ha_insights_mcp.tools.common import READ_ONLY

# Clients use these hints to decide when to ask the user for permission to run the tool.
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

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
ReasonArg = Annotated[
    str,
    Field(
        min_length=5,
        max_length=300,
        description="Why, in one sentence (the user's request). Shown to the human and logged",
    ),
]


class Confirm(BaseModel):
    """The form shown to the human. A checkbox, so 'accept' alone is not enough."""

    confirm: bool = Field(title="Allow this action", description="Tick to allow the action")


class NotAsked(BaseModel):
    """Resolver outcome when no question was shown to the human."""

    why: Literal["not_required", "unavailable", "denied", "rate_limited", "no_scope"]


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
    register_preview(mcp)
    register_execute(mcp)
    register_log(mcp)


def register_preview(mcp: MCPServer) -> None:
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
                name: Bounds(min=_finite(low), max=_finite(high), unit=decision.units.get(name))
                for name, (low, high) in decision.effective_bounds.items()
            },
            next_step=next_step,
        )


def confirmation_message(assessment: Assessment, reason: str) -> str:
    """Built by the server from validated data, never from free text alone."""
    request = assessment.request
    lines = [f"Home Assistant action, risk {assessment.decision.risk}: {request.service}"]
    for change in planned_changes(assessment):
        note = " (already in this state)" if change.already_satisfied else ""
        lines.append(f"- {change.name} ({change.entity_id}): {change.expected or 'see data'}{note}")
    if request.data:
        lines.append("Data: " + ", ".join(f"{k}={v}" for k, v in sorted(request.data.items())))
    # The reason is the model's text: label it, so the human weighs it accordingly.
    lines.append(f"Assistant's reason (unverified): {reason}")
    return "\n".join(lines)


async def ask_confirmation(
    ctx: Context[AppContext],
    service: str,
    entity_ids: list[str],
    data: dict | None,
    reason: str,
) -> Elicit[Confirm] | NotAsked:
    """Resolver: ask the human only when the policy requires it and the client can.

    May run on every round of a multi-round call, so it has no side effects.
    """
    actions = actions_of(ctx)
    if not has_scope(SCOPE_ACTIONS):
        # Never ask a human to confirm what this token may not do anyway.
        return NotAsked(why="no_scope")
    assessment = await assess_request(ctx, request_of(service, entity_ids, data or {}))
    decision = assessment.decision
    if not decision.allowed:
        return NotAsked(why="denied")
    if not decision.needs_confirmation:
        return NotAsked(why="not_required")
    if not can_confirm(ctx):
        return NotAsked(why="unavailable")
    if actions.limiter.retry_after() > 0:
        # Do not ask a human for something that would be refused anyway.
        return NotAsked(why="rate_limited")
    return Elicit(confirmation_message(assessment, reason), Confirm)


def human_answer(outcome: ElicitationResult[Confirm]) -> Confirmation:
    match outcome:
        case AcceptedElicitation(data=Confirm(confirm=True)):
            return "accepted"
        case AcceptedElicitation(data=Confirm()) | DeclinedElicitation():
            return "declined"
        case CancelledElicitation():
            return "cancelled"
        case AcceptedElicitation(data=NotAsked(why="not_required")):
            return "not_required"
    return "unavailable"


def action_result(
    entry: AuditEntry,
    assessment: Assessment,
    message: str,
    verifications: dict | None = None,
    after: dict | None = None,
) -> ActionResult:
    verifications, after = verifications or {}, after or {}
    return ActionResult(
        outcome=entry.outcome,
        action_id=entry.id or 0,
        risk=assessment.decision.risk,
        confirmation=entry.confirmation or "not_required",
        message=message,
        failed_checks=[
            ActionCheckOut(check=c.name, ok=c.ok, detail=c.detail)
            for c in assessment.decision.failures
        ],
        results=[
            TargetResult(
                entity_id=e,
                before=entry.before.get(e),
                after=after.get(e),
                verification=verifications.get(e, "not_checked"),
            )
            for e in assessment.request.entity_ids
        ]
        if entry.outcome == "executed"
        else [],
    )


REFUSALS: dict[Confirmation, tuple[Outcome, str]] = {
    "declined": ("declined_by_user", "The user declined the action. Nothing was changed."),
    "cancelled": ("cancelled_by_user", "The user dismissed the confirmation. Nothing changed."),
    "unavailable": (
        "no_confirmation_channel",
        "This action needs a human confirmation, but this MCP client cannot show one. "
        "Nothing was changed; the user can do it in Home Assistant.",
    ),
}

VERIFICATION_MESSAGES = {
    "confirmed": "Done; the new state was confirmed in Home Assistant.",
    "pending": "Sent; the device is still moving. Check again shortly.",
    "not_confirmed": "Sent, but the device did not end up in the expected state.",
    "not_checked": "Sent; the result could not be verified automatically.",
}


def register_execute(mcp: MCPServer) -> None:
    @mcp.tool(title="Execute action", annotations=WRITE)
    async def execute_action(
        ctx: Context[AppContext],
        service: ServiceArg,
        entity_ids: EntityIdsArg,
        reason: ReasonArg,
        confirmation: Annotated[ElicitationResult[Confirm], Resolve(ask_confirmation)],
        data: DataArg | None = None,
    ) -> ActionResult:
        """Perform a device action allowed by the action policy, e.g. turn on a light.

        Every check from preview_action runs again here. Risky actions (medium/high) are
        confirmed by the human in the client; if the client cannot ask, they are refused.
        The result says whether the new state was verified. Every call is audit-logged.
        """
        context, actions = app(ctx), actions_of(ctx)
        # Re-assess: states may have changed while the human was deciding.
        assessment = await assess_request(ctx, request_of(service, entity_ids, data or {}))
        decision = assessment.decision
        at, actor = context.now(), current_actor()

        async def refuse(outcome: Outcome, answer: Confirmation | None, msg: str) -> ActionResult:
            entry = await record_refusal(
                actions.audit_log,
                assessment,
                at=at,
                reason=reason,
                outcome=outcome,
                confirmation=answer,
                actor=actor,
                detail=msg,
            )
            return action_result(entry, assessment, msg)

        if not has_scope(SCOPE_ACTIONS):
            return await refuse(
                "denied_scope",
                None,
                f"This client's token lacks the {SCOPE_ACTIONS} scope: it may preview actions "
                "but not execute them. Nothing was changed.",
            )
        if not decision.allowed:
            return await refuse(
                "denied_policy",
                None,
                "Refused by the action policy: " + "; ".join(c.detail for c in decision.failures),
            )
        answer = human_answer(confirmation)
        if decision.needs_confirmation and answer != "accepted":
            if answer == "not_required":  # cannot happen unless the policy changed mid-call
                answer = "unavailable"
            outcome, message = REFUSALS[answer]
            return await refuse(outcome, answer, message)
        if not actions.limiter.try_acquire():
            wait = actions.limiter.retry_after()
            return await refuse(
                "rate_limited",
                answer,
                f"Too many actions; try again in {wait:.0f}s. Is the assistant in a loop?",
            )

        entry, verifications, after = await execute(
            context.client,
            actions.audit_log,
            assessment,
            at=at,
            reason=reason,
            confirmation=answer,
            actor=actor,
            verify_settings=actions.verify,
        )
        if entry.outcome == "executed":
            message = VERIFICATION_MESSAGES[entry.verification or "not_checked"]
        elif entry.outcome == "unknown":
            message = f"{entry.detail}. Check the state before retrying."
        else:
            message = f"Home Assistant did not perform the action: {entry.detail}"
        return action_result(entry, assessment, message, verifications, after)


def register_log(mcp: MCPServer) -> None:
    @mcp.tool(title="Action log", annotations=READ_ONLY)
    async def get_action_log(
        ctx: Context[AppContext],
        limit: Annotated[int, Field(ge=1, le=100, description="Max entries")] = 20,
        entity_id: Annotated[
            str | None, Field(pattern=ENTITY_ID_PATTERN, description="Only actions on this entity")
        ] = None,
        outcome: Annotated[OutcomeOut | None, Field(description="Only this outcome")] = None,
    ) -> ActionLog:
        """Past action attempts from the audit log, newest first: what was requested, why,
        whether a human confirmed, and what happened. Includes refused attempts."""
        entries = await actions_of(ctx).audit_log.recent(limit, entity_id, outcome)
        return ActionLog(
            entries=[
                ActionLogEntry(
                    action_id=e.id or 0,
                    at=e.at,
                    service=e.service,
                    entity_ids=e.entity_ids,
                    data=e.data,
                    reason=e.reason,
                    risk=e.risk,
                    confirmation=e.confirmation,
                    outcome=e.outcome,
                    verification=e.verification,
                    detail=e.detail,
                    actor=e.actor,
                )
                for e in entries
            ]
        )
