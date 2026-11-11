"""Terminal demo against the dev HA: diagnostics, a refused action, a confirmed one.

This script is a real MCP client over stdio. It plays the role of the client app: when
the server asks for a human confirmation (elicitation), the question is shown here and
you answer it. Used for the README GIF (docs/demo.tape).

    uv run python dev/demo.py
"""

import asyncio
import os
import sys
from typing import Any

from mcp import Client, types
from mcp.client.stdio import StdioServerParameters

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)


def step(title: str) -> None:
    print(f"\n{BOLD}▶ {title}{RESET}")


def call_line(tool: str, args: dict[str, Any]) -> None:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"{DIM}  {tool}({shown}){RESET}")


async def ask_human(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
    print(f"\n{YELLOW}┌─ Confirmation requested by the server ─{RESET}")
    for line in params.message.splitlines():
        print(f"{YELLOW}│{RESET} {line}")
    answer = await asyncio.to_thread(input, f"{YELLOW}└─ Allow? [y/N] {RESET}")
    if answer.strip().lower() in ("y", "yes"):
        return types.ElicitResult(action="accept", content={"confirm": True})
    return types.ElicitResult(action="decline")


async def run(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    call_line(tool, args)
    result = await client.call_tool(tool, args)
    if result.is_error:
        sys.exit(f"{tool} failed: {result.content[0].text}")
    return result.structured_content


async def main() -> None:
    env = {
        **os.environ,
        "ACTIONS_ENABLED": "true",
        "AUDIT_DB_PATH": "data/demo-audit.db",
        "LOG_LEVEL": "WARNING",
    }
    server = StdioServerParameters(command="uv", args=["run", "ha-insights-mcp"], env=env)
    async with Client(server, elicitation_callback=ask_human) as client:
        step("What is broken in my home?")
        health = await run(client, "check_device_health", min_severity="error")
        print(f"  {health['counts']}")
        for issue in health["issues"][:3]:
            print(f"  {RED}{issue['code']:<12}{RESET} {issue['name']}: {issue['detail']}")

        audit = await run(client, "audit_automations", min_severity="error")
        for finding in audit["findings"][:3]:
            print(f"  {RED}{finding['code']:<16}{RESET} {finding['name']}: {finding['message']}")

        step("Assistant: 'set the thermostat to 24' (this HA uses °F)")
        preview = await run(
            client,
            "preview_action",
            service="climate.set_temperature",
            entity_ids=["climate.hvac"],
            data={"temperature": 24},
        )
        for check in preview["checks"]:
            if not check["ok"]:
                print(f"  {RED}✗ {check['check']}{RESET}: {check['detail']}")

        step("Assistant: 'unlock the front door, a guest is arriving'")
        result = await run(
            client,
            "execute_action",
            service="lock.unlock",
            entity_ids=["lock.front_door"],
            reason="Guest is arriving, the user asked to let them in",
        )
        color = GREEN if result["outcome"] == "executed" else YELLOW
        print(f"  {color}{result['outcome']}{RESET}: {result['message']}")
        for target in result["results"]:
            print(
                f"  {target['entity_id']}: {target['before']} → {target['after']} "
                f"({target['verification']})"
            )

        step("Audit log")
        log = await run(client, "get_action_log", limit=3)
        for entry in log["entries"]:
            print(
                f"  #{entry['action_id']} {entry['service']:<16} {entry['outcome']:<18} "
                f"risk={entry['risk']} by={entry['actor']}"
            )

        # Leave the dev home as we found it (lock is low risk: no question).
        await client.call_tool(
            "execute_action",
            {"service": "lock.lock", "entity_ids": ["lock.front_door"], "reason": "demo cleanup"},
        )


if __name__ == "__main__":
    asyncio.run(main())
