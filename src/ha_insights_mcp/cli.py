"""Command line: `ha-insights-mcp` serves; `ha-insights-mcp token create` makes a token."""

import argparse

from ha_insights_mcp.auth import SCOPE_ACTIONS, SCOPE_READ, TokenEntry, generate_token, hash_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha-insights-mcp",
        description="MCP server for Home Assistant. Without a command, runs the server "
        "(transport from MCP_TRANSPORT).",
    )
    commands = parser.add_subparsers(dest="command")
    token = commands.add_parser("token", help="manage HTTP bearer tokens")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    create = token_commands.add_parser("create", help="generate a token and its tokens.toml entry")
    create.add_argument("--name", required=True, help="client name, shown in the audit log")
    create.add_argument(
        "--scope",
        action="append",
        choices=[SCOPE_READ, SCOPE_ACTIONS],
        help=f"repeatable; default {SCOPE_READ}. {SCOPE_ACTIONS} allows execute_action",
    )
    return parser


def token_create(name: str, scopes: list[str] | None) -> str:
    """The text printed by `token create`: the secret once, plus the entry to store."""
    token = generate_token()
    entry = TokenEntry(name=name, sha256=hash_token(token), scopes=scopes or [SCOPE_READ])
    scope_list = ", ".join(f'"{s}"' for s in entry.scopes)
    return (
        f"Token (shown once, give it to the client):\n  {token}\n\n"
        "Add to tokens.toml:\n\n"
        f'[[tokens]]\nname = "{entry.name}"\nsha256 = "{entry.sha256}"\nscopes = [{scope_list}]\n'
    )
