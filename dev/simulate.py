"""Dev-only: make the demo home realistic for analytics, health checks and audits.

The demo integration is static: no history, no automations, nothing broken. This script
WRITES to the dev Home Assistant (state overrides, automation configs, service calls) to
create a known set of problems. It lives outside `src/` because the MCP server is read-only.

    make dev-simulate      (re-runnable; state overrides are lost on HA restart)

What it creates:
- automations: one healthy, one disabled, one referencing missing entities, one never
  triggered that uses an unavailable light, one whose action fails at runtime
- device health problems: low battery, unavailable light, unknown sensor, flapping motion
  sensor (which also triggers the failing automation -> error traces)
"""

import asyncio
import re
import sys
from typing import Any

import httpx

from ha_insights_mcp.config import Settings

AUTOMATIONS: dict[str, dict[str, Any]] = {
    "dev_porch_light_at_sunset": {
        "alias": "Porch light at sunset",
        "description": "Healthy automation",
        "mode": "single",
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "conditions": [],
        "actions": [
            {
                "action": "light.turn_on",
                "target": {"entity_id": "light.entrance_color_white_lights"},
            }
        ],
    },
    "dev_night_mode": {
        "alias": "Night mode",
        "description": "Disabled by the simulator",
        "mode": "single",
        "triggers": [{"trigger": "time", "at": "23:30:00"}],
        "conditions": [],
        "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.bed_light"}}],
    },
    "dev_front_door_greeting": {
        "alias": "Front door greeting",
        "description": "References entities that do not exist",
        "mode": "single",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.front_door_old", "to": "on"}],
        "conditions": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hallway_ceiling"}}],
    },
    "dev_office_wake_up": {
        "alias": "Office wake up",
        "description": "Never triggered; targets an unavailable light",
        "mode": "single",
        "triggers": [{"trigger": "time", "at": "03:33:00"}],
        "conditions": [{"condition": "time", "weekday": ["sat"]}],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.office_rgbw_lights"}}
        ],
    },
    "dev_backyard_motion_alert": {
        "alias": "Backyard motion alert",
        "description": "Second action calls a non-existent script -> runtime error",
        "mode": "single",
        "triggers": [
            {"trigger": "state", "entity_id": "binary_sensor.movement_backyard", "to": "on"}
        ],
        "conditions": [],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.ceiling_lights"}},
            {"action": "script.does_not_exist"},
        ],
    },
}

FLAP_ENTITY = "binary_sensor.movement_backyard"
FLAP_TOGGLES = 40


def automation_entity_id(alias: str) -> str:
    # HA derives the entity_id from the alias, not from the config id.
    return "automation." + re.sub(r"[^a-z0-9]+", "_", alias.lower()).strip("_")


class HA:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def state(self, entity_id: str) -> dict[str, Any]:
        r = await self.http.get(f"/api/states/{entity_id}")
        r.raise_for_status()
        return r.json()

    async def override_state(self, entity_id: str, state: str, **attrs: Any) -> None:
        # POST /api/states replaces attributes, so keep the existing ones.
        current = await self.state(entity_id)
        body = {"state": state, "attributes": {**current["attributes"], **attrs}}
        (await self.http.post(f"/api/states/{entity_id}", json=body)).raise_for_status()

    async def service(self, domain: str, service: str, **data: Any) -> None:
        (await self.http.post(f"/api/services/{domain}/{service}", json=data)).raise_for_status()


async def wait_for(ha: HA, entity_ids: list[str], attempts: int = 60) -> None:
    for _ in range(attempts):
        r = await ha.http.get("/api/states")
        present = {s["entity_id"] for s in r.json()}
        if all(e in present for e in entity_ids):
            return
        await asyncio.sleep(0.5)
    sys.exit(f"Timed out waiting for {entity_ids}")


async def main() -> None:
    s = Settings()
    headers = {"Authorization": f"Bearer {s.ha_token.get_secret_value()}"}
    async with httpx.AsyncClient(base_url=s.ha_url, headers=headers, timeout=30) as http:
        ha = HA(http)

        print("automations:")
        for automation_id, config in AUTOMATIONS.items():
            r = await http.post(f"/api/config/automation/config/{automation_id}", json=config)
            r.raise_for_status()
            print(f"  saved {automation_id}")
        entity_ids = [automation_entity_id(c["alias"]) for c in AUTOMATIONS.values()]
        await wait_for(ha, entity_ids)
        await ha.service("automation", "turn_off", entity_id="automation.night_mode")
        await ha.service("automation", "trigger", entity_id="automation.porch_light_at_sunset")
        print("  disabled night_mode, triggered porch_light once")

        print("device health:")
        await ha.override_state("sensor.outside_temperature_battery", "8")
        await ha.override_state("light.office_rgbw_lights", "unavailable")
        await ha.override_state("sensor.outside_humidity", "unknown")
        print("  battery 8%, office light unavailable, outside humidity unknown")

        for i in range(FLAP_TOGGLES):
            await ha.override_state(FLAP_ENTITY, "on" if i % 2 == 0 else "off")
            await asyncio.sleep(0.3)
        print(f"  {FLAP_ENTITY}: {FLAP_TOGGLES} toggles (triggers the failing automation)")


if __name__ == "__main__":
    asyncio.run(main())
