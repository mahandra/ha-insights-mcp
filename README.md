# HA Insights MCP

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents **analytics,
diagnostics and guarded actions** for [Home Assistant](https://www.home-assistant.io).

> Status: **phase 1 of 4** — read-only discovery tools over stdio. See [Roadmap](#roadmap).

## Positioning: why another Home Assistant MCP server?

Home Assistant already ships a built-in
[MCP Server integration](https://www.home-assistant.io/integrations/mcp_server/), and
community servers (e.g. `ha-mcp`) expose broad control of HA. This project deliberately
does **not** compete with them on "turn on the kitchen light".

| | Built-in HA MCP Server | HA Insights MCP (this project) |
|---|---|---|
| Purpose | Voice/Assist-style control of exposed entities | Understanding and auditing the home |
| Data | Current state of exposed entities | States joined with area/device/entity registries; history *(phase 2)* |
| Analytics | — | Usage patterns, anomalies, energy *(phase 2)* |
| Diagnostics | — | Unavailable/flapping devices, low batteries, automation audit *(phase 2)* |
| Actions | Direct intents | **Guarded**: allowlist, value bounds, dry-run, confirmation, audit log *(phase 3)* |
| Output | Assist API responses | Compact, typed, token-efficient structured output |

Use the built-in server to *control* your home. Use HA Insights to ask *"what is going
on in my home, what is broken, and is this change safe?"*. Both can run side by side.

## Features (phase 1)

**Tools** (all annotated `readOnlyHint`):

| Tool | Purpose | Returns |
|---|---|---|
| `search_entities(query, domain?, area?, limit=25)` | Discover entities by text, domain, area | `entity_id`, name, state, area; `total`, `truncated` |
| `get_entity_state(entity_id)` | Live state of one entity | state, unit, area, device, `last_changed`, domain-relevant attributes |
| `list_areas()` | Rooms/zones of the home | `area_id`, name, entity count; unassigned count |

**Resource:** `ha://home/summary` — markdown overview: areas with entity counts, entities
per domain, number of unavailable entities.

Outputs are pydantic models (MCP structured content with an output schema). Raw HA JSON is
never returned: attributes are filtered per domain (no `supported_features` bitmasks, icons,
pictures or GPS coordinates).

## Architecture

```
 MCP client (Claude, Inspector)
        │  JSON-RPC over stdio
┌───────▼──────────────────────────────────────────────┐
│ server.py     MCPServer, lifespan, instructions      │  MCP layer
│ tools.py      search_entities / get_entity_state /   │
│               list_areas  → schemas.py (outputs)     │
│ resources.py  ha://home/summary                      │
│ context.py    AppContext, HAError → ToolError        │
├──────────────────────────────────────────────────────┤
│ snapshot.py   states ⋈ registries, area resolution,  │  domain layer
│               TTL cache, single-flight refresh       │
├──────────────────────────────────────────────────────┤
│ ha/client.py  REST (httpx): states, config           │  HA client
│               WebSocket: area/device/entity registry │  (no MCP imports)
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
- **Errors are part of the conversation.** HA failures become MCP tool errors with
  actionable text ("Unknown area 'attic'. Known areas: …"), so the model can self-correct.
- **Security from day one.** Token only from env/`.env`, held as `SecretStr`, never logged
  or included in errors. `entity_id` is validated before it reaches a URL. Phase 1 has no
  write methods at all — annotations are hints, not enforcement.
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

Onboard, create a long-lived access token and seed areas — see
[dev/README.md](dev/README.md). Any real HA instance works too: set `HA_URL` and
`HA_TOKEN` (an admin user's token, required for the registries).

### Configuration

| Variable | Default | |
|---|---|---|
| `HA_URL` | `http://localhost:8123` | Base URL of Home Assistant |
| `HA_TOKEN` | — (required) | Long-lived access token |
| `HA_TIMEOUT_SECONDS` | `10` | Per-request timeout |
| `HA_CACHE_TTL_SECONDS` | `10` | Snapshot cache TTL (0 disables) |
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
"What areas does my home have?" or reference the resource with `@ha-insights:ha://home/summary`.

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
make test     # pytest, no real HA calls (respx + fake WebSocket + in-memory MCP client)
make lint     # ruff
make fmt
```

Tool tests run the whole stack: in-memory MCP `Client` → server → real `HAClient` →
mocked HA. Conventions for contributors and AI agents: [CLAUDE.md](CLAUDE.md).

## Roadmap

1. **Skeleton + read-only tools over stdio**, dev HA in Docker ← *done*
2. History analytics, device health (unavailable / flapping / battery), automation audit
3. Guarded actions: domain allowlist, value bounds, dry-run, confirmation for risky
   actions, SQLite audit log
4. Streamable HTTP transport with auth, Docker image, README demo GIF

## License

MIT
