from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    topic: str
    payload: dict[str, Any]


class EventBus:
    """In-process bus. A Redis/Kafka/NATS adapter can implement the same methods."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)

    def subscribe(self, topic: str, maxsize: int = 1000) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers[topic].append(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue[Event]) -> None:
        listeners = self._subscribers.get(topic, [])
        if queue in listeners:
            listeners.remove(queue)

    async def publish(self, event: Event) -> None:
        self._emit(event.topic, event)
        if event.topic != "*":
            self._emit("*", event)

    def _emit(self, topic: str, event: Event) -> None:
        for queue in list(self._subscribers.get(topic, [])):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    def publish_nowait(self, event: Event) -> None:
        for queue in list(self._subscribers.get(event.topic, [])):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue


@dataclass
class EngineLogBuffer:
    limit: int = 200
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, item: dict[str, Any]) -> None:
        self.items.append(item)
        if len(self.items) > self.limit:
            del self.items[: len(self.items) - self.limit]
