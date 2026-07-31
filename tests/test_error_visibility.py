"""Failures that are survivable must still be visible.

Across the package, dozens of handlers were `except Exception: pass`. Swallowing
is often the right call — a failed typing indicator must not take down a turn —
but silence is not: the operator of a self-hosted bot is the person debugging
it, and "nothing in the log" is the worst possible answer to "why did that stop
working?".

These tests pin the *pairing*: the operation stays survivable AND says
something. They deliberately assert on log level rather than exact wording, so
a message can be reworded without breaking them.

Run:
    pytest tests/test_error_visibility.py -v
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
from telechat_pkg import health, store, updater


class TestUpdaterVersionLookup:
    def test_falls_back_without_raising(self):
        # Both lookups can legitimately fail (odd install layouts); the caller
        # gets a string either way.
        assert isinstance(updater.current_version(), str)

    def test_a_failed_metadata_lookup_is_logged(self, monkeypatch, caplog):
        def no_metadata(_name):
            raise RuntimeError("no package metadata")

        monkeypatch.setattr("importlib.metadata.version", no_metadata)
        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.updater"):
            updater.current_version()
        assert any(r.levelno == logging.DEBUG for r in caplog.records)


class TestBridgeSchemaFailureIsAnnounced:
    def test_a_failed_bridge_schema_warns_rather_than_vanishing(self, monkeypatch, caplog):
        # Without this, every bridge feature fails later with "no such table"
        # and nothing points at the cause.
        from telechat_pkg import desktop_bridge

        def cannot_create(_conn):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(desktop_bridge, "init_bridge_schema", cannot_create)
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setattr(store, "DB_PATH", os.path.join(tmp, "t.db"))
            store._reset_conn_state()
            with caplog.at_level(logging.WARNING, logger="telechat_pkg.store"):
                store.init_db()   # must still succeed
            store._reset_conn_state()

        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a failed bridge schema init must not be silent"
        )

    def test_the_rest_of_the_schema_still_exists_afterwards(self, monkeypatch):
        from telechat_pkg import desktop_bridge

        monkeypatch.setattr(
            desktop_bridge, "init_bridge_schema",
            lambda _conn: (_ for _ in ()).throw(sqlite3.OperationalError("nope")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setattr(store, "DB_PATH", os.path.join(tmp, "t.db"))
            store._reset_conn_state()
            store.init_db()
            tables = {
                row[0] for row in store._get_conn().execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            store._reset_conn_state()
        assert "conversations" in tables


class TestWatchdogStatePersistence:
    def test_an_unwritable_state_path_warns_and_continues(self, monkeypatch, caplog, tmp_path):
        # State that can't be persisted silently resets the fix-rate limit on
        # restart, freeing the watchdog to retry a fix it had given up on.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setattr(health, "WATCHDOG_STATE_PATH", blocker / "state.json")
        with caplog.at_level(logging.WARNING, logger="telechat_pkg.health"):
            health.Watchdog()._save_state()
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


class TestNothingSwallowsSilentlyInTheSweptModules:
    """A regression guard for the sweep itself.

    `except Exception: pass` with no logging is the pattern this file exists to
    remove. Narrow excepts (OSError, ValueError, CancelledError) are exempt —
    those are deliberate control flow, not swallowed bugs.
    """

    @pytest.mark.parametrize("module", [
        "slack_bot.py",
        "updater.py",
        "health.py",
        "desktop_bridge.py",
        "browser_automation.py",
    ])
    def test_no_bare_except_exception_pass(self, module):
        import ast
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "telechat_pkg" / module
        tree = ast.parse(path.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
                continue
            caught = node.type
            broad = caught is None or (
                isinstance(caught, ast.Name) and caught.id in ("Exception", "BaseException")
            )
            if broad:
                offenders.append(node.lineno)
        assert not offenders, (
            f"{module} swallows every exception with no log line at "
            f"line(s) {offenders} — log it at debug level at least"
        )
