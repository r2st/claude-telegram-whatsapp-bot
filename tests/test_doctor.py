"""
Behavior-organized tests for telechat_pkg.doctor — the `telechat doctor`
diagnostic command.

doctor.py is what a user runs to find out why the bot isn't working. The thing
that matters is not only the pass/fail boolean of each check, but the
human-readable message and fix hint the user actually reads. So every check is
tested for:

  * the pass case,
  * the fail case, and
  * the wording / severity / fix_hint of the failure output.

External boundaries (sqlite, the network, PATH lookup, the filesystem) are
monkeypatched at the boundary call rather than mocking check internals, so the
tests read as "doctor reports X when sqlite is unwritable", not "doctor calls Y".

Run:
    pytest tests/test_doctor.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import doctor


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures (local to this module)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every env var the doctor reads so each test starts from a known,
    empty configuration and opts in to exactly what it needs."""
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "BOT_MODE",
        "RATE_LIMIT_REQUESTS",
        "RATE_LIMIT_WINDOW",
        "TELEGRAM_ALLOWED_USER_IDS",
        "WHATSAPP_ALLOWED_NUMBERS",
        "SLACK_ALLOWED_USER_IDS",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class FakeResponse:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    """Stand-in for aiohttp.ClientSession used as an async context manager."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.requested_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.requested_url = url
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def install_fake_aiohttp(monkeypatch, session):
    """Replace the aiohttp module imported inside check_telegram_connectivity."""
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = lambda *a, **k: session
    fake.ClientTimeout = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return fake


# ══════════════════════════════════════════════════════════════════════════════
# CheckResult / DoctorReport plumbing
# ══════════════════════════════════════════════════════════════════════════════


class TestDoctorReport:
    def test_add_passed_increments_passed(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", True, "ok"))
        assert report.passed == 1
        assert report.warnings == 0
        assert report.errors == 0

    def test_add_warning_increments_warnings(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", False, "meh", severity="warning"))
        assert report.warnings == 1
        assert report.errors == 0

    def test_add_error_increments_errors(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", False, "bad", severity="error"))
        assert report.errors == 1

    def test_add_failed_default_severity_counts_as_error(self):
        # severity defaults to "info" but a failed non-warning check is an error
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", False, "bad"))
        assert report.errors == 1
        assert report.warnings == 0

    def test_healthy_true_when_no_errors(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", True, "ok"))
        report.add(doctor.CheckResult("w", False, "warn", severity="warning"))
        assert report.healthy is True

    def test_healthy_false_when_errors(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("c", False, "bad", severity="error"))
        assert report.healthy is False


class TestReportFormat:
    def test_format_passing_check_shows_check_mark(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("Python version", True, "3.12.0"))
        out = report.format()
        assert "✅ Python version: 3.12.0" in out

    def test_format_warning_shows_warning_icon(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("Disk space", False, "low", severity="warning"))
        out = report.format()
        assert "⚠️ Disk space: low" in out

    def test_format_error_shows_cross_icon(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("Claude CLI", False, "missing", severity="error"))
        out = report.format()
        assert "❌ Claude CLI: missing" in out

    def test_format_includes_fix_hint_for_failed_check(self):
        report = doctor.DoctorReport()
        report.add(
            doctor.CheckResult("X", False, "broken", fix_hint="do the thing", severity="error")
        )
        out = report.format()
        assert "💡 do the thing" in out

    def test_format_omits_fix_hint_for_passed_check(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("X", True, "fine", fix_hint="never shown"))
        out = report.format()
        assert "never shown" not in out

    def test_format_omits_hint_when_failed_but_no_hint(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("X", False, "broken", severity="error"))
        out = report.format()
        assert "💡" not in out

    def test_format_summary_line_counts(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("a", True, "ok"))
        report.add(doctor.CheckResult("b", False, "warn", severity="warning"))
        report.add(doctor.CheckResult("c", False, "err", severity="error"))
        out = report.format()
        assert "Passed: 1  Warnings: 1  Errors: 1" in out

    def test_format_all_good_when_healthy(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("a", True, "ok"))
        out = report.format()
        assert "✅ All good!" in out

    def test_format_issues_found_when_unhealthy(self):
        report = doctor.DoctorReport()
        report.add(doctor.CheckResult("a", False, "err", severity="error"))
        out = report.format()
        assert "❌ Issues found — see hints above." in out

    def test_format_has_report_header(self):
        out = doctor.DoctorReport().format()
        assert out.startswith("🩺 Telechat Doctor Report")


# ══════════════════════════════════════════════════════════════════════════════
# Python version check
# ══════════════════════════════════════════════════════════════════════════════


from collections import namedtuple

# Mirrors the relevant surface of sys.version_info: tuple-comparable (for the
# `>= (3, 10)` test) with .major/.minor/.micro attribute access for the message.
_FakeVersion = namedtuple("_FakeVersion", ["major", "minor", "micro"])


class TestPythonVersionCheck:
    def test_pass_on_current_interpreter(self):
        # The test suite itself requires >= 3.10, so this is the real pass path.
        result = doctor.check_python_version()
        assert result.passed is True
        assert result.name == "Python version"
        assert f"{sys.version_info.major}.{sys.version_info.minor}" in result.message

    def test_fail_when_below_310(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "version_info", _FakeVersion(3, 9, 7))
        result = doctor.check_python_version()
        assert result.passed is False
        assert result.severity == "error"

    def test_fail_message_states_required_version(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "version_info", _FakeVersion(3, 9, 7))
        result = doctor.check_python_version()
        assert "need 3.10+" in result.message
        assert "3.9.7" in result.message
        assert "Upgrade Python" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Claude CLI check
# ══════════════════════════════════════════════════════════════════════════════


class TestClaudeInPathCheck:
    def test_pass_when_claude_on_path(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
        result = doctor.check_claude_cli()
        assert result.passed is True
        assert "/usr/local/bin/claude" in result.message

    def test_fail_when_claude_missing(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        result = doctor.check_claude_cli()
        assert result.passed is False
        assert result.severity == "error"

    def test_fail_message_gives_install_command(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        result = doctor.check_claude_cli()
        assert result.message == "Not found in PATH"
        assert "npm install -g @anthropic-ai/claude-code" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Environment file check
# ══════════════════════════════════════════════════════════════════════════════


class TestEnvFileCheck:
    def test_pass_when_env_file_present(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=x\n")
        # store.DB_PATH lives next to the .env candidate we created.
        from telechat_pkg import store
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
        result = doctor.check_env_file()
        assert result.passed is True
        assert str(env_file) in result.message

    def test_fail_when_no_env_file_anywhere(self, monkeypatch, tmp_path):
        from telechat_pkg import store
        # Point every candidate location at empty temp dirs.
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "nodb" / "bot.db"))
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        monkeypatch.chdir(tmp_path / "cwd" if (tmp_path / "cwd").mkdir() or True else tmp_path)
        result = doctor.check_env_file()
        assert result.passed is False
        assert result.severity == "error"

    def test_fail_message_points_to_init(self, monkeypatch, tmp_path):
        from telechat_pkg import store
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "nodb" / "bot.db"))
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        (tmp_path / "cwd").mkdir()
        monkeypatch.chdir(tmp_path / "cwd")
        result = doctor.check_env_file()
        assert result.message == "No .env file found"
        assert "telechat init" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Telegram bot token check
# ══════════════════════════════════════════════════════════════════════════════


class TestTelegramTokenCheck:
    def test_pass_with_valid_token(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABCdefGhIJKlmno")
        result = doctor.check_bot_token()
        assert result.passed is True
        # Token is partially redacted in the message.
        assert "123456789" not in result.message  # full token never shown
        assert "12345678" in result.message  # first 8 chars only

    def test_fail_on_placeholder_token(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "CHANGE_ME_ROTATE_TOKEN")
        result = doctor.check_bot_token()
        assert result.passed is False
        assert result.severity == "error"
        assert result.message == "Invalid or placeholder token"
        assert "@BotFather" in result.fix_hint

    def test_fail_on_token_without_colon(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "notarealtoken")
        result = doctor.check_bot_token()
        assert result.passed is False
        assert result.message == "Invalid or placeholder token"

    def test_fail_when_token_missing_and_telegram_enabled(self, clean_env):
        # No token, default BOT_MODE is "telegram" so the token is required.
        result = doctor.check_bot_token()
        assert result.passed is False
        assert result.severity == "error"
        assert result.message == "Not set"
        assert "TELEGRAM_BOT_TOKEN" in result.fix_hint

    def test_skip_when_telegram_not_enabled(self, clean_env):
        clean_env.setenv("BOT_MODE", "slack")
        result = doctor.check_bot_token()
        assert result.passed is True
        assert result.message == "Not needed (Telegram not enabled)"


# ══════════════════════════════════════════════════════════════════════════════
# Database / SQLite writable check
# ══════════════════════════════════════════════════════════════════════════════


class _FakeConn:
    """A minimal sqlite-like connection backed by a real in-memory DB so the
    doctor's queries execute against actual SQLite semantics."""

    def __init__(self, tables, conv_rows=0):
        self._conn = sqlite3.connect(":memory:")
        for t in tables:
            self._conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
        for _ in range(conv_rows):
            if "conversations" in tables:
                self._conn.execute("INSERT INTO conversations DEFAULT VALUES")
        self._conn.commit()

    def execute(self, *a, **k):
        return self._conn.execute(*a, **k)


