"""Pure automation audit: config references, run results, staleness.

Inputs are plain data (configs, traces, snapshot); output is a list of findings with
stable codes. No I/O, no MCP.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ha_insights_mcp.analytics.health import SEVERITY_ORDER, Severity
from ha_insights_mcp.ha.models import ENTITY_ID_RE, AutomationTrace
from ha_insights_mcp.snapshot import HomeSnapshot

NOT_TRIGGERED_DAYS = 30
# Values allowed in entity_id fields that are not entity references.
SPECIAL_ENTITY_VALUES = frozenset({"all", "none"})


@dataclass(frozen=True, slots=True)
class AutomationInfo:
    entity_id: str
    name: str
    state: str  # on | off | unavailable
    config_id: str | None
    mode: str | None
    last_triggered: datetime | None
    config: dict[str, Any] | None
    # Stored runs of this automation, oldest first (HA keeps only the last few).
    traces: list[AutomationTrace] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Finding:
    entity_id: str
    code: str
    severity: Severity
    message: str


@dataclass(frozen=True, slots=True)
class References:
    entities: set[str]
    devices: set[str]
    templated: bool  # some references are Jinja templates and cannot be checked statically


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and "," in value:
        return [v.strip() for v in value.split(",")]
    return [value]


def extract_references(config: dict[str, Any]) -> References:
    """Entity and device ids referenced anywhere in triggers, conditions and actions."""
    entities: set[str] = set()
    devices: set[str] = set()
    templated = False
    for section in ("triggers", "trigger", "conditions", "condition", "actions", "action"):
        for key, value in _walk(config.get(section)):
            if key not in ("entity_id", "device_id"):
                continue
            for item in _as_list(value):
                if not isinstance(item, str):
                    continue
                if "{{" in item or "{%" in item:
                    templated = True
                elif key == "device_id":
                    devices.add(item)
                elif item not in SPECIAL_ENTITY_VALUES and ENTITY_ID_RE.fullmatch(item):
                    entities.add(item)
    return References(entities=entities, devices=devices, templated=templated)


def latest_run(info: AutomationInfo) -> str:
    """'ok' | 'error' | 'dropped' | 'running' | 'none' for the newest stored run."""
    return run_result(info.traces[-1]) if info.traces else "none"


def run_result(trace: AutomationTrace) -> str:
    """'ok' | 'error' | 'dropped' | 'running' for one stored run."""
    if trace.state == "running":
        return "running"
    if trace.script_execution == "error" or trace.error:
        return "error"
    if trace.script_execution in ("failed_single", "failed_max_runs"):
        return "dropped"
    return "ok"


def audit_automation(info: AutomationInfo, snapshot: HomeSnapshot, now: datetime) -> list[Finding]:
    def finding(code: str, severity: Severity, message: str) -> Finding:
        return Finding(info.entity_id, code, severity, message)

    if info.state == "unavailable":
        return [finding("config_invalid", "error", "automation failed to load: config is invalid")]

    findings = []
    if info.config is None:
        findings.append(
            finding(
                "no_id",
                "info",
                "YAML automation without an id: config not readable, not editable in the UI",
            )
        )
    else:
        refs = extract_references(info.config)
        missing = sorted(refs.entities - snapshot.entities.keys())
        if missing:
            findings.append(
                finding(
                    "missing_entity", "error", "references missing entities: " + ", ".join(missing)
                )
            )
        unavailable = sorted(
            e
            for e in refs.entities & snapshot.entities.keys()
            if snapshot.entities[e].state.state == "unavailable"
        )
        if unavailable:
            findings.append(
                finding(
                    "unavailable_entity",
                    "warning",
                    "references unavailable entities: " + ", ".join(unavailable),
                )
            )
        known_devices = {v.device.id for v in snapshot.entities.values() if v.device}
        missing_devices = sorted(refs.devices - known_devices)
        if missing_devices:
            findings.append(
                finding(
                    "missing_device",
                    "error",
                    "references missing devices: " + ", ".join(missing_devices),
                )
            )
        if refs.templated:
            findings.append(
                finding("templated_refs", "info", "some references are templates; not checked")
            )

    failed = [t for t in info.traces if t.script_execution == "error" or t.error]
    if info.traces and latest_run(info) == "error":
        last_error = failed[-1].error or "unknown error"
        findings.append(
            finding(
                "last_run_failed",
                "error",
                f"{len(failed)} of the last {len(info.traces)} runs failed; "
                f"latest error: {last_error}",
            )
        )
    dropped = [t for t in info.traces if t.script_execution in ("failed_single", "failed_max_runs")]
    if dropped:
        findings.append(
            finding(
                "runs_dropped",
                "warning",
                f"{len(dropped)} of the last {len(info.traces)} triggers were dropped because the "
                f"automation was still running (mode: {info.mode or 'single'})",
            )
        )

    if info.state == "off":
        findings.append(finding("disabled", "info", "automation is turned off"))
    elif info.last_triggered is None:
        findings.append(finding("never_triggered", "info", "has never been triggered"))
    elif now - info.last_triggered > timedelta(days=NOT_TRIGGERED_DAYS):
        days = (now - info.last_triggered).days
        findings.append(
            finding("not_triggered_recently", "info", f"last triggered {days} days ago")
        )
    return findings


def audit_all(infos: list[AutomationInfo], snapshot: HomeSnapshot, now: datetime) -> list[Finding]:
    findings = [f for info in infos for f in audit_automation(info, snapshot, now)]
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code, f.entity_id))
