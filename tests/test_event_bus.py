"""
Behavior tests for the async event bus (telechat_pkg.event_bus).

Covers: Event construction, pub/sub semantics, wildcard + prefix matching,
subscriber error isolation, history ring-buffer, the async queue processor,
the webhook receiver (signature/bearer verification), and the singleton.

Run:
    pytest tests/test_event_bus.py -v
"""

import asyncio
import hashlib
import hmac
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import event_bus as eb
from telechat_pkg.event_bus import (
    Event,
    EventBus,
    EventTypes,
    WebhookReceiver,
    get_event_bus,
)


@pytest.fixture
def bus():
    return EventBus()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Event dataclass
# ══════════════════════════════════════════════════════════════════════════════


class TestEvent:
    def test_defaults_timestamp_and_id(self):
        e = Event(type="chat.x")
        assert e.timestamp > 0
        assert e.id.startswith("chat.x:")

    def test_id_derived_from_timestamp(self):
        e = Event(type="t", timestamp=1.5)
        assert e.id == "t:1500"

    def test_explicit_id_preserved(self):
        e = Event(type="t", id="custom-id")
        assert e.id == "custom-id"

    def test_explicit_timestamp_preserved(self):
        e = Event(type="t", timestamp=42.0)
        assert e.timestamp == 42.0

    def test_data_default_empty_dict(self):
        e = Event(type="t")
        assert e.data == {}


# ══════════════════════════════════════════════════════════════════════════════
# 2. subscribe / publish dispatch
# ══════════════════════════════════════════════════════════════════════════════


class TestPublishDispatch:
    @pytest.mark.asyncio
    async def test_exact_type_match(self, bus):
        got = []

        async def handler(e):
            got.append(e)

        bus.subscribe("chat.message_received", handler)
        await bus.publish(Event(type="chat.message_received"))
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_non_matching_type_not_delivered(self, bus):
        got = []

        async def handler(e):
            got.append(e)

        bus.subscribe("chat.a", handler)
        await bus.publish(Event(type="chat.b"))
        assert got == []

    @pytest.mark.asyncio
    async def test_wildcard_receives_all(self, bus):
        got = []

        async def handler(e):
            got.append(e.type)

        bus.subscribe("*", handler)
        await bus.publish(Event(type="any.thing"))
        await bus.publish(Event(type="other.thing"))
        assert got == ["any.thing", "other.thing"]

    @pytest.mark.asyncio
    async def test_prefix_match(self, bus):
        got = []

        async def handler(e):
            got.append(e.type)

        bus.subscribe("webhook.*", handler)
        await bus.publish(Event(type="webhook.github"))
        await bus.publish(Event(type="chat.x"))
        assert got == ["webhook.github"]

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_type(self, bus):
        calls = []

        async def h1(e):
            calls.append("h1")

        async def h2(e):
            calls.append("h2")

        bus.subscribe("t", h1)
        bus.subscribe("t", h2)
        await bus.publish(Event(type="t"))
        assert sorted(calls) == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_exact_and_wildcard_both_fire(self, bus):
        calls = []

        async def exact(e):
            calls.append("exact")

        async def star(e):
            calls.append("star")

        bus.subscribe("t", exact)
        bus.subscribe("*", star)
        await bus.publish(Event(type="t"))
        assert sorted(calls) == ["exact", "star"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. unsubscribe
# ══════════════════════════════════════════════════════════════════════════════


class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, bus):
        got = []

        async def handler(e):
            got.append(e)

        bus.subscribe("t", handler)
        bus.unsubscribe("t", handler)
        await bus.publish(Event(type="t"))
        assert got == []

    def test_unsubscribe_unknown_type_noop(self, bus):
        async def handler(e):
            pass

        # Should not raise
        bus.unsubscribe("never-subscribed", handler)

    def test_unsubscribe_only_removes_target(self, bus):
        async def h1(e):
            pass

        async def h2(e):
            pass

        bus.subscribe("t", h1)
        bus.subscribe("t", h2)
        bus.unsubscribe("t", h1)
        assert bus._subscribers["t"] == [h2]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Subscriber error isolation
