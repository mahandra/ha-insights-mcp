"""Thin async client for the Home Assistant REST and WebSocket APIs.

- REST (httpx) for states and config.
- WebSocket for the area/device/entity registries, which REST does not expose.

This module knows nothing about MCP. It raises the typed errors from `ha.errors`.
Phase 1: read-only — there are intentionally no methods that change HA state.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self

import httpx
from pydantic import BaseModel, SecretStr, ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from ha_insights_mcp.ha.errors import (
    HAAuthError,
    HAConnectionError,
    HAError,
    HANotFoundError,
    HAResponseError,
    HATimeoutError,
)
from ha_insights_mcp.ha.models import (
    ENTITY_ID_RE,
    Area,
    AutomationTrace,
    Device,
    EntityRegistryEntry,
    EntityState,
    HAConfig,
    Registries,
    RepairIssue,
    StatBucket,
    StatePoint,
)

logger = logging.getLogger(__name__)

# Registry lists on big installations can be several MB.
_WS_MAX_MESSAGE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WSCommandError:
    """A failed WebSocket command, returned instead of raised when errors are allowed."""

    code: str
    message: str


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


WebSocketConnect = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]


def _default_ws_connect(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    return ws_connect(url, max_size=_WS_MAX_MESSAGE_BYTES, open_timeout=None)


class HAClient:
    """Async Home Assistant client. Use as `async with HAClient(...) as ha:`."""

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect_factory: WebSocketConnect = _default_ws_connect,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._ws_connect = ws_connect_factory
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=timeout,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- REST

    async def get_config(self) -> HAConfig:
        return _parse(HAConfig, await self._get_json("/api/config"), "config")

    async def get_states(self) -> list[EntityState]:
        payload = await self._get_json("/api/states")
        if not isinstance(payload, list):
            raise HAResponseError("Unexpected /api/states payload: expected a list")
        return [_parse(EntityState, item, "state") for item in payload]

    async def get_state(self, entity_id: str) -> EntityState:
        # Validate before building the URL: entity_id comes from the LLM.
        if not ENTITY_ID_RE.fullmatch(entity_id):
            raise ValueError(f"Invalid entity_id {entity_id!r}: expected '<domain>.<object_id>'")
        try:
            payload = await self._get_json(f"/api/states/{entity_id}")
        except HANotFoundError:
            raise HANotFoundError(f"Entity {entity_id!r} not found in Home Assistant") from None
        return _parse(EntityState, payload, "state")

    async def _get_json(self, path: str) -> Any:
        try:
            response = await self._http.get(path)
        except httpx.TimeoutException:
            raise HATimeoutError(
                f"Home Assistant at {self._base_url} did not respond within {self._timeout:g}s"
            ) from None
        except httpx.TransportError as exc:
            raise HAConnectionError(
                f"Cannot connect to Home Assistant at {self._base_url} "
                f"({type(exc).__name__}). Is it running and is HA_URL correct?"
            ) from None

        status = response.status_code
        if status in (401, 403):
            raise HAAuthError(
                f"Home Assistant rejected the access token (HTTP {status}). "
                "Check HA_TOKEN: create a long-lived token in your HA profile."
            )
        if status == 404:
            raise HANotFoundError(f"Not found: {path}")
        if status >= 400:
            raise HAResponseError(f"Home Assistant returned HTTP {status} for {path}")
        try:
            return response.json()
        except ValueError:
            raise HAResponseError(f"Home Assistant returned invalid JSON for {path}") from None

    # ----------------------------------------------------------- WebSocket

    async def get_registries(self) -> Registries:
        """Fetch area, device and entity registries in a single WebSocket session."""
        areas, devices, entities = await self._ws_commands(
            {"type": "config/area_registry/list"},
            {"type": "config/device_registry/list"},
            {"type": "config/entity_registry/list"},
        )
        return Registries(
            areas=[_parse(Area, a, "area") for a in areas],
            devices=[_parse(Device, d, "device") for d in devices],
            entities=[_parse(EntityRegistryEntry, e, "entity registry entry") for e in entities],
        )

    async def get_history(
        self, entity_ids: Iterable[str], start: datetime, end: datetime | None = None
    ) -> dict[str, list[StatePoint]]:
        """State changes per entity. The first point is the state at `start`."""
        ids = list(entity_ids)
        if not ids:
            return {}
        command: dict[str, Any] = {
            "type": "history/history_during_period",
            "start_time": start.isoformat(),
            "entity_ids": ids,
            "minimal_response": True,
            "no_attributes": True,
            "significant_changes_only": False,
            "include_start_time_state": True,
        }
        if end is not None:
            command["end_time"] = end.isoformat()
        (result,) = await self._ws_commands(command)
        if not isinstance(result, dict):
            raise HAResponseError("Unexpected history payload: expected an object")
        return {
            entity_id: [_state_point(p) for p in points] for entity_id, points in result.items()
        }

    async def get_statistics(
        self, statistic_ids: Iterable[str], start: datetime, end: datetime, period: str = "hour"
    ) -> dict[str, list[StatBucket]]:
        """Long-term statistics (mean/min/max) per statistic id, e.g. a sensor entity_id."""
        ids = list(statistic_ids)
        if not ids:
            return {}
        (result,) = await self._ws_commands(
            {
                "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": ids,
                "period": period,
                "types": ["mean", "min", "max"],
            }
        )
        if not isinstance(result, dict):
            raise HAResponseError("Unexpected statistics payload: expected an object")
        return {
            stat_id: [
                _parse(
                    StatBucket,
                    {**b, "start": _from_ms(b.get("start")), "end": _from_ms(b.get("end"))},
                    "statistics bucket",
                )
                for b in buckets
            ]
            for stat_id, buckets in result.items()
        }

    async def get_automation_configs(
        self, entity_ids: Iterable[str]
    ) -> dict[str, dict[str, Any] | None]:
        """Automation configs by entity_id (one WS session). None if HA has no config,
        e.g. a YAML automation without `id`."""
        ids = list(entity_ids)
        if not ids:
            return {}
        results = await self._ws_commands(
            *({"type": "automation/config", "entity_id": e} for e in ids), allow_errors=True
        )
        configs: dict[str, dict[str, Any] | None] = {}
        for entity_id, result in zip(ids, results, strict=True):
            config = None if isinstance(result, WSCommandError) else result.get("config")
            configs[entity_id] = config if isinstance(config, dict) else None
        return configs

    async def list_automation_traces(self) -> list[AutomationTrace]:
        """Stored runs of all automations, newest last per HA's ordering."""
        (result,) = await self._ws_commands({"type": "trace/list", "domain": "automation"})
        return [
            _parse(
                AutomationTrace,
                {
                    **t,
                    "started_at": (t.get("timestamp") or {}).get("start"),
                    "finished_at": (t.get("timestamp") or {}).get("finish"),
                },
                "automation trace",
            )
            for t in result or []
        ]

    async def list_repair_issues(self) -> list[RepairIssue]:
        """Issues Home Assistant itself has raised (Settings -> Repairs)."""
        (result,) = await self._ws_commands({"type": "repairs/list_issues"})
        return [_parse(RepairIssue, i, "repair issue") for i in (result or {}).get("issues", [])]

    @property
    def _ws_url(self) -> str:
        scheme, rest = self._base_url.split("://", 1)
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/api/websocket"

    async def _ws_commands(
        self, *commands: dict[str, Any], allow_errors: bool = False
    ) -> list[Any]:
        """Authenticate, run commands sequentially, return their `result` payloads.

        With `allow_errors`, a failed command yields a WSCommandError in its slot instead of
        raising (auth failures still raise).
        """
        try:
            async with asyncio.timeout(self._timeout):
                async with self._ws_connect(self._ws_url) as ws:
                    await self._ws_authenticate(ws)
                    return [
                        await self._ws_call(ws, msg_id, command, allow_errors)
                        for msg_id, command in enumerate(commands, start=1)
                    ]
        except TimeoutError:
            raise HATimeoutError(
                f"Home Assistant WebSocket at {self._base_url} did not respond "
                f"within {self._timeout:g}s"
            ) from None
        except HAError:
            raise
        except InvalidStatus as exc:
            raise HAConnectionError(
                f"Home Assistant refused the WebSocket handshake "
                f"(HTTP {exc.response.status_code}). Is websocket_api enabled?"
            ) from None
        except (OSError, WebSocketException) as exc:
            raise HAConnectionError(
                f"Cannot connect to Home Assistant WebSocket at {self._base_url} "
                f"({type(exc).__name__}). Is it running and is HA_URL correct?"
            ) from None

    async def _ws_authenticate(self, ws: WebSocketLike) -> None:
        hello = await _ws_recv(ws)
        if hello.get("type") != "auth_required":
            raise HAResponseError(f"Unexpected WebSocket greeting: {hello.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token.get_secret_value()}))
        reply = await _ws_recv(ws)
        if reply.get("type") == "auth_invalid":
            raise HAAuthError(
                "Home Assistant rejected the access token over WebSocket. Check HA_TOKEN."
            )
        if reply.get("type") != "auth_ok":
            raise HAResponseError(f"Unexpected WebSocket auth reply: {reply.get('type')!r}")

    async def _ws_call(
        self, ws: WebSocketLike, msg_id: int, command: dict[str, Any], allow_errors: bool
    ) -> Any:
        command_type = command["type"]
        await ws.send(json.dumps({"id": msg_id, **command}))
        while True:
            message = await _ws_recv(ws)
            # Skip anything that is not the answer to this command (e.g. pings, events).
            if message.get("id") == msg_id and message.get("type") == "result":
                break
        if not message.get("success"):
            error = message.get("error") or {}
            code, text = error.get("code", "unknown"), error.get("message", "")
            if code == "unauthorized":
                raise HAAuthError(
                    f"Token is not allowed to run {command_type!r}: it requires an admin user"
                )
            if allow_errors:
                return WSCommandError(code=code, message=text)
            raise HAResponseError(
                f"WebSocket command {command_type!r} failed: {code} {text}".strip()
            )
        return message.get("result")


async def _ws_recv(ws: WebSocketLike) -> dict[str, Any]:
    raw = await ws.recv()
    try:
        message = json.loads(raw)
    except ValueError:
        raise HAResponseError("Home Assistant sent invalid JSON over WebSocket") from None
    if not isinstance(message, dict):
        raise HAResponseError("Unexpected WebSocket message: expected a JSON object")
    return message


def _from_ms(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, UTC) if isinstance(value, int | float) else None


def _state_point(point: Any) -> StatePoint:
    # Compressed history format: s=state, lu=last_updated, lc=last_changed (epoch seconds;
    # lc is omitted when equal to lu).
    if not isinstance(point, dict) or "s" not in point or "lu" not in point:
        raise HAResponseError("Unexpected history point from Home Assistant")
    changed = point.get("lc", point["lu"])
    return StatePoint(state=str(point["s"]), changed_at=datetime.fromtimestamp(changed, UTC))


def _parse[M: BaseModel](model: type[M], payload: Any, what: str) -> M:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise HAResponseError(
            f"Unexpected {what} payload from Home Assistant ({exc.error_count()} errors)"
        ) from None
