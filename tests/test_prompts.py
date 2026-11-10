"""MCP prompts: listing and rendering."""

import pytest

from tests.conftest import STATES, mcp_client, state

pytestmark = pytest.mark.anyio

AUTOMATIONS = [
    state("automation.kitchen_on", "on", friendly_name="Kitchen on", id="k"),
    state("automation.other", "on", friendly_name="Other", id="o"),
]
CONFIGS = {
    "automation.kitchen_on": {
        "triggers": [{"trigger": "time", "at": "07:00:00"}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen_lights"}}],
    },
    "automation.other": {"triggers": [], "actions": []},
}


@pytest.fixture
async def client(ha_api):
    ha_api.get("/api/states").respond(json=[*STATES, *AUTOMATIONS])
    ws = {
        "automation/config": lambda c: {"config": CONFIGS[c["entity_id"]]},
        "trace/list": [],
    }
    async with mcp_client(ws) as c:
        yield c


async def test_prompts_are_listed_with_arguments(client) -> None:
    prompts = {p.name: p for p in (await client.list_prompts()).prompts}
    assert set(prompts) == {"home_health_report", "investigate_entity"}
    assert [(a.name, a.required) for a in prompts["home_health_report"].arguments] == [
        ("area", False)
    ]
    assert [(a.name, a.required) for a in prompts["investigate_entity"].arguments] == [
        ("entity_id", True)
    ]


async def test_home_health_report(client) -> None:
    result = await client.get_prompt("home_health_report", {})
    (message,) = result.messages
    assert message.role == "user"
    text = message.content.text
    assert "check_device_health and audit_automations" in text
    assert "read-only" in text


async def test_home_health_report_scoped_to_area(client) -> None:
    result = await client.get_prompt("home_health_report", {"area": "Kitchen"})
    assert "check_device_health(area='Kitchen')" in result.messages[0].content.text


async def test_investigate_entity_embeds_referencing_automations(client) -> None:
    result = await client.get_prompt("investigate_entity", {"entity_id": "light.kitchen_lights"})
    text = result.messages[0].content.text
    assert "`light.kitchen_lights`" in text
    assert "These automations reference it: automation.kitchen_on." in text
    assert "automation.other" not in text


async def test_investigate_entity_without_automations(client) -> None:
    result = await client.get_prompt("investigate_entity", {"entity_id": "sun.sun"})
    assert "No automation references it directly" in result.messages[0].content.text
