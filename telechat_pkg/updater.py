"""
Auto-update checker.

Operators currently get no signal that a newer telechat is published. This
module compares the installed version against the latest on PyPI (``telechatai``)
and npm (``telechat``), logs the result, caches it for the ``/health`` endpoint,
and publishes an event on the event bus so other channels can surface it.

It never restarts the process. ``apply_update`` is opt-in (``UPDATE_AUTO_APPLY``)
and even then only shells out to ``pip install --upgrade`` — the operator (or a
watchdog policy) decides when to actually restart. Network access and the
upgrade subprocess are both injectable so the logic is fully testable offline.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

PYPI_PACKAGE = os.getenv("UPDATE_PYPI_PACKAGE", "telechatai")
NPM_PACKAGE = os.getenv("UPDATE_NPM_PACKAGE", "telechat")
# How often the boot/cron caller should re-check. Default 24h.
UPDATE_CHECK_INTERVAL = int(os.getenv("UPDATE_CHECK_INTERVAL", str(24 * 3600)))
# Opt-in: actually run the upgrade. Off by default — notify only.
UPDATE_AUTO_APPLY = os.getenv("UPDATE_AUTO_APPLY", "false").lower() in ("1", "true", "yes")

# Event type kept as a local constant so this module doesn't have to edit
# event_bus.EventTypes (publishing only needs the string).
UPDATE_AVAILABLE_EVENT = "system.update_available"

# Cache of the most recent check so /health can read it without doing network IO.
_last_status: Optional["UpdateInfo"] = None


@dataclass
class UpdateInfo:
    current: str
    pypi_latest: Optional[str] = None
    npm_latest: Optional[str] = None
    update_available: bool = False
    sources: list[str] = field(default_factory=list)
    checked_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "current": self.current,
            "pypi_latest": self.pypi_latest,
            "npm_latest": self.npm_latest,
            "update_available": self.update_available,
            "sources": self.sources,
            "checked_at": self.checked_at,
        }


def current_version() -> str:
    """Installed telechat version. Falls back to parsing pyproject if the
    package metadata isn't available (e.g. running from a source checkout)."""
    try:
        from importlib.metadata import version
        return version("telechatai")
    except Exception:
        pass
    try:
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple, ignoring pre-release suffixes.

    ``"1.2.0"`` → ``(1, 2, 0)``; ``"1.2.0rc1"`` → ``(1, 2, 0)``. Non-numeric or
    empty input yields ``(0,)`` so comparisons stay total.
    """
    if not v:
        return (0,)
    parts: list[int] = []
    for chunk in v.strip().lstrip("v").split("."):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts) or (0,)


def is_newer(latest: Optional[str], current: str) -> bool:
    """True if ``latest`` is a strictly higher version than ``current``."""
    if not latest:
        return False
    return _parse_version(latest) > _parse_version(current)


def _default_http_get_json(url: str) -> Optional[dict]:
    """Fetch JSON with a short timeout. Returns None on any failure so a
    registry hiccup never propagates into the caller."""
    try:
        import requests
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # network, timeout, JSON, HTTP error
        log.debug("update check fetch failed for %s: %s", url, exc)
        return None


def fetch_pypi_latest(
    package: str = PYPI_PACKAGE,
    *,
    fetch: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[str]:
    """Latest version string from the PyPI JSON API, or None."""
    data = (fetch or _default_http_get_json)(f"https://pypi.org/pypi/{package}/json")
    if not data:
        return None
    return (data.get("info") or {}).get("version")


def fetch_npm_latest(
    package: str = NPM_PACKAGE,
    *,
    fetch: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[str]:
    """Latest version string from the npm registry, or None."""
    data = (fetch or _default_http_get_json)(f"https://registry.npmjs.org/{package}")
    if not data:
        return None
    return (data.get("dist-tags") or {}).get("latest")


def check_for_updates(
    *,
    current: Optional[str] = None,
    pypi_fetch: Optional[Callable[[str], Optional[dict]]] = None,
    npm_fetch: Optional[Callable[[str], Optional[dict]]] = None,
    publish: bool = True,
    now: Optional[float] = None,
) -> UpdateInfo:
    """Compare the installed version against PyPI and npm.

    Caches the result for ``/health`` and, when an update is available and
    ``publish`` is set, emits an event on the bus. Registry failures degrade to
    "no update from that source" rather than raising.
    """
    global _last_status
    cur = current or current_version()
    now = time.time() if now is None else now
    pypi_latest = fetch_pypi_latest(fetch=pypi_fetch)
    npm_latest = fetch_npm_latest(fetch=npm_fetch)

    sources: list[str] = []
    if is_newer(pypi_latest, cur):
        sources.append("pypi")
    if is_newer(npm_latest, cur):
        sources.append("npm")

    info = UpdateInfo(
        current=cur,
        pypi_latest=pypi_latest,
        npm_latest=npm_latest,
        update_available=bool(sources),
        sources=sources,
        checked_at=now,
    )
    _last_status = info

    if info.update_available:
        log.info(
            "telechat update available: current=%s pypi=%s npm=%s",
            cur, pypi_latest, npm_latest,
        )
        if publish:
            _publish_event(info)
    else:
        log.debug("telechat is up to date (%s)", cur)
    return info


def _publish_event(info: UpdateInfo) -> None:
    """Emit an update-available event; swallow bus errors (notification is
    best-effort and must not crash a boot/cron check).

    ``EventBus.publish`` is a coroutine but the checker runs synchronously, so
    we drive it on the running loop when there is one (scheduled, fire-and-
    forget) and otherwise spin a throwaway loop to deliver it inline.
    """
    try:
        import asyncio
        from .event_bus import get_event_bus, Event
        coro = get_event_bus().publish(Event(type=UPDATE_AVAILABLE_EVENT, data=info.as_dict()))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(coro)
        else:
            asyncio.run(coro)
    except Exception as exc:  # defensive — notification must not crash the check
        log.debug("failed to publish update event: %s", exc)


def get_last_status() -> Optional[dict]:
    """Cached result of the last check for /health, or None if never run."""
    return _last_status.as_dict() if _last_status else None


def apply_update(*, runner: Optional[Callable[[list[str]], int]] = None) -> bool:
    """Upgrade the package in place — only when ``UPDATE_AUTO_APPLY`` is set.

    Returns True if the upgrade command ran and reported success. With the flag
    off (the default) it logs and returns False without running anything, so an
    accidental call can't mutate a production install.
    """
    if not UPDATE_AUTO_APPLY:
        log.info("apply_update called but UPDATE_AUTO_APPLY is off — skipping")
        return False
    import sys
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PYPI_PACKAGE]

    def _default_runner(args: list[str]) -> int:
        import subprocess
        return subprocess.call(args)

    rc = (runner or _default_runner)(cmd)
    if rc == 0:
        log.info("telechat upgraded via pip — restart to apply")
        return True
    log.warning("telechat upgrade failed (pip exit %s)", rc)
    return False


def start_background_check(*, interval: Optional[int] = None) -> None:
    """Spawn a daemon thread that checks for updates on boot and every
    ``interval`` seconds thereafter. Best-effort; never blocks startup.

    Disabled by setting ``UPDATE_CHECK_INTERVAL=0``.
    """
    every = UPDATE_CHECK_INTERVAL if interval is None else interval
    if every <= 0:
        log.debug("auto-update checks disabled (UPDATE_CHECK_INTERVAL=0)")
        return

    def _loop() -> None:
        import time as _time
        while True:
            try:
                check_for_updates()
            except Exception as exc:  # pragma: no cover - belt-and-suspenders
                log.debug("background update check failed: %s", exc)
            _time.sleep(every)

    import threading
    threading.Thread(target=_loop, daemon=True, name="update-checker").start()
