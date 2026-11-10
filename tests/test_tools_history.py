"""End-to-end history tool tests with a fixed clock."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import call, error_text, mcp_client

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 1, 12, 0, 30, 123456, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # rounded down to the minute


def ts(hours_ago: float) -> float:
    return (WINDOW_END - timedelta(hours=hours_ago)).timestamp()


def history(command: dict) -> dict:
    data = {
        "light.bed_light": [
            {"s": "off", "lu": ts(10)},
            {"s": "on", "lu": ts(8)},
            {"s": "off", "lu": ts(2)},
        ],
        "light.kitchen_lights": [{"s": "on", "lu": ts(10)}],
        "climate.hvac": [{"s": "cool", "lu": ts(10)}, {"s": "heat", "lu": ts(1)}],
        "sun.sun": [{"s": "above_horizon", "lu": ts(10)}],
        "sensor.outside_temperature": [{"s": "10", "lu": ts(10)}, {"s": "20", "lu": ts(5)}],
    }
    return {e: data[e] for e in command["entity_ids"] if e in data}


def statistics(command: dict) -> dict:
    start = (WINDOW_END - timedelta(hours=2)).timestamp() * 1000
    hour = 3_600_000
    return {
        "sensor.outside_temperature": [
            {"start": start, "end": start + hour, "min": 14.0, "mean": 15.0, "max": 16.0},
            {
                "start": start + hour,
                "end": start + 2 * hour,
                "min": 15.0,
                "mean": 17.0,
                "max": 19.0,
            },
        ]
    }


@pytest.fixture
async def client(ha_api):
    ws = {"history/history_during_period": history, "recorder/statistics_during_period": statistics}
    async with mcp_client(ws, now=lambda: NOW) as c:
        yield c


async def test_summarize_discrete(client) -> None:
    out = await call(client, "summarize_history", entity_id="light.bed_light", hours=10)
    assert out["kind"] == "discrete"
    assert out["numeric"] is None
    assert out["discrete"]["changes"] == 2
    assert out["discrete"]["time_in_state_pct"] == {"on": 60.0, "off": 40.0}
    assert [c["state"] for c in out["discrete"]["recent_changes"]] == ["on", "off"]


async def test_summarize_numeric_uses_statistics(client) -> None:
    out = await call(
        client, "summarize_history", entity_id="sensor.outside_temperature", hours=2, buckets=2
    )
    assert out["kind"] == "numeric"
    assert out["unit"] == "°C"
    numeric = out["numeric"]
    assert numeric["source"] == "statistics"
    assert (numeric["min"], numeric["mean"], numeric["max"]) == (14.0, 16.0, 19.0)
    assert [b["start"] for b in numeric["buckets"]] == [
        "2026-11-01T10:00:00Z",
        "2026-11-01T11:00:00Z",
    ]


async def test_summarize_numeric_falls_back_to_history(ha_api) -> None:
    ws = {"history/history_during_period": history, "recorder/statistics_during_period": {}}
    async with mcp_client(ws, now=lambda: NOW) as client:
        out = await call(
            client, "summarize_history", entity_id="sensor.outside_temperature", hours=10
        )
    assert out["numeric"]["source"] == "history"
    assert out["numeric"]["mean"] == 15.0  # 10 for 5h, 20 for 5h


async def test_summarize_unknown_entity(client) -> None:
    result = await client.call_tool("summarize_history", {"entity_id": "light.nope"})
    assert "search_entities" in error_text(result)


async def test_summarize_rejects_too_long_window(client) -> None:
    result = await client.call_tool(
        "summarize_history", {"entity_id": "light.bed_light", "hours": 1000}
    )
    assert result.is_error


async def test_rank_activity_excludes_numeric_and_idle(client) -> None:
    out = await call(client, "rank_activity", hours=10)
    assert out["numeric_sensors_excluded"] is True
    assert out["considered"] == 4  # sensor.outside_temperature excluded
    assert [(e["entity_id"], e["changes"]) for e in out["entities"]] == [
        ("light.bed_light", 2),
        ("climate.hvac", 1),
    ]


async def test_rank_activity_with_domain_includes_numeric(client) -> None:
    out = await call(client, "rank_activity", hours=10, domain="sensor")
    assert out["numeric_sensors_excluded"] is False
    assert [(e["entity_id"], e["changes"]) for e in out["entities"]] == [
        ("sensor.outside_temperature", 1)
    ]


async def test_rank_activity_by_area(client) -> None:
    out = await call(client, "rank_activity", hours=10, area="Bedroom")
    assert [e["entity_id"] for e in out["entities"]] == ["light.bed_light", "climate.hvac"]
