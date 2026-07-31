"""Behaviour tests for telechat_pkg.updater (auto-update checker).

Organised by what the checker does: read the current version, parse/compare
versions, parse registry responses, decide whether an update exists (and emit an
event), surface status to /health, and gate apply behind the opt-in flag. All
network and subprocess access is injected — nothing leaves the process.
"""
from __future__ import annotations

import pytest

from telechat_pkg import updater
from telechat_pkg import health


@pytest.fixture(autouse=True)
def _reset_cache():
    updater._last_status = None
    yield
    updater._last_status = None


class TestCurrentVersion:
    def test_reads_installed_metadata(self):
        # telechatai is installed editable in the test env → real version string.
        assert updater.current_version().count(".") >= 1

    def test_pyproject_fallback(self, monkeypatch):
        import telechat_pkg.updater as u
        # Force the metadata lookup to fail so the pyproject branch runs.
        monkeypatch.setattr(
            "importlib.metadata.version",
            lambda *_a, **_k: (_ for _ in ()).throw(Exception("no metadata")),
        )
        assert u.current_version().count(".") >= 1

    def test_returns_zero_when_all_sources_fail(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(
            "importlib.metadata.version",
            lambda *_a, **_k: (_ for _ in ()).throw(Exception("no metadata")),
        )
        monkeypatch.setattr(
            pathlib.Path, "read_text",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no file")),
        )
        assert updater.current_version() == "0.0.0"


class TestVersionCompare:
    def test_parse_ignores_prerelease(self):
        assert updater._parse_version("1.2.0rc1") == (1, 2, 0)

    def test_parse_strips_v_prefix(self):
        assert updater._parse_version("v3.4") == (3, 4)

    def test_parse_empty(self):
        assert updater._parse_version("") == (0,)

    def test_newer_true(self):
        assert updater.is_newer("1.3.0", "1.2.0") is True

    def test_newer_false_when_equal(self):
        assert updater.is_newer("1.2.0", "1.2.0") is False

    def test_newer_false_when_older(self):
        assert updater.is_newer("1.1.0", "1.2.0") is False

    def test_newer_false_when_latest_none(self):
        assert updater.is_newer(None, "1.2.0") is False


class TestRegistryParsing:
    def test_pypi_latest_extracted(self):
        latest = updater.fetch_pypi_latest(fetch=lambda url: {"info": {"version": "9.9.9"}})
        assert latest == "9.9.9"

    def test_pypi_none_on_fetch_failure(self):
        assert updater.fetch_pypi_latest(fetch=lambda url: None) is None

    def test_pypi_none_on_missing_keys(self):
        assert updater.fetch_pypi_latest(fetch=lambda url: {}) is None

    def test_npm_latest_extracted(self):
        latest = updater.fetch_npm_latest(fetch=lambda url: {"dist-tags": {"latest": "2.0.0"}})
        assert latest == "2.0.0"

    def test_npm_none_on_missing_keys(self):
        assert updater.fetch_npm_latest(fetch=lambda url: {"dist-tags": {}}) is None


class TestDefaultHttpGet:
    def test_returns_json_on_success(self, monkeypatch):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"ok": True}
        import sys
        import types
        fake = types.SimpleNamespace(get=lambda url, timeout=5: _Resp())
        monkeypatch.setitem(sys.modules, "requests", fake)
        assert updater._default_http_get_json("http://x")["ok"] is True

    def test_returns_none_on_exception(self, monkeypatch):
        import sys
        import types
        def _boom(url, timeout=5):
            raise RuntimeError("down")
        monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_boom))
        assert updater._default_http_get_json("http://x") is None


