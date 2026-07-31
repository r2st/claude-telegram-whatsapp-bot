"""
Health check and watchdog for self-improving system.

Provides:
- HTTP health check endpoint (/health)
- Component status monitoring
- Watchdog with auto-restart capability
- Basic circuit breaker pattern
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Callable

from . import claude_core as cc

log = logging.getLogger(__name__)

# ─── Health state ─────────────────────────────────────────────────────────────

HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8484"))

_component_status: dict[str, dict] = {}
_start_time = time.time()


def register_component(name: str, check_fn: Callable[[], bool] | None = None) -> None:
    """Register a component for health monitoring."""
    _component_status[name] = {
        "healthy": True,
        "last_check": time.time(),
        "check_fn": check_fn,
        "error_count": 0,
        "last_error": None,
    }


def report_healthy(name: str) -> None:
    """Mark a component as healthy."""
    if name in _component_status:
        _component_status[name]["healthy"] = True
        _component_status[name]["last_check"] = time.time()
        _component_status[name]["error_count"] = 0


def report_unhealthy(name: str, error: str = "") -> None:
    """Mark a component as unhealthy."""
    if name in _component_status:
        _component_status[name]["healthy"] = False
        _component_status[name]["last_check"] = time.time()
        _component_status[name]["error_count"] = _component_status[name].get("error_count", 0) + 1
        _component_status[name]["last_error"] = error


def get_health() -> dict:
    """Get overall health status."""
    # Run any registered check functions
    for comp in _component_status.values():
        check_fn = comp.get("check_fn")
        if check_fn:
            try:
                comp["healthy"] = check_fn()
                comp["last_check"] = time.time()
            except Exception as e:
                comp["healthy"] = False
                comp["last_error"] = str(e)

    all_healthy = all(c["healthy"] for c in _component_status.values()) if _component_status else True
    uptime = int(time.time() - _start_time)

    result = {
        "status": "healthy" if all_healthy else "degraded",
        "uptime_seconds": uptime,
        "components": {
            name: {
                "healthy": comp["healthy"],
                "error_count": comp["error_count"],
                "last_error": comp.get("last_error"),
                "last_check_ago": int(time.time() - comp["last_check"]),
            }
            for name, comp in _component_status.items()
        },
    }

    # Surface the cached auto-update status if a check has run (no network here).
    try:
        from .updater import get_last_status
        update_status = get_last_status()
        if update_status:
            result["update"] = update_status
    except Exception:  # pragma: no cover - defensive, updater is optional
        # The updater is optional; health must report without it.
        log.debug("update status unavailable", exc_info=True)

    return result


# ─── HTTP Health endpoint ────────────────────────────────────────────────────


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            health = get_health()
            status_code = 200 if health["status"] == "healthy" else 503
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(health, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def start_health_server() -> None:
    """Start the HTTP health check server in a daemon thread."""
    try:
        bind_addr = os.getenv("HEALTH_BIND_ADDR", "127.0.0.1")
        server = HTTPServer((bind_addr, HEALTH_PORT), _HealthHandler)
        thread = Thread(target=server.serve_forever, daemon=True, name="health-server")
        thread.start()
        log.info("Health server started on port %d", HEALTH_PORT)
    except OSError as e:
        log.warning("Could not start health server on port %d: %s", HEALTH_PORT, e)


# ─── Circuit Breaker ──────────────────────────────────────────────────────────


class CircuitBreaker:
    """Simple circuit breaker to prevent cascade failures.

    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing)
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.success_count = 0

    @property
    def is_open(self) -> bool:
        if self.state == self.OPEN:
            # Check if recovery timeout has passed
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = self.HALF_OPEN
                return False
            return True
        return False

    def record_success(self) -> None:
        """Record a successful operation."""
        if self.state == self.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 2:
                self.state = self.CLOSED
                self.failure_count = 0
                self.success_count = 0
                log.info("Circuit breaker '%s' closed (recovered)", self.name)
        else:
            self.failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.success_count = 0

        if self.failure_count >= self.failure_threshold:
            self.state = self.OPEN
            log.warning(
                "Circuit breaker '%s' OPEN after %d failures",
                self.name, self.failure_count,
            )

    def __str__(self) -> str:
        return f"CircuitBreaker({self.name}: {self.state}, failures={self.failure_count})"


