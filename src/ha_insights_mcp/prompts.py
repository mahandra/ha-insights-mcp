"""MCP prompts: user-selected workflows (e.g. slash commands in Claude Code).

A prompt is a message template the *user* picks. It tells the model which tools to use
and how to present the result. Prompts can also embed server-side context.
"""

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.prompts.base import Message, UserMessage
from pydantic import Field

from ha_insights_mcp.analytics.audit import extract_references
from ha_insights_mcp.context import AppContext, app
from ha_insights_mcp.ha.models import ENTITY_ID_PATTERN
from ha_insights_mcp.tools.automations import load_automations

HEALTH_REPORT = """\
Prepare a health report for my Home Assistant home{scope}.

1. Call check_device_health{health_args} and audit_automations.
2. Correlate the results: e.g. a flapping or unavailable entity that is referenced by a
   failing automation, or several issues on the same device. Use summarize_history or
   get_automation only where it clarifies a finding.
3. Answer with three sections, most important first:
   - **Fix now** — errors: offline devices, failing or broken automations, critical batteries.
   - **Should fix** — warnings.
   - **FYI** — disabled or never-triggered automations and other info.
   For each item: what is wrong, the evidence (entity/automation, since when), and one
   concrete next step for me. Do not invent causes the data does not show.
4. This server is read-only: do not claim to have changed anything.
"""

INVESTIGATE = """\
Investigate the Home Assistant entity `{entity_id}` and tell me whether it behaves normally.

1. Call get_entity_state and summarize_history (last 24 hours) for it.
2. Call check_device_health for its domain and check whether it appears there.
3. {automations}
4. Summarize: current state, recent behaviour, any problems, and what I should check next.
"""


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(title="Home health report")
    def home_health_report(
        area: Annotated[str | None, Field(description="Limit to one area (id or name)")] = None,
    ) -> list[Message]:
        """Prioritized report of broken devices and automations, with next steps."""
        text = HEALTH_REPORT.format(
            scope=f" (area: {area})" if area else "",
            health_args=f"(area={area!r})" if area else "",
        )
        return [UserMessage(text)]

    @mcp.prompt(title="Investigate entity")
    async def investigate_entity(
        ctx: Context[AppContext],
        entity_id: Annotated[
            str, Field(description="Entity to investigate", pattern=ENTITY_ID_PATTERN)
        ],
    ) -> list[Message]:
        """Check one entity: state, 24h history, health and the automations that use it."""
        # Server-side context: which automations reference this entity. Saves the model
        # one get_automation call per automation.
        _, infos = await load_automations(app(ctx), with_configs=True)
        users = sorted(
            i.entity_id
            for i in infos
            if i.config and entity_id in extract_references(i.config).entities
        )
        automations = (
            "These automations reference it: "
            + ", ".join(users)
            + ". Call audit_automations for each and get_automation where a finding needs detail."
            if users
            else "No automation references it directly (templated references are not checked)."
        )
        return [UserMessage(INVESTIGATE.format(entity_id=entity_id, automations=automations))]
