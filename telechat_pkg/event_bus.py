"""
Event Bus / Webhook System (Feature 6) — typed async event bus with webhook support.

Inspired by the Claude Quickstarts architecture where external automation, scheduled
jobs, and direct chat are decoupled by an event bus and notification service.

The event bus allows any component to publish events that other components can
subscribe to, enabling loose coupling between features.

Usage:
    from telechat_pkg.event_bus import EventBus, Event
    bus = EventBus()
    bus.subscribe("webhook.github", handler_fn)
    await bus.publish(Event(type="webhook.github", data={"action": "push"}))
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

log = logging.getLogger(__name__)


@dataclass
class Event:
    type: str
    data: dict = field(default_factory=dict)
    source: str = ""
    timestamp: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.id:
            self.id = f"{self.type}:{int(self.timestamp * 1000)}"


# Standard event types
class EventTypes:
    # Chat events
    MESSAGE_RECEIVED = "chat.message_received"
    RESPONSE_SENT = "chat.response_sent"
    SESSION_STARTED = "chat.session_started"
    SESSION_ENDED = "chat.session_ended"

    # Webhook events
    WEBHOOK_GITHUB = "webhook.github"
    WEBHOOK_GENERIC = "webhook.generic"

    # Scheduled events
    SCHEDULED_TASK = "scheduled.task"
    SCHEDULED_REMINDER = "scheduled.reminder"

    # System events
    HEALTH_CHECK = "system.health_check"
    BUDGET_WARNING = "system.budget_warning"
    ERROR = "system.error"

    # Agent events
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    MEMORY_EXTRACTED = "agent.memory_extracted"


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus with typed subscriptions and wildcard support."""

    def __init__(self, max_queue: int = 1000):
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._history: list[Event] = []
        self._max_history = 100

    def subscribe(self, event_type: str, handler: EventHandler):
        """Subscribe to events of a given type. Use '*' for all events."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        log.debug("Subscribed %s to %s", handler.__name__, event_type)

    def unsubscribe(self, event_type: str, handler: EventHandler):
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                h for h in self._subscribers[event_type] if h != handler
            ]

    async def publish(self, event: Event):
        """Publish an event to all matching subscribers."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        handlers = list(self._subscribers.get(event.type, []))
        # Wildcard subscribers
        handlers.extend(self._subscribers.get("*", []))
        # Prefix matching: "webhook.*" matches "webhook.github"
        for pattern, subs in self._subscribers.items():
            if pattern.endswith(".*") and event.type.startswith(pattern[:-2]):
                handlers.extend(subs)

        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                log.error("Event handler %s failed for %s: %s", handler.__name__, event.type, e)

    async def publish_async(self, event: Event):
        """Non-blocking publish via queue."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("Event bus queue full, dropping event: %s", event.type)

    async def start(self):
        """Start the async event processor."""
        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        log.info("Event bus started")

    async def stop(self):
        """Stop the event processor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Event bus stopped")

    async def _process_loop(self):
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self.publish(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Event processing error: %s", e)

    def recent_events(self, event_type: str | None = None, limit: int = 20) -> list[Event]:
        events = self._history
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]


# ─── Webhook receiver (FastAPI) ──────────────────────────────────────────────

class WebhookReceiver:
    """Lightweight webhook receiver that publishes events to the event bus."""

    def __init__(self, event_bus: EventBus, github_secret: str = "", bearer_token: str = ""):
        self.bus = event_bus
        self.github_secret = github_secret
        self.bearer_token = bearer_token

    def verify_github_signature(self, payload: bytes, signature: str) -> bool:
        if not self.github_secret:
            return True
        expected = "sha256=" + hmac.new(
            self.github_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_bearer(self, auth_header: str) -> bool:
        if not self.bearer_token:
            return True
        return auth_header == f"Bearer {self.bearer_token}"

    async def handle_github(self, payload: dict, event_name: str = "push") -> Event:
        event = Event(
            type=EventTypes.WEBHOOK_GITHUB,
            data={"payload": payload, "github_event": event_name},
            source="github",
        )
        await self.bus.publish(event)
        return event

    async def handle_generic(self, payload: dict, source: str = "unknown") -> Event:
        event = Event(
            type=EventTypes.WEBHOOK_GENERIC,
            data=payload,
            source=source,
        )
        await self.bus.publish(event)
        return event


# ─── Singleton ────────────────────────────────────────────────────────────────

_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
