# CLAUDE.md — conventions for this repo

## What this is
HA Insights MCP: an MCP server for Home Assistant focused on analytics, diagnostics and
*guarded* actions. It deliberately does NOT duplicate HA's built-in MCP Server integration
(basic entity control via Assist). See README "Positioning".

## Language
- Code, comments, commit messages, docs: English.

## Stack
- Python 3.12, uv, official MCP Python SDK **v2** (`from mcp.server.mcpserver import MCPServer`).
  v1 names (`FastMCP`, `mcp.server.fastmcp`) do not exist — check v2 docs before using an API.
- httpx (async) for HA REST, `websockets` for HA WebSocket API (registries).
- pydantic v2 for models, pydantic-settings for config.
- pytest + pytest-asyncio + respx. Unit tests never talk to a real HA.

## Layout
- `src/ha_insights_mcp/ha/` — Home Assistant client. **Must not import `mcp`.**
- `src/ha_insights_mcp/snapshot.py` — joins states + registries, short TTL cache.
- `src/ha_insights_mcp/analytics/` — pure functions (no I/O, no MCP): history, health, audit.
  Test them with plain fixtures, no mocks.
- `src/ha_insights_mcp/tools/` — one module per theme, each exposing `register(mcp)`;
  shared annotations in `tools/common.py`.
- `src/ha_insights_mcp/{server,context,resources,schemas}.py` — MCP layer.
- `dev/` — dev Home Assistant config and dev-only scripts (`seed_areas.py`, `simulate.py`
  write to the dev HA; never import them from `src/`).

## Rules
- Security: HA token only from env/.env, stored as `SecretStr`; never log it, never put it in
  error messages or reprs.
- Phase 1 is read-only: no HA write calls in `src/`. All tools are annotated `readOnlyHint=True`.
- stdio transport: stdout belongs to JSON-RPC. Log to stderr only; never `print()`.
- Tool outputs are pydantic models with compact fields. Never return raw HA JSON.
- Aggregate on the server: return conclusions (counts, percentages, buckets), not raw
  history. Prefer HA statistics over raw points for numeric sensors.
- Time-dependent code takes "now" from `AppContext.now` so tests can pin the clock.
- Tool docstrings and `Field(description=...)` are prompts for the LLM: say when to use the
  tool, what it returns, and what to call next.
- HA errors become typed exceptions in `ha/errors.py`; the MCP layer converts them into
  `ToolError` with an actionable message.
- Small, focused commits; `make check` must pass before committing.

## Commands
`make install | dev-ha-up | dev-seed | dev-simulate | run | test | lint | check | inspect`