class TestSQLiteWritableCheck:
    def test_pass_when_expected_tables_present(self, monkeypatch):
        from telechat_pkg import store
        fake = _FakeConn(["conversations", "usage", "memories"], conv_rows=3)
        monkeypatch.setattr(store, "_get_conn", lambda: fake)
        result = doctor.check_database()
        assert result.passed is True
        assert "3 tables" in result.message
        assert "3 messages" in result.message

    def test_warning_when_tables_missing(self, monkeypatch):
        from telechat_pkg import store
        fake = _FakeConn(["conversations"])  # missing "usage"
        monkeypatch.setattr(store, "_get_conn", lambda: fake)
        result = doctor.check_database()
        assert result.passed is False
        assert result.severity == "warning"
        assert "Missing tables" in result.message
        assert "usage" in result.message
        assert "initialized on first run" in result.fix_hint

    def test_error_when_connection_raises(self, monkeypatch):
        from telechat_pkg import store

        def boom():
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(store, "_get_conn", boom)
        result = doctor.check_database()
        assert result.passed is False
        assert result.severity == "error"
        assert "readonly database" in result.message
        assert "file permissions" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Disk space check
# ══════════════════════════════════════════════════════════════════════════════


class _Usage:
    def __init__(self, free_bytes):
        self.total = free_bytes
        self.used = 0
        self.free = free_bytes


