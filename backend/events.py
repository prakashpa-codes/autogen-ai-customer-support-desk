"""Ticket event broker: persists every agent event and fans it out to live subscribers.

Events are written to SQLite first, then pushed to in-memory queues. An SSE client therefore
gets a complete timeline even if it connects late or reconnects: it replays stored events
after its last seen sequence number, then follows live ones.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator

from core.store import TicketStore

TERMINAL_EVENTS = {"ticket_completed", "ticket_failed"}
KEEPALIVE_SECONDS = 15


class EventBroker:
    def __init__(self, store: TicketStore) -> None:
        self._store = store
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, ticket_id: str, event_type: str, agent: str | None, data: dict[str, Any]) -> dict:
        event = self._store.add_event(ticket_id, event_type, agent, data)
        for queue in list(self._subscribers.get(ticket_id, ())):
            queue.put_nowait(event)
        return event

    async def follow(self, ticket_id: str, after_seq: int = 0) -> AsyncIterator[dict | None]:
        """Yield stored then live events until the ticket finishes. Yields None as a keep-alive."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[ticket_id].add(queue)  # subscribe before replay so nothing falls in the gap
        try:
            last = after_seq
            for event in self._store.events(ticket_id, after_seq):
                last = event["seq"]
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if event["seq"] <= last:
                    continue
                last = event["seq"]
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
        finally:
            self._subscribers[ticket_id].discard(queue)
            if not self._subscribers[ticket_id]:
                self._subscribers.pop(ticket_id, None)
