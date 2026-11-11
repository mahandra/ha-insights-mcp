"""The only code path that changes Home Assistant state.

Takes an assessment that the caller has just made (and, for risky actions, a human
confirmation), calls the service, re-reads the targets to verify the effect, and writes
every outcome to the audit log. Refusals are logged here too, so the log is complete.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime

from ha_insights_mcp.actions.audit_log import AuditEntry, AuditLog, Confirmation, Outcome
from ha_insights_mcp.actions.effects import Verification, expect, verify
from ha_insights_mcp.actions.guard import Assessment
from ha_insights_mcp.ha import HAClient, HAError, HATimeoutError
from ha_insights_mcp.ha.models import EntityState

logger = logging.getLogger(__name__)

# Worst first: the overall verification of a multi-entity action.
_VERIFICATION_ORDER: tuple[Verification, ...] = (
    "not_confirmed",
    "pending",
    "not_checked",
    "confirmed",
)


@dataclass(frozen=True, slots=True)
class VerifySettings:
    timeout: float = 3.0
    interval: float = 0.5


def overall(verifications: dict[str, Verification]) -> Verification | None:
    if not verifications:
        return None
    return min(verifications.values(), key=_VERIFICATION_ORDER.index)


def _entry(
    assessment: Assessment,
    *,
    at: datetime,
    reason: str,
    outcome: Outcome,
    confirmation: Confirmation | None,
    detail: str | None = None,
) -> AuditEntry:
    request = assessment.request
    return AuditEntry(
        at=at,
        service=request.service,
        entity_ids=request.entity_ids,
        data=request.data,
        reason=reason,
        outcome=outcome,
        risk=assessment.decision.risk,
        confirmation=confirmation,
        detail=detail,
        before={e: s.state for e, s in assessment.states.items()},
    )


async def record_refusal(
    log: AuditLog,
    assessment: Assessment,
    *,
    at: datetime,
    reason: str,
    outcome: Outcome,
    confirmation: Confirmation | None,
    detail: str,
) -> AuditEntry:
    return await log.record(
        _entry(
            assessment,
            at=at,
            reason=reason,
            outcome=outcome,
            confirmation=confirmation,
            detail=detail,
        )
    )


async def execute(
    client: HAClient,
    log: AuditLog,
    assessment: Assessment,
    *,
    at: datetime,
    reason: str,
    confirmation: Confirmation,
    verify_settings: VerifySettings,
) -> tuple[AuditEntry, dict[str, Verification], dict[str, str]]:
    """Run an allowed action. Returns the audit entry, per-entity verification and the
    states observed afterwards."""
    if not assessment.decision.allowed:
        raise ValueError("execute() called with an action the policy does not allow")
    request = assessment.request
    domain, _, service = request.service.partition(".")
    base = _entry(assessment, at=at, reason=reason, outcome="executed", confirmation=confirmation)
    try:
        context_id = await client.call_service(domain, service, request.entity_ids, request.data)
    except HATimeoutError as exc:
        entry = await log.record(replace(base, outcome="unknown", detail=str(exc)))
        return entry, {}, {}
    except HAError as exc:
        entry = await log.record(replace(base, outcome="failed", detail=str(exc)))
        return entry, {}, {}

    verifications, after = await _verify(client, assessment, verify_settings)
    entry = await log.record(
        replace(
            base,
            context_id=context_id,
            after=after,
            verification=overall(verifications),
        )
    )
    logger.info("Action %s on %s: %s", request.service, request.entity_ids, entry.verification)
    return entry, verifications, after


async def _verify(
    client: HAClient, assessment: Assessment, settings: VerifySettings
) -> tuple[dict[str, Verification], dict[str, str]]:
    """Poll target states until every effect is confirmed or the timeout passes."""
    request = assessment.request
    expectations = {
        e: expect(request.service, request.data, s) for e, s in assessment.states.items()
    }
    deadline = time.monotonic() + settings.timeout
    verifications: dict[str, Verification] = {}
    after: dict[str, EntityState] = {}
    while True:
        try:
            states = {s.entity_id: s for s in await client.get_states()}
        except HAError as exc:
            logger.warning("Could not re-read states to verify an action: %s", exc)
            return {e: "not_checked" for e in expectations}, {}
        after = {e: states[e] for e in expectations if e in states}
        verifications = {e: verify(x, after.get(e)) for e, x in expectations.items()}
        settled = all(v in ("confirmed", "not_checked") for v in verifications.values())
        if settled or time.monotonic() >= deadline:
            break
        await asyncio.sleep(settings.interval)
    return verifications, {e: s.state for e, s in after.items()}