class TestDiskSpaceCheck:
    def test_pass_with_ample_space(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: _Usage(50 * 1024 ** 3))
        result = doctor.check_disk_space()
        assert result.passed is True
        assert "50.0 GB free" in result.message

    def test_warning_when_low_space(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: _Usage(int(0.5 * 1024 ** 3)))
        result = doctor.check_disk_space()
        assert result.passed is False
        assert result.severity == "warning"
        assert "0.50 GB free" in result.message
        assert "Free up disk space" in result.fix_hint

    def test_skipped_when_disk_usage_raises(self, monkeypatch):
        def boom(p):
            raise OSError("no such file")

        monkeypatch.setattr(doctor.shutil, "disk_usage", boom)
        result = doctor.check_disk_space()
        # A check failure here must not be treated as a problem the user can fix.
        assert result.passed is True
        assert result.message == "Could not check (skipped)"


# ══════════════════════════════════════════════════════════════════════════════
# Dependencies check
# ══════════════════════════════════════════════════════════════════════════════


class TestDependenciesCheck:
    def test_pass_when_all_present(self, monkeypatch):
        # Force every probed module to import successfully (incl. optionals)
        # so the message is the clean "all installed" path.
        monkeypatch.setattr("builtins.__import__", lambda *a, **k: object())
        result = doctor.check_dependencies()
        assert result.passed is True
        assert result.message == "All required installed"

    def test_pass_with_optional_missing_is_reported(self, monkeypatch):
        real_import = __import__

        def fake_import(name, *a, **k):
            if name in ("fitz", "docx", "playwright"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", fake_import)
        result = doctor.check_dependencies()
        assert result.passed is True
        assert "optional missing" in result.message
        assert "PDF extraction" in result.message

    def test_fail_when_required_missing(self, monkeypatch):
        real_import = __import__

        def fake_import(name, *a, **k):
            if name in ("aiohttp", "dotenv"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", fake_import)
        result = doctor.check_dependencies()
        assert result.passed is False
        assert result.severity == "error"
        assert "Missing required" in result.message
        assert "aiohttp" in result.message
        assert result.fix_hint.startswith("pip install")


# ══════════════════════════════════════════════════════════════════════════════
# Rate limiting check
# ══════════════════════════════════════════════════════════════════════════════


class TestRateLimitsCheck:
    def test_pass_with_positive_limits(self, clean_env):
        clean_env.setenv("RATE_LIMIT_REQUESTS", "30")
        clean_env.setenv("RATE_LIMIT_WINDOW", "60")
        result = doctor.check_rate_limits()
        assert result.passed is True
        assert "30 requests per 60s" in result.message

    def test_pass_with_default_values(self, clean_env):
        # No env set: defaults (20 / 60) are still positive.
        result = doctor.check_rate_limits()
        assert result.passed is True
        assert "20 requests per 60s" in result.message

    def test_warning_when_disabled(self, clean_env):
        clean_env.setenv("RATE_LIMIT_REQUESTS", "0")
        clean_env.setenv("RATE_LIMIT_WINDOW", "60")
        result = doctor.check_rate_limits()
        assert result.passed is False
        assert result.severity == "warning"
        assert result.message == "Rate limiting disabled"
        assert "RATE_LIMIT_REQUESTS" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Access control / allowed users check
# ══════════════════════════════════════════════════════════════════════════════


class TestAllowedUsersCheck:
    def test_pass_with_telegram_allowlist(self, clean_env):
        clean_env.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2,3")
        result = doctor.check_allowed_users()
        assert result.passed is True
        assert "Telegram (3 users)" in result.message

    def test_pass_with_all_platforms(self, clean_env):
        clean_env.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
        clean_env.setenv("WHATSAPP_ALLOWED_NUMBERS", "+1,+2,+3")
        clean_env.setenv("SLACK_ALLOWED_USER_IDS", "U1")
        result = doctor.check_allowed_users()
        assert result.passed is True
        assert "Telegram (2 users)" in result.message
        assert "WhatsApp (3 numbers)" in result.message
        assert "Slack (1 users)" in result.message

    def test_warning_when_no_allowlist(self, clean_env):
        result = doctor.check_allowed_users()
        assert result.passed is False
        assert result.severity == "warning"
        assert "open to anyone" in result.message
        assert "TELEGRAM_ALLOWED_USER_IDS" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Telegram API connectivity check (async)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestTelegramConnectivityCheck:
    async def test_skip_when_no_token(self, clean_env):
        result = await doctor.check_telegram_connectivity()
        assert result.passed is True
        assert result.message == "Skipped (no token)"

    async def test_skip_when_token_has_no_colon(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "nocolon")
        result = await doctor.check_telegram_connectivity()
        assert result.passed is True
        assert result.message == "Skipped (no token)"

    async def test_pass_on_successful_getme(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        resp = FakeResponse(200, {"ok": True, "result": {"username": "mybot"}})
        session = FakeSession(response=resp)
        install_fake_aiohttp(clean_env, session)
        result = await doctor.check_telegram_connectivity()
        assert result.passed is True
        assert "Connected (@mybot)" in result.message
        # The token must be embedded in the getMe URL.
        assert "123:ABC" in session.requested_url

    async def test_pass_uses_placeholder_username_when_absent(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        resp = FakeResponse(200, {"ok": True, "result": {}})
        install_fake_aiohttp(clean_env, FakeSession(response=resp))
        result = await doctor.check_telegram_connectivity()
        assert result.passed is True
        assert "@???" in result.message

    async def test_fail_on_non_200_status(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        resp = FakeResponse(401, {"ok": False})
        install_fake_aiohttp(clean_env, FakeSession(response=resp))
        result = await doctor.check_telegram_connectivity()
        assert result.passed is False
        assert result.severity == "error"
        assert "HTTP 401" in result.message
        assert "invalid or revoked" in result.fix_hint

    async def test_fail_when_200_but_not_ok(self, clean_env):
        # Telegram returns 200 with {"ok": false} for some auth errors.
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        resp = FakeResponse(200, {"ok": False, "description": "Unauthorized"})
        install_fake_aiohttp(clean_env, FakeSession(response=resp))
        result = await doctor.check_telegram_connectivity()
        assert result.passed is False
        assert result.severity == "error"
        assert "HTTP 200" in result.message

    async def test_fail_on_network_exception(self, clean_env):
        clean_env.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        session = FakeSession(raise_exc=OSError("name resolution failed"))
        install_fake_aiohttp(clean_env, session)
        result = await doctor.check_telegram_connectivity()
        assert result.passed is False
        assert result.severity == "error"
        assert "Connection error" in result.message
        assert "internet connectivity" in result.fix_hint


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration: run_doctor_sync / run_doctor
# ══════════════════════════════════════════════════════════════════════════════


class TestRunDoctorSync:
    def test_runs_all_sync_checks(self, monkeypatch):
        report = doctor.run_doctor_sync()
        names = {c.name for c in report.checks}
        assert names == {
            "Python version",
            "Claude CLI",
            "Environment file",
            "Telegram bot token",
            "Database",
            "Disk space",
            "Dependencies",
            "Rate limiting",
            "Access control",
            "Unknown settings",
        }

    def test_counts_are_consistent_with_checks(self):
        report = doctor.run_doctor_sync()
        assert report.passed + report.warnings + report.errors == len(report.checks)


@pytest.mark.asyncio
class TestRunDoctor:
    async def test_includes_telegram_api_check(self, clean_env):
        # No token -> connectivity check is skipped but still recorded.
        report = await doctor.run_doctor()
        names = [c.name for c in report.checks]
        assert "Telegram API" in names
        # run_doctor adds exactly one more check than run_doctor_sync.
        assert names.count("Telegram API") == 1
        assert len(report.checks) == 11


# ══════════════════════════════════════════════════════════════════════════════
# check_unknown_env_keys
#
# `.env.example` documented SYSTEM_PROMPT for a long time while the code read
# CLAUDE_SYSTEM_PROMPT, so everyone who set a custom system prompt was ignored
# — silently, and looking like a broken feature rather than a wrong name. A
# typo fails identically. This is the check that catches both.
# ══════════════════════════════════════════════════════════════════════════════


class TestUnknownEnvKeys:
    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        """Point the doctor's .env lookup at a file we control."""
        path = tmp_path / ".env"

        def fake_path():
            return path if path.exists() else None

        monkeypatch.setattr(doctor, "_env_file_path", fake_path)
        return path

    def test_a_clean_env_passes(self, env_file):
        env_file.write_text("TELEGRAM_BOT_TOKEN=x\nCLAUDE_MODE=cli\n")
        result = doctor.check_unknown_env_keys()
        assert result.passed is True

    def test_a_typo_is_reported_by_name(self, env_file):
        env_file.write_text("CLAUDE_MOED=cli\n")
        result = doctor.check_unknown_env_keys()
        assert result.passed is False
        assert "CLAUDE_MOED" in result.message

    def test_the_original_bug_would_have_been_caught(self, env_file):
        # SYSTEM_PROMPT is now read as a legacy fallback, so use the shape of
        # the bug rather than the name: a plausible-looking key nothing reads.
        env_file.write_text("SYSTEM_PROMTP=be terse\n")
        result = doctor.check_unknown_env_keys()
        assert result.passed is False
        assert "docs/configuration.md" in result.fix_hint

    def test_it_warns_rather_than_erroring(self, env_file):
        # An operator may keep unrelated variables in the same file; this must
        # never be the thing that blocks a start.
        env_file.write_text("MY_OWN_THING=1\n")
        assert doctor.check_unknown_env_keys().severity == "warning"

    def test_watchdog_settings_are_accepted(self, env_file):
        # scripts/watchdog.py is a separate program shipped alongside the bot.
        env_file.write_text("WATCHDOG_ENABLED=true\nWATCHDOG_DRY_RUN=false\n")
        assert doctor.check_unknown_env_keys().passed is True

    def test_legacy_names_are_accepted(self, env_file):
        # These are read as fallbacks, so they must not be reported as typos.
        env_file.write_text("SYSTEM_PROMPT=x\nCLAUDE_CLI_ADD_DIRS=/tmp\n")
        assert doctor.check_unknown_env_keys().passed is True

    def test_comments_and_blank_lines_are_ignored(self, env_file):
        env_file.write_text("# NOT_A_SETTING=1\n\n   \nCLAUDE_MODE=cli\n")
        assert doctor.check_unknown_env_keys().passed is True

    def test_lowercase_keys_are_ignored(self, env_file):
        # Shell-style locals in a sourced file are not telechat settings.
        env_file.write_text("my_local=1\nCLAUDE_MODE=cli\n")
        assert doctor.check_unknown_env_keys().passed is True

    def test_many_unknowns_are_summarised_not_dumped(self, env_file):
        env_file.write_text("".join(f"NOPE_{i}=1\n" for i in range(20)))
        result = doctor.check_unknown_env_keys()
        assert "20 setting(s)" in result.message
        assert result.message.count(",") <= 5   # a sample, not all twenty

    def test_no_env_file_is_a_skip_not_a_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "_env_file_path", lambda: None)
        result = doctor.check_unknown_env_keys()
        assert result.passed is True
        assert "Skipped" in result.message

    def test_an_unreadable_env_file_is_a_skip(self, tmp_path, monkeypatch):
        missing = tmp_path / "gone.env"
        monkeypatch.setattr(doctor, "_env_file_path", lambda: missing)
        result = doctor.check_unknown_env_keys()
        assert result.passed is True
        assert "Skipped" in result.message


class TestEnvFilePath:
    def test_prefers_the_data_home_env(self, tmp_path, monkeypatch):
        from telechat_pkg import store
        home_env = tmp_path / ".env"
        home_env.write_text("X=1\n")
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
        assert doctor._env_file_path() == home_env

    def test_returns_none_when_there_is_no_env_anywhere(self, tmp_path, monkeypatch):
        from telechat_pkg import store
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        monkeypatch.chdir(tmp_path)
        assert doctor._env_file_path() is None
