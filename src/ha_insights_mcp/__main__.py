"""Entry point: `ha-insights-mcp` / `python -m ha_insights_mcp`. Runs over stdio."""

import logging
import sys

from pydantic import ValidationError

from ha_insights_mcp.config import Settings
from ha_insights_mcp.server import server_from_settings


def main() -> None:
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

    server_from_settings(settings).run("stdio")


if __name__ == "__main__":
    main()
