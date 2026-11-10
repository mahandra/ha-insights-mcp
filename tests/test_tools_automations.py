"""End-to-end automation tool tests."""

from datetime import UTC, datetime

import pytest

from tests.conftest import STATES, FakeError, call, error_text, mcp_client, state

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

AUTOMATION_STATES = [
    state(
        "automation.motion_alert",
        "on",
        friendly_name="Motion alert",
        id="motion",
        mode="single",
        last_triggered="2026-11-02T11:00:00+00:00",
    ),
    state("automation.night", "off", friendly_name="Night", id="night", mode="single"),
    state(
        "automation.yaml_legacy",
        "on",
        friendly_name="Legacy",
        mode="single",
        last_triggered="2026-11-01T09:00:00+00:00",
    ),
]

CONFIGS = {
    "automation.motion_alert": {
        "id": "motion",
        "alias": "Motion alert",
        "description": "Turn on kitchen lights",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.motion_gone", "to": "on"}],
        "conditions": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen_lights"}}],
        "mode": "single",
    },
    "automation.night": {
        "id": "night",
        "triggers": {"trigger": "time", "at": "23:00:00"},  # single dict, not a list
        "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.bed_light"}}],
    },
}


def config(command: dict):
    found = CONFIGS.get(command["entity_id"])
    return {"config": found} if found else FakeError("not_found", "Entity not found")


def trace(run: str, start: str, execution: str, error: str | None = None) -> dict:
    return {
        "item_id": "motion",
        "run_id": run,
        "state": "stopped",
        "script_execution": execution,
        "error": error,
        "trigger": "state of binary_sensor.motion_gone",
        "domain": "automation",
        "timestamp": {"start": start, "finish": start},
    }


TRACES = [
    trace("r2", "2026-11-02T11:00:00+00:00", "error", "Action light.turn_on failed"),
    trace("r1", "2026-11-02T10:00:00+00:00", "finished"),
]


@pytest.fixture
async def client(ha_api):
    ha_api.get("/api/states").respond(json=[*STATES, *AUTOMATION_STATES])
    ws = {"automation/config": config, "trace/list": TRACES}
    async with mcp_client(ws, now=lambda: NOW) as c:
        yield c


async def test_list_automations(client) -> None:
    out = await call(client, "list_automations")
    assert [(a["entity_id"], a["state"], a["last_run"]) for a in out["automations"]] == [
        ("automation.motion_alert", "on", "error"),
        ("automation.night", "off", "none"),
        ("automation.yaml_legacy", "on", "none"),
    ]


async def test_list_automations_filtered(client) -> None:
    out = await call(client, "list_automations", state="off")
    assert [a["entity_id"] for a in out["automations"]] == ["automation.night"]


async def test_get_automation(client) -> None:
    out = await call(client, "get_automation", entity_id="automation.motion_alert")
    assert out["description"] == "Turn on kitchen lights"
    assert out["config_available"] is True
    assert out["referenced_entities"] == ["binary_sensor.motion_gone", "light.kitchen_lights"]
    # Newest run first.
    assert [(r["result"], r["error"]) for r in out["recent_runs"]] == [
        ("error", "Action light.turn_on failed"),
        ("ok", None),
    ]


async def test_get_automation_normalizes_single_trigger(client) -> None:
    out = await call(client, "get_automation", entity_id="automation.night")
    assert out["triggers"] == [{"trigger": "time", "at": "23:00:00"}]
    assert out["recent_runs"] == []


async def test_get_automation_without_config(client) -> None:
    out = await call(client, "get_automation", entity_id="automation.yaml_legacy")
    assert out["config_available"] is False
    assert out["actions"] == []


async def test_get_automation_unknown(client) -> None:
    result = await client.call_tool("get_automation", {"entity_id": "automation.nope"})
    assert "list_automations" in error_text(result)


async def test_get_automation_rejects_non_automation(client) -> None:
    result = await client.call_tool("get_automation", {"entity_id": "light.bed_light"})
    assert result.is_error


async def test_audit_automations(client) -> None:
    out = await call(client, "audit_automations")
    assert out["automations_checked"] == 3
    assert out["counts"] == {"error": 2, "warning": 0, "info": 2}
    assert [(f["entity_id"], f["code"]) for f in out["findings"]] == [
        ("automation.motion_alert", "last_run_failed"),
        ("automation.motion_alert", "missing_entity"),
        ("automation.night", "disabled"),
        ("automation.yaml_legacy", "no_id"),
    ]


async def test_audit_min_severity(client) -> None:
    out = await call(client, "audit_automations", min_severity="error")
    assert {f["severity"] for f in out["findings"]} == {"error"}
    assert out["counts"]["info"] == 2  # counts are before filtering


async def test_audit_single_automation(client) -> None:
    out = await call(client, "audit_automations", entity_id="automation.night")
    assert out["automations_checked"] == 1
    assert [f["code"] for f in out["findings"]] == ["disabled"]
