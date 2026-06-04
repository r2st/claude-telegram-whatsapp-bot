"""
Behavior tests for health/watchdog (telechat_pkg.health).

Covers: component registration + status reporting, get_health aggregation
(incl. check_fn success/failure), the HTTP health handler (200/503/404), the
circuit breaker state machine, and the Watchdog recovery tiers + monitor loop.

The HTTP server bind itself is exercised by driving _HealthHandler directly
with a fake socket so no real port is opened.

Run:
    pytest tests/test_health.py -v
"""

import asyncio
import io
import json
import os
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import health


@pytest.fixture(autouse=True)
def _clean_components():
    health._component_status.clear()
    yield
    health._component_status.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Component registration + reporting
# ══════════════════════════════════════════════════════════════════════════════


class TestComponents:
    def test_register_component(self):
        health.register_component("db")
        assert "db" in health._component_status
        assert health._component_status["db"]["healthy"] is True

    def test_report_unhealthy(self):
        health.register_component("db")
        health.report_unhealthy("db", "connection lost")
        comp = health._component_status["db"]
        assert comp["healthy"] is False
        assert comp["error_count"] == 1
        assert comp["last_error"] == "connection lost"

    def test_report_healthy_resets(self):
        health.register_component("db")
        health.report_unhealthy("db", "err")
        health.report_healthy("db")
        comp = health._component_status["db"]
        assert comp["healthy"] is True
        assert comp["error_count"] == 0

    def test_report_unknown_component_noop(self):
        health.report_healthy("ghost")      # not registered
        health.report_unhealthy("ghost")    # not registered
        assert "ghost" not in health._component_status


# ══════════════════════════════════════════════════════════════════════════════
# 2. get_health
# ══════════════════════════════════════════════════════════════════════════════


