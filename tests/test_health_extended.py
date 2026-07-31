"""
Comprehensive tests for health.py — component status, CircuitBreaker,
_HealthHandler HTTP, start_health_server, and Watchdog.

Run:
    pytest tests/test_health_extended.py -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Environment setup (must happen before any package import) ──────────────────
_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_health.db")

import telechat_pkg.health as health_module
from telechat_pkg.health import (
    CircuitBreaker,
    Watchdog,
    _HealthHandler,
    _component_status,
    get_health,
    register_component,
    report_healthy,
    report_unhealthy,
    start_health_server,
)

# ── Shared fixture: clear global state between every test ──────────────────────


@pytest.fixture(autouse=True)
def clean_status():
    _component_status.clear()
    yield
    _component_status.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Component Status
# ══════════════════════════════════════════════════════════════════════════════


class TestRegisterComponent:
    def test_adds_entry_to_component_status(self):
        register_component("db")
        assert "db" in _component_status

    def test_default_healthy_true(self):
        register_component("db")
        assert _component_status["db"]["healthy"] is True

    def test_default_error_count_zero(self):
        register_component("db")
        assert _component_status["db"]["error_count"] == 0

    def test_default_check_fn_is_none(self):
        register_component("db")
        assert _component_status["db"]["check_fn"] is None

    def test_default_last_error_is_none(self):
        register_component("db")
        assert _component_status["db"]["last_error"] is None

    def test_stores_check_fn(self):
        def fn():
            return True
        register_component("api", check_fn=fn)
        assert _component_status["api"]["check_fn"] is fn

    def test_multiple_components(self):
        register_component("db")
        register_component("api")
        register_component("cache")
        assert set(_component_status.keys()) == {"db", "api", "cache"}

    def test_overwrites_existing_component(self):
        register_component("db")
        report_unhealthy("db", "some error")
        register_component("db")  # re-register resets to defaults
        assert _component_status["db"]["healthy"] is True
        assert _component_status["db"]["error_count"] == 0


class TestReportHealthy:
    def test_sets_healthy_true(self):
        register_component("db")
        report_unhealthy("db", "oops")
        report_healthy("db")
        assert _component_status["db"]["healthy"] is True

    def test_resets_error_count(self):
        register_component("db")
        _component_status["db"]["error_count"] = 5
        report_healthy("db")
        assert _component_status["db"]["error_count"] == 0

    def test_updates_last_check(self):
        register_component("db")
        before = time.time()
        report_healthy("db")
        assert _component_status["db"]["last_check"] >= before

    def test_unknown_component_is_noop(self):
        # Should not raise
        report_healthy("nonexistent")
        assert "nonexistent" not in _component_status


class TestReportUnhealthy:
    def test_sets_healthy_false(self):
        register_component("db")
        report_unhealthy("db", "connection refused")
        assert _component_status["db"]["healthy"] is False

    def test_increments_error_count(self):
        register_component("db")
        report_unhealthy("db", "err1")
        report_unhealthy("db", "err2")
        assert _component_status["db"]["error_count"] == 2

    def test_records_error_message(self):
        register_component("db")
        report_unhealthy("db", "timeout")
        assert _component_status["db"]["last_error"] == "timeout"

    def test_updates_last_check(self):
        register_component("db")
        before = time.time()
        report_unhealthy("db")
        assert _component_status["db"]["last_check"] >= before

    def test_unknown_component_is_noop(self):
        report_unhealthy("ghost")
        assert "ghost" not in _component_status

    def test_empty_error_message_is_stored(self):
        register_component("db")
        report_unhealthy("db")  # default empty string
        assert _component_status["db"]["last_error"] == ""


class TestGetHealth:
    def test_empty_components_returns_healthy(self):
        result = get_health()
        assert result["status"] == "healthy"

    def test_all_healthy_returns_healthy(self):
        register_component("db")
        register_component("api")
        result = get_health()
        assert result["status"] == "healthy"

    def test_any_unhealthy_returns_degraded(self):
        register_component("db")
        register_component("api")
        report_unhealthy("db", "down")
        result = get_health()
        assert result["status"] == "degraded"

    def test_includes_uptime_seconds(self):
        result = get_health()
        assert "uptime_seconds" in result
        assert result["uptime_seconds"] >= 0

    def test_includes_components_dict(self):
        register_component("db")
        result = get_health()
        assert "components" in result
        assert "db" in result["components"]

    def test_component_dict_has_expected_keys(self):
        register_component("db")
        result = get_health()
        comp = result["components"]["db"]
        assert "healthy" in comp
        assert "error_count" in comp
        assert "last_error" in comp
        assert "last_check_ago" in comp

    def test_runs_check_fn_for_each_component(self):
        called = []
        def check_fn():
            called.append(True)
            return True
        register_component("api", check_fn=check_fn)
        get_health()
        assert len(called) == 1

    def test_check_fn_returning_false_marks_degraded(self):
        register_component("api", check_fn=lambda: False)
        result = get_health()
        assert result["status"] == "degraded"

    def test_check_fn_returning_true_stays_healthy(self):
        register_component("api", check_fn=lambda: True)
        result = get_health()
        assert result["status"] == "healthy"

    def test_check_fn_raising_exception_marks_unhealthy(self):
        def bad_check():
            raise RuntimeError("check failed")
        register_component("api", check_fn=bad_check)
        result = get_health()
        assert result["status"] == "degraded"
        assert result["components"]["api"]["healthy"] is False

    def test_check_fn_exception_stores_error(self):
        def bad_check():
            raise ValueError("disk full")
        register_component("api", check_fn=bad_check)
        get_health()
        assert "disk full" in _component_status["api"].get("last_error", "")


# ══════════════════════════════════════════════════════════════════════════════
# 2. CircuitBreaker
# ══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerInit:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitBreaker.CLOSED

    def test_is_open_false_initially(self):
        cb = CircuitBreaker("test")
        assert cb.is_open is False

    def test_default_failure_threshold(self):
        cb = CircuitBreaker("test")
        assert cb.failure_threshold == 5

    def test_default_recovery_timeout(self):
        cb = CircuitBreaker("test")
        assert cb.recovery_timeout == 60

    def test_custom_failure_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.failure_threshold == 3

    def test_custom_recovery_timeout(self):
        cb = CircuitBreaker("test", recovery_timeout=120)
        assert cb.recovery_timeout == 120

    def test_initial_failure_count_zero(self):
        cb = CircuitBreaker("test")
        assert cb.failure_count == 0

    def test_initial_success_count_zero(self):
        cb = CircuitBreaker("test")
        assert cb.success_count == 0


class TestCircuitBreakerClosed:
    def test_record_success_in_closed_resets_failure_count(self):
        cb = CircuitBreaker("test")
        cb.failure_count = 3
        cb.record_success()
        assert cb.failure_count == 0

    def test_record_failure_increments_count(self):
        cb = CircuitBreaker("test")
        cb.record_failure()
        assert cb.failure_count == 1

    def test_multiple_failures_increment_count(self):
        cb = CircuitBreaker("test")
        for _ in range(3):
            cb.record_failure()
        assert cb.failure_count == 3

    def test_transitions_closed_to_open_at_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_is_open_true_when_open(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True

    def test_does_not_open_before_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_open is False


class TestCircuitBreakerOpenToHalfOpen:
    def test_transitions_open_to_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=1)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        with patch("time.time", return_value=time.time() + 2):
            result = cb.is_open
        assert result is False
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_is_open_stays_true_before_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60)
        cb.record_failure()
        # time hasn't advanced, so still OPEN
        assert cb.is_open is True


class TestCircuitBreakerHalfOpen:
    def _put_in_half_open(self, cb: CircuitBreaker) -> None:
        """Helper: drive the breaker into HALF_OPEN state."""
        cb.record_failure()  # opens it (threshold=1)
        cb.last_failure_time = time.time() - (cb.recovery_timeout + 1)
        # Trigger the transition by checking is_open
        _ = cb.is_open
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_record_success_increments_success_count(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        self._put_in_half_open(cb)
        cb.record_success()
        assert cb.success_count == 1

    def test_two_successes_closes_breaker(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        self._put_in_half_open(cb)
        cb.record_success()
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED

    def test_closed_after_recovery_resets_counts(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        self._put_in_half_open(cb)
        cb.record_success()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.success_count == 0

    def test_record_failure_in_half_open_resets_success_count(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        self._put_in_half_open(cb)
        cb.record_success()
        assert cb.success_count == 1
        cb.record_failure()
        assert cb.success_count == 0


class TestCircuitBreakerStr:
    def test_str_representation(self):
        cb = CircuitBreaker("myservice")
        s = str(cb)
        assert "myservice" in s
        assert "closed" in s
        assert "failures" in s

    def test_str_shows_failure_count(self):
        cb = CircuitBreaker("svc", failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        s = str(cb)
        assert "2" in s


# ══════════════════════════════════════════════════════════════════════════════
# 3. _HealthHandler HTTP
# ══════════════════════════════════════════════════════════════════════════════


def _make_handler(path: str) -> _HealthHandler:
    """Create a _HealthHandler instance with mocked socket infrastructure."""
    request = MagicMock()
    request.makefile.return_value = io.BytesIO()

    buf = io.BytesIO()

    handler = _HealthHandler.__new__(_HealthHandler)
    handler.path = path
    handler.wfile = buf
    handler.request = request
    handler.client_address = ("127.0.0.1", 0)
    handler.server = MagicMock()

    # Capture send_response / send_header / end_headers calls
    handler._response_code = None
    handler._headers = {}

    def fake_send_response(code):
        handler._response_code = code

    def fake_send_header(key, val):
        handler._headers[key] = val

    def fake_end_headers():
        pass

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers

    return handler


class TestHealthHandler:
    def test_health_path_returns_200_when_healthy(self):
        register_component("db")
        handler = _make_handler("/health")
        handler.do_GET()
        assert handler._response_code == 200

    def test_health_path_returns_503_when_degraded(self):
        register_component("db")
        report_unhealthy("db", "down")
        handler = _make_handler("/health")
        handler.do_GET()
        assert handler._response_code == 503

    def test_health_response_is_valid_json(self):
        register_component("db")
        handler = _make_handler("/health")
        handler.do_GET()
        raw = handler.wfile.getvalue()
        parsed = json.loads(raw.decode())
        assert "status" in parsed

    def test_health_response_content_type_header(self):
        register_component("db")
        handler = _make_handler("/health")
        handler.do_GET()
        assert handler._headers.get("Content-Type") == "application/json"

    def test_unknown_path_returns_404(self):
        handler = _make_handler("/unknown")
        handler.do_GET()
        assert handler._response_code == 404

    def test_log_message_is_suppressed(self):
        """log_message should be a no-op — does not raise."""
        handler = _make_handler("/health")
        handler.log_message("%s", "some access log")  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 4. start_health_server
# ══════════════════════════════════════════════════════════════════════════════


class TestStartHealthServer:
    def test_starts_daemon_thread(self):
        with patch("telechat_pkg.health.HTTPServer") as mock_server_cls, \
             patch("telechat_pkg.health.Thread") as mock_thread_cls:
            mock_srv = MagicMock()
            mock_server_cls.return_value = mock_srv
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            start_health_server()
            mock_thread_cls.assert_called_once_with(
                target=mock_srv.serve_forever, daemon=True, name="health-server"
            )
            mock_thread.start.assert_called_once()

    def test_handles_oserror_port_in_use(self):
        with patch("telechat_pkg.health.HTTPServer", side_effect=OSError("Address in use")):
            # Must not raise — should log a warning instead
            start_health_server()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Watchdog
# ══════════════════════════════════════════════════════════════════════════════


class TestWatchdogInit:
    def test_default_check_interval(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WATCHDOG_INTERVAL", None)
            wd = Watchdog()
            assert wd.check_interval == 30

    def test_custom_check_interval_from_env(self):
        with patch.dict(os.environ, {"WATCHDOG_INTERVAL": "60"}):
            wd = Watchdog()
            assert wd.check_interval == 60

    def test_initial_running_false(self):
        wd = Watchdog()
        assert wd._running is False

    def test_initial_task_none(self):
        wd = Watchdog()
        assert wd._task is None

    def test_initial_fix_attempts_empty(self):
        wd = Watchdog()
        assert wd._fix_attempts == []

    def test_max_fixes_per_hour_default(self):
        wd = Watchdog()
        assert wd.max_fixes_per_hour == 3


class TestWatchdogStartStop:
    def test_start_sets_running_true(self):
        wd = Watchdog()
        loop = asyncio.new_event_loop()
        try:
            async def _inner():
                wd.start()
                return wd._running
            result = loop.run_until_complete(_inner())
            assert result is True
        finally:
            if wd._task and not wd._task.done():
                wd._task.cancel()
            loop.close()

    def test_start_creates_task(self):
        wd = Watchdog()
        loop = asyncio.new_event_loop()
        try:
            async def _inner():
                wd.start()
                return wd._task
            task = loop.run_until_complete(_inner())
            assert task is not None
        finally:
            if wd._task and not wd._task.done():
                wd._task.cancel()
            loop.close()

    def test_start_when_already_running_is_noop(self):
        wd = Watchdog()
        loop = asyncio.new_event_loop()
        try:
            async def _inner():
                wd.start()
                first_task = wd._task
                wd.start()  # second call
                return first_task, wd._task
            first, second = loop.run_until_complete(_inner())
            assert first is second  # task unchanged
        finally:
            if wd._task and not wd._task.done():
                wd._task.cancel()
            loop.close()

    def test_start_without_event_loop_logs_warning(self):
        wd = Watchdog()
        with patch.object(health_module.log, "warning") as mock_warn:
            # Call outside any running loop — RuntimeError expected internally
            wd.start()
            mock_warn.assert_called()
        assert wd._running is True  # _running is still set

    def test_stop_sets_running_false(self):
        wd = Watchdog()
        loop = asyncio.new_event_loop()
        try:
            async def _inner():
                wd.start()
                wd.stop()
                return wd._running
            result = loop.run_until_complete(_inner())
            assert result is False
        finally:
            loop.close()

    def test_stop_cancels_task(self):
        wd = Watchdog()
        loop = asyncio.new_event_loop()
        try:
            async def _inner():
                wd.start()
                task = wd._task
                wd.stop()
                await asyncio.sleep(0)  # let cancellation propagate
                return task.cancelled()
            cancelled = loop.run_until_complete(_inner())
            assert cancelled is True
        finally:
            loop.close()

    def test_stop_when_not_running_is_noop(self):
        wd = Watchdog()
        # Must not raise
        wd.stop()
        assert wd._running is False


class TestWatchdogCheckHealth:
    @pytest.mark.asyncio
    async def test_check_health_healthy_returns_early(self):
        register_component("db")
        wd = Watchdog()
        wd._attempt_recovery = MagicMock()
        await wd._check_health()
        wd._attempt_recovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_health_unhealthy_calls_attempt_recovery(self):
        register_component("db")
        report_unhealthy("db", "error")
        wd = Watchdog()
        wd._attempt_recovery = AsyncMock()
        await wd._check_health()
        wd._attempt_recovery.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitor_loop_calls_check_health(self):
        wd = Watchdog()
        wd.check_interval = 0
        call_count = []

        async def fake_check():
            call_count.append(1)
            if len(call_count) >= 2:
                wd._running = False

        wd._check_health = fake_check
        wd._running = True
        await wd._monitor_loop()
        assert len(call_count) >= 2


class TestWatchdogAttemptRecovery:
    @pytest.mark.asyncio
    async def test_respects_cooldown(self):
        register_component("db")
        wd = Watchdog()
        wd._cooldowns["db"] = time.time()  # just now → within 5 min cooldown
        report_unhealthy("db", "err")
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "err"})
        assert len(wd._fix_attempts) == 0

    @pytest.mark.asyncio
    async def test_respects_hourly_limit(self):
        register_component("db")
        wd = Watchdog()
        now = time.time()
        # Inject max_fixes_per_hour recent fix attempts
        wd._fix_attempts = [{"timestamp": now - 10, "component": "db"} for _ in range(3)]
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "err"})
        # No new attempt added
        assert len(wd._fix_attempts) == 3

    @pytest.mark.asyncio
    async def test_tier1_soft_reset_for_low_error_count(self):
        register_component("db")
        report_unhealthy("db", "minor")
        wd = Watchdog()
        await wd._attempt_recovery("db", {"error_count": 5, "last_error": "minor"})
        assert len(wd._fix_attempts) == 1
        assert wd._fix_attempts[0]["tier"] == 1
        assert wd._fix_attempts[0]["success"] is True
        # Component should be re-marked healthy
        assert _component_status["db"]["healthy"] is True

    @pytest.mark.asyncio
    async def test_tier4_for_high_error_count(self):
        register_component("db")
        report_unhealthy("db", "critical")
        wd = Watchdog()
        await wd._attempt_recovery("db", {"error_count": 15, "last_error": "critical"})
        assert len(wd._fix_attempts) == 1
        assert wd._fix_attempts[0]["tier"] == 4

    @pytest.mark.asyncio
    async def test_records_fix_attempt_fields(self):
        register_component("db")
        report_unhealthy("db", "some error")
        wd = Watchdog()
        await wd._attempt_recovery("db", {"error_count": 3, "last_error": "some error"})
        rec = wd._fix_attempts[0]
        assert "timestamp" in rec
        assert rec["component"] == "db"
        assert "fingerprint" in rec
        assert "description" in rec

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_attempt(self):
        register_component("db")
        report_unhealthy("db", "err")
        wd = Watchdog()
        before = time.time()
        await wd._attempt_recovery("db", {"error_count": 1, "last_error": "err"})
        assert wd._cooldowns.get("db", 0) >= before


class TestWatchdogSaveState:
    def test_save_state_writes_json(self, tmp_path):
        wd = Watchdog()
        wd._fix_attempts = [{"timestamp": time.time(), "component": "db"}]
        wd._cooldowns = {"db": time.time()}

        state_path = tmp_path / ".watchdog_state.json"
        with patch.object(health_module, "WATCHDOG_STATE_PATH", state_path):
            wd._save_state()

        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert "fix_attempts" in data
        assert "cooldowns" in data
        assert "fixes_this_hour" in data

    def test_save_state_handles_write_error_silently(self):
        wd = Watchdog()
        with patch.object(health_module, "WATCHDOG_STATE_PATH", MagicMock(**{"write_text.side_effect": OSError("no space")})):
            # Must not raise
            wd._save_state()


class TestWatchdogGetStatus:
    def test_get_status_returns_running_state(self):
        wd = Watchdog()
        status = wd.get_status()
        assert status["running"] is False

    def test_get_status_returns_total_fixes(self):
        wd = Watchdog()
        wd._fix_attempts = [{"timestamp": time.time(), "component": "db"} for _ in range(3)]
        status = wd.get_status()
        assert status["total_fixes"] == 3

    def test_get_status_counts_fixes_this_hour(self):
        wd = Watchdog()
        now = time.time()
        wd._fix_attempts = [
            {"timestamp": now - 100, "component": "db"},    # recent
            {"timestamp": now - 4000, "component": "api"},  # older than 1h
        ]
        status = wd.get_status()
        assert status["fixes_this_hour"] == 1

    def test_get_status_includes_recent_fixes(self):
        wd = Watchdog()
        wd._fix_attempts = [{"timestamp": time.time(), "component": f"c{i}"} for i in range(7)]
        status = wd.get_status()
        assert len(status["recent_fixes"]) == 5  # last 5
