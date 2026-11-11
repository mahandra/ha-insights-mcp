from pathlib import Path

import pytest

from ha_insights_mcp.actions.policy import (
    ActionRequest,
    Policy,
    PolicyError,
    convert_temperature,
    evaluate,
    load_policy,
    schema_fields,
    selector_bounds,
)
from ha_insights_mcp.ha.models import EntityState
from tests.conftest import STATES, state

EXAMPLE = Path(__file__).parent.parent / "policy.example.toml"

STATE_MAP = {
    s.entity_id: s
    for s in (
        *(EntityState.model_validate(raw) for raw in STATES),
        EntityState.model_validate(state("lock.front_door", "locked", friendly_name="Front")),
        EntityState.model_validate(state("light.office", "unavailable")),
        EntityState.model_validate(
            state("light.warm", "off", min_color_temp_kelvin=2700, max_color_temp_kelvin=4000)
        ),
        EntityState.model_validate(state("switch.server_power", "on")),
        *(EntityState.model_validate(state(f"light.l{i}", "off")) for i in range(12)),
    )
}

SERVICES = {
    "light": {
        "turn_on": {
            "fields": {
                "brightness_pct": {"selector": {"number": {"min": 0, "max": 100}}},
                "advanced_fields": {
                    "collapsed": True,
                    "fields": {
                        "color_temp_kelvin": {
                            "selector": {"color_temp": {"unit": "kelvin", "min": 2000, "max": 6500}}
                        },
                        "transition": {"selector": {"number": {"min": 0, "max": 300}}},
                    },
                },
            }
        },
        "turn_off": {"fields": {"transition": {}}},
    },
    "climate": {
        "set_temperature": {
            "fields": {"temperature": {"selector": {"number": {"min": 0, "max": 250}}}}
        }
    },
    "cover": {
        "set_cover_position": {
            "fields": {"position": {"required": True, "selector": {"number": {"min": 0}}}}
        }
    },
}


@pytest.fixture(scope="module")
def policy() -> Policy:
    base = load_policy(EXAMPLE)
    return base.model_copy(
        update={"deny": base.deny.model_copy(update={"entities": ["switch.server_power"]})}
    )


def decide(policy: Policy, service: str, ids: list[str], **data):
    return evaluate(policy, ActionRequest(service, ids, data), STATE_MAP, SERVICES)


def failed(decision) -> set[str]:
    return {c.name for c in decision.failures}


# ------------------------------------------------------------------ loading