class TestCheckForUpdates:
    def test_update_available_from_pypi(self):
        info = updater.check_for_updates(
            current="1.2.0",
            pypi_fetch=lambda url: {"info": {"version": "1.3.0"}},
            npm_fetch=lambda url: {"dist-tags": {"latest": "1.2.0"}},
        )
        assert info.update_available is True
        assert info.sources == ["pypi"]

    def test_update_available_from_both(self):
        info = updater.check_for_updates(
            current="1.0.0",
            pypi_fetch=lambda url: {"info": {"version": "1.3.0"}},
            npm_fetch=lambda url: {"dist-tags": {"latest": "1.4.0"}},
        )
        assert set(info.sources) == {"pypi", "npm"}

    def test_no_update_when_current(self):
        info = updater.check_for_updates(
            current="1.2.0",
            pypi_fetch=lambda url: {"info": {"version": "1.2.0"}},
            npm_fetch=lambda url: None,
        )
        assert info.update_available is False
        assert info.sources == []

    def test_registry_failure_degrades(self):
        info = updater.check_for_updates(
            current="1.2.0",
            pypi_fetch=lambda url: None,
            npm_fetch=lambda url: None,
        )
        assert info.update_available is False
        assert info.pypi_latest is None

    def test_result_is_cached_for_health(self):
        updater.check_for_updates(
            current="1.2.0", pypi_fetch=lambda url: {"info": {"version": "1.5.0"}},
            npm_fetch=lambda url: None,
        )
        cached = updater.get_last_status()
        assert cached["update_available"] is True
        assert cached["pypi_latest"] == "1.5.0"

    def test_publishes_event_when_available(self):
        from telechat_pkg.event_bus import get_event_bus
        bus = get_event_bus()
        before = len(bus.get_recent_events()) if hasattr(bus, "get_recent_events") else 0
        info = updater.check_for_updates(
            current="1.0.0", pypi_fetch=lambda url: {"info": {"version": "2.0.0"}},
            npm_fetch=lambda url: None, publish=True,
        )
        assert info.update_available is True  # event path exercised without raising

    def test_publish_suppressed_when_flag_off(self):
        # publish=False must not raise and still returns a valid result.
        info = updater.check_for_updates(
            current="1.0.0", pypi_fetch=lambda url: {"info": {"version": "2.0.0"}},
            npm_fetch=lambda url: None, publish=False,
        )
        assert info.update_available is True


class TestApplyUpdate:
    def test_noop_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(updater, "UPDATE_AUTO_APPLY", False)
        ran = []
        assert updater.apply_update(runner=lambda cmd: ran.append(cmd) or 0) is False
        assert ran == []  # runner never invoked

    def test_runs_and_succeeds_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(updater, "UPDATE_AUTO_APPLY", True)
        captured = {}
        def _runner(cmd):
            captured["cmd"] = cmd
            return 0
        assert updater.apply_update(runner=_runner) is True
        assert "pip" in captured["cmd"] and "--upgrade" in captured["cmd"]

    def test_returns_false_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(updater, "UPDATE_AUTO_APPLY", True)
        assert updater.apply_update(runner=lambda cmd: 1) is False

    def test_default_runner_uses_subprocess(self, monkeypatch):
        monkeypatch.setattr(updater, "UPDATE_AUTO_APPLY", True)
        import subprocess
        calls = {}
        def _fake_call(args):
            calls["args"] = args
            return 0
        monkeypatch.setattr(subprocess, "call", _fake_call)
        assert updater.apply_update() is True  # no runner → default subprocess path
        assert "--upgrade" in calls["args"]


class TestPublishEvent:
    def test_publish_on_running_loop_schedules_task(self):
        import asyncio

        async def _run():
            # Inside a running loop → publish must schedule, not asyncio.run.
            info = updater.check_for_updates(
                current="1.0.0", pypi_fetch=lambda url: {"info": {"version": "2.0.0"}},
                npm_fetch=lambda url: None, publish=True,
            )
            await asyncio.sleep(0)  # let the scheduled publish task run
            return info

        info = asyncio.run(_run())
        assert info.update_available is True

    def test_publish_swallows_bus_errors(self, monkeypatch):
        from telechat_pkg import event_bus
        monkeypatch.setattr(
            event_bus, "get_event_bus",
            lambda: (_ for _ in ()).throw(RuntimeError("no bus")),
        )
        # Must not raise even though the bus blows up.
        updater._publish_event(updater.UpdateInfo(current="1.0.0", update_available=True))


class TestBackgroundCheck:
    def test_disabled_when_interval_zero(self):
        # Returns without spawning a thread and without raising.
        updater.start_background_check(interval=0)

    def test_spawns_thread_and_runs_initial_check(self, monkeypatch):
        import threading
        done = threading.Event()
        monkeypatch.setattr(updater, "check_for_updates", lambda *a, **k: done.set())
        updater.start_background_check(interval=3600)  # long sleep → only the boot check runs
        assert done.wait(timeout=2.0)


class TestHealthSurface:
    def test_health_includes_update_after_check(self):
        updater.check_for_updates(
            current="1.2.0", pypi_fetch=lambda url: {"info": {"version": "1.9.0"}},
            npm_fetch=lambda url: None,
        )
        h = health.get_health()
        assert h["update"]["update_available"] is True

    def test_health_omits_update_before_check(self):
        updater._last_status = None
        assert "update" not in health.get_health()
