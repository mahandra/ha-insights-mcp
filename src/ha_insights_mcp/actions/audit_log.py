"""Append-only SQLite log of every action attempt, allowed or not.

stdlib sqlite3 run in a worker thread; one connection guarded by a lock. UPDATE and DELETE
are blocked by triggers, so the server itself cannot rewrite history. No secrets are
stored: only the request, the decision and the observed states.
"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Outcome = Literal[
    "executed",  # HA accepted the call
    "failed",  # HA rejected the call or the connection failed
    "unknown",  # timeout: the call may or may not have been applied
    "denied_policy",  # the action policy rejected the request
    "declined_by_user",  # the human said no
    "cancelled_by_user",  # the human dismissed the confirmation
    "no_confirmation_channel",  # confirmation needed, but the client cannot ask a human
    "rate_limited",
]
Confirmation = Literal["not_required", "accepted", "declined", "cancelled", "unavailable"]

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    service TEXT NOT NULL,
    entity_ids TEXT NOT NULL,
    data TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk TEXT,
    confirmation TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    context_id TEXT,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    verified INTEGER
);
CREATE INDEX IF NOT EXISTS actions_at ON actions (at);
CREATE TRIGGER IF NOT EXISTS actions_no_update BEFORE UPDATE ON actions
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS actions_no_delete BEFORE DELETE ON actions
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    at: datetime
    service: str
    entity_ids: list[str]
    data: dict[str, Any]
    reason: str
    outcome: Outcome
    risk: str | None = None
    confirmation: Confirmation | None = None
    detail: str | None = None
    context_id: str | None = None
    # entity_id -> state before and after the call
    before: dict[str, str] = field(default_factory=dict)
    after: dict[str, str] = field(default_factory=dict)
    verified: bool | None = None
    id: int | None = None


class AuditLog:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def record(self, entry: AuditEntry) -> AuditEntry:
        return await asyncio.to_thread(self._record, entry)

    async def recent(
        self, limit: int = 20, entity_id: str | None = None, outcome: Outcome | None = None
    ) -> list[AuditEntry]:
        """Newest first."""
        return await asyncio.to_thread(self._recent, limit, entity_id, outcome)

    def _record(self, entry: AuditEntry) -> AuditEntry:
        row = (
            entry.at.isoformat(),
            entry.service,
            json.dumps(entry.entity_ids),
            json.dumps(entry.data, sort_keys=True),
            entry.reason,
            entry.risk,
            entry.confirmation,
            entry.outcome,
            entry.detail,
            entry.context_id,
            json.dumps(entry.before, sort_keys=True),
            json.dumps(entry.after, sort_keys=True),
            None if entry.verified is None else int(entry.verified),
        )
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO actions (at, service, entity_ids, data, reason, risk, confirmation,"
                " outcome, detail, context_id, before, after, verified)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        return replace(entry, id=cursor.lastrowid)

    def _recent(self, limit: int, entity_id: str | None, outcome: str | None) -> list[AuditEntry]:
        where, params = [], []
        if entity_id is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(entity_ids) WHERE value = ?)")
            params.append(entity_id)
        if outcome is not None:
            where.append("outcome = ?")
            params.append(outcome)
        sql = "SELECT * FROM actions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [_entry(row) for row in rows]


def _entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        at=datetime.fromisoformat(row["at"]),
        service=row["service"],
        entity_ids=json.loads(row["entity_ids"]),
        data=json.loads(row["data"]),
        reason=row["reason"],
        risk=row["risk"],
        confirmation=row["confirmation"],
        outcome=row["outcome"],
        detail=row["detail"],
        context_id=row["context_id"],
        before=json.loads(row["before"]),
        after=json.loads(row["after"]),
        verified=None if row["verified"] is None else bool(row["verified"]),
    )
