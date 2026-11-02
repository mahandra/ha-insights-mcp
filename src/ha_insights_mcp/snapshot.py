"""A joined, cached view of Home Assistant: states + area/device/entity registries.

Area resolution follows Home Assistant semantics: an entity's own `area_id` wins;
otherwise the entity inherits the area of its device.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from ha_insights_mcp.ha.client import HAClient
from ha_insights_mcp.ha.models import Area, Device, EntityState, Registries


@dataclass(frozen=True, slots=True)
class EntityView:
    """One entity with its registry context resolved."""

    state: EntityState
    area: Area | None
    device: Device | None

    @property
    def entity_id(self) -> str:
        return self.state.entity_id

    @property
    def domain(self) -> str:
        return self.state.domain

    @property
    def name(self) -> str:
        return self.state.friendly_name or self.state.entity_id


@dataclass(frozen=True, slots=True)
class HomeSnapshot:
    entities: dict[str, EntityView]
    areas: dict[str, Area]
    fetched_at: float

    @classmethod
    def build(
        cls, states: list[EntityState], registries: Registries, fetched_at: float = 0.0
    ) -> "HomeSnapshot":
        areas = {a.area_id: a for a in registries.areas}
        devices = {d.id: d for d in registries.devices}
        entries = {e.entity_id: e for e in registries.entities}

        entities: dict[str, EntityView] = {}
        for st in states:
            entry = entries.get(st.entity_id)
            device = devices.get(entry.device_id) if entry and entry.device_id else None
            area_id = (entry.area_id if entry else None) or (device.area_id if device else None)
            entities[st.entity_id] = EntityView(
                state=st, area=areas.get(area_id) if area_id else None, device=device
            )
        return cls(entities=entities, areas=areas, fetched_at=fetched_at)

    def find_area(self, area: str) -> Area | None:
        """Look up an area by id or (case-insensitive) name."""
        if area in self.areas:
            return self.areas[area]
        needle = area.strip().casefold()
        return next((a for a in self.areas.values() if a.name.casefold() == needle), None)


class SnapshotProvider:
    """Fetches a HomeSnapshot and caches it for `ttl` seconds.

    Concurrent callers share one in-flight refresh instead of hammering HA.
    """

    def __init__(
        self, client: HAClient, ttl: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._clock = clock
        self._snapshot: HomeSnapshot | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> HomeSnapshot:
        async with self._lock:
            if self._snapshot is None or self._clock() - self._snapshot.fetched_at >= self._ttl:
                states, registries = await asyncio.gather(
                    self._client.get_states(), self._client.get_registries()
                )
                self._snapshot = HomeSnapshot.build(states, registries, self._clock())
            return self._snapshot