def test_example_policy_loads(policy: Policy) -> None:
    assert policy.allows("light.turn_on")
    assert not policy.allows("alarm_control_panel.alarm_disarm")
    assert policy.risk_of("lock.lock") == "low"
    assert policy.risk_of("lock.unlock") == "high"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text)
    return path


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[domains.light]\nrisk = 'low'\nservices = ['turn_on']\nfeilds = []", "feilds"),
        ("[domains.light]\nrisk = 'none'\nservices = ['turn_on']", "risk"),
        ("[domains.script]\nrisk = 'low'\nservices = ['turn_on']", "never be allowed"),
        (
            "[domains.light]\nrisk = 'low'\nservices = ['on']\nbounds = {x = [0, 1]}",
            "not in fields",
        ),
        (
            "[domains.light]\nrisk = 'low'\nservices = ['on']\nfields = ['x']\n"
            "bounds = {x = [2, 1]}",
            "greater than max",
        ),
        (
            "[domains.lock]\nrisk = 'low'\nservices = ['lock']\nservice_risk = {open = 'high'}",
            "open",
        ),
        ("not toml [", "cannot read"),
    ],
)
def test_invalid_policies_fail_loudly(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        load_policy(write(tmp_path, text))


# ------------------------------------------------------------- schema helpers


def test_schema_fields_flattens_sections() -> None:
    fields = schema_fields(SERVICES, "light", "turn_on")
    assert set(fields) == {"brightness_pct", "color_temp_kelvin", "transition"}
    assert schema_fields(SERVICES, "light", "blink") is None


def test_selector_bounds() -> None:
    assert selector_bounds({"selector": {"number": {"min": 0, "max": 100}}}) == (0, 100)
    assert selector_bounds({"selector": {"number": {"min": 0}}}) == (0, None)
    assert selector_bounds({"selector": {"text": None}}) == (None, None)


# ----------------------------------------------------------------- decisions


def test_allowed_low_risk(policy: Policy) -> None:
    d = decide(policy, "light.turn_on", ["light.kitchen_lights"], brightness_pct=40)
    assert d.allowed, d.failures
    assert (d.risk, d.needs_confirmation) == ("low", False)
    assert d.effective_bounds["brightness_pct"] == (0, 100)


def test_service_not_in_policy(policy: Policy) -> None:
    d = decide(policy, "alarm_control_panel.alarm_disarm", ["lock.front_door"])
    assert not d.allowed
    assert d.risk is None
    assert "light.turn_on" in d.checks[0].detail  # tells the model what is allowed


def test_service_not_in_ha(policy: Policy) -> None:
    d = decide(policy, "light.toggle", ["light.kitchen_lights"])
    assert failed(d) == {"service_exists"}


def test_entity_checks(policy: Policy) -> None:
    d = decide(policy, "light.turn_off", ["light.nope", "climate.hvac", "light.office"])
    assert failed(d) == {"entities_exist", "entities_domain", "entities_available"}


def test_deny_list(policy: Policy) -> None:
    d = evaluate(policy, ActionRequest("switch.turn_off", ["switch.server_power"]), STATE_MAP)
    assert failed(d) == {"entities_not_denied"}


def test_too_many_entities(policy: Policy) -> None:
    d = decide(policy, "light.turn_off", [f"light.l{i}" for i in range(11)])
    assert failed(d) == {"entity_count"}


def test_field_not_allowed_by_policy(policy: Policy) -> None:
    d = decide(policy, "light.turn_on", ["light.kitchen_lights"], flash="long")
    assert "fields_allowed" in failed(d)


def test_required_field_from_schema(policy: Policy) -> None:
    d = decide(policy, "cover.set_cover_position", [])
    assert "fields_valid" in failed(d)


def test_policy_bounds_are_tighter_than_ha_schema(policy: Policy) -> None:
    # HA's selector says 0..250, the thermostat says 7..35, the policy says 16..26.
    d = decide(policy, "climate.set_temperature", ["climate.hvac"], temperature=30)
    assert failed(d) == {"value.temperature"}
    assert "outside [16, 26]" in d.failures[0].detail
    assert d.risk == "medium"
    assert d.needs_confirmation


def test_entity_limits_tighten_bounds(policy: Policy) -> None:
    d = decide(policy, "light.turn_on", ["light.warm"], color_temp_kelvin=5000)
    assert d.effective_bounds["color_temp_kelvin"] == (2700, 4000)
    assert failed(d) == {"value.color_temp_kelvin"}


def test_non_numeric_value_for_bounded_field(policy: Policy) -> None:
    for value in ("50", True, float("nan")):
        d = decide(policy, "light.turn_on", ["light.kitchen_lights"], brightness_pct=value)
        assert failed(d) == {"value.brightness_pct"}


def test_option_must_be_supported_by_entity(policy: Policy) -> None:
    ok = evaluate(
        policy,
        ActionRequest("climate.set_hvac_mode", ["climate.hvac"], {"hvac_mode": "heat"}),
        STATE_MAP,
    )
    assert ok.allowed
    bad = evaluate(
        policy,
        ActionRequest("climate.set_hvac_mode", ["climate.hvac"], {"hvac_mode": "dry"}),
        STATE_MAP,
    )
    assert "options: off, heat, cool, auto" in bad.failures[0].detail


def test_service_risk_override(policy: Policy) -> None:
    d = evaluate(policy, ActionRequest("lock.unlock", ["lock.front_door"]), STATE_MAP)
    assert d.allowed
    assert (d.risk, d.needs_confirmation) == ("high", True)


def test_bulk_action_raises_risk(policy: Policy) -> None:
    d = decide(policy, "light.turn_off", [f"light.l{i}" for i in range(4)])
    assert d.allowed
    assert (d.risk, d.needs_confirmation) == ("medium", True)


def test_temperature_bounds_follow_ha_unit_system(policy: Policy) -> None:
    # A US installation: HA reports and accepts °F. The policy's 16..26 °C becomes
    # 60.8..78.8 °F, so "24" (which would be -4 °C) is refused.
    fahrenheit = EntityState.model_validate(
        state("climate.us", "heat", temperature=68, min_temp=45, max_temp=95)
    )
    states = {"climate.us": fahrenheit}

    def decide_f(value: float):
        request = ActionRequest("climate.set_temperature", ["climate.us"], {"temperature": value})
        return evaluate(policy, request, states, SERVICES, temperature_unit="°F")

    low = decide_f(24)
    assert not low.allowed
    assert low.failures[0].detail == "24 is outside [60.8, 78.8] °F"
    assert low.effective_bounds["temperature"] == (60.8, 78.8)
    assert low.units["temperature"] == "°F"
    assert decide_f(72).allowed


def test_convert_temperature() -> None:
    assert convert_temperature(16, "°C", "°F") == 60.8
    assert convert_temperature(78.8, "°F", "°C") == 26
    assert convert_temperature(20, "°C", "°C") == 20
