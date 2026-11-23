from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


class EventBroadcaster:
    """In-memory async pub/sub broadcaster driving <50ms SSE updates to the Director Cockpit."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        envelope = json.dumps(
            {
                "type": event_type,
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            },
            ensure_ascii=False,
        )
        dead: list[asyncio.Queue[str]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)


broadcaster = EventBroadcaster()
