"""In-process event bus: the default way modules interact.

Publishing never blocks on subscribers: each handler runs as its own task, and a
failing handler is logged without affecting the publisher or other handlers. The
interface is deliberately small so it can later be backed by a network queue without
changing modules.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("itops.bus")


@dataclass(frozen=True)
class Event:
    name: str  # fully qualified: "<module>.<event>"
    payload: dict[str, Any]
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_name: str, handler: Handler, *, subscriber: str) -> None:
        self._handlers[event_name].append((subscriber, handler))

    def publish(self, event: Event) -> None:
        for subscriber, handler in self._handlers.get(event.name, []):
            task = asyncio.create_task(self._run(subscriber, handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, subscriber: str, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception(
                "handler in %r failed for event %s (%s)", subscriber, event.name, event.id
            )

    async def drain(self) -> None:
        """Wait for all in-flight handlers (shutdown and tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
