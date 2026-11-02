"""MCP-facing output models: what the LLM actually sees.

Kept deliberately small. Field descriptions end up in each tool's output schema,
so they double as documentation for the model.
"""

from datetime import datetime
from typing import Any

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