class TestGetHealth:
    def test_healthy_when_empty(self):
        h = health.get_health()
        assert h["status"] == "healthy"
        assert h["components"] == {}
        assert h["uptime_seconds"] >= 0

    def test_degraded_when_component_unhealthy(self):
        health.register_component("db")
        health.report_unhealthy("db", "boom")
        h = health.get_health()
        assert h["status"] == "degraded"
        assert h["components"]["db"]["healthy"] is False

    def test_check_fn_success(self):
        health.register_component("api", check_fn=lambda: True)
        h = health.get_health()
        assert h["components"]["api"]["healthy"] is True

    def test_check_fn_returns_false(self):
        health.register_component("api", check_fn=lambda: False)
        h = health.get_health()
        assert h["status"] == "degraded"

    def test_check_fn_raises_marks_unhealthy(self):
        def boom():
            raise RuntimeError("check failed")

        health.register_component("api", check_fn=boom)
        h = health.get_health()
        assert h["components"]["api"]["healthy"] is False
        assert "check failed" in h["components"]["api"]["last_error"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. HTTP handler
# ══════════════════════════════════════════════════════════════════════════════


class _FakeHandler(health._HealthHandler):
    """Drive _HealthHandler without a real socket."""

    def __init__(self, path):
        self.path = path
        self.wfile = io.BytesIO()
        self._status = None
        self._headers = []

    def send_response(self, code):
        self._status = code

    def send_header(self, k, v):
        self._headers.append((k, v))

    def end_headers(self):
        pass


class TestHealthHandler:
    def test_health_path_200_when_healthy(self):
        h = _FakeHandler("/health")
        h.do_GET()
        assert h._status == 200
        body = json.loads(h.wfile.getvalue().decode())
        assert body["status"] == "healthy"

    def test_health_path_503_when_degraded(self):
        health.register_component("db")
        health.report_unhealthy("db", "boom")
        h = _FakeHandler("/health")
        h.do_GET()
        assert h._status == 503

    def test_unknown_path_404(self):
        h = _FakeHandler("/other")
        h.do_GET()
        assert h._status == 404

    def test_log_message_suppressed(self):
        h = _FakeHandler("/health")
        # Should not raise / write anything.
        h.log_message("%s", "ignored")


class TestStartServer:
    def test_start_server_bind_failure_handled(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("address in use")

        monkeypatch.setattr(health, "HTTPServer", boom)
        # Should swallow OSError and not raise.
        health.start_health_server()

    def test_start_server_success(self, monkeypatch):
        started = {"serve": False}

        class FakeServer:
            def __init__(self, addr, handler):
                self.addr = addr

            def serve_forever(self):
                started["serve"] = True

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target

            def start(self):
                pass  # don't actually run serve_forever

        monkeypatch.setattr(health, "HTTPServer", FakeServer)
        monkeypatch.setattr(health, "Thread", FakeThread)
        health.start_health_server()  # no exception


# ══════════════════════════════════════════════════════════════════════════════
# 4. Circuit breaker
# ══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = health.CircuitBreaker("svc")
        assert cb.state == cb.CLOSED
        assert cb.is_open is False

    def test_opens_after_threshold(self):
        cb = health.CircuitBreaker("svc", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == cb.OPEN
        assert cb.is_open is True

    def test_success_in_closed_resets_failures(self):
        cb = health.CircuitBreaker("svc", failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0

    def test_half_open_after_recovery_timeout(self):
        cb = health.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=0)
        cb.record_failure()  # opens
        time.sleep(0.001)
        # recovery_timeout=0 → next is_open check transitions to HALF_OPEN
        assert cb.is_open is False
        assert cb.state == cb.HALF_OPEN

    def test_half_open_two_successes_close(self):
        cb = health.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        _ = cb.is_open  # → HALF_OPEN
        cb.record_success()
        cb.record_success()
        assert cb.state == cb.CLOSED
        assert cb.failure_count == 0

    def test_str_representation(self):
        cb = health.CircuitBreaker("svc")
        assert "svc" in str(cb)
        assert "closed" in str(cb)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Watchdog
# ══════════════════════════════════════════════════════════════════════════════


class TestWatchdog:
    def test_start_no_event_loop(self):
        wd = health.Watchdog()
        wd.start()  # no running loop → logs + skips
        assert wd._running is True
        wd._running = False

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        wd = health.Watchdog()
        wd.start()
        first = wd._task
        wd.start()  # already running → no new task
        assert wd._task is first
        wd.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        wd = health.Watchdog()
        wd.start()
        assert wd._task is not None
        wd.stop()
        assert wd._running is False

    @pytest.mark.asyncio
    async def test_check_health_healthy_does_nothing(self):
        wd = health.Watchdog()
        await wd._check_health()  # no components → healthy → returns
        assert wd.get_status()["total_fixes"] == 0

    @pytest.mark.asyncio
    async def test_check_health_triggers_recovery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", tmp_path / "wd.json")
        health.register_component("db")
        health.report_unhealthy("db", "down")
        wd = health.Watchdog()
        await wd._check_health()
        status = wd.get_status()
        assert status["total_fixes"] == 1
        assert status["recent_fixes"][0]["tier"] == 1

    @pytest.mark.asyncio
    async def test_recovery_cooldown_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", tmp_path / "wd.json")
        wd = health.Watchdog()
        wd._cooldowns["db"] = time.time()  # recent fix
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "x"})
        assert wd.get_status()["total_fixes"] == 0

    @pytest.mark.asyncio
    async def test_recovery_hourly_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", tmp_path / "wd.json")
        wd = health.Watchdog()
        now = time.time()
        wd._fix_attempts = [{"timestamp": now} for _ in range(wd.max_fixes_per_hour)]
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "x"})
        # No new fix appended (limit reached)
        assert len(wd._fix_attempts) == wd.max_fixes_per_hour

    @pytest.mark.asyncio
    async def test_recovery_tier4_manual(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", tmp_path / "wd.json")
        wd = health.Watchdog()
        await wd._attempt_recovery("db", {"error_count": 50, "last_error": "fatal"})
        assert wd.get_status()["recent_fixes"][-1]["tier"] == 4

    @pytest.mark.asyncio
    async def test_recovery_inner_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", tmp_path / "wd.json")

        def boom(name):
            raise RuntimeError("report failed")

        monkeypatch.setattr(health, "report_healthy", boom)
        wd = health.Watchdog()
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "x"})
        rec = wd.get_status()["recent_fixes"][-1]
        assert rec["success"] is False
        assert "Error:" in rec["description"]

    @pytest.mark.asyncio
    async def test_monitor_loop_swallows_check_error(self, monkeypatch):
        wd = health.Watchdog()
        wd.check_interval = 0
        calls = {"n": 0}

        async def flaky_check():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("check crashed")
            wd._running = False  # exit after second iteration

        monkeypatch.setattr(wd, "_check_health", flaky_check)
        wd._running = True
        await wd._monitor_loop()
        assert calls["n"] >= 2

    def test_save_state_failure_swallowed(self, monkeypatch):
        wd = health.Watchdog()
        monkeypatch.setattr(
            type(health.WATCHDOG_STATE_PATH), "write_text",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError("ro fs")),
        )
        wd._save_state()  # must not raise

    def test_get_status_shape(self):
        wd = health.Watchdog()
        s = wd.get_status()
        assert set(s) == {"running", "total_fixes", "fixes_this_hour", "recent_fixes"}
