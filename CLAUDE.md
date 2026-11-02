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
- `src/ha_insights_mcp/{server,tools,resources,schemas}.py` — MCP layer only.
- `dev/` — dev Home Assistant config and dev-only scripts.

## Rules
- Security: HA token only from env/.env, stored as `SecretStr`; never log it, never put it in
  error messages or reprs.
- Phase 1 is read-only: no HA write calls in `src/`. All tools are annotated `readOnlyHint=True`.
- stdio transport: stdout belongs to JSON-RPC. Log to stderr only; never `print()`.
- Tool outputs are pydantic models with compact fields. Never return raw HA JSON.
- Tool docstrings and `Field(description=...)` are prompts for the LLM: say when to use the
  tool, what it returns, and what to call next.
- HA errors become typed exceptions in `ha/errors.py`; the MCP layer converts them into
  `ToolError` with an actionable message.
- Small, focused commits; `make lint test` must pass before committing.

## Commands
`make install | dev-ha-up | dev-seed | run | test | lint | inspect`
