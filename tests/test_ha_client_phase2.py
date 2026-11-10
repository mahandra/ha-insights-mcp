"""HA client: history, statistics, automation configs, traces, repairs (WebSocket)."""

from datetime import UTC, datetime

import pytest

from ha_insights_mcp.ha import HAClient, HAResponseError
from tests.conftest import HA_URL, TOKEN, FakeError, FakeWebSocket, fake_ws_factory

pytestmark = pytest.mark.anyio

START = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
END = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T0 = START.timestamp()


def client_with(results: dict) -> tuple[HAClient, FakeWebSocket]:
    ws = FakeWebSocket(results)
    return HAClient(HA_URL, TOKEN, ws_connect_factory=fake_ws_factory(ws)), ws


async def test_get_history_parses_compressed_points() -> None:
    client, ws = client_with(
        {
            "history/history_during_period": {
                "light.bed_light": [
                    {"s": "off", "lu": T0},
                    {"s": "on", "lu": T0 + 600},
                    # last_changed differs from last_updated: lc wins.
                    {"s": "off", "lu": T0 + 1300, "lc": T0 + 1200},
                ]
            }
        }
    )
    async with client:
        history = await client.get_history(["light.bed_light"], START, END)

    points = history["light.bed_light"]
    assert [p.state for p in points] == ["off", "on", "off"]
    assert points[1].changed_at == datetime(2026, 11, 1, 10, 10, tzinfo=UTC)
    assert points[2].changed_at == datetime(2026, 11, 1, 10, 20, tzinfo=UTC)

    command = ws.sent[1]
    assert command["entity_ids"] == ["light.bed_light"]
    assert command["minimal_response"] is True
    assert command["no_attributes"] is True
    assert command["include_start_time_state"] is True
    assert command["start_time"] == START.isoformat()
    assert command["end_time"] == END.isoformat()


async def test_get_history_with_no_entities_skips_ha() -> None:
    client, ws = client_with({})
    async with client:
        assert await client.get_history([], START) == {}
    assert ws.sent == []


async def test_get_history_rejects_malformed_point() -> None:
    client, _ = client_with({"history/history_during_period": {"light.x": [{"state": "on"}]}})
    async with client:
        with pytest.raises(HAResponseError, match="history point"):
            await client.get_history(["light.x"], START)


async def test_get_statistics() -> None:
    start_ms = int(T0 * 1000)
    client, ws = client_with(
        {
            "recorder/statistics_during_period": {
                "sensor.temp": [
                    {
                        "start": start_ms,
                        "end": start_ms + 3_600_000,
                        "mean": 20.5,
                        "min": 19.0,
                        "max": 22.0,
                    }
                ]
            }
        }
    )
    async with client:
        stats = await client.get_statistics(["sensor.temp"], START, END)

    (bucket,) = stats["sensor.temp"]
    assert bucket.start == START
    assert bucket.end == datetime(2026, 11, 1, 11, 0, tzinfo=UTC)
    assert (bucket.min, bucket.mean, bucket.max) == (19.0, 20.5, 22.0)
    assert ws.sent[1]["types"] == ["mean", "min", "max"]
    assert ws.sent[1]["period"] == "hour"


async def test_get_automation_configs_tolerates_missing() -> None:
    def config(command: dict):
        if command["entity_id"] == "automation.yaml_without_id":
            return FakeError("not_found", "Entity not found")
        return {"config": {"id": "a1", "alias": "Morning"}}

    client, ws = client_with({"automation/config": config})
    async with client:
        configs = await client.get_automation_configs(
            ["automation.morning", "automation.yaml_without_id"]
        )

    assert configs == {
        "automation.morning": {"id": "a1", "alias": "Morning"},
        "automation.yaml_without_id": None,
    }
    # Both commands went through one authenticated session.
    assert [m["type"] for m in ws.sent] == ["auth", "automation/config", "automation/config"]


async def test_list_automation_traces() -> None:
    client, _ = client_with(
        {
            "trace/list": [
                {
                    "item_id": "dev_backyard",
                    "run_id": "r1",
                    "state": "stopped",
                    "script_execution": "error",
                    "error": "Action script.does_not_exist not found",
                    "trigger": "state of binary_sensor.movement_backyard",
                    "last_step": "action/1",
                    "domain": "automation",
                    "timestamp": {
                        "start": "2026-11-01T10:00:00+00:00",
                        "finish": "2026-11-01T10:00:01+00:00",
                    },
                }
            ]
        }
    )
    async with client:
        (trace,) = await client.list_automation_traces()

    assert trace.item_id == "dev_backyard"
    assert trace.script_execution == "error"
    assert trace.started_at == START
    assert trace.error.startswith("Action script.does_not_exist")


async def test_list_repair_issues() -> None:
    client, _ = client_with(
        {
            "repairs/list_issues": {
                "issues": [
                    {
                        "domain": "automation",
                        "issue_id": "x_service_not_found",
                        "severity": "error",
                        "ignored": False,
                        "translation_key": "service_not_found",
                        "is_fixable": False,
                    }
                ]
            }
        }
    )
    async with client:
        (issue,) = await client.list_repair_issues()
    assert (issue.domain, issue.severity, issue.translation_key) == (
        "automation",
        "error",
        "service_not_found",
    )
