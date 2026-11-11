"""MCP resources: read-only context the client application can attach for the model."""

from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from ha_insights_mcp.actions.policy import FORBIDDEN_DOMAINS, Policy
from ha_insights_mcp.context import AppContext
from ha_insights_mcp.ha import HAError
from ha_insights_mcp.snapshot import HomeSnapshot

HOME_SUMMARY_URI = "ha://home/summary"
ACTION_POLICY_URI = "ha://actions/policy"


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

    if context.actions is not None:
        policy = context.actions.policy

        @mcp.resource(ACTION_POLICY_URI, title="Action policy", mime_type="text/markdown")
        def action_policy() -> str:
            """Which device actions are allowed, their risk and the allowed value ranges."""
            return render_policy(policy)


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


def render_policy(policy: Policy) -> str:
    d = policy.defaults
    lines = [
        "# Action policy",
        "Anything not listed is denied. Risk 'medium' and 'high' need a human confirmation.",
        f"At most {d.max_entities} entities per action; more than {d.bulk_threshold} raise the "
        f"risk one level. At most {d.rate_limit_per_minute} executed actions per minute.",
        "",
        "## Allowed services",
    ]
    for domain, rules in sorted(policy.domains.items()):
        for service in rules.services:
            risk = rules.service_risk.get(service, rules.risk)
            lines.append(f"- `{domain}.{service}` — risk {risk}")
        fields = ", ".join(
            f"{f} [{rules.bounds[f][0]:g}, {rules.bounds[f][1]:g}]" if f in rules.bounds else f
            for f in rules.fields
        )
        lines.append(f"  - data fields for {domain}: {fields or 'none'}")
    if policy.deny.entities:
        lines += ["", "## Denied entities", *(f"- `{e}`" for e in policy.deny.entities)]
    lines += ["", "## Never allowed", ", ".join(sorted(FORBIDDEN_DOMAINS))]
    return "\n".join(lines) + "\n"
