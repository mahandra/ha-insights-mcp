"""MCP server assembly: lifespan, tools and resources."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime

from mcp.server.mcpserver import MCPServer

from ha_insights_mcp import __version__
from ha_insights_mcp.actions.audit_log import AuditLog
from ha_insights_mcp.actions.policy import load_policy
from ha_insights_mcp.actions.ratelimit import RateLimiter
from ha_insights_mcp.analytics.health import HealthThresholds
from ha_insights_mcp.config import Settings
from ha_insights_mcp.context import ActionsContext, AppContext
from ha_insights_mcp.ha import HAClient, HAError
from ha_insights_mcp.prompts import register_prompts
from ha_insights_mcp.resources import register_resources
from ha_insights_mcp.snapshot import SnapshotProvider
from ha_insights_mcp.tools import actions as action_tools
from ha_insights_mcp.tools import register_tools

logger = logging.getLogger(__name__)

# Sent to the client at initialization; most clients put it into the model's context.
INSTRUCTIONS = """\
Analytics and diagnostics for a Home Assistant home.
- Discover: list_areas, search_entities -> entity_ids; get_entity_state for one entity.
- History: summarize_history (one entity), rank_activity (most active entities).
- Health: check_device_health (offline, batteries, stale, flapping, HA repairs).
- Automations: list_automations, audit_automations (problems), get_automation (details).
The resource ha://home/summary gives a compact overview of the whole home.
Results are already aggregated; do not ask for raw history.
"""
READ_ONLY_NOTE = "This server cannot change device states.\n"
ACTIONS_NOTE = """\
Guarded actions: preview_action (checks only), then execute_action with a short reason.
Allowed services and value ranges: resource ha://actions/policy. Risky actions are
confirmed by the human; never claim an action happened unless execute_action says so.
get_action_log lists past actions.
"""


def create_server(
    client_factory: Callable[[], HAClient],
    cache_ttl: float,
    now: Callable[[], datetime] | None = None,
    thresholds: HealthThresholds | None = None,
    actions: ActionsContext | None = None,
) -> MCPServer:
    client = client_factory()
    app_context = AppContext(client=client, snapshots=SnapshotProvider(client, ttl=cache_ttl))
    if now is not None:
        app_context = replace(app_context, now=now)
    if thresholds is not None:
        app_context = replace(app_context, thresholds=thresholds)
    if actions is not None:
        app_context = replace(app_context, actions=actions)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppContext]:
        async with client:
            try:
                config = await client.get_config()
                logger.info("Connected to Home Assistant %s at %s", config.version, client.base_url)
            except HAError as exc:
                # Do not fail startup: tools will report the problem to the model.
                logger.warning("Home Assistant check failed: %s", exc)
            yield app_context

    mcp = MCPServer(
        "ha-insights",
        title="HA Insights",
        version=__version__,
        instructions=INSTRUCTIONS + (ACTIONS_NOTE if actions else READ_ONLY_NOTE),
        lifespan=lifespan,
    )
    register_tools(mcp)
    if actions is not None:
        action_tools.register(mcp)
    register_resources(mcp, app_context)
    register_prompts(mcp)
    return mcp


def actions_from_settings(settings: Settings) -> ActionsContext | None:
    """Raises PolicyError if actions are enabled but the policy file is missing/invalid."""
    if not settings.actions_enabled:
        return None
    policy = load_policy(settings.actions_policy_path)
    logger.info(
        "Guarded actions enabled: %d domains in %s, audit log %s",
        len(policy.domains),
        settings.actions_policy_path,
        settings.audit_db_path,
    )
    return ActionsContext(
        policy=policy,
        audit_log=AuditLog(settings.audit_db_path),
        limiter=RateLimiter(policy.defaults.rate_limit_per_minute),
    )


def server_from_settings(settings: Settings) -> MCPServer:
    return create_server(
        client_factory=lambda: HAClient(
            settings.ha_url, settings.ha_token, timeout=settings.ha_timeout_seconds
        ),
        cache_ttl=settings.ha_cache_ttl_seconds,
        thresholds=HealthThresholds(
            battery_low_percent=settings.battery_low_percent,
            stale_hours=settings.stale_hours,
            flapping_changes_per_hour=settings.flapping_changes_per_hour,
        ),
        actions=actions_from_settings(settings),
    )
