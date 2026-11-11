"""Assess a requested action against fresh Home Assistant data. Read-only.

Shared by preview and execute. Execute calls it again right before writing, so a
preview is never treated as a permission (state may change in between).
"""

from dataclasses import dataclass

from ha_insights_mcp.actions.policy import ActionRequest, Decision, Policy, evaluate
from ha_insights_mcp.ha.client import HAClient
from ha_insights_mcp.ha.models import EntityState


@dataclass(frozen=True, slots=True)
class Assessment:
    request: ActionRequest
    decision: Decision
    # Fresh states of the targets that exist.
    states: dict[str, EntityState]


async def assess(client: HAClient, policy: Policy, request: ActionRequest) -> Assessment:
    if not policy.allows(request.service):
        # Denied by the allowlist alone: no need to touch HA.
        return Assessment(request, evaluate(policy, request, {}), {})
    wanted = set(request.entity_ids)
    states = {s.entity_id: s for s in await client.get_states() if s.entity_id in wanted}
    services = await client.get_services()
    return Assessment(request, evaluate(policy, request, states, services), states)
