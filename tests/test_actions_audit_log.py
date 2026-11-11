import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ha_insights_mcp.actions.audit_log import AuditEntry, AuditLog
from ha_insights_mcp.actions.ratelimit import RateLimiter

pytestmark = pytest.mark.anyio

AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def entry(i: int = 0, **overrides) -> AuditEntry:
    values = {
        "at": AT + timedelta(minutes=i),
        "service": "light.turn_on",
        "entity_ids": ["light.bed_light"],
        "data": {"brightness_pct": 40},
        "reason": "user asked for dim light",
        "outcome": "executed",
        "risk": "low",
        "confirmation": "not_required",
        "context_id": f"ctx{i}",
        "before": {"light.bed_light": "off"},
        "after": {"light.bed_light": "on"},
        "verified": True,
    }
    return AuditEntry(**(values | overrides))


@pytest.fixture
def log(tmp_path: Path):
    audit = AuditLog(tmp_path / "sub" / "audit.db")
    yield audit
    audit.close()


async def test_record_and_read_back(log: AuditLog) -> None:
    stored = await log.record(entry())
    assert stored.id == 1
    (back,) = await log.recent()
    assert back == stored


async def test_recent_is_newest_first_and_filtered(log: AuditLog) -> None:
    await log.record(entry(0))
    await log.record(entry(1, entity_ids=["lock.front_door"], outcome="declined_by_user"))
    await log.record(entry(2, entity_ids=["light.bed_light", "light.kitchen_lights"]))
    assert [e.id for e in await log.recent()] == [3, 2, 1]
    assert [e.id for e in await log.recent(limit=1)] == [3]
    assert [e.id for e in await log.recent(entity_id="light.kitchen_lights")] == [3]
    assert [e.id for e in await log.recent(outcome="declined_by_user")] == [2]
    # entity filter matches whole ids, not substrings
    assert await log.recent(entity_id="light.bed") == []


async def test_log_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    audit = AuditLog(path)
    await audit.record(entry())
    audit.close()
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE actions SET outcome = 'failed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM actions")
    conn.close()


async def test_log_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    first = AuditLog(path)
    await first.record(entry())
    first.close()
    second = AuditLog(path)
    assert len(await second.recent()) == 1
    second.close()


def test_rate_limiter_sliding_window() -> None:
    now = [0.0]
    limiter = RateLimiter(2, window_seconds=60, clock=lambda: now[0])
    assert limiter.try_acquire()
    now[0] = 10
    assert limiter.try_acquire()
    assert not limiter.try_acquire()
    assert limiter.retry_after() == 50
    now[0] = 60  # the first event leaves the window
    assert limiter.retry_after() == 0
    assert limiter.try_acquire()
    assert not limiter.try_acquire()
