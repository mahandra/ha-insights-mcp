"""Internal typed views of Home Assistant data.

These mirror the parts of HA payloads we use. Unknown fields are ignored so that HA
releases adding fields do not break us. These are *not* what the LLM sees — see
`ha_insights_mcp.schemas` for the compact MCP-facing models.
"""

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

ENTITY_ID_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"
ENTITY_ID_RE = re.compile(ENTITY_ID_PATTERN)


class _HAModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class EntityState(_HAModel):
    entity_id: str
    state: str
    attributes: dict[str, Any] = {}
    last_changed: datetime
    last_updated: datetime
    # Updated on every report, even if nothing changed (HA 2024.3+).
    last_reported: datetime | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def friendly_name(self) -> str | None:
        name = self.attributes.get("friendly_name")
        return str(name) if name is not None else None


class Area(_HAModel):
    area_id: str
    name: str
    floor_id: str | None = None


class Device(_HAModel):
    id: str
    name: str | None = None
    name_by_user: str | None = None
    area_id: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    disabled_by: str | None = None

    @property
    def display_name(self) -> str | None:
        return self.name_by_user or self.name


class EntityRegistryEntry(_HAModel):
    entity_id: str
    device_id: str | None = None
    area_id: str | None = None
    name: str | None = None
    platform: str | None = None
    disabled_by: str | None = None
    hidden_by: str | None = None


class HAConfig(_HAModel):
    version: str
    location_name: str | None = None


class Registries(_HAModel):
    """Area, device and entity registries fetched in one WebSocket session."""

    areas: list[Area]
    devices: list[Device]
    entities: list[EntityRegistryEntry]


class StatePoint(_HAModel):
    """One state change from the recorder history."""

    state: str
    changed_at: datetime


class StatBucket(_HAModel):
    """One long-term statistics bucket (recorder), e.g. an hour."""

    start: datetime
    end: datetime
    mean: float | None = None
    min: float | None = None
    max: float | None = None


class AutomationTrace(_HAModel):
    """Summary of one stored automation run (HA keeps only the last few per automation)."""

    item_id: str  # automation config id, not entity_id
    run_id: str
    state: str  # running | stopped
    script_execution: str | None = None  # finished | error | failed_conditions | ...
    error: str | None = None
    trigger: str | None = None
    last_step: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class RepairIssue(_HAModel):
    domain: str
    issue_id: str
    severity: str
    ignored: bool = False
    translation_key: str | None = None
