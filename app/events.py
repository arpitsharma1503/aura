"""Server-Sent Events: tell open dashboards and phones when data changes.

Events carry only a small payload (e.g. the grocery list revision); clients
then fetch the data over the normal API. EventSource reconnects by itself, and
clients re-sync on every (re)connect, so a dropped connection can't leave a
screen out of date.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15
MAX_SUBSCRIBERS = 32


class TooManySubscribers(Exception):
    pass


class Broker:
    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self.max_subscribers = max_subscribers
        self._subscribers: set[asyncio.Queue] = set()
        self.closed = False

    @property
    def count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        if len(self._subscribers) >= self.max_subscribers:
            raise TooManySubscribers()
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: str, data: dict) -> None:
        message = (event, data)
        for q in list(self._subscribers):
            if q.full():  # a stalled client: drop its oldest message, it will re-sync anyway
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(message)

    def close(self) -> None:
        """Ask every open stream to finish (used on shutdown)."""
        self.closed = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                q.get_nowait()
                q.put_nowait(None)


def format_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream(
    broker: Broker,
    is_disconnected: Callable[[], Awaitable[bool]],
    hello: dict,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    q = broker.subscribe()
    try:
        yield "retry: 3000\n\n"
        yield format_event("hello", hello)
        while not broker.closed:
            try:
                message = await asyncio.wait_for(q.get(), timeout=keepalive)
            except TimeoutError:
                if await is_disconnected():
                    break
                yield ": keepalive\n\n"
                continue
            if message is None:
                break
            yield format_event(*message)
    finally:
        broker.unsubscribe(q)
