"""End-to-end device health tool tests."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import STATES, call, mcp_client, state

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def history(command: dict) -> dict:
    base = NOW - timedelta(hours=3)
    points = [
        {"s": "on" if i % 2 else "off", "lu": (base + timedelta(seconds=30 * i)).timestamp()}
        for i in range(30)
    ]
    return {"light.bed_light": points} if "light.bed_light" in command["entity_ids"] else {}


REPAIRS = {
    "issues": [
        {
            "domain": "zha",
            "issue_id": "a",
            "severity": "warning",
            "ignored": False,
            "translation_key": "radio_lost",
        },
        {"domain": "hacs", "issue_id": "b", "severity": "error", "ignored": True},
    ]
}


@pytest.fixture
def ha_states(ha_api):
    states = [
        *STATES,
        state("sensor.door_battery", "9", friendly_name="Door Battery", device_class="battery"),
        state("light.garage", "unavailable", friendly_name="Garage"),
        state("sensor.backup_state", "unknown", friendly_name="Backup state"),
        state("button.restart", "unknown", friendly_name="Restart"),
    ]
    ha_api.get("/api/states").respond(json=states)
    return ha_api


@pytest.fixture
async def client(ha_states):
    ws = {"history/history_during_period": history, "repairs/list_issues": REPAIRS}
    async with mcp_client(ws, now=lambda: NOW) as c:
        yield c


async def test_health_report(client) -> None:
    out = await call(client, "check_device_health")
    assert out["counts"] == {"error": 2, "warning": 1, "info": 1}
    assert [(i["code"], i["entity_id"]) for i in out["issues"]] == [
        ("low_battery", "sensor.door_battery"),
        ("unavailable", "light.garage"),
        ("flapping", "light.bed_light"),
    ]
    flapping = out["issues"][2]
    assert flapping["area"] == "Bedroom"
    assert flapping["detail"] == "29 state changes within one hour"
    # Ignored repairs are hidden.
    assert out["ha_repairs"] == [{"domain": "zha", "issue": "radio_lost", "severity": "warning"}]
    assert out["checks_skipped"] == []


async def test_health_min_severity_info_lists_unknown_but_not_stateless(client) -> None:
    out = await call(client, "check_device_health", min_severity="info")
    unknown = [i["entity_id"] for i in out["issues"] if i["code"] == "unknown_state"]
    assert unknown == ["sensor.backup_state"]


async def test_health_min_severity_error_and_limit(client) -> None:
    out = await call(client, "check_device_health", min_severity="error", limit=1)
    assert [i["code"] for i in out["issues"]] == ["low_battery"]
    assert out["truncated"] is True


async def test_health_battery_threshold_override(client) -> None:
    out = await call(client, "check_device_health", battery_threshold=5)
    assert "low_battery" not in {i["code"] for i in out["issues"]}


async def test_health_filtered_by_area(client) -> None:
    out = await call(client, "check_device_health", area="Kitchen")
    assert out["issues"] == []


async def test_health_reports_progress(client) -> None:
    seen = []

    async def progress(p, total, message):
        seen.append((p, total))

    result = await client.call_tool("check_device_health", {}, progress_callback=progress)
    assert not result.is_error
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