# ══════════════════════════════════════════════════════════════════════════════


class TestErrorIsolation:
    @pytest.mark.asyncio
    async def test_failing_handler_does_not_block_others(self, bus):
        got = []

        async def boom(e):
            raise RuntimeError("handler exploded")

        async def good(e):
            got.append(e)

        bus.subscribe("t", boom)
        bus.subscribe("t", good)
        await bus.publish(Event(type="t"))  # must not raise
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_failure_logged_not_raised(self, bus):
        async def boom(e):
            raise ValueError("nope")

        bus.subscribe("t", boom)
        await bus.publish(Event(type="t"))  # no exception escapes


# ══════════════════════════════════════════════════════════════════════════════
# 5. History ring buffer
# ══════════════════════════════════════════════════════════════════════════════


class TestHistory:
    @pytest.mark.asyncio
    async def test_recent_events_records_published(self, bus):
        await bus.publish(Event(type="a"))
        await bus.publish(Event(type="b"))
        recent = bus.recent_events()
        assert [e.type for e in recent] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_recent_events_filter_by_type(self, bus):
        await bus.publish(Event(type="a"))
        await bus.publish(Event(type="b"))
        await bus.publish(Event(type="a"))
        recent = bus.recent_events(event_type="a")
        assert len(recent) == 2
        assert all(e.type == "a" for e in recent)

    @pytest.mark.asyncio
    async def test_recent_events_limit(self, bus):
        for i in range(10):
            await bus.publish(Event(type="t"))
        assert len(bus.recent_events(limit=3)) == 3

    @pytest.mark.asyncio
    async def test_history_capped_at_max(self, bus):
        bus._max_history = 5
        for i in range(20):
            await bus.publish(Event(type="t", data={"i": i}))
        assert len(bus._history) == 5
        # newest retained
        assert bus._history[-1].data["i"] == 19


# ══════════════════════════════════════════════════════════════════════════════
# 6. Async queue processor (start/stop/publish_async)
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncProcessor:
    @pytest.mark.asyncio
    async def test_publish_async_processed_by_loop(self, bus):
        got = asyncio.Event()

        async def handler(e):
            got.set()

        bus.subscribe("queued", handler)
        await bus.start()
        await bus.publish_async(Event(type="queued"))
        await asyncio.wait_for(got.wait(), timeout=2)
        await bus.stop()

    @pytest.mark.asyncio
    async def test_publish_async_queue_full_drops(self):
        small = EventBus(max_queue=1)
        await small.publish_async(Event(type="t"))
        # second one overflows the queue → dropped without raising
        await small.publish_async(Event(type="t"))
        assert small._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, bus):
        await bus.stop()  # _task is None

    @pytest.mark.asyncio
    async def test_start_then_stop_cancels_task(self, bus):
        await bus.start()
        assert bus._task is not None
        await bus.stop()
        assert bus._running is False

    @pytest.mark.asyncio
    async def test_process_loop_handler_exception_keeps_loop_alive(self, bus):
        """A handler raising inside the queued path is swallowed by publish()
        and the loop keeps draining subsequent events."""
        seen = []

        async def boom(e):
            raise RuntimeError("kaboom")

        async def good(e):
            seen.append(e.type)

        bus.subscribe("boom", boom)
        bus.subscribe("good", good)
        await bus.start()
        await bus.publish_async(Event(type="boom"))
        await bus.publish_async(Event(type="good"))
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        await bus.stop()
        assert seen == ["good"]

    @pytest.mark.asyncio
    async def test_process_loop_logs_unexpected_error(self, bus, monkeypatch):
        """If publish() itself raises (not a handler), the loop logs and
        continues rather than dying — covers the generic except branch."""
        calls = {"n": 0}
        orig_publish = bus.publish

        async def flaky_publish(event):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient processing error")
            return await orig_publish(event)

        monkeypatch.setattr(bus, "publish", flaky_publish)
        await bus.start()
        await bus.publish_async(Event(type="t"))
        await bus.publish_async(Event(type="t"))
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.02)
        await bus.stop()
        assert calls["n"] >= 2

    @pytest.mark.asyncio
    async def test_process_loop_continues_on_empty_queue_timeout(self, monkeypatch):
        """With an empty queue the get() times out and the loop continues.
        We shrink the wait_for timeout so the test is fast."""
        bus = EventBus()
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, timeout=0.05)

        monkeypatch.setattr(eb.asyncio, "wait_for", fast_wait_for)
        seen = []

        async def handler(e):
            seen.append(e.type)

        bus.subscribe("late", handler)
        await bus.start()
        # Let the loop spin through at least one empty-queue timeout (continue).
        await asyncio.sleep(0.15)
        await bus.publish_async(Event(type="late"))
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        await bus.stop()
        assert seen == ["late"]

    @pytest.mark.asyncio
    async def test_process_loop_breaks_on_cancel(self, bus):
        """Cancelling the processor task breaks the loop cleanly."""
        await bus.start()
        # Let the loop reach its wait_for, then cancel directly.
        await asyncio.sleep(0.01)
        bus._task.cancel()
        try:
            await bus._task
        except asyncio.CancelledError:
            pass
        bus._running = False


