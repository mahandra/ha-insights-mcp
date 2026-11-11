# HA Insights MCP

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents **analytics,
diagnostics and guarded actions** for [Home Assistant](https://www.home-assistant.io).

> Status: **all 4 phases done** — discovery, history analytics, device health, automation
> audit and opt-in guarded actions, over stdio or authenticated Streamable HTTP (Docker).
> See [Roadmap](#roadmap).

The demo (`dev/demo.py`, a real MCP client) runs diagnostics, gets a thermostat value
refused (°C policy on a °F home) and unlocks a door after a human confirmation. Render the
GIF with [VHS](https://github.com/charmbracelet/vhs): `vhs docs/demo.tape` → `docs/demo.gif`.

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
| Actions | Direct intents | **Guarded** and off by default: allowlist, value bounds, preview, human confirmation, verification, audit log |
| Output | Assist API responses | Compact, typed, token-efficient structured output |

Use the built-in server to *control* your home. Use HA Insights to ask *"what is going
on in my home, what is broken, and is this change safe?"*. Both can run side by side.

## Features

By default every tool is read-only (`readOnlyHint`) and the server never changes Home
Assistant. Guarded actions are opt-in (`ACTIONS_ENABLED=true`), see below.

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

**Guarded actions** (only with `ACTIONS_ENABLED=true` and a `policy.toml`)

| Tool | Purpose |
|---|---|
| `preview_action(service, entity_ids, data?)` | Read-only dry run: every policy check, risk, whether a human must confirm (and whether this client can ask), expected change per entity, allowed value ranges |
| `execute_action(service, entity_ids, reason, data?)` | Re-checks everything, asks the human for medium/high risk, calls HA, re-reads the state and reports `confirmed` / `pending` / `not_confirmed` |
| `get_action_log(limit?, entity_id?, outcome?)` | Audit log: every attempt with reason, risk, confirmation, outcome |

Resource `ha://actions/policy` shows the effective policy to the model up front.

Outputs are pydantic models (MCP structured content with an output schema). Raw HA JSON is
never returned: attributes are filtered per domain (no `supported_features` bitmasks, icons,
pictures or GPS coordinates).

## Architecture

```
 MCP client (Claude, Inspector)
        │  JSON-RPC over stdio, or Streamable HTTP /mcp + Bearer token
┌───────▼──────────────────────────────────────────────┐
│ http.py, auth.py  uvicorn app, SDK bearer auth,      │  HTTP transport
│                   Host/Origin allowlist, /healthz    │  (MCP_TRANSPORT=http)
┌───────▼──────────────────────────────────────────────┐
│ server.py     MCPServer, lifespan, instructions      │  MCP layer
│ tools/        discovery, history, health,            │
│               automations, actions → schemas.py      │
│ resources.py  ha://home/summary, ha://actions/policy │
│ prompts.py    home_health_report, investigate_entity │
│ context.py    AppContext, HAError → ToolError        │
├──────────────────────────────────────────────────────┤
│ analytics/    history · health · audit               │  pure functions
│               (no I/O, no MCP; plain-fixture tests)  │
│ actions/      policy · effects (pure)                │  guarded actions
│               guard (assess) → executor (the only    │
│               writer) → audit_log (SQLite)           │
│ snapshot.py   states ⋈ registries, area resolution,  │
│               TTL cache, single-flight refresh       │
├──────────────────────────────────────────────────────┤
│ ha/client.py  REST (httpx): states, config           │  HA client
│               WebSocket: registries, history,        │  (no MCP imports)
│               statistics, automation configs,        │
│               traces, repairs, services,             │
│               call_service (the only write)          │
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
  or included in errors. `entity_id` is validated before it reaches a URL. Annotations are
  hints for clients, not enforcement — enforcement lives in the server (see below).
- **stdio hygiene.** stdout is the JSON-RPC channel; all logs go to stderr.

## Guarded actions

Writes are off unless `ACTIONS_ENABLED=true`. Then the server needs a policy file
(`cp policy.example.toml policy.toml`); if it is missing or invalid the server refuses to
start. Layers, in the order a request meets them:

1. **Two tools, two permissions.** `preview_action` is read-only and can be auto-approved
   in the client; `execute_action` is `destructiveHint`, so the client asks separately.
2. **Allowlist** (`policy.toml`, which the model cannot change): domains, services and data
   fields. Everything else is denied; `script`, `automation`, `scene`, `homeassistant`,
   `shell_command`, `update` and a few more can never be allowed — they would bypass the
   policy. Targets are explicit `entity_ids` only (no areas, no `all`, ≤ 10).
3. **Value bounds** = policy ∩ the entity's own limits (e.g. `min_temp`) ∩ HA's service
   schema. HA's own selector for `climate.set_temperature` is 0–250, so it is not enough.
   Policy temperatures carry a unit and are converted to HA's unit system: 16–26 °C
   becomes 60.8–78.8 °F on an imperial installation.
4. **Risk** comes from the policy, not from the model: `low` runs, `medium`/`high` need a
   human. More than `bulk_threshold` targets raise the risk one level.
5. **Human confirmation via MCP elicitation.** The server builds the question from
   validated data; the assistant's reason is shown labelled as unverified. The form needs a
   ticked checkbox. There is no `confirm` argument — the model cannot confirm for the
   user. Clients without elicitation get `no_confirmation_channel`: risky actions are
   refused, low-risk ones still work.
6. **Re-check before writing.** `execute_action` assesses again with fresh states; a
   preview is never a permission. A rate limit (10/min) stops runaway loops.
7. **Verification.** After the call the server re-reads the targets: devices can ignore a
   command or jam. A timeout is reported as "may or may not have been applied".
8. **Audit log.** SQLite, append-only (UPDATE/DELETE blocked by triggers): every attempt,
   including refusals, with the reason, risk, confirmation, before/after states and HA's
   `context_id` (links the entry to HA's logbook).

The write path is one method (`HAClient.call_service`, over WebSocket because REST only
answers "400: Bad Request"), called from one module (`actions/executor.py`); an
architecture test fails the build otherwise.

Confirmation uses the SDK's `Resolve` + `Elicit`, which works on both protocol eras: a live
`elicitation/create` request on 2025-era connections and `InputRequiredResult` (the client
retries the call with the answer) on 2026-07-28.

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
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` (Streamable HTTP on `/mcp`) |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP listen address |
| `MCP_PUBLIC_URL` | `http://<host>:<port>` | URL clients use (behind a proxy: its `https://` URL); sets the allowed `Host`/`Origin` |
| `MCP_TOKENS_PATH` | `tokens.toml` | Bearer token hashes and scopes; required for HTTP |
| `MCP_ALLOWED_ORIGINS` | `[]` | Extra browser origins (JSON list) |
| `ACTIONS_ENABLED` | `false` | Register the guarded action tools |
| `ACTIONS_POLICY_PATH` | `policy.toml` | Action policy (see `policy.example.toml`) |
| `AUDIT_DB_PATH` | `data/audit.db` | SQLite audit log |
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

### HTTP and Docker

Over HTTP every request needs a bearer token. Tokens are stored only as SHA-256 hashes:

```bash
uv run ha-insights-mcp token create --name laptop --scope ha:read   # prints the token once
# append the printed [[tokens]] entry to tokens.toml (see tokens.example.toml)
MCP_TRANSPORT=http uv run ha-insights-mcp                          # http://127.0.0.1:8000/mcp
```

Docker, next to the dev HA (the service is behind the `mcp` compose profile):

```bash
make mcp-up                  # builds the image, http://localhost:8000/mcp (MCP_HOST_PORT to change)
make mcp-logs / make mcp-down
```

The image (~165 MB) holds only the virtualenv and runs as an unprivileged user with a
read-only root filesystem. Runtime inputs: `HA_URL`/`HA_TOKEN` (env), `/config/tokens.toml`
and `/config/policy.toml` (read-only mounts), `/data` (audit log volume). `tokens.toml` holds
only hashes, so it may be world-readable for the container user.

Connect a client:

```bash
claude mcp add --transport http ha-insights http://localhost:8000/mcp \
  --header "Authorization: Bearer hai_..."
npx -y @modelcontextprotocol/inspector --cli http://localhost:8000/mcp --transport http \
  --header "Authorization: Bearer hai_..." --method tools/list
```

**Security model over HTTP.**

- Auth, session ownership and principal-bound `request_state` come from the SDK
  (`token_verifier` + `AuthSettings`): a session or a pending confirmation cannot be reused
  with another token. Unauthenticated requests get `401` with a pointer to
  `/.well-known/oauth-protected-resource`.
- Scopes: `ha:read` (all analytics, `preview_action`) and `ha:actions` (`execute_action`). A
  read-only token gets `denied_scope`, logged, and the human is never asked to confirm it.
- **A holder of an `ha:actions` token can also answer the confirmation prompt.** Over stdio
  the client is the user's own app; over HTTP the confirmation is only as trustworthy as the
  client that holds the token. Give `ha:actions` only to clients you trust.
- DNS rebinding protection is always on with an explicit `Host`/`Origin` allowlist (the SDK
  enables it by itself only for loopback binds, not for `0.0.0.0` in a container).
- The server has no TLS: terminate HTTPS at a reverse proxy and set `MCP_PUBLIC_URL` to its
  URL. A warning is logged when listening beyond loopback over plain HTTP.
- `/healthz` needs no token and answers only `ok`.
- The server refuses to start over HTTP without a valid token file (fail closed).

### Claude Code

```bash
claude mcp add ha-insights -- uv run --directory /path/to/ha_mcp ha-insights-mcp
claude mcp list              # should show ha-insights: connected
```

`--directory` makes uv run in the repo, so `.env` is found. To allow guarded actions, add
`-e ACTIONS_ENABLED=true` before `--`. In a session, ask
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
mocked HA. HTTP tests drive the real Starlette app through an ASGI transport (auth, Host and
Origin checks, session ownership, scopes). CI (GitHub Actions) runs lint, tests, the image
build and a container smoke test. Conventions for contributors and AI agents:
[CLAUDE.md](CLAUDE.md).

## Roadmap

1. **Skeleton + read-only tools over stdio**, dev HA in Docker ← *done*
2. **History analytics, device health, automation audit, prompts** ← *done*
3. **Guarded actions**: allowlist, value bounds, preview, human confirmation, verification,
   SQLite audit log ← *done*
4. **Streamable HTTP with bearer auth and scopes, Docker image, CI, demo** ← *done*

## License

MIT
