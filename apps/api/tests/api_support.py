"""Test helpers for the API tests (importable because of pytest `pythonpath`)."""

from datetime import UTC, datetime, timedelta

from argus_api.db import Database, ts
from argus_core.tasks import TaskResult

DONE = TaskResult(summary="done")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def insert_agent(db: Database, agent_id: str, state: str = "active") -> None:
    """Shortcut for tests that need an agent row but not the registration flow."""
    async with db.write() as tx:
        ended = ts(tx.now) if state in ("gone", "lost") else None
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, capabilities, state,"
            " last_heartbeat_at, created_at, ended_at)"
            " VALUES (?, ?, 'worker', '[]', ?, ?, ?, ?)",
            (agent_id, agent_id, state, ts(tx.now), ts(tx.now), ended),
        )
