import pytest

from ha_insights_mcp.actions.effects import Expectation, describe, expect, is_met, verify
from ha_insights_mcp.ha.models import EntityState
from tests.conftest import state


def st(entity_id: str, value: str, **attributes) -> EntityState:
    return EntityState.model_validate(state(entity_id, value, **attributes))


@pytest.mark.parametrize(
    ("service", "data", "current", "expected"),
    [
        ("light.turn_on", {"brightness_pct": 40}, "off", "on"),
        ("switch.turn_off", {}, "on", "off"),
        ("light.toggle", {}, "on", "off"),
        ("lock.unlock", {}, "locked", "unlocked"),
        ("cover.close_cover", {}, "open", "closed"),
        ("climate.set_hvac_mode", {"hvac_mode": "heat"}, "cool", "heat"),
    ],
)
def test_expected_state(service: str, data: dict, current: str, expected: str) -> None:
    domain = service.split(".")[0]
    assert expect(service, data, st(f"{domain}.x", current)).state == expected


def test_expected_attributes() -> None:
    exp = expect("climate.set_temperature", {"temperature": 24}, st("climate.x", "heat"))
    assert exp == Expectation(state=None, attributes={"temperature": 24})


def test_unpredictable_effects() -> None:
    assert expect("light.toggle", {}, st("light.x", "unknown")) is None
    assert expect("media_player.play_media", {}, st("media_player.x", "idle")) is None


def test_verify() -> None:
    exp = Expectation(state="closed")
    assert verify(exp, st("cover.x", "closed")) == "confirmed"
    assert verify(exp, st("cover.x", "closing")) == "pending"
    assert verify(exp, st("cover.x", "open")) == "not_confirmed"
    assert verify(None, st("cover.x", "open")) == "not_checked"
    # A jammed lock is a failure, not "still moving".
    assert verify(Expectation(state="unlocked"), st("lock.x", "jammed")) == "not_confirmed"
    temp = Expectation(attributes={"temperature": 24})
    assert verify(temp, st("climate.x", "heat", temperature=24.0)) == "confirmed"


def test_describe_and_already_satisfied() -> None:
    current = st("climate.x", "heat", temperature=21)
    exp = expect("climate.set_temperature", {"temperature": 24}, current)
    assert describe(exp, current) == "temperature 21 -> 24"
    assert not is_met(exp, current)
    on = st("light.x", "on")
    assert is_met(expect("light.turn_on", {}, on), on)
