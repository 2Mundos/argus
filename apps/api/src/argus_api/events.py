"""Event log and in-process bus (single-node 3.3).

`record()` writes an event row in the caller's transaction; the database publishes
it on the bus only after that transaction commits.
"""

import asyncio
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argus_api.db import Tx


@dataclass(frozen=True)
class Event:
    # 0 marks a transient event: published live only, never in the events table.
    id: int
    at: datetime
    actor: str
    subject_type: str
    subject_id: str
    kind: str
    data: dict[str, Any]


class OverflowedError(Exception):
    """The subscriber fell too far behind and missed events. Re-read state (or
    resume from the events table) and subscribe again."""


class Subscription:
    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)
        self.overflowed = False

    def _put(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    async def next_matching(self, predicate: Callable[[Event], bool]) -> Event:
        """Wait for the next matching event; bound it with `asyncio.timeout`."""
        while True:
            if self.overflowed and self._queue.empty():
                raise OverflowedError
            event = await self._queue.get()
            if predicate(event):
                return event


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: set[Subscription] = set()

    def publish(self, event: Event) -> None:
        for sub in self._subscriptions:
            sub._put(event)

    @contextmanager
    def subscribe(self, maxsize: int = 1000) -> Generator[Subscription]:
        """Subscribe *before* checking state, so no event is missed in between.

        A subscriber that falls `maxsize` events behind is marked overflowed
        rather than buffering without bound.
        """
        sub = Subscription(maxsize)
        self._subscriptions.add(sub)
        try:
            yield sub
        finally:
            self._subscriptions.discard(sub)


async def record(
    tx: "Tx",
    actor: str,
    subject_type: str,
    subject_id: str,
    kind: str,
    data: dict[str, Any] | None = None,
) -> Event:
    from argus_api.db import ts

    data = data or {}
    cur = await tx.conn.execute(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts(tx.now), actor, subject_type, subject_id, kind, json.dumps(data)),
    )
    assert cur.lastrowid is not None
    event = Event(cur.lastrowid, tx.now, actor, subject_type, subject_id, kind, data)
    tx.events.append(event)
    return event


def transient(
    tx: "Tx",
    actor: str,
    subject_type: str,
    subject_id: str,
    kind: str,
    data: dict[str, Any],
) -> None:
    """Publish an event live after commit without storing it (no replay, no id)."""
    tx.transient.append(Event(0, tx.now, actor, subject_type, subject_id, kind, data))


def to_sse(event: Event) -> str:
    payload = {
        "id": event.id,
        "at": event.at.isoformat(),
        "actor": event.actor,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "kind": event.kind,
        "data": event.data,
    }
    # A transient event has no id, so it never moves the client's Last-Event-ID.
    ident = f"id: {event.id}\n" if event.id else ""
    return f"{ident}data: {json.dumps(payload)}\n\n"
