# Dev Home Assistant

A disposable Home Assistant with the `demo` integration (fake lights, sensors, climate,
covers, media players, ...). Runtime state lives in `dev/ha-config/` and is git-ignored,
except `configuration.yaml`.

## Start

```bash
make dev-ha-up        # http://localhost:8123, first start takes ~1 minute
```

## Onboarding and access token (once)

1. Open http://localhost:8123 and create the owner user (any name/password, it is local).
2. Finish the onboarding wizard (location, analytics — defaults are fine).
3. Click your user avatar (bottom-left) → **Security** tab → **Long-lived access tokens**
   → **Create token**, name it `ha-insights-mcp`.
4. Copy the token (it is shown only once) into `.env` in the repo root:
   ```
   HA_URL=http://localhost:8123
   HA_TOKEN=<paste here>
   ```
5. Check it:
   ```bash
   set -a; . ./.env; set +a
   curl -s -H "Authorization: Bearer $HA_TOKEN" http://localhost:8123/api/   # {"message":"API running."}
   ```

The token belongs to the owner (admin) user. Admin rights are required for the
area/device/entity registries that this server reads over the WebSocket API.

## Seed areas

The demo integration creates devices but does not put them into areas. To get realistic
data for `list_areas` / `search_entities(area=...)`:

```bash
make dev-seed
```

This is a dev-only script that **writes** to the dev instance (creates areas, assigns
devices). The MCP server itself is read-only in phase 1.

## Simulate problems and history

The demo integration is static. To get data for history analytics, device health and the
automation audit:

```bash
make dev-simulate
```

Dev-only, **writes** to the dev instance:

- creates 5 automations (`automations.yaml`): healthy, disabled, referencing missing
  entities, never triggered, failing at runtime;
- overrides states: low battery (8%), unavailable light, unknown humidity sensor;
- toggles `binary_sensor.movement_backyard` 40 times (flapping), which triggers the failing
  automation and produces error traces.

State overrides are lost when HA restarts; re-run the script. HA keeps only the last 5
traces per automation.

## Reset

```bash
make dev-ha-down
git clean -fdX dev/ha-config   # removes everything git-ignored there (HA state)
```
