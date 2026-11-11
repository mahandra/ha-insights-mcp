from pathlib import Path

import pytest

from ha_insights_mcp.auth import AuthConfigError
from ha_insights_mcp.config import Settings
from ha_insights_mcp.server import server_from_settings


def settings(**values) -> Settings:
    return Settings(_env_file=None, ha_token="t", **values)


def test_http_without_token_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(AuthConfigError, match="not found"):
        server_from_settings(
            settings(mcp_transport="http", mcp_tokens_path=tmp_path / "missing.toml")
        )


def test_stdio_needs_no_tokens(tmp_path: Path) -> None:
    server_from_settings(settings(mcp_tokens_path=tmp_path / "missing.toml"))


@pytest.mark.parametrize(
    ("values", "url"),
    [
        ({}, "http://127.0.0.1:8000"),
        ({"mcp_host": "0.0.0.0", "mcp_port": 9000}, "http://localhost:9000"),
        ({"mcp_public_url": "https://ha-mcp.example.com/"}, "https://ha-mcp.example.com"),
    ],
)
def test_public_url(values: dict, url: str) -> None:
    assert settings(**values).public_url == url


def test_public_url_must_be_http() -> None:
    with pytest.raises(ValueError, match="MCP_PUBLIC_URL"):
        settings(mcp_public_url="ftp://x")
