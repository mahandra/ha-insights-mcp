"""execute_action end to end: policy, human confirmation, HA call, verification, audit."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import types

from ha_insights_mcp.actions.policy import load_policy
from tests.conftest import (
    ACTION_STATES,
    EXAMPLE_POLICY,
    HA_SERVICES,
    REGISTRY_RESULTS,
    STATES,
    FakeError,
    FakeWebSocket,
    actions_context,
    call,
    mcp_client,
)

pytestmark = pytest.mark.anyio

EFFECTS = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "close_cover": "closed",
}


class FakeHome:
    """HA states that call_service actually changes, so verification can be tested."""

    def __init__(self) -> None:
        self.states = {s["entity_id"]: copy.deepcopy(s) for s in STATES + ACTION_STATES}
        self.calls: list[dict[str, Any]] = []
        # entity_id -> state it ends up in instead of the requested one (e.g. "jammed")
        self.misbehave: dict[str, str] = {}
        self.reject: FakeError | None = None

    def call_service(self, command: dict[str, Any]) -> Any:
        self.calls.append(command)
        if self.reject:
            return self.reject
        for entity_id in command["target"]["entity_id"]:
            new = self.misbehave.get(entity_id) or EFFECTS.get(command["service"])
            if new:
                self.states[entity_id]["state"] = new
        return {"context": {"id": f"ctx{len(self.calls)}"}}

    def states_response(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=list(self.states.values()))


def answering(action: str, content: dict | None = None, seen: list | None = None) -> Callable:
    async def callback(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        if seen is not None:
            seen.append(params)
        return types.ElicitResult(action=action, content=content)

    return callback


ACCEPT = answering("accept", {"confirm": True})


@pytest.fixture
def home(ha_api) -> FakeHome:
    home = FakeHome()
    ha_api.get("/api/states").mock(side_effect=home.states_response)
    return home


@pytest.fixture
def actions(tmp_path: Path):
    context = actions_context(tmp_path)
    yield context
    context.audit_log.close()


def session(home: FakeHome, actions, **options):
    ws = FakeWebSocket(
        {**REGISTRY_RESULTS, "get_services": HA_SERVICES, "call_service": home.call_service}
    )
    return mcp_client(actions=actions, ws=ws, **options)


async def execute(client, service: str, entity_ids: list[str], **data) -> dict:
    args = {"service": service, "entity_ids": entity_ids, "reason": "the user asked for it"}
    if data:
        args["data"] = data
    return await call(client, "execute_action", **args)


# ----------------------------------------------------------------- low risk


async def test_low_risk_runs_without_asking(home, actions) -> None:
    seen: list = []
    async with session(home, actions, elicitation_callback=answering("decline", seen=seen)) as c:
        out = await execute(c, "light.turn_on", ["light.bed_light"], brightness_pct=40)
    assert seen == []  # nobody was asked
    assert (out["outcome"], out["confirmation"]) == ("executed", "not_required")
    assert out["results"] == [
        {
            "entity_id": "light.bed_light",
            "before": "off",
            "after": "on",
            "verification": "confirmed",
        }
    ]
    (command,) = home.calls
    assert command["service_data"] == {"brightness_pct": 40}
    assert command["target"] == {"entity_id": ["light.bed_light"]}

    (entry,) = await actions.audit_log.recent()
    assert entry.id == out["action_id"]
    assert (entry.outcome, entry.context_id, entry.reason) == (
        "executed",
        "ctx1",
        "the user asked for it",
    )
    assert (entry.before, entry.after) == ({"light.bed_light": "off"}, {"light.bed_light": "on"})


async def test_denied_by_policy_is_logged_and_not_sent(home, actions) -> None:
    async with session(home, actions) as c:
        out = await execute(c, "climate.set_temperature", ["climate.hvac"], temperature=30)
    assert out["outcome"] == "denied_policy"
    assert [f["check"] for f in out["failed_checks"]] == ["value.temperature"]
    assert home.calls == []
    assert (await actions.audit_log.recent())[0].outcome == "denied_policy"


# ---------------------------------------------------------------- risky


@pytest.mark.parametrize("mode", ["legacy", "modern"])
async def test_risky_action_runs_after_human_accepts(home, actions, mode) -> None:
    # legacy: a live elicitation/create request; modern (2026-07-28): the question rides
    # an InputRequiredResult and the client retries the call with the answer.
    seen: list = []
    options = {"mode": "legacy"} if mode == "legacy" else {}
    callback = answering("accept", {"confirm": True}, seen)
    async with session(home, actions, elicitation_callback=callback, **options) as c:
        out = await execute(c, "lock.unlock", ["lock.front_door"])
    assert (out["outcome"], out["confirmation"], out["risk"]) == ("executed", "accepted", "high")
    assert out["results"][0]["verification"] == "confirmed"
    assert len(home.calls) == 1
    (params,) = seen
    assert "Front Door (lock.front_door): state locked -> unlocked" in params.message
    assert "Assistant's reason (unverified): the user asked for it" in params.message


@pytest.mark.parametrize(
    ("action", "content", "outcome"),
    [
        ("decline", None, "declined_by_user"),
        ("cancel", None, "cancelled_by_user"),
        ("accept", {"confirm": False}, "declined_by_user"),  # box not ticked
    ],
)
async def test_risky_action_refused_by_human(home, actions, action, content, outcome) -> None:
    async with session(home, actions, elicitation_callback=answering(action, content)) as c:
        out = await execute(c, "lock.unlock", ["lock.front_door"])
    assert out["outcome"] == outcome
    assert home.calls == []
    assert (await actions.audit_log.recent())[0].outcome == outcome


async def test_risky_action_refused_without_confirmation_channel(home, actions) -> None:
    async with session(home, actions) as c:  # no elicitation_callback
        out = await execute(c, "lock.unlock", ["lock.front_door"])
    assert (out["outcome"], out["confirmation"]) == ("no_confirmation_channel", "unavailable")
    assert home.calls == []


async def test_bulk_action_needs_confirmation(home, actions) -> None:
    lights = [f"light.bulk_{i}" for i in range(4)]
    async with session(home, actions) as c:
        out = await execute(c, "light.turn_on", lights)
    assert (out["outcome"], out["risk"]) == ("no_confirmation_channel", "medium")


async def test_reason_is_required(home, actions) -> None:
    async with session(home, actions) as c:
        result = await c.call_tool(
            "execute_action", {"service": "light.turn_on", "entity_ids": ["light.bed_light"]}
        )
    assert result.is_error
    assert home.calls == []


# --------------------------------------------------------- HA and devices


async def test_device_that_does_not_obey_is_reported(home, actions) -> None:
    home.misbehave["lock.front_door"] = "jammed"
    async with session(home, actions, elicitation_callback=ACCEPT) as c:
        out = await execute(c, "lock.unlock", ["lock.front_door"])
    assert out["outcome"] == "executed"
    assert out["results"][0] == {
        "entity_id": "lock.front_door",
        "before": "locked",
        "after": "jammed",
        "verification": "not_confirmed",
    }
    assert "did not end up" in out["message"]
    assert (await actions.audit_log.recent())[0].verification == "not_confirmed"


async def test_ha_rejection_is_failed(home, actions) -> None:
    home.reject = FakeError("invalid_format", "value must be at most 100")
    async with session(home, actions) as c:
        out = await execute(c, "light.turn_on", ["light.bed_light"], brightness_pct=50)
    assert out["outcome"] == "failed"
    assert "at most 100" in out["message"]
    assert out["results"] == []


async def test_rate_limit(home, tmp_path) -> None:
    policy = load_policy(EXAMPLE_POLICY)
    policy = policy.model_copy(
        update={"defaults": policy.defaults.model_copy(update={"rate_limit_per_minute": 1})}
    )
    actions = actions_context(tmp_path, policy)
    async with session(home, actions) as c:
        first = await execute(c, "light.turn_on", ["light.bed_light"])
        second = await execute(c, "light.turn_off", ["light.bed_light"])
    actions.audit_log.close()
    assert first["outcome"] == "executed"
    assert second["outcome"] == "rate_limited"
    assert len(home.calls) == 1


async def test_tool_annotations(home, actions) -> None:
    async with session(home, actions) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    execute_tool = tools["execute_action"]
    assert execute_tool.annotations.destructive_hint is True
    assert execute_tool.annotations.read_only_hint is False
    assert "confirmation" not in execute_tool.input_schema["properties"]
    assert set(execute_tool.input_schema["required"]) == {"service", "entity_ids", "reason"}
    assert tools["preview_action"].annotations.read_only_hint is True


async def test_action_log_tool(home, actions) -> None:
    async with session(home, actions) as c:
        await execute(c, "light.turn_on", ["light.bed_light"])
        await execute(c, "lock.unlock", ["lock.front_door"])  # refused: no channel
        out = await call(c, "get_action_log")
        only_lock = await call(c, "get_action_log", entity_id="lock.front_door")
        executed = await call(c, "get_action_log", outcome="executed")
    assert [e["outcome"] for e in out["entries"]] == ["no_confirmation_channel", "executed"]
    assert out["entries"][1]["verification"] == "confirmed"
    assert [e["service"] for e in only_lock["entries"]] == ["lock.unlock"]
    assert [e["service"] for e in executed["entries"]] == ["light.turn_on"]


async def test_confirmation_marks_no_op_targets(home, actions) -> None:
    seen: list = []
    callback = answering("decline", seen=seen)
    async with session(home, actions, elicitation_callback=callback) as c:
        await execute(c, "cover.close_cover", ["cover.garage_door"])
    assert "state closed -> closed (already in this state)" in seen[0].message
