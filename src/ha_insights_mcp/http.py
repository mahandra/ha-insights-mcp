"""Streamable HTTP transport: bearer auth, DNS rebinding protection, health check.

Authentication and session ownership come from the SDK: `MCPServer(token_verifier=...,
auth=AuthSettings(...))` installs the bearer middleware on /mcp and binds every session
(and every sealed request_state) to the token that created it.
"""

import logging
from urllib.parse import urlsplit

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from ha_insights_mcp.auth import SCOPE_READ, StaticTokenVerifier, load_tokens
from ha_insights_mcp.config import Settings

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def auth_settings(public_url: str) -> AuthSettings:
    return AuthSettings(
        # No authorization server: tokens are issued out of band (`token create`).
        issuer_url=public_url,
        resource_server_url=public_url + MCP_PATH,
        # Every token has ha:read; tools check ha:actions themselves.
        required_scopes=[SCOPE_READ],
        # Static tokens are not bound to a resource indicator.
        validate_token_resource=False,
    )


def token_verifier(settings: Settings) -> StaticTokenVerifier:
    """Raises AuthConfigError: HTTP never starts without tokens."""
    return StaticTokenVerifier(load_tokens(settings.mcp_tokens_path))


def transport_security(public_url: str, extra_origins: list[str]) -> TransportSecuritySettings:
    """Accept only our own Host/Origin headers (DNS rebinding). Loopback stays allowed so a
    health check inside the container works."""
    public = urlsplit(public_url)
    origin = f"{public.scheme}://{public.netloc}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[origin, *extra_origins],
    )


def build_http_app(mcp: MCPServer, public_url: str, extra_origins: list[str]) -> Starlette:
    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> PlainTextResponse:
        # Liveness only: no versions, no HA details, no auth needed.
        return PlainTextResponse("ok")

    return mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security(public_url, extra_origins),
    )


def run_http(mcp: MCPServer, settings: Settings) -> None:
    public_url = settings.public_url
    if settings.mcp_host not in LOOPBACK and public_url.startswith("http://"):
        logger.warning(
            "Listening on %s without TLS (MCP_PUBLIC_URL=%s): put an HTTPS reverse proxy in "
            "front of the server, bearer tokens travel in plain text otherwise",
            settings.mcp_host,
            public_url,
        )
    app = build_http_app(mcp, public_url, settings.mcp_allowed_origins)
    logger.info("Serving MCP on %s%s", public_url, MCP_PATH)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        server_header=False,
    )
