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
    id: int
    at: datetime
    actor: str
    subject_type: str
    subject_id: str
    kind: str
    data: dict[str, Any]


class Subscription:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    def _put(self, event: Event) -> None:
        self._queue.put_nowait(event)

    async def next_matching(self, predicate: Callable[[Event], bool]) -> Event:
        """Wait for the next matching event; bound it with `asyncio.timeout`."""
        while not predicate(event := await self._queue.get()):
            pass
        return event


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: set[Subscription] = set()

    def publish(self, event: Event) -> None:
        for sub in self._subscriptions:
            sub._put(event)

    @contextmanager
    def subscribe(self) -> Generator[Subscription]:
        """Subscribe *before* checking state, so no event is missed in between."""
        sub = Subscription()
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
