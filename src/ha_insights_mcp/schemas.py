"""MCP-facing output models: what the LLM actually sees.

Kept deliberately small. Field descriptions end up in each tool's output schema,
so they double as documentation for the model.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EntitySummary(BaseModel):
    entity_id: str
    name: str = Field(description="Friendly name")
    state: str = Field(description="Current state, e.g. 'on', '21.5', 'unavailable'")
    area: str | None = Field(description="Area name, null if the entity has no area")


class SearchEntitiesResult(BaseModel):
    total: int = Field(description="Number of matching entities before applying the limit")
    truncated: bool = Field(description="True if more matches exist; narrow the query or filters")
    entities: list[EntitySummary]


class EntityDetails(BaseModel):
    entity_id: str
    name: str
    domain: str
    state: str
    unit: str | None = Field(description="Unit of measurement of the state, if any")
    area: str | None
    device: str | None = Field(description="Name of the device this entity belongs to")
    last_changed: datetime = Field(description="When the state value last changed (UTC)")
    attributes: dict[str, Any] = Field(
        description="Relevant attributes for this domain; noisy/internal attributes are omitted"
    )


class AreaSummary(BaseModel):
    area_id: str
    name: str
    entity_count: int


class ListAreasResult(BaseModel):
    areas: list[AreaSummary]
    unassigned_entity_count: int = Field(description="Entities that belong to no area")


# ------------------------------------------------------------------ history


class HistoryBucket(BaseModel):
    start: datetime
    min: float | None
    mean: float | None
    max: float | None


class NumericHistory(BaseModel):
    min: float | None
    mean: float | None
    max: float | None
    missing_pct: float = Field(description="% of time the entity was unavailable/unknown")
    buckets: list[HistoryBucket] = Field(description="Evenly sized time buckets, oldest first")
    source: str = Field(description="'statistics' (HA long-term stats) or 'history' (raw)")


class StateChange(BaseModel):
    at: datetime
    state: str


class DiscreteHistory(BaseModel):
    changes: int = Field(description="Number of state changes in the window")
    time_in_state_pct: dict[str, float] = Field(
        description="Share of the window spent in each state, in percent"
    )
    recent_changes: list[StateChange] = Field(description="Up to 10 latest changes, oldest first")


class HistorySummary(BaseModel):
    entity_id: str
    name: str
    unit: str | None
    current_state: str
    window_hours: int
    kind: Literal["numeric", "discrete"]
    numeric: NumericHistory | None = Field(description="Set when kind == 'numeric'")
    discrete: DiscreteHistory | None = Field(description="Set when kind == 'discrete'")


class ActivityEntry(BaseModel):
    entity_id: str
    name: str
    area: str | None
    changes: int
    last_changed: datetime


class ActivityResult(BaseModel):
    window_hours: int
    considered: int = Field(description="Entities whose history was analysed")
    numeric_sensors_excluded: bool = Field(
        description="True if numeric sensors were skipped (pass domain='sensor' to include)"
    )
    entities: list[ActivityEntry] = Field(description="Most state changes first; idle omitted")


# ------------------------------------------------------------------- health


class HealthIssueOut(BaseModel):
    code: str = Field(description="unavailable | low_battery | stale | flapping | unknown_state")
    severity: Literal["error", "warning", "info"]
    entity_id: str
    name: str
    device: str | None
    area: str | None
    detail: str
    since: datetime | None = Field(description="When the problem started, if known")


class RepairIssueOut(BaseModel):
    domain: str
    issue: str = Field(description="HA's issue key, e.g. 'service_not_found'")
    severity: str


class HealthReport(BaseModel):
    counts: dict[str, int] = Field(
        description="Issues per severity across all checks, before min_severity/limit"
    )
    issues: list[HealthIssueOut] = Field(description="Most severe first")
    truncated: bool
    ha_repairs: list[RepairIssueOut] = Field(
        description="Problems Home Assistant itself reports (Settings -> Repairs)"
    )
    checks_skipped: list[str] = Field(description="Checks not run and why")


# -------------------------------------------------------------- automations

RunResult = Literal["ok", "error", "dropped", "running", "none"]


class AutomationSummary(BaseModel):
    entity_id: str
    name: str
    state: str = Field(description="on | off | unavailable (config failed to load)")
    mode: str | None
    last_triggered: datetime | None
    last_run: RunResult = Field(description="Result of the newest stored run")


class AutomationList(BaseModel):
    automations: list[AutomationSummary]


class AutomationRun(BaseModel):
    started_at: datetime
    result: RunResult
    trigger: str | None
    error: str | None


class AutomationDetails(BaseModel):
    entity_id: str
    name: str
    state: str
    mode: str | None
    description: str | None
    last_triggered: datetime | None
    config_available: bool = Field(description="False for YAML automations without an id")
    triggers: list[Any]
    conditions: list[Any]
    actions: list[Any]
    referenced_entities: list[str]
    recent_runs: list[AutomationRun] = Field(
        description="Newest first; HA stores only the last few runs per automation"
    )


class AuditFinding(BaseModel):
    entity_id: str
    name: str
    code: str = Field(
        description=(
            "config_invalid | missing_entity | missing_device | last_run_failed | "
            "unavailable_entity | runs_dropped | disabled | never_triggered | "
            "not_triggered_recently | no_id | templated_refs"
        )
    )
    severity: Literal["error", "warning", "info"]
    message: str


class AuditReport(BaseModel):
    automations_checked: int
    counts: dict[str, int] = Field(description="Findings per severity, before min_severity")
    findings: list[AuditFinding] = Field(description="Most severe first")


# ------------------------------------------------------------------ actions

RiskOut = Literal["low", "medium", "high"]


class ActionCheckOut(BaseModel):
    check: str = Field(description="Rule name, e.g. service_allowed, value.temperature")
    ok: bool
    detail: str


class PlannedChange(BaseModel):
    entity_id: str
    name: str
    current_state: str
    expected: str | None = Field(
        description="Expected change, e.g. 'state off -> on'; null if it cannot be predicted"
    )
    already_satisfied: bool = Field(description="The entity is already in the expected state")


class Bounds(BaseModel):
    min: float | None
    max: float | None


class ActionPreview(BaseModel):
    allowed: bool = Field(description="True if execute_action would pass every policy check now")
    risk: RiskOut | None = Field(description="Null when the service is not in the policy")
    needs_confirmation: bool = Field(description="execute_action will ask the human to confirm")
    confirmation_available: bool = Field(
        description="The MCP client can show a confirmation to the human (elicitation)"
    )
    checks: list[ActionCheckOut]
    changes: list[PlannedChange]
    allowed_ranges: dict[str, Bounds] = Field(
        description="Allowed values per data field (policy ∩ device limits ∩ HA schema)"
    )
    next_step: str
