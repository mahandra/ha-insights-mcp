"""Bearer tokens for the HTTP transport: a token file of SHA-256 hashes with scopes.

The raw token is shown once at creation (`ha-insights-mcp token create`) and never stored:
a leaked tokens.toml does not leak access. Over stdio there is no token; the client is the
local user's own process and is trusted.

Scopes:
- ha:read     analytics, diagnostics and preview_action
- ha:actions  execute_action (implies ha:read). Whoever holds this token can also answer
              the human confirmation of risky actions, so give it only to trusted clients.
"""

import hashlib
import hmac
import secrets
import tomllib
from pathlib import Path
from typing import Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Scope = Literal["ha:read", "ha:actions"]
SCOPE_READ: Scope = "ha:read"
SCOPE_ACTIONS: Scope = "ha:actions"
TOKEN_PREFIX = "hai_"
LOCAL_ACTOR = "local"


class AuthConfigError(ValueError):
    """The token file is missing or invalid."""


class TokenEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$", description="Shown in the audit log")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: list[Scope] = [SCOPE_READ]

    @field_validator("scopes")
    @classmethod
    def _actions_imply_read(cls, scopes: list[Scope]) -> list[Scope]:
        return sorted({*scopes, SCOPE_READ})


class TokenFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: list[TokenEntry] = []

    @field_validator("tokens")
    @classmethod
    def _unique(cls, tokens: list[TokenEntry]) -> list[TokenEntry]:
        if len({t.name for t in tokens}) != len(tokens):
            raise ValueError("token names must be unique")
        if len({t.sha256 for t in tokens}) != len(tokens):
            raise ValueError("the same token is listed twice")
        return tokens


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_tokens(path: Path) -> list[TokenEntry]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthConfigError(f"token file not found: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuthConfigError(f"cannot read token file {path}: {exc}") from None
    try:
        tokens = TokenFile.model_validate(raw).tokens
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'tokens'}: {e['msg']}" for e in exc.errors()
        )
        raise AuthConfigError(f"invalid token file {path}: {problems}") from None
    if not tokens:
        raise AuthConfigError(f"no tokens in {path}: the HTTP transport needs at least one")
    return tokens


class StaticTokenVerifier:
    """SDK TokenVerifier over a fixed set of token hashes."""

    def __init__(self, entries: list[TokenEntry]) -> None:
        self._entries = list(entries)

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hash_token(token)
        match = None
        # Compare against every entry in constant time; do not stop at the first hit.
        for entry in self._entries:
            if hmac.compare_digest(digest, entry.sha256):
                match = entry
        if match is None:
            return None
        return AccessToken(
            # The SDK keeps this for the request; hold the hash, not the secret.
            token=digest,
            client_id=match.name,
            subject=match.name,
            scopes=list(match.scopes),
        )


def require_scope(scope: Scope) -> None:
    """Raise a ToolError if the HTTP caller's token lacks `scope`. No-op over stdio."""
    token = get_access_token()
    if token is not None and scope not in token.scopes:
        raise ToolError(f"This token does not allow this tool (needs scope {scope}).")


def current_actor() -> str:
    """Token name for HTTP callers, 'local' over stdio. Recorded in the audit log."""
    token = get_access_token()
    return token.client_id if token is not None else LOCAL_ACTOR
