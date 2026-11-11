"""Streamable HTTP end to end: real Starlette app + SDK auth, no network (ASGI transport)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ha_insights_mcp.auth import StaticTokenVerifier, TokenEntry, hash_token
from ha_insights_mcp.ha import HAClient
from ha_insights_mcp.http import auth_settings, build_http_app
from ha_insights_mcp.server import create_server
from tests.conftest import (
    HA_URL,
    REGISTRY_RESULTS,
    TOKEN,
    FakeWebSocket,
    actions_context,
    fake_ws_factory,
)

pytestmark = pytest.mark.anyio

PUBLIC_URL = "http://mcp.test"
READER = "hai_reader-token-for-tests-0123456789abcdef"
ADMIN = "hai_admin-token-for-tests-0123456789abcdef"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def http_app(tmp_path: Path, *, with_actions: bool = False) -> AsyncIterator:
    ws = FakeWebSocket(REGISTRY_RESULTS)
    actions = actions_context(tmp_path) if with_actions else None
    verifier = StaticTokenVerifier(
        [
            TokenEntry(name="reader", sha256=hash_token(READER)),
            TokenEntry(name="admin", sha256=hash_token(ADMIN), scopes=["ha:actions"]),
        ]
    )
    server = create_server(
        client_factory=lambda: HAClient(
            HA_URL, TOKEN, timeout=2, ws_connect_factory=fake_ws_factory(ws)
        ),
        cache_ttl=10,
        actions=actions,
        token_verifier=verifier,
        auth=auth_settings(PUBLIC_URL),
    )
    app = build_http_app(server, PUBLIC_URL, [])
    async with app.router.lifespan_context(app):
        yield app
    if actions:
        actions.audit_log.close()


def raw_client(app, **headers: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=PUBLIC_URL, headers=headers
    )


@asynccontextmanager
async def mcp_over_http(app, token: str, **options) -> AsyncIterator[Client]:
    http = raw_client(app, Authorization=f"Bearer {token}")
    async with (
        http,
        Client(streamable_http_client(f"{PUBLIC_URL}/mcp", http_client=http), **options) as client,
    ):
        yield client


async def test_full_session_with_valid_token(tmp_path, ha_api) -> None:
    async with http_app(tmp_path) as app, mcp_over_http(app, READER) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("list_areas", {})
    assert "list_areas" in tools
    assert not result.is_error
    assert result.structured_content["areas"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer hai_wrong"},
        {"Authorization": f"Basic {READER}"},
    ],
)
async def test_requests_without_a_valid_token_are_rejected(tmp_path, headers) -> None:
    async with http_app(tmp_path) as app, raw_client(app, **headers) as http:
        response = await http.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401
    # Points clients to the protected resource metadata (RFC 9728).
    assert "resource_metadata" in response.headers["www-authenticate"]


async def test_token_in_query_string_is_ignored(tmp_path) -> None:
    async with http_app(tmp_path) as app, raw_client(app) as http:
        response = await http.post(f"/mcp?access_token={READER}", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401


async def test_foreign_host_header_is_rejected(tmp_path) -> None:
    # DNS rebinding: a page on evil.example resolving to our IP sends its own Host.
    async with http_app(tmp_path) as app, raw_client(app, Authorization=f"Bearer {READER}") as http:
        response = await http.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"}
        )
    assert response.status_code in (400, 421)


async def test_foreign_origin_is_rejected(tmp_path) -> None:
    async with http_app(tmp_path) as app, raw_client(app, Authorization=f"Bearer {READER}") as http:
        response = await http.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "Origin": "https://evil.example"}
        )
    assert response.status_code == 403


async def test_session_cannot_be_used_with_another_token(tmp_path, ha_api) -> None:
    async with http_app(tmp_path) as app:
        async with raw_client(app, Authorization=f"Bearer {ADMIN}") as admin:
            init = await admin.post("/mcp", json=INIT, headers=MCP_HEADERS)
            session_id = init.headers["mcp-session-id"]
        async with raw_client(app, Authorization=f"Bearer {READER}") as reader:
            hijack = await reader.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={**MCP_HEADERS, "mcp-session-id": session_id},
            )
    assert init.status_code == 200
    assert hijack.status_code in (403, 404)


async def test_health_check_needs_no_token_and_says_nothing(tmp_path) -> None:
    async with http_app(tmp_path) as app, raw_client(app) as http:
        response = await http.get("/healthz")
    assert (response.status_code, response.text) == (200, "ok")


async def test_protected_resource_metadata(tmp_path) -> None:
    async with http_app(tmp_path) as app, raw_client(app) as http:
        response = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.json()["resource"] == f"{PUBLIC_URL}/mcp"
