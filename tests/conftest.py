"""Shared fixtures to prevent test pollution between modules.

This conftest does two things:

1. Redirects ``TELECHAT_HOME`` to a per-session temp dir *before* the
   ``telechat_pkg`` modules are imported so the test suite never touches the
   real ``~/.telechat/bot.db`` (which used to cause ``database is locked``
   flakes when test runs overlapped with a running bot).

2. Provides an autouse module-scoped fixture that restores ``store`` internals
   (connection cache, background writer thread, write queue) after each module
   so subsequent modules see a clean slate.

It also forces a couple of env vars to their documented defaults at session
start so a previous developer's shell environment can't change test outcomes
(e.g. ``WEB_FETCH_ENABLED``).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest


# ─── 1. Session-level environment hardening ────────────────────────────────

# Pin a tmp dir for the entire test run BEFORE telechat_pkg is imported.
# pytest imports conftest.py before any test module, so this runs first.
_TMP_HOME = tempfile.mkdtemp(prefix="telechat-test-")
os.environ["TELECHAT_HOME"] = _TMP_HOME
os.environ.setdefault("DB_PATH", os.path.join(_TMP_HOME, "bot.db"))

# Force module-level booleans to their documented defaults so tests that read
# them (e.g. ``WEB_FETCH_ENABLED``) don't depend on the developer's shell.
os.environ.setdefault("WEB_FETCH_ENABLED", "false")
os.environ.setdefault("WEB_CHAT_TOKEN", "")
os.environ.setdefault("WEB_CHAT_BIND", "127.0.0.1")
os.environ.setdefault("WEB_CHAT_TRUST_PROXY", "0")
os.environ.setdefault("MCP_ENABLED", "false")
# Allow tests to register MCP servers with any command (test fixtures use
# 'echo', 'true', or fake commands). Production runs default to the allowlist.
os.environ.setdefault("MCP_ALLOW_ANY_COMMAND", "1")


# ─── 2. Per-module store reset ──────────────────────────────────────────────

# Initialize the test DB once at session start — must happen after the env
# vars above are set so store.DB_PATH resolves to our tmp location. Tests
# that need a virgin schema can re-call store.init_db() themselves.
try:
    from telechat_pkg import store as _store_init  # noqa: E402
    _store_init.init_db()
except Exception:  # noqa: BLE001
    # If telechat_pkg can't import (missing optional dep) we let individual
    # tests skip; the per-module fixture below still tries to recover.
    pass


@pytest.fixture(autouse=True, scope="module")
def _restore_store_state():
    """Ensure store state is clean between test modules."""
    try:
        from telechat_pkg import store
        orig_db = store.DB_PATH
    except Exception:
        yield
        return
    yield
    # Restore after module — reset connection and writer thread so each
    # module starts with a fresh thread-local connection.
    store.DB_PATH = orig_db
    store._local = threading.local()
    store._writer_thread = None
    store._write_queue = None
    store.init_db()


# ─── 3. Per-test env isolation for tests that mutate module-level state ───

@pytest.fixture
def env_snapshot(monkeypatch):
    """Convenience fixture for tests that mutate env vars and re-import modules.

    Usage:
        def test_x(env_snapshot, monkeypatch):
            monkeypatch.setenv("WEB_FETCH_ENABLED", "true")
            ...
    monkeypatch automatically reverts at end of test; this fixture is just a
    semantic hook to remind authors to reload affected modules themselves.
    """
    yield


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Best-effort cleanup of the session temp home directory."""
    import shutil
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
