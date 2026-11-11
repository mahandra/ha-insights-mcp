"""Guarded action tools end to end: MCP client -> server -> HA fakes."""

from pathlib import Path
from typing import Any

import pytest
from mcp import types

from tests.conftest import (
    ACTION_STATES,
    HA_SERVICES,
    STATES,
    actions_context,
    call,
    mcp_client,
)

pytestmark = pytest.mark.anyio


async def accept_all(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"confirm": True})


@pytest.fixture
def api(ha_api):
    ha_api.get("/api/states").respond(json=STATES + ACTION_STATES)
    return ha_api


@pytest.fixture
def actions(tmp_path: Path):
    context = actions_context(tmp_path)
    yield context
    context.audit_log.close()


@pytest.fixture
async def client(api, actions):
    async with mcp_client({"get_services": HA_SERVICES}, actions=actions) as c:
        yield c


# ------------------------------------------------------------------ preview


async def test_preview_allowed_low_risk(client) -> None:
    out = await call(
        client,
        "preview_action",
        service="light.turn_on",
        entity_ids=["light.bed_light"],
        data={"brightness_pct": 40},
    )
    assert out["allowed"] is True
    assert (out["risk"], out["needs_confirmation"]) == ("low", False)
    assert out["changes"] == [
        {
            "entity_id": "light.bed_light",
            "name": "Bed Light",
            "current_state": "off",
            "expected": "state off -> on",
            "already_satisfied": False,
        }
    ]
    assert out["allowed_ranges"]["brightness_pct"] == {"min": 0, "max": 100}
    assert out["next_step"].startswith("Allowed")


async def test_preview_denied_lists_failed_checks(client) -> None:
    out = await call(
        client,
        "preview_action",
        service="climate.set_temperature",
        entity_ids=["climate.hvac"],
        data={"temperature": 30},
    )
    assert out["allowed"] is False
    failed = [c for c in out["checks"] if not c["ok"]]
    assert [c["check"] for c in failed] == ["value.temperature"]
    assert "[16, 26]" in failed[0]["detail"]
    assert out["allowed_ranges"]["temperature"] == {"min": 16, "max": 26}


async def test_preview_unknown_service_does_not_touch_ha(client, api) -> None:
    out = await call(
        client, "preview_action", service="script.turn_on", entity_ids=["script.anything"]
    )
    assert out["allowed"] is False
    assert out["risk"] is None
    assert not any(c.request.url.path == "/api/states" for c in api.calls)


async def test_preview_risky_without_confirmation_channel(client) -> None:
    out = await call(
        client, "preview_action", service="lock.unlock", entity_ids=["lock.front_door"]
    )
    assert out["allowed"] is True
    assert (out["risk"], out["needs_confirmation"]) == ("high", True)
    assert out["confirmation_available"] is False
    assert "cannot show one" in out["next_step"]


async def test_preview_risky_with_confirmation_channel(api, actions) -> None:
    async with mcp_client(
        {"get_services": HA_SERVICES}, actions=actions, elicitation_callback=accept_all
    ) as client:
        out = await call(
            client, "preview_action", service="lock.unlock", entity_ids=["lock.front_door"]
        )
    assert out["confirmation_available"] is True
    assert "asked to confirm" in out["next_step"]


async def test_preview_marks_already_satisfied(client) -> None:
    out = await call(
        client, "preview_action", service="light.turn_on", entity_ids=["light.kitchen_lights"]
    )
    assert out["changes"][0]["already_satisfied"] is True


async def test_preview_rejects_bad_arguments(client) -> None:
    for args in (
        {"service": "light", "entity_ids": ["light.bed_light"]},
        {"service": "light.turn_on", "entity_ids": []},
        {"service": "light.turn_on", "entity_ids": ["all"]},
        {"service": "light.turn_on", "entity_ids": ["light.bed_light"], "data": {"x": [1]}},
    ):
        assert (await client.call_tool("preview_action", args)).is_error, args


# ----------------------------------------------------------------- resource


async def test_policy_resource(client) -> None:
    result = await client.read_resource("ha://actions/policy")
    text = result.contents[0].text
    assert "`lock.unlock` — risk high" in text
    assert "temperature [16, 26]" in text
    assert "script" in text  # never allowed


async def test_action_tools_absent_when_disabled(ha_api) -> None:
    async with mcp_client() as client:
        names = {t.name for t in (await client.list_tools()).tools}
        resources = {str(r.uri) for r in (await client.list_resources()).resources}
    assert "preview_action" not in names
    assert "ha://actions/policy" not in resources
