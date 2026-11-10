from datetime import UTC, datetime, timedelta

from ha_insights_mcp.analytics.audit import (
    AutomationInfo,
    audit_all,
    audit_automation,
    extract_references,
    latest_run,
)
from ha_insights_mcp.ha.models import AutomationTrace, EntityState, Registries
from ha_insights_mcp.snapshot import HomeSnapshot
from tests.conftest import REGISTRY_RESULTS, STATES

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def snapshot(**overrides: str) -> HomeSnapshot:
    states = [EntityState.model_validate(s) for s in STATES]
    states = [s.model_copy(update={"state": overrides.get(s.entity_id, s.state)}) for s in states]
    registries = Registries(
        areas=REGISTRY_RESULTS["config/area_registry/list"],
        devices=REGISTRY_RESULTS["config/device_registry/list"],
        entities=REGISTRY_RESULTS["config/entity_registry/list"],
    )
    return HomeSnapshot.build(states, registries)


def trace(minutes_ago: int, execution: str = "finished", error: str | None = None):
    return AutomationTrace(
        item_id="a1",
        run_id=f"r{minutes_ago}",
        state="stopped",
        script_execution=execution,
        error=error,
        started_at=NOW - timedelta(minutes=minutes_ago),
    )


def info(
    config: dict | None = None,
    *,
    state: str = "on",
    last_triggered: datetime | None = NOW - timedelta(days=1),
    traces: list[AutomationTrace] | None = None,
) -> AutomationInfo:
    return AutomationInfo(
        entity_id="automation.test",
        name="Test",
        state=state,
        config_id="a1",
        mode="single",
        last_triggered=last_triggered,
        config=config if config is not None else {"triggers": [], "actions": []},
        traces=traces or [],
    )


def codes(findings) -> list[str]:
    return [f.code for f in findings]


# --------------------------------------------------------------- references


def test_extract_references_all_shapes() -> None:
    config = {
        "triggers": [
            {"trigger": "state", "entity_id": ["binary_sensor.a", "binary_sensor.b"]},
            {"trigger": "device", "device_id": "dev1", "domain": "zha"},
        ],
        "conditions": [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.x, light.y"}},
            {"action": "light.turn_off", "target": {"entity_id": "all"}},
            {"choose": [{"sequence": [{"action": "switch.toggle", "entity_id": "switch.deep"}]}]},
            {"action": "light.turn_on", "target": {"entity_id": "{{ states('input_text.l') }}"}},
        ],
        # Outside triggers/conditions/actions: ignored.
        "variables": {"entity_id": "sensor.not_a_ref"},
    }
    refs = extract_references(config)
    assert refs.entities == {
        "binary_sensor.a",
        "binary_sensor.b",
        "sun.sun",
        "light.x",
        "light.y",
        "switch.deep",
    }
    assert refs.devices == {"dev1"}
    assert refs.templated is True


def test_extract_references_legacy_singular_keys() -> None:
    config = {"trigger": {"platform": "state", "entity_id": "light.a"}, "action": []}
    assert extract_references(config).entities == {"light.a"}


# -------------------------------------------------------------------- rules


def test_healthy_automation_has_no_findings() -> None:
    config = {"triggers": [{"trigger": "state", "entity_id": "light.bed_light"}], "actions": []}
    assert audit_automation(info(config, traces=[trace(5)]), snapshot(), NOW) == []


def test_config_invalid_short_circuits() -> None:
    findings = audit_automation(info(None, state="unavailable"), snapshot(), NOW)
    assert codes(findings) == ["config_invalid"]


def test_missing_and_unavailable_entities_and_devices() -> None:
    config = {
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.gone"}],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.kitchen_lights"}},
            {"device_id": "dev_unknown", "domain": "light", "type": "turn_on"},
        ],
    }
    findings = audit_automation(
        info(config), snapshot(**{"light.kitchen_lights": "unavailable"}), NOW
    )
    by_code = {f.code: f for f in findings}
    assert by_code["missing_entity"].message.endswith("binary_sensor.gone")
    assert by_code["unavailable_entity"].message.endswith("light.kitchen_lights")
    assert by_code["missing_device"].message.endswith("dev_unknown")


def test_known_device_is_fine() -> None:
    config = {"triggers": [{"trigger": "device", "device_id": "dev_hvac"}], "actions": []}
    assert audit_automation(info(config), snapshot(), NOW) == []


def test_last_run_failed_counts_failures() -> None:
    traces = [trace(30, "error", "boom 1"), trace(20), trace(10, "error", "boom 2")]
    findings = audit_automation(info(traces=traces), snapshot(), NOW)
    (failed,) = [f for f in findings if f.code == "last_run_failed"]
    assert failed.message == "2 of the last 3 runs failed; latest error: boom 2"


def test_old_failure_with_successful_latest_run_is_not_reported() -> None:
    traces = [trace(30, "error", "boom"), trace(10)]
    assert "last_run_failed" not in codes(audit_automation(info(traces=traces), snapshot(), NOW))


def test_runs_dropped() -> None:
    traces = [trace(30), trace(20, "failed_single"), trace(10, "failed_single")]
    findings = audit_automation(info(traces=traces), snapshot(), NOW)
    assert codes(findings) == ["runs_dropped"]
    assert findings[0].message.startswith("2 of the last 3 triggers were dropped")


def test_trigger_staleness() -> None:
    assert codes(audit_automation(info(state="off"), snapshot(), NOW)) == ["disabled"]
    assert codes(audit_automation(info(last_triggered=None), snapshot(), NOW)) == [
        "never_triggered"
    ]
    old = info(last_triggered=NOW - timedelta(days=45))
    (finding,) = audit_automation(old, snapshot(), NOW)
    assert (finding.code, finding.message) == (
        "not_triggered_recently",
        "last triggered 45 days ago",
    )


def test_no_config_means_no_id() -> None:
    automation = AutomationInfo("automation.yaml", "Yaml", "on", None, "single", NOW, None)
    assert codes(audit_automation(automation, snapshot(), NOW)) == ["no_id"]


def test_latest_run() -> None:
    assert latest_run(info()) == "none"
    assert latest_run(info(traces=[trace(10, "error", "x")])) == "error"
    assert latest_run(info(traces=[trace(20, "error", "x"), trace(10)])) == "ok"
    assert latest_run(info(traces=[trace(10, "failed_single")])) == "dropped"


def test_audit_all_sorted_by_severity() -> None:
    broken = info({"triggers": [{"trigger": "state", "entity_id": "light.gone"}], "actions": []})
    disabled = AutomationInfo("automation.a", "A", "off", "a", "single", NOW, {"actions": []})
    findings = audit_all([disabled, broken], snapshot(), NOW)
    assert [(f.severity, f.code) for f in findings] == [
        ("error", "missing_entity"),
        ("info", "disabled"),
    ]