# ══════════════════════════════════════════════════════════════════════════════
# 7. WebhookReceiver
# ══════════════════════════════════════════════════════════════════════════════


class TestWebhookReceiver:
    def test_github_signature_skipped_without_secret(self, bus):
        rcv = WebhookReceiver(bus)
        assert rcv.verify_github_signature(b"payload", "anything") is True

    def test_github_signature_valid(self, bus):
        secret = "s3cret"
        rcv = WebhookReceiver(bus, github_secret=secret)
        payload = b'{"a":1}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert rcv.verify_github_signature(payload, sig) is True

    def test_github_signature_invalid(self, bus):
        rcv = WebhookReceiver(bus, github_secret="s3cret")
        assert rcv.verify_github_signature(b"payload", "sha256=bad") is False

    def test_bearer_skipped_without_token(self, bus):
        rcv = WebhookReceiver(bus)
        assert rcv.verify_bearer("anything") is True

    def test_bearer_valid(self, bus):
        rcv = WebhookReceiver(bus, bearer_token="tok")
        assert rcv.verify_bearer("Bearer tok") is True

    def test_bearer_invalid(self, bus):
        rcv = WebhookReceiver(bus, bearer_token="tok")
        assert rcv.verify_bearer("Bearer wrong") is False

    @pytest.mark.asyncio
    async def test_handle_github_publishes(self, bus):
        got = []

        async def handler(e):
            got.append(e)

        bus.subscribe(EventTypes.WEBHOOK_GITHUB, handler)
        rcv = WebhookReceiver(bus)
        event = await rcv.handle_github({"action": "push"}, event_name="push")
        assert event.source == "github"
        assert event.data["github_event"] == "push"
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_handle_generic_publishes(self, bus):
        got = []

        async def handler(e):
            got.append(e)

        bus.subscribe(EventTypes.WEBHOOK_GENERIC, handler)
        rcv = WebhookReceiver(bus)
        event = await rcv.handle_generic({"k": "v"}, source="zapier")
        assert event.source == "zapier"
        assert event.data == {"k": "v"}
        assert len(got) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 8. Singleton
# ══════════════════════════════════════════════════════════════════════════════


class TestSingleton:
    def test_get_event_bus_returns_same_instance(self):
        eb._event_bus = None
        a = get_event_bus()
        b = get_event_bus()
        assert a is b
        assert isinstance(a, EventBus)
