"""Dev-only: create areas and assign demo devices to them.

This script WRITES to Home Assistant (area/device registries). It is intentionally kept
outside `src/`: the MCP server itself is read-only in phase 1. Run against the dev
instance only:  make dev-seed

Idempotent: existing areas are reused, devices that already have an area are left alone.
"""

import asyncio
import json
import sys
from typing import Any

from websockets.asyncio.client import connect

from ha_insights_mcp.config import Settings

# area name -> demo device names (as created by HA's `demo` integration)
LAYOUT: dict[str, list[str]] = {
    "Kitchen": ["Kitchen Lights", "Kitchen Window", "Large Range", "Small Range"],
    "Living Room": [
        "Living Room RGBWW Lights",
        "Living Room Window",
        "Ceiling Lights",
        "Decorative Lights",
        "HeatPump",
    ],
    "Bedroom": ["Bed Light", "AC", "Thermostat"],
    "Office": ["Office RGBW Lights", "Ecobee"],
    "Hallway": ["Hall Window", "Entrance Color + White Lights"],
    "Garage": ["Garage Door", "Power consumption"],
    "Basement": ["Basement Floor Wet", "Hvac"],
    "Garden": [
        "Back Garden",
        "Front Garden",
        "Orchard",
        "Trees",
        "Movement Backyard",
        "Pergola Roof",
        "Outside Temperature",
        "Outside Humidity",
    ],
}


class WS:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._id = 0

    async def call(self, command: str, **payload: Any) -> Any:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "type": command, **payload}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                break
        if not msg["success"]:
            raise RuntimeError(f"{command} failed: {msg['error']}")
        return msg["result"]


async def main() -> None:
    settings = Settings()
    url = settings.ha_url.replace("https://", "wss://").replace("http://", "ws://")
    async with connect(f"{url}/api/websocket", max_size=None) as raw:
        await raw.recv()  # auth_required
        await raw.send(
            json.dumps({"type": "auth", "access_token": settings.ha_token.get_secret_value()})
        )
        if json.loads(await raw.recv()).get("type") != "auth_ok":
            sys.exit("Authentication failed: check HA_TOKEN in .env")
        ws = WS(raw)

        areas = {a["name"]: a["area_id"] for a in await ws.call("config/area_registry/list")}
        devices = await ws.call("config/device_registry/list")
        by_name = {(d["name_by_user"] or d["name"]): d for d in devices}

        assigned = 0
        for area_name, device_names in LAYOUT.items():
            if area_name not in areas:
                created = await ws.call("config/area_registry/create", name=area_name)
                areas[area_name] = created["area_id"]
                print(f"created area {area_name!r}")
            for device_name in device_names:
                device = by_name.get(device_name)
                if device is None:
                    print(f"  skip {device_name!r}: no such device")
                    continue
                if device["area_id"]:
                    continue
                await ws.call(
                    "config/device_registry/update",
                    device_id=device["id"],
                    area_id=areas[area_name],
                )
                assigned += 1
        print(f"assigned {assigned} devices; {len(devices) - assigned} left as they were")


if __name__ == "__main__":
    asyncio.run(main())
