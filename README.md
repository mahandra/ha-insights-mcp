# HA Insights MCP

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents **analytics,
diagnostics and guarded actions** for [Home Assistant](https://www.home-assistant.io).

> Status: **phase 2 of 4** — read-only discovery, history analytics, device health and
> automation audit over stdio. See [Roadmap](#roadmap).

## Positioning: why another Home Assistant MCP server?

Home Assistant already ships a built-in
[MCP Server integration](https://www.home-assistant.io/integrations/mcp_server/), and
community servers (e.g. `ha-mcp`) expose broad control of HA. This project deliberately
does **not** compete with them on "turn on the kitchen light".

| | Built-in HA MCP Server | HA Insights MCP (this project) |
|---|---|---|
| Purpose | Voice/Assist-style control of exposed entities | Understanding and auditing the home |
| Data | Current state of exposed entities | States joined with area/device/entity registries, recorder history and statistics, automation configs and traces |
| Analytics | — | Time in state, change counts, min/mean/max trends, most active entities |
| Diagnostics | — | Offline devices, low batteries, stale and flapping sensors, HA repairs, automation audit (broken references, failing runs) |
| Actions | Direct intents | **Guarded**: allowlist, value bounds, dry-run, confirmation, audit log *(phase 3)* |
| Output | Assist API responses | Compact, typed, token-efficient structured output |

Use the built-in server to *control* your home. Use HA Insights to ask *"what is going
on in my home, what is broken, and is this change safe?"*. Both can run side by side.

## Features

All tools are annotated `readOnlyHint`; the server has no write path to Home Assistant.

**Discovery**

| Tool | Purpose |
|---|---|
| `search_entities(query, domain?, area?, limit=25)` | Find entities by text, domain, area → `entity_id`, name, state, area |
| `get_entity_state(entity_id)` | Live state, unit, area, device, domain-relevant attributes |
| `list_areas()` | Areas with entity counts |

**History analytics** (aggregated on the server, never raw points)

| Tool | Purpose |
|---|---|
| `summarize_history(entity_id, hours=24, buckets=12)` | Numeric: min/mean/max + bucketed series (from HA statistics, raw history as fallback). Discrete: changes, % time in each state, latest changes |
| `rank_activity(hours=24, domain?, area?, limit=10)` | Entities with the most state changes ("what was used most") |

**Device health**

| Tool | Purpose |
|---|---|
| `check_device_health(area?, domain?, min_severity, battery_threshold?)` | `unavailable` (grouped per device), `low_battery`, `stale` (no report for 24h, via `last_reported`), `flapping` (≥20 changes in a sliding hour), `unknown_state`, plus HA Repairs |

**Automations**

| Tool | Purpose |
|---|---|
| `list_automations(state?)` | State, mode, last triggered, result of the last run |
| `get_automation(entity_id)` | Triggers/conditions/actions, referenced entities, recent runs with errors |
| `audit_automations(entity_id?, min_severity)` | `config_invalid`, `missing_entity`, `missing_device`, `unavailable_entity`, `last_run_failed`, `runs_dropped`, `disabled`, `never_triggered`, `not_triggered_recently`, `no_id` |

Findings and issues carry stable `code`s and a `severity` (error / warning / info).

**Resource:** `ha://home/summary` — markdown overview: areas with entity counts, entities
per domain, number of unavailable entities.

**Prompts** (user-selected, e.g. slash commands in Claude Code):

- `home_health_report(area?)` — runs health check + audit, correlates findings, answers
  with "Fix now / Should fix / FYI" and next steps.
- `investigate_entity(entity_id)` — state, 24h history, health, and the automations that
  reference the entity (found server-side and embedded in the prompt).

Outputs are pydantic models (MCP structured content with an output schema). Raw HA JSON is
never returned: attributes are filtered per domain (no `supported_features` bitmasks, icons,
pictures or GPS coordinates).

## Architecture

```
 MCP client (Claude, Inspector)
        │  JSON-RPC over stdio
┌───────▼──────────────────────────────────────────────┐
│ server.py     MCPServer, lifespan, instructions      │  MCP layer
│ tools/        discovery, history, health,            │
│               automations  → schemas.py (outputs)    │
│ resources.py  ha://home/summary                      │
│ prompts.py    home_health_report, investigate_entity │
│ context.py    AppContext, HAError → ToolError        │
├──────────────────────────────────────────────────────┤
│ analytics/    history · health · audit               │  pure functions
│               (no I/O, no MCP; plain-fixture tests)  │
│ snapshot.py   states ⋈ registries, area resolution,  │
│               TTL cache, single-flight refresh       │
├──────────────────────────────────────────────────────┤
│ ha/client.py  REST (httpx): states, config           │  HA client
│               WebSocket: registries, history,        │  (no MCP imports)
│               statistics, automation configs,        │
│               traces, repairs                        │
│ ha/errors.py  HAAuthError, HAConnectionError, ...    │
└───────┬──────────────────────────────────────────────┘
        │  HTTP + WebSocket, Bearer token
   Home Assistant
```

Key decisions:

- **Separate HA client.** `ha/` knows nothing about MCP, so it is testable on its own and
  reusable for the HTTP transport (phase 4).
- **REST + WebSocket.** REST has no area/device/entity registries; without them you cannot
  answer "what is in the kitchen". Registries are fetched in one WebSocket session.
- **Area resolution like HA:** the entity's own area wins, otherwise the device's area.
- **Snapshot cache (10 s default)** shared by tools and the resource; `get_entity_state`
  always reads the state fresh.
- **Aggregate on the server.** A week of a sensor is thousands of points; the model gets
  ~200 tokens of conclusions. Numeric entities use HA's recorder statistics (5-minute for
  ≤24h windows, hourly beyond), with time-weighted raw history as fallback.
- **Pure analytics core.** History, health and audit rules are pure functions with stable
  codes — easy to test and reusable for phase 3 dry-runs.
- **Honest about limits.** HA keeps only the last 5 traces per automation, templated
  references cannot be checked statically, history is capped at 7 days — tool descriptions
  and outputs say so (`source`, `checks_skipped`, `templated_refs`).
- **Errors are part of the conversation.** HA failures become MCP tool errors with
  actionable text ("Unknown area 'attic'. Known areas: …"), so the model can self-correct.
- **Security from day one.** Token only from env/`.env`, held as `SecretStr`, never logged
  or included in errors. `entity_id` is validated before it reaches a URL. The server has
  no write methods at all — annotations are hints, not enforcement.
- **stdio hygiene.** stdout is the JSON-RPC channel; all logs go to stderr.

## Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker (for the dev HA),
Node.js (only for MCP Inspector).

```bash
make install                 # uv sync
cp .env.example .env         # then set HA_URL and HA_TOKEN
```

### Dev Home Assistant

```bash
make dev-ha-up               # HA with the demo integration on http://localhost:8123
```

Onboard, create a long-lived access token, then `make dev-seed` (areas) and
`make dev-simulate` (automations, low battery, offline light, flapping sensor) — see
[dev/README.md](dev/README.md). Any real HA instance works too: set `HA_URL` and
`HA_TOKEN` (an admin user's token, required for the registries).

### Configuration

| Variable | Default | |
|---|---|---|
| `HA_URL` | `http://localhost:8123` | Base URL of Home Assistant |
| `HA_TOKEN` | — (required) | Long-lived access token |
| `HA_TIMEOUT_SECONDS` | `10` | Per-request timeout |
| `HA_CACHE_TTL_SECONDS` | `10` | Snapshot cache TTL (0 disables) |
| `BATTERY_LOW_PERCENT` | `20` | Low-battery threshold (below half of it = error) |
| `STALE_HOURS` | `24` | Measurement sensor with no report for this long = stale |
| `FLAPPING_CHANGES_PER_HOUR` | `20` | State changes within one hour = flapping |
| `LOG_LEVEL` | `INFO` | Logs go to stderr |

## Usage

### MCP Inspector

```bash
make inspect
```

Opens the Inspector in the browser, connected over stdio. Click **Connect**, then:
**Tools → List Tools → `list_areas` → Run**; **Resources → `ha://home/summary`**.
Check the output schema and the structured result of each tool.

Headless (CLI mode), handy for scripts and CI:

```bash
npx -y @modelcontextprotocol/inspector --cli uv run ha-insights-mcp --method tools/list
npx -y @modelcontextprotocol/inspector --cli uv run ha-insights-mcp \
  --method tools/call --tool-name search_entities --tool-arg query=garage
```

### Claude Code

```bash
claude mcp add ha-insights -- uv run --directory /path/to/ha_mcp ha-insights-mcp
claude mcp list              # should show ha-insights: connected
```

`--directory` makes uv run in the repo, so `.env` is found. In a session, ask
"What is broken in my home?" or "How often did the backyard motion sensor trigger today?",
reference the resource with `@ha-insights:ha://home/summary`, or run a prompt:
`/mcp__ha-insights__home_health_report`.

### Claude Desktop

`~/.config/Claude/claude_desktop_config.json` (Linux) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "ha-insights": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ha_mcp", "ha-insights-mcp"]
    }
  }
}
```

Prefer keeping the token in the repo's `.env` (git-ignored) over an `env` block in this
file.

## Development

```bash
make check    # lint + tests; run before every commit
make test     # pytest, no real HA calls (respx + fake WebSocket + in-memory MCP client)
make fmt
```

Tool tests run the whole stack: in-memory MCP `Client` → server → real `HAClient` →
mocked HA. Conventions for contributors and AI agents: [CLAUDE.md](CLAUDE.md).

## Roadmap

1. **Skeleton + read-only tools over stdio**, dev HA in Docker ← *done*
2. **History analytics, device health, automation audit, prompts** ← *done*
3. Guarded actions: domain allowlist, value bounds, dry-run, confirmation for risky
   actions, SQLite audit log
4. Streamable HTTP transport with auth, Docker image, README demo GIF

## License

MIT
