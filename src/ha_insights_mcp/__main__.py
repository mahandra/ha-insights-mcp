"""Entry point: `ha-insights-mcp` / `python -m ha_insights_mcp`. Runs over stdio by default,
Streamable HTTP with MCP_TRANSPORT=http."""

import logging
import sys

from pydantic import ValidationError

from ha_insights_mcp.actions.policy import PolicyError
from ha_insights_mcp.auth import AuthConfigError
from ha_insights_mcp.cli import build_parser, token_create
from ha_insights_mcp.config import Settings
from ha_insights_mcp.server import server_from_settings


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "token":
        print(token_create(args.name, args.scope), end="")
        return

    try:
        settings = Settings()
    except ValidationError as exc:
        # Report only field names and reasons; never echo input values (could be the token).
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in exc.errors()
        )
        sys.exit(f"ha-insights-mcp: invalid configuration: {problems}")

    # stdout is the JSON-RPC channel in stdio transport, so logs must go to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        server = server_from_settings(settings)
    except (PolicyError, AuthConfigError) as exc:
        # Fail closed: no guessing what is allowed or who may connect.
        sys.exit(f"ha-insights-mcp: {exc}")
    if settings.mcp_transport == "http":
        from ha_insights_mcp.http import run_http

        run_http(server, settings)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