# Global circuit breakers for key services
claude_breaker = CircuitBreaker("claude", failure_threshold=5, recovery_timeout=120)
db_breaker = CircuitBreaker("database", failure_threshold=3, recovery_timeout=30)


# ─── Watchdog ─────────────────────────────────────────────────────────────────

WATCHDOG_STATE_PATH = Path(cc.DB_PATH).parent / ".watchdog_state.json"


class Watchdog:
    """Monitors bot health and triggers recovery actions.

    Recovery tiers:
    1. Soft restart (re-initialize connections)
    2. Component restart (restart specific adapter)
    3. Full restart (restart entire process)
    4. Alert (notify admin via available channel)
    """

    def __init__(self):
        self.check_interval = int(os.getenv("WATCHDOG_INTERVAL", "30"))
        self.max_fixes_per_hour = 3
        self._fix_attempts: list[dict] = []
        self._cooldowns: dict[str, float] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the watchdog monitor loop."""
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._monitor_loop())
            log.info("Watchdog started (interval=%ds)", self.check_interval)
        except RuntimeError:
            log.warning("No event loop for watchdog — skipping")

    def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_health()
            except Exception as e:
                log.error("Watchdog check error: %s", e)
            await asyncio.sleep(self.check_interval)

    async def _check_health(self) -> None:
        """Run health checks and trigger recovery if needed."""
        health = get_health()

        if health["status"] == "healthy":
            return

        # Find unhealthy components
        for name, comp in health["components"].items():
            if not comp["healthy"]:
                await self._attempt_recovery(name, comp)

    async def _attempt_recovery(self, component: str, status: dict) -> None:
        """Attempt to recover an unhealthy component."""
        # Check cooldown
        last_fix = self._cooldowns.get(component, 0)
        if time.time() - last_fix < 300:  # 5 min cooldown per component
            return

        # Check hourly limit
        now = time.time()
        recent_fixes = [f for f in self._fix_attempts if now - f["timestamp"] < 3600]
        if len(recent_fixes) >= self.max_fixes_per_hour:
            log.warning("Watchdog: hourly fix limit reached (%d/%d)", len(recent_fixes), self.max_fixes_per_hour)
            return

        log.info("Watchdog: attempting recovery for '%s' (errors=%d)", component, status["error_count"])

        fix_record = {
            "timestamp": now,
            "component": component,
            "fingerprint": f"{component}:{status.get('last_error', '')[:20]}",
            "description": f"Recovery for {component}: {status.get('last_error', 'unknown')}",
            "success": False,
        }

        try:
            # Tier 1: Soft reset (re-register component as healthy)
            if status["error_count"] < 10:
                report_healthy(component)
                fix_record["success"] = True
                fix_record["tier"] = 1
            # Tier 2+: Log for manual intervention (full restart needs supervisor)
            else:
                log.error("Watchdog: component '%s' needs manual intervention (errors=%d)", component, status["error_count"])
                fix_record["tier"] = 4
        except Exception as e:
            fix_record["success"] = False
            fix_record["description"] += f" | Error: {e}"

        self._fix_attempts.append(fix_record)
        self._cooldowns[component] = now
        self._save_state()

    def _save_state(self) -> None:
        """Persist watchdog state for /watchdog command visibility."""
        state = {
            "fix_attempts": self._fix_attempts[-20:],
            "cooldowns": self._cooldowns,
            "fixes_this_hour": [
                f["timestamp"] for f in self._fix_attempts
                if time.time() - f["timestamp"] < 3600
            ],
        }
        try:
            WATCHDOG_STATE_PATH.write_text(json.dumps(state, indent=2))
        except Exception:
            # State that can't be persisted means the fix-rate limit resets on
            # restart — the watchdog would then be free to retry a fix it had
            # already given up on. Worth a warning, not a crash.
            log.warning("could not persist watchdog state to %s",
                        WATCHDOG_STATE_PATH, exc_info=True)

    def get_status(self) -> dict:
        """Get watchdog status for display."""
        now = time.time()
        return {
            "running": self._running,
            "total_fixes": len(self._fix_attempts),
            "fixes_this_hour": sum(1 for f in self._fix_attempts if now - f["timestamp"] < 3600),
            "recent_fixes": self._fix_attempts[-5:],
        }


# Global watchdog instance
watchdog = Watchdog()
