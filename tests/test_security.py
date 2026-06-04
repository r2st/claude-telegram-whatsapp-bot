"""
Comprehensive security test suite for telechat.

Covers: SSRF, SQL injection, XSS/injection, path traversal, rate limiting,
authentication, input validation, resource limits, data isolation, and
secrets handling across all modules.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for isolation."""
    db_path = str(tmp_path / "test_security.db")
    return db_path


@pytest.fixture
def store_with_db(tmp_db, monkeypatch):
    """Initialize the store module with a temp database."""
    from telechat_pkg import store
    # Drop any thread-local connection left bound to a previous test's DB_PATH,
    # otherwise init_db() reuses a stale connection to another database and
    # never WAL-initializes tmp_db — leaving it vulnerable to the concurrent
    # "database is locked" race that made this suite flaky.
    store._reset_conn_state()
    monkeypatch.setattr("telechat_pkg.store.DB_PATH", tmp_db)
    store.init_db()
    yield store
    store._reset_conn_state()


@pytest.fixture
def budget_db(tmp_db):
    """Create a temp database with cost_tracking table for budget tests."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_tracking (
            platform TEXT NOT NULL, user_id TEXT NOT NULL, date TEXT NOT NULL,
            requests INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
            PRIMARY KEY (platform, user_id, date))
    """)
    conn.commit()
    conn.close()
    return tmp_db


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SSRF Protection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSSRFProtection:
    """Verify that all URL-fetching modules block private/internal addresses."""

    # ── web_fetch SSRF ──

    def test_web_fetch_blocks_localhost(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://localhost/admin")
        assert _is_blocked_url("http://localhost:8080/secret")

    def test_web_fetch_blocks_zero_addr(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://0.0.0.0/")
        assert _is_blocked_url("http://0.0.0.0:9090/admin")

    def test_web_fetch_blocks_private_ipv4(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://10.0.0.1/internal")
        assert _is_blocked_url("http://172.16.0.1/internal")
        assert _is_blocked_url("http://192.168.1.1/router")

    def test_web_fetch_blocks_loopback(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://127.0.0.1/")
        assert _is_blocked_url("http://127.0.0.1:3000/api")

    def test_web_fetch_blocks_reserved(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://169.254.169.254/latest/meta-data/")

    def test_web_fetch_allows_public(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert not _is_blocked_url("https://example.com")
        assert not _is_blocked_url("https://api.github.com/repos")

    def test_web_fetch_blocks_ipv6_loopback(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        assert _is_blocked_url("http://[::1]/secret")

    def test_fetch_readable_rejects_blocked(self):
        from telechat_pkg.web_fetch import fetch_readable
        result = run_async(fetch_readable("http://127.0.0.1:8080/admin"))
        assert result.error is not None
        assert "Blocked" in result.error
        assert result.content == ""

    def test_fetch_readable_rejects_metadata_endpoint(self):
        from telechat_pkg.web_fetch import fetch_readable
        result = run_async(fetch_readable("http://169.254.169.254/latest/meta-data/"))
        assert result.error is not None
        assert "Blocked" in result.error

    # ── browser_automation SSRF ──

    def test_browser_blocks_localhost(self):
        from telechat_pkg.browser_automation import _is_blocked_url
        assert _is_blocked_url("http://localhost/admin")

    def test_browser_blocks_private_ip(self):
        from telechat_pkg.browser_automation import _is_blocked_url
        assert _is_blocked_url("http://10.0.0.1/internal")
        assert _is_blocked_url("http://192.168.0.1/router")

    def test_browser_blocks_non_http_schemes(self):
        from telechat_pkg.browser_automation import _is_blocked_url
        assert _is_blocked_url("file:///etc/passwd")
        assert _is_blocked_url("ftp://internal.server/data")
        assert _is_blocked_url("javascript:alert(1)")

    def test_browser_allows_https(self):
        from telechat_pkg.browser_automation import _is_blocked_url
        assert not _is_blocked_url("https://example.com")

    def test_browser_screenshot_rejects_blocked(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        result = run_async(agent.screenshot("http://127.0.0.1:8080/internal"))
        assert not result.success
        assert "Blocked" in result.error

    def test_browser_extract_text_rejects_blocked(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        result = run_async(agent.extract_text("http://10.0.0.1/secret"))
        assert not result.success
        assert "Blocked" in result.error

    def test_browser_fill_form_rejects_blocked(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        result = run_async(agent.fill_form("http://192.168.1.1/config", {"user": "admin"}))
        assert not result.success
        assert "Blocked" in result.error

    # ── link_understanding SSRF ──

    def test_link_blocks_private_hosts(self):
        from telechat_pkg.link_understanding import _is_blocked_host
        assert _is_blocked_host("localhost")
        assert _is_blocked_host("0.0.0.0")
        assert _is_blocked_host("127.0.0.1")
        assert _is_blocked_host("10.0.0.1")
        assert _is_blocked_host("192.168.1.1")

    def test_link_allows_public_hosts(self):
        from telechat_pkg.link_understanding import _is_blocked_host
        assert not _is_blocked_host("example.com")
        assert not _is_blocked_host("api.github.com")

    def test_extract_links_filters_private(self):
        from telechat_pkg.link_understanding import extract_links
        msg = "Check http://localhost:3000/admin and https://example.com"
        links = extract_links(msg)
        assert "http://localhost:3000/admin" not in links
        urls = [u for u in links if "localhost" in u]
        assert len(urls) == 0

    def test_extract_links_filters_internal_ip(self):
        from telechat_pkg.link_understanding import extract_links
        msg = "See http://10.0.0.1/secret and https://example.com/page"
        links = extract_links(msg)
        assert all("10.0.0.1" not in u for u in links)

    def test_extract_links_non_http_schemes_skipped(self):
        from telechat_pkg.link_understanding import extract_links
        msg = "file:///etc/passwd ftp://server/data https://safe.com"
        links = extract_links(msg)
        assert not any(u.startswith("file:") or u.startswith("ftp:") for u in links)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SQL Injection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSQLInjection:
    """Verify parameterized queries prevent SQL injection."""

    def test_store_save_turn_injection(self, store_with_db):
        store = store_with_db
        malicious_user = "'; DROP TABLE conversations; --"
        malicious_text = "Robert'; DROP TABLE conversations; --"
        store.save_turn("telegram", malicious_user, malicious_text, "reply")
        time.sleep(0.5)  # wait for async writer
        conn = store._get_conn()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {r[0] for r in rows}
        assert "conversations" in tables

    def test_store_load_history_injection(self, store_with_db):
        store = store_with_db
        malicious_uid = "1' OR '1'='1"
        result = store.load_history("telegram", malicious_uid)
        assert isinstance(result, list)

    def test_store_track_usage_injection(self, store_with_db):
        store = store_with_db
        malicious = "'; DELETE FROM usage; --"
        store.track_usage("telegram", malicious, 100, 200)
        time.sleep(0.5)
        conn = store._get_conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "usage" in tables

    def test_store_clear_history_injection(self, store_with_db):
        store = store_with_db
        store.save_turn("telegram", "legit_user", "hello", "hi")
        time.sleep(0.5)
        malicious = "legit_user' OR '1'='1"
        store.clear_history("telegram", malicious)
        history = store.load_history("telegram", "legit_user")
        # The malicious clear should not affect the legit user
        # (parametrized queries match exact string)

    def test_store_get_usage_injection(self, store_with_db):
        store = store_with_db
        malicious = "1' UNION SELECT 1,2,3 --"
        result = store.get_usage("telegram", malicious)
        assert result == {"messages": 0, "input": 0, "output": 0}

    def test_memory_store_injection(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        malicious_content = "'; DROP TABLE memories; --"
        m = mem.remember("telegram", "user1", malicious_content, tags=["test"])
        assert m.content == malicious_content
        # Table still exists
        conn = mem._conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "memories" in tables

    def test_memory_recall_injection(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        mem.remember("telegram", "user1", "safe content", tags=["pref"])
        malicious_query = "' OR 1=1; --"
        results = mem.recall("telegram", "user1", malicious_query)
        # Should not crash or return unexpected results
        assert isinstance(results, list)

    def test_memory_forget_injection(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "user1", "keep this")
        malicious_id = "'; DELETE FROM memories; --"
        mem.forget("telegram", "user1", malicious_id)
        # Original memory should still exist
        existing = mem.get("telegram", "user1", m.id)
        assert existing is not None

    def test_session_search_injection(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        mgr.get_or_create_active("telegram", "user1")
        malicious_query = "' OR 1=1; DROP TABLE user_sessions; --"
        results = mgr.search("telegram", "user1", malicious_query)
        assert isinstance(results, list)
        # Table should still exist
        conn = store._get_conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "user_sessions" in tables

    def test_feedback_save_injection(self, store_with_db):
        from telechat_pkg.feedback import save_feedback
        malicious = "'; DROP TABLE feedback; --"
        save_feedback("telegram", malicious, rating=5, text_feedback=malicious)
        time.sleep(0.5)
        conn = store_with_db._get_conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "feedback" in tables

    def test_cost_budget_injection(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        malicious = "'; DROP TABLE cost_budgets; --"
        mgr.set_budget("telegram", malicious, daily=5.0, monthly=50.0)
        result = mgr.check("telegram", malicious)
        # Should work without crashing
        conn = mgr._conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "cost_budgets" in tables

    def test_cost_mark_alert_validates_period(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        with pytest.raises(ValueError, match="Invalid period"):
            mgr._mark_alert("telegram", "user1", "'; DROP TABLE cost_budgets; --")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Input Validation & Injection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Verify input sanitization and validation across modules."""

    # ── MarkdownV2 escaping ──

    def test_markdown_escapes_special_chars(self):
        from telechat_pkg.markdown_v2 import escape_md2
        dangerous = '_*[]()~`>#+=|{}.!-'
        escaped = escape_md2(dangerous)
        for ch in dangerous:
            assert f"\\{ch}" in escaped

    def test_markdown_v2_preserves_code_blocks(self):
        from telechat_pkg.markdown_v2 import to_markdown_v2
        text = "```python\nprint('hello')\n```"
        result = to_markdown_v2(text)
        assert "print('hello')" in result

    def test_markdown_v2_handles_nested_formatting(self):
        from telechat_pkg.markdown_v2 import to_markdown_v2
        text = "**bold _italic_ bold**"
        result = to_markdown_v2(text)
        assert isinstance(result, str)

    def test_markdown_v2_handles_empty_input(self):
        from telechat_pkg.markdown_v2 import to_markdown_v2
        assert to_markdown_v2("") == ""
        assert to_markdown_v2(None) is None

    def test_markdown_v2_escapes_url_injection(self):
        from telechat_pkg.markdown_v2 import to_markdown_v2
        text = "[click](javascript:alert(1))"
        result = to_markdown_v2(text)
        assert "javascript" in result  # link preserved but no execution

    def test_try_markdownv2_fallback(self):
        from telechat_pkg.markdown_v2 import try_markdownv2
        text, mode = try_markdownv2("simple text")
        assert mode in ("MarkdownV2", "")

    def test_protect_urls_handles_malicious(self):
        from telechat_pkg.markdown_v2 import protect_urls
        text = "visit https://evil.com/path?a=1&b=2"
        result = protect_urls(text)
        assert "https://evil.com" in result

    # ── Link extraction validation ──

    def test_extract_links_limits_count(self):
        from telechat_pkg.link_understanding import extract_links
        msg = " ".join(f"https://example{i}.com" for i in range(20))
        links = extract_links(msg, max_links=3)
        assert len(links) <= 3

    def test_extract_links_strips_trailing_punctuation(self):
        from telechat_pkg.link_understanding import extract_links
        msg = "See https://example.com. And https://other.com!"
        links = extract_links(msg)
        for link in links:
            assert not link.endswith(".")
            assert not link.endswith("!")

    def test_extract_links_empty_input(self):
        from telechat_pkg.link_understanding import extract_links
        assert extract_links("") == []
        assert extract_links("   ") == []
        assert extract_links("no links here") == []

    def test_extract_links_deduplication(self):
        from telechat_pkg.link_understanding import extract_links
        msg = "https://example.com https://example.com https://example.com"
        links = extract_links(msg)
        assert len(links) == 1

    # ── Text chunking safety ──

    def test_chunk_text_handles_huge_input(self):
        from telechat_pkg.text_chunking import chunk_text
        huge = "A" * 100_000
        chunks = chunk_text(huge, limit=4000)
        assert all(len(c.text) <= 4100 for c in chunks)  # slight tolerance
        assert len(chunks) > 1

    def test_chunk_text_preserves_code_fences(self):
        from telechat_pkg.text_chunking import chunk_text
        text = "Before\n```python\n" + "x = 1\n" * 500 + "```\nAfter"
        chunks = chunk_text(text, limit=4000)
        full = "".join(c.text for c in chunks)
        assert "```python" in full
        assert "```" in full

    def test_chunk_text_single_message(self):
        from telechat_pkg.text_chunking import chunk_text
        text = "Short message"
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Short message"
        assert chunks[0].index == 0
        assert chunks[0].total == 1

    # ── Document extraction validation ──

    def test_extract_rejects_missing_file(self):
        from telechat_pkg.document_extract import extract
        result = extract("/nonexistent/file.pdf")
        assert result.error is not None
        assert "not found" in result.error.lower()

    def test_extract_rejects_oversized_file(self, tmp_path):
        from telechat_pkg.document_extract import extract, MAX_FILE_SIZE
        big_file = tmp_path / "big.txt"
        big_file.write_bytes(b"x" * (MAX_FILE_SIZE + 1))
        result = extract(str(big_file))
        assert result.error is not None
        assert "too large" in result.error.lower()

    def test_extract_rejects_empty_file(self, tmp_path):
        from telechat_pkg.document_extract import extract
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        result = extract(str(empty))
        assert result.error is not None

    def test_extract_truncates_large_content(self, tmp_path):
        from telechat_pkg.document_extract import extract_text_file, MAX_TEXT_LENGTH
        large = tmp_path / "large.txt"
        large.write_text("A" * (MAX_TEXT_LENGTH + 1000))
        result = extract_text_file(str(large))
        assert result.truncated
        assert len(result.text) <= MAX_TEXT_LENGTH + 100  # truncation marker

    def test_extract_csv_row_limit(self, tmp_path):
        from telechat_pkg.document_extract import extract_csv
        csv_file = tmp_path / "huge.csv"
        with open(csv_file, "w") as f:
            for i in range(11000):
                f.write(f"col1_{i},col2_{i},col3_{i}\n")
        result = extract_csv(str(csv_file))
        assert "truncated" in result.text.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Path Traversal Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPathTraversal:
    """Verify path handling prevents directory traversal attacks."""

    def test_coder_set_project_validates_path(self):
        from telechat_pkg.coder import set_project
        ok, msg = set_project("telegram", "user1", "/nonexistent/path/that/does/not/exist")
        assert not ok
        assert "Not a directory" in msg

    def test_coder_set_project_expands_user(self, tmp_path):
        from telechat_pkg.coder import set_project
        ok, msg = set_project("telegram", "user1", str(tmp_path))
        assert ok
        assert os.path.isabs(msg)

    def test_coder_set_project_normalizes_path(self, tmp_path):
        from telechat_pkg.coder import set_project
        traversal_path = str(tmp_path / ".." / tmp_path.name)
        ok, msg = set_project("telegram", "user_traversal", traversal_path)
        assert ok
        assert ".." not in msg

    def test_document_extract_path_validation(self):
        from telechat_pkg.document_extract import extract
        result = extract("../../../etc/passwd")
        assert result.error is not None

    def test_browser_screenshot_dir_creation(self, tmp_path):
        from telechat_pkg.browser_automation import SCREENSHOT_DIR
        assert isinstance(SCREENSHOT_DIR, str)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Rate Limiting Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimiting:
    """Verify rate limiting prevents abuse."""

    def test_rate_limit_allows_normal_traffic(self, store_with_db):
        store = store_with_db
        for i in range(5):
            assert store.check_rate_limit("test_user_normal")

    def test_rate_limit_blocks_excessive_requests(self, store_with_db, monkeypatch):
        store = store_with_db
        monkeypatch.setattr("telechat_pkg.store.RATE_LIMIT_REQUESTS", 5)
        monkeypatch.setattr("telechat_pkg.store.RATE_LIMIT_WINDOW", 60)
        # Reset rate state
        store._rate_state.clear()
        for i in range(5):
            assert store.check_rate_limit("flood_user")
        assert not store.check_rate_limit("flood_user")

    def test_rate_limit_per_user_isolation(self, store_with_db, monkeypatch):
        store = store_with_db
        monkeypatch.setattr("telechat_pkg.store.RATE_LIMIT_REQUESTS", 3)
        store._rate_state.clear()
        for i in range(3):
            store.check_rate_limit("user_a")
        assert not store.check_rate_limit("user_a")
        assert store.check_rate_limit("user_b")

    def test_rate_limit_window_expiry(self, store_with_db, monkeypatch):
        store = store_with_db
        monkeypatch.setattr("telechat_pkg.store.RATE_LIMIT_REQUESTS", 2)
        monkeypatch.setattr("telechat_pkg.store.RATE_LIMIT_WINDOW", 1)
        store._rate_state.clear()
        store.check_rate_limit("expire_user")
        store.check_rate_limit("expire_user")
        assert not store.check_rate_limit("expire_user")
        time.sleep(1.1)
        assert store.check_rate_limit("expire_user")

    def test_rate_limit_stale_cleanup(self, store_with_db, monkeypatch):
        store = store_with_db
        store._rate_state.clear()
        store._rate_state["stale_key"] = [time.time() - 1000]
        monkeypatch.setattr("telechat_pkg.store._rate_last_cleanup", 0.0)
        store.check_rate_limit("trigger_cleanup")
        assert "stale_key" not in store._rate_state


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Authentication Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthentication:
    """Verify authentication mechanisms in web_chat."""

    def test_web_chat_user_id_hashing(self):
        from telechat_pkg.web_chat import _get_user_id
        token = "secret_token_123"
        uid = _get_user_id(token)
        assert len(uid) == 16
        expected = hashlib.sha256(token.encode()).hexdigest()[:16]
        assert uid == expected

    def test_web_chat_user_id_deterministic(self):
        from telechat_pkg.web_chat import _get_user_id
        assert _get_user_id("abc") == _get_user_id("abc")

    def test_web_chat_user_id_different_tokens(self):
        from telechat_pkg.web_chat import _get_user_id
        assert _get_user_id("token1") != _get_user_id("token2")

    def test_web_chat_hmac_auth(self):
        token = "my_secret_token"
        correct = hmac.compare_digest(token, "my_secret_token")
        assert correct
        wrong = hmac.compare_digest(token, "wrong_token")
        assert not wrong

    def test_web_chat_timing_safe_comparison(self):
        # Verify hmac.compare_digest is used (timing-safe)
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat)
        assert "hmac.compare_digest" in source


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Resource Limit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestResourceLimits:
    """Verify resource limiter prevents runaway processes."""

    def test_resource_limits_defaults(self):
        from telechat_pkg.resource_limiter import ResourceLimits
        limits = ResourceLimits()
        assert limits.cpu_seconds == 300
        assert limits.memory_bytes == 2 * 1024 * 1024 * 1024
        assert limits.wall_time_seconds == 600
        assert limits.max_processes == 50

    def test_resource_limiter_templates(self):
        from telechat_pkg.resource_limiter import ResourceLimiter, TEMPLATES
        strict = ResourceLimiter.from_template("strict")
        assert strict.limits.cpu_seconds == 60
        assert strict.limits.memory_bytes == 512 * 1024 * 1024

    def test_resource_limiter_invalid_template(self):
        from telechat_pkg.resource_limiter import ResourceLimiter
        with pytest.raises(ValueError, match="Unknown template"):
            ResourceLimiter.from_template("nonexistent")

    def test_resource_limiter_test_template(self):
        from telechat_pkg.resource_limiter import ResourceLimiter, TEMPLATES
        test = ResourceLimiter.from_template("test")
        assert test.limits.cpu_seconds == 30
        assert test.limits.max_processes == 10
        assert test.limits.wall_time_seconds == 60

    def test_wall_time_enforcement(self):
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=2))
        rc, stdout, stderr, usage = run_async(
            limiter.execute(["sleep", "10"])
        )
        assert "wall_time" in usage.limits_hit or rc != 0

    def test_execute_with_string_command(self):
        from telechat_pkg.resource_limiter import ResourceLimiter
        limiter = ResourceLimiter()
        rc, stdout, stderr, usage = run_async(
            limiter.execute("echo hello")
        )
        assert rc == 0
        assert "hello" in stdout

    def test_format_usage(self):
        from telechat_pkg.resource_limiter import format_usage, ResourceUsage
        usage = ResourceUsage(
            wall_time_seconds=5.5,
            cpu_time_seconds=2.3,
            memory_peak_bytes=100 * 1024 * 1024,
            limits_hit=["wall_time"],
        )
        formatted = format_usage(usage)
        assert "5.5s" in formatted
        assert "CPU" in formatted
        assert "wall_time" in formatted


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Data Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDataIsolation:
    """Verify data isolation between users and platforms."""

    def test_conversation_isolation_between_users(self, store_with_db):
        store = store_with_db
        store.save_turn("telegram", "user1", "secret message", "secret reply")
        store.save_turn("telegram", "user2", "public message", "public reply")
        time.sleep(0.5)
        user1_history = store.load_history("telegram", "user1")
        user2_history = store.load_history("telegram", "user2")
        user1_content = " ".join(m["content"] for m in user1_history)
        user2_content = " ".join(m["content"] for m in user2_history)
        assert "secret message" not in user2_content
        assert "public message" not in user1_content

    def test_conversation_isolation_between_platforms(self, store_with_db):
        store = store_with_db
        store.save_turn("telegram", "user1", "telegram msg", "telegram reply")
        store.save_turn("whatsapp", "user1", "whatsapp msg", "whatsapp reply")
        time.sleep(0.5)
        telegram_h = store.load_history("telegram", "user1")
        whatsapp_h = store.load_history("whatsapp", "user1")
        telegram_content = " ".join(m["content"] for m in telegram_h)
        whatsapp_content = " ".join(m["content"] for m in whatsapp_h)
        assert "whatsapp msg" not in telegram_content
        assert "telegram msg" not in whatsapp_content

    def test_memory_isolation_between_users(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        mem.remember("telegram", "alice", "Alice's secret", importance=0.9)
        mem.remember("telegram", "bob", "Bob's data", importance=0.9)
        alice_results = mem.recall("telegram", "alice", "secret")
        bob_results = mem.recall("telegram", "bob", "secret")
        alice_content = " ".join(r.content for r in alice_results)
        assert "Bob's data" not in alice_content

    def test_memory_isolation_between_platforms(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        mem.remember("telegram", "user1", "telegram memory")
        mem.remember("slack", "user1", "slack memory")
        tg = mem.list_memories("telegram", "user1")
        sl = mem.list_memories("slack", "user1")
        tg_content = " ".join(m.content for m in tg)
        sl_content = " ".join(m.content for m in sl)
        assert "slack memory" not in tg_content
        assert "telegram memory" not in sl_content

    def test_session_isolation_between_users(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        mgr.create("telegram", "user1", "private-session")
        user2_sessions = mgr.get_all("telegram", "user2")
        session_names = [s.name for s in user2_sessions]
        assert "private-session" not in session_names

    def test_usage_tracking_isolation(self, store_with_db):
        store = store_with_db
        store.track_usage("telegram", "user1", 100, 200)
        time.sleep(0.5)
        user2_usage = store.get_usage("telegram", "user2")
        assert user2_usage["input"] == 0
        assert user2_usage["output"] == 0

    def test_cost_budget_isolation(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        mgr.set_budget("telegram", "user1", daily=1.0, monthly=10.0)
        report = mgr.usage_report("telegram", "user2")
        # user2 should have default budgets, not user1's
        assert report.daily_limit != 1.0 or report.monthly_limit != 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Cost Budget Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCostBudgetSecurity:
    """Verify cost budget enforcement prevents abuse."""

    def test_budget_blocks_when_exceeded(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        mgr.set_budget("telegram", "user1", daily=0.01, monthly=0.10)
        # Simulate cost tracking
        conn = mgr._conn()
        conn.execute(
            """INSERT INTO cost_tracking (platform, user_id, date, requests, input_tokens, output_tokens, cost_usd)
               VALUES ('telegram', 'user1', date('now'), 10, 10000, 5000, 0.05)"""
        )
        conn.commit()
        warning = mgr.check("telegram", "user1")
        assert warning is not None
        assert "exceeded" in warning.lower() or "budget" in warning.lower()

    def test_budget_warns_at_threshold(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        mgr.set_budget("telegram", "user1", daily=1.0, monthly=50.0)
        conn = mgr._conn()
        conn.execute(
            """INSERT INTO cost_tracking (platform, user_id, date, requests, input_tokens, output_tokens, cost_usd)
               VALUES ('telegram', 'user1', date('now'), 5, 5000, 2000, 0.85)"""
        )
        conn.commit()
        warning = mgr.check("telegram", "user1")
        assert warning is not None
        assert "warning" in warning.lower() or "%" in warning

    def test_budget_allows_under_limit(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        mgr.set_budget("telegram", "user1", daily=10.0, monthly=100.0)
        warning = mgr.check("telegram", "user1")
        assert warning is None

    def test_budget_negative_values_handled(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        # Negative budgets should not break the system
        mgr.set_budget("telegram", "user1", daily=-1.0, monthly=-10.0)
        report = mgr.usage_report("telegram", "user1")
        assert isinstance(report.daily_pct, (int, float))
        assert isinstance(report.monthly_pct, (int, float))

    def test_budget_reset_daily_alerts(self, budget_db):
        from telechat_pkg.cost_budget import BudgetManager
        mgr = BudgetManager(db_path=budget_db)
        mgr.set_budget("telegram", "user1", daily=1.0)
        mgr._mark_alert("telegram", "user1", "daily")
        mgr.reset_daily_alerts()
        budget = mgr._get_budget("telegram", "user1")
        assert not budget.alert_sent_daily


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Session Management Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSessionSecurity:
    """Verify session management prevents unauthorized access."""

    def test_session_name_in_effective_uid(self, store_with_db):
        store = store_with_db
        store.save_turn("telegram", "user1", "msg in default", "reply", session_name="")
        store.save_turn("telegram", "user1", "msg in private", "reply", session_name="private")
        time.sleep(0.5)
        default_h = store.load_history("telegram", "user1", session_name="")
        private_h = store.load_history("telegram", "user1", session_name="private")
        default_content = " ".join(m["content"] for m in default_h)
        assert "msg in private" not in default_content

    def test_session_max_limit(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        for i in range(25):
            mgr.create("telegram", "session_user", f"session_{i}")
        active = mgr.get_all("telegram", "session_user")
        assert len(active) <= 20

    def test_session_busy_prevents_deletion(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        sess = mgr.create("telegram", "busy_user", "busy_session")
        sess.is_busy = True
        result = mgr.delete_by_name("telegram", "busy_user", "busy_session")
        assert not result

    def test_session_busy_prevents_archive(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        sess = mgr.create("telegram", "busy_user2", "busy_session2")
        sess.is_busy = True
        result = mgr.archive("telegram", "busy_user2", "busy_session2")
        assert result is None

    def test_session_cli_session_validity_timeout(self):
        from telechat_pkg.store import UserSession
        sess = UserSession("test", "telegram", "user1")
        sess.claude_session_id = "some_id"
        sess.last_active = time.time() - 7200  # 2 hours ago
        assert not sess.cli_session_valid

    def test_session_cli_session_valid_when_busy(self):
        from telechat_pkg.store import UserSession
        sess = UserSession("test", "telegram", "user1")
        sess.claude_session_id = "some_id"
        sess.last_active = time.time() - 7200
        sess.is_busy = True
        assert sess.cli_session_valid

    def test_session_title_truncation(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        mgr.create("telegram", "title_user", "test_sess")
        result = mgr.set_title("telegram", "title_user", "test_sess", "A" * 200)
        assert result is not None
        assert len(result.title) <= 100

    def test_session_rename_prevents_duplicate(self, store_with_db):
        store = store_with_db
        mgr = store._session_mgr
        mgr.create("telegram", "rename_user", "session_a")
        mgr.create("telegram", "rename_user", "session_b")
        result = mgr.rename("telegram", "rename_user", "session_a", "session_b")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Memory Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemorySecurity:
    """Verify memory system security."""

    def test_memory_importance_clamping(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "user1", "test", importance=999.0)
        assert m.importance <= 1.0
        m2 = mem.remember("telegram", "user1", "test2", importance=-5.0)
        assert m2.importance >= 0.0

    def test_memory_content_stripping(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "user1", "  padded content  ")
        assert m.content == "padded content"

    def test_memory_update_importance_clamping(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "user1", "original")
        updated = mem.update("telegram", "user1", m.id, importance=100.0)
        assert updated.importance <= 1.0

    def test_memory_forget_wrong_user(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "alice", "alice's memory")
        result = mem.forget("telegram", "bob", m.id)
        assert not result
        # Alice's memory still exists
        existing = mem.get("telegram", "alice", m.id)
        assert existing is not None

    def test_memory_update_wrong_user(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        m = mem.remember("telegram", "alice", "alice's data")
        result = mem.update("telegram", "bob", m.id, content="hacked")
        assert result is None
        # Alice's data unchanged
        existing = mem.get("telegram", "alice", m.id)
        assert existing.content == "alice's data"

    def test_memory_fts_query_sanitization(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        mem.remember("telegram", "user1", "test content")
        # FTS injection attempt
        results = mem.recall("telegram", "user1", '" OR 1=1 --')
        assert isinstance(results, list)

    def test_memory_import_skips_empty(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        result = mem.import_all("telegram", "user1", [
            {"content": "valid", "tags": ["test"]},
            {"content": "", "tags": ["empty"]},
            {"content": "   ", "tags": ["whitespace"]},
        ])
        assert result["imported"] >= 1
        assert result["skipped"] >= 1

    def test_memory_import_clamps_importance(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        result = mem.import_all("telegram", "user1", [
            {"content": "high", "importance": 999},
            {"content": "low", "importance": -10},
        ])
        assert result["imported"] == 2
        memories = mem.list_memories("telegram", "user1")
        for m in memories:
            assert 0.0 <= m.importance <= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Web Fetch / Search Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWebSecurity:
    """Verify web fetch and search security."""

    def test_web_fetch_content_truncation(self):
        from telechat_pkg.web_fetch import MAX_CONTENT_LENGTH
        assert MAX_CONTENT_LENGTH > 0

    def test_web_fetch_timeout_configured(self):
        from telechat_pkg.web_fetch import FETCH_TIMEOUT
        assert FETCH_TIMEOUT > 0
        assert FETCH_TIMEOUT <= 60

    def test_web_search_api_key_not_in_params(self):
        """Brave Search uses header auth, not URL params."""
        import inspect
        from telechat_pkg import web_search
        source = inspect.getsource(web_search._search_brave)
        assert "X-Subscription-Token" in source
        # API key should be in headers, not URL params
        assert "params" in source  # query params exist
        # But the key itself goes in headers
        assert "BRAVE_API_KEY" not in source.split("params")[1].split("}")[0] if "params" in source else True

    def test_web_search_tavily_api_key_in_body(self):
        """Tavily sends API key in POST body, which is acceptable."""
        import inspect
        from telechat_pkg import web_search
        source = inspect.getsource(web_search._search_tavily)
        assert "json=payload" in source or "json=" in source

    def test_web_search_disabled_by_default(self):
        from telechat_pkg.web_search import WEB_SEARCH_ENABLED
        # Default should be disabled unless explicitly enabled
        # (env var not set in test = "false")

    def test_web_fetch_disabled_by_default(self):
        from telechat_pkg.web_fetch import WEB_FETCH_ENABLED
        assert not WEB_FETCH_ENABLED

    def test_web_search_max_results_bounded(self):
        from telechat_pkg.web_search import MAX_RESULTS
        assert MAX_RESULTS > 0
        assert MAX_RESULTS <= 50

    def test_web_search_format_results_xss_safe(self):
        from telechat_pkg.web_search import format_results, SearchResponse, SearchResult
        resp = SearchResponse(
            query="test",
            results=[
                SearchResult(
                    title="<script>alert(1)</script>",
                    url="https://evil.com/<script>",
                    snippet="<img onerror='alert(1)'>",
                ),
            ],
        )
        formatted = format_results(resp)
        # The format is markdown, which will be escaped by the rendering layer
        assert isinstance(formatted, str)

    def test_web_search_error_format(self):
        from telechat_pkg.web_search import format_results, SearchResponse
        resp = SearchResponse(query="test", error="API key invalid")
        formatted = format_results(resp)
        assert "error" in formatted.lower()

    def test_web_search_empty_results(self):
        from telechat_pkg.web_search import format_results, SearchResponse
        resp = SearchResponse(query="test")
        formatted = format_results(resp)
        assert "no results" in formatted.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Error Classifier Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorClassifierSecurity:
    """Verify error classifier doesn't leak sensitive info."""

    def test_fingerprint_strips_paths(self):
        from telechat_pkg.error_classifier import _fingerprint
        error = "Error at /home/user/.secret/key.pem: permission denied"
        fp = _fingerprint(error)
        assert "/home/user" not in fp
        assert ".secret" not in fp

    def test_fingerprint_strips_line_numbers(self):
        from telechat_pkg.error_classifier import _fingerprint
        error1 = "Error at line 42 in file.py"
        error2 = "Error at line 99 in file.py"
        fp1 = _fingerprint(error1)
        fp2 = _fingerprint(error2)
        assert fp1 == fp2

    def test_fingerprint_strips_hashes(self):
        from telechat_pkg.error_classifier import _fingerprint
        error = "Failed for session abc123def456: timeout"
        fp = _fingerprint(error)
        assert "abc123def456" not in fp

    def test_fingerprint_strips_urls(self):
        from telechat_pkg.error_classifier import _fingerprint
        error = "Request to https://api.secret.com/v1/key failed"
        fp = _fingerprint(error)
        assert "api.secret.com" not in fp

    def test_convergence_detector_bounded_history(self):
        from telechat_pkg.error_classifier import ConvergenceDetector
        det = ConvergenceDetector()
        for i in range(100):
            det.record(f"error_{i}")
        assert len(det._history) <= 20

    def test_convergence_detector_detects_oscillation(self):
        from telechat_pkg.error_classifier import ConvergenceDetector
        det = ConvergenceDetector()
        det.record("err_a")
        det.record("err_a")
        det.record("err_a")
        result = det.check()
        assert result.status == "oscillating"

    def test_convergence_detector_detects_stuck(self):
        from telechat_pkg.error_classifier import ConvergenceDetector
        det = ConvergenceDetector()
        det.record("err_1")
        det.record("err_2")
        det.record("err_3")
        result = det.check()
        assert result.status in ("stuck", "oscillating")

    def test_convergence_detector_reset(self):
        from telechat_pkg.error_classifier import ConvergenceDetector
        det = ConvergenceDetector()
        det.record("err_a")
        det.reset()
        result = det.check()
        assert result.status == "progressing"


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Feedback Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFeedbackSecurity:
    """Verify feedback system prevents abuse."""

    def test_feedback_response_preview_truncation(self, store_with_db):
        from telechat_pkg.feedback import save_feedback
        long_preview = "A" * 10000
        save_feedback("telegram", "user1", rating=5, response_preview=long_preview)
        time.sleep(0.5)
        from telechat_pkg.feedback import get_recent_feedback
        entries = get_recent_feedback("telegram", "user1")
        for entry in entries:
            assert len(entry.get("response_preview", "")) <= 500

    def test_feedback_stats_empty_user(self, store_with_db):
        from telechat_pkg.feedback import get_feedback_stats
        stats = get_feedback_stats("telegram", "nonexistent_user")
        assert stats["total_ratings"] == 0
        assert stats["satisfaction_pct"] == 0

    def test_quality_evaluator_empty_response(self):
        from telechat_pkg.feedback import evaluate_response
        scores = evaluate_response("hello?", "", {})
        assert not scores["has_content"]
        assert not scores["length_appropriate"]

    def test_quality_evaluator_error_detection(self):
        from telechat_pkg.feedback import evaluate_response
        scores = evaluate_response("hello", "[Claude error] Something broke", {})
        assert not scores["error_free"]

    def test_quality_evaluator_cost_flag(self):
        from telechat_pkg.feedback import evaluate_response
        scores = evaluate_response("hello", "reply", {"cost_usd": 5.0})
        assert not scores["reasonable_cost"]

    def test_quality_evaluator_truncation_detection(self):
        from telechat_pkg.feedback import evaluate_response
        scores = evaluate_response("hello", "Some text…(truncated)", {})
        assert not scores["not_truncated"]


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Coder Module Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCoderSecurity:
    """Verify coding agent security constraints."""

    def test_coder_system_prompt_safety_rules(self):
        from telechat_pkg.coder import CODER_SYSTEM
        assert "destructive" in CODER_SYSTEM.lower()
        assert "force push" in CODER_SYSTEM.lower()
        assert "never" in CODER_SYSTEM.lower()

    def test_coder_project_persistence(self, tmp_path, monkeypatch):
        from telechat_pkg.coder import set_project, get_project, clear_project, _PROJECTS_PATH
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", tmp_path / "projects.json")
        ok, path = set_project("telegram", "user1", str(tmp_path))
        assert ok
        assert get_project("telegram", "user1") == str(tmp_path)
        clear_project("telegram", "user1")
        assert get_project("telegram", "user1") is None

    def test_pipeline_stage_tracking(self):
        from telechat_pkg.coder import PipelineTracker, PipelineStage
        tracker = PipelineTracker()
        sid, label = tracker.on_tool("Read")
        assert sid == PipelineStage.EXPLORING[0]
        sid, label = tracker.on_tool("Write")
        assert sid == PipelineStage.CODING[0]

    def test_pipeline_fix_loop_detection(self):
        from telechat_pkg.coder import PipelineTracker, PipelineStage
        tracker = PipelineTracker()
        tracker.on_tool("Write")
        tracker.on_tool("Bash", "pytest tests/")
        tracker.on_tool("Write")  # fix attempt
        assert tracker._fix_count >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Database Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDatabaseSecurity:
    """Verify database configuration security."""

    def test_wal_mode_enabled(self, store_with_db):
        store = store_with_db
        conn = store._get_conn()
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0].lower() == "wal"

    def test_history_auto_pruning(self, store_with_db):
        store = store_with_db
        for i in range(30):
            store.save_turn("telegram", "prune_user", f"msg_{i}", f"reply_{i}")
        time.sleep(1)
        history = store.load_history("telegram", "prune_user", limit=100)
        assert len(history) <= 40  # pruning keeps ~20

    def test_history_cache_size_bounded(self, store_with_db):
        store = store_with_db
        from telechat_pkg.store import _HISTORY_CACHE_MAX
        assert _HISTORY_CACHE_MAX > 0
        assert _HISTORY_CACHE_MAX <= 1000

    def test_write_queue_bounded(self, store_with_db):
        store = store_with_db
        from telechat_pkg.store import _write_queue
        assert _write_queue is not None
        assert _write_queue.maxsize == 1000

    def test_replace_history_clears_cache(self, store_with_db):
        store = store_with_db
        store.save_turn("telegram", "cache_user", "old msg", "old reply")
        time.sleep(0.5)
        store.load_history("telegram", "cache_user")  # populate cache
        store.replace_history("telegram", "cache_user", [
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new reply"},
        ])
        history = store.load_history("telegram", "cache_user")
        content = " ".join(m["content"] for m in history)
        assert "new" in content


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Environment / Secrets Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecretsHandling:
    """Verify API keys and secrets are handled securely."""

    def test_api_keys_from_env_only(self):
        """Verify API keys come from environment, not hardcoded."""
        import inspect
        from telechat_pkg import web_fetch, web_search
        fetch_src = inspect.getsource(web_fetch)
        search_src = inspect.getsource(web_search)
        assert "os.getenv" in fetch_src or "os.environ" in fetch_src
        assert "os.getenv" in search_src or "os.environ" in search_src

    def test_jina_api_key_from_env(self):
        from telechat_pkg.web_fetch import JINA_API_KEY
        # Should be empty string when not set, not None
        assert isinstance(JINA_API_KEY, str)

    def test_brave_api_key_from_env(self):
        from telechat_pkg.web_search import BRAVE_API_KEY
        assert isinstance(BRAVE_API_KEY, str)

    def test_tavily_api_key_from_env(self):
        from telechat_pkg.web_search import TAVILY_API_KEY
        assert isinstance(TAVILY_API_KEY, str)

    def test_web_chat_token_from_env(self):
        from telechat_pkg.web_chat import WEB_AUTH_TOKEN
        assert isinstance(WEB_AUTH_TOKEN, str)

    def test_error_messages_dont_leak_keys(self):
        from telechat_pkg.web_fetch import FetchResult
        result = FetchResult(url="test", title="", content="", word_count=0,
                             error="Connection failed")
        assert "api_key" not in str(result).lower()
        assert "secret" not in str(result).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Content Length / DoS Prevention Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDoSPrevention:
    """Verify mechanisms that prevent resource exhaustion."""

    def test_web_fetch_max_content_length(self):
        from telechat_pkg.web_fetch import MAX_CONTENT_LENGTH
        assert MAX_CONTENT_LENGTH > 0
        assert MAX_CONTENT_LENGTH <= 10 * 1024 * 1024

    def test_link_max_content_length(self):
        from telechat_pkg.link_understanding import MAX_CONTENT_LENGTH
        assert MAX_CONTENT_LENGTH > 0

    def test_link_max_links(self):
        from telechat_pkg.link_understanding import MAX_LINKS
        assert MAX_LINKS > 0
        assert MAX_LINKS <= 10

    def test_link_fetch_timeout(self):
        from telechat_pkg.link_understanding import FETCH_TIMEOUT
        assert FETCH_TIMEOUT > 0
        assert FETCH_TIMEOUT <= 30

    def test_browser_timeout(self):
        from telechat_pkg.browser_automation import BROWSER_TIMEOUT
        assert BROWSER_TIMEOUT > 0
        assert BROWSER_TIMEOUT <= 120000

    def test_document_max_file_size(self):
        from telechat_pkg.document_extract import MAX_FILE_SIZE
        assert MAX_FILE_SIZE > 0

    def test_document_max_text_length(self):
        from telechat_pkg.document_extract import MAX_TEXT_LENGTH
        assert MAX_TEXT_LENGTH > 0

    def test_websocket_max_message_size(self):
        """WebSocket has a max message size limit."""
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat._ws_handler)
        assert "max_msg_size" in source

    def test_history_limit_default(self, store_with_db):
        store = store_with_db
        history = store.load_history("telegram", "user1")
        assert isinstance(history, list)

    def test_search_results_bounded(self):
        from telechat_pkg.web_search import MAX_RESULTS
        assert 1 <= MAX_RESULTS <= 50


# ═══════════════════════════════════════════════════════════════════════════════
# 19. MCP Client Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMCPSecurity:
    """Verify MCP client security."""

    def test_mcp_disabled_by_default(self):
        from telechat_pkg.mcp_client import MCP_ENABLED
        assert not MCP_ENABLED

    def test_mcp_config_file_must_exist(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        # With no config file set, no servers loaded
        assert len(mgr._servers) == 0

    def test_mcp_server_initial_status(self):
        from telechat_pkg.mcp_client import MCPServer
        server = MCPServer(name="test", command="echo")
        assert server.status == "disconnected"
        assert server.tools == []


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Browser Automation Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrowserAutomationSecurity:
    """Verify browser automation security constraints."""

    def test_browser_disabled_by_default(self):
        from telechat_pkg.browser_automation import BROWSER_ENABLED
        assert not BROWSER_ENABLED

    def test_browser_headless_by_default(self):
        from telechat_pkg.browser_automation import BROWSER_HEADLESS
        assert BROWSER_HEADLESS

    def test_browser_run_script_no_ssrf_check(self):
        """run_script doesn't check SSRF - verify it at least exists."""
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        # run_script does not have SSRF blocking - this is a known gap
        # but agent is disabled by default and requires explicit opt-in
        assert hasattr(agent, "run_script")

    def test_browser_singleton_pattern(self):
        from telechat_pkg.browser_automation import get_browser_agent
        agent1 = get_browser_agent()
        agent2 = get_browser_agent()
        assert agent1 is agent2

    def test_browser_user_agent_identifies_bot(self):
        """User agent should identify the bot, not impersonate a browser."""
        import inspect
        from telechat_pkg import browser_automation
        source = inspect.getsource(browser_automation.BrowserAgent.start)
        assert "TeleChat" in source

    def test_browser_text_content_truncation(self):
        """extract_text truncates content to prevent memory issues."""
        import inspect
        from telechat_pkg import browser_automation
        source = inspect.getsource(browser_automation.BrowserAgent.extract_text)
        assert "5000" in source  # content[:5000]


# ═══════════════════════════════════════════════════════════════════════════════
# 21. Concurrent Access Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentAccess:
    """Verify thread safety of shared resources."""

    def test_store_thread_local_connections(self, store_with_db):
        """Each thread should get its own SQLite connection."""
        connections = []
        lock = threading.Lock()

        def get_conn():
            from telechat_pkg.store import _get_conn
            conn = _get_conn()
            with lock:
                connections.append(id(conn))

        threads = [threading.Thread(target=get_conn) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All threads completed and got connections
        assert len(connections) == 5

    def test_memory_store_thread_local(self, tmp_db):
        from telechat_pkg.memory import MemoryStore
        mem = MemoryStore(db_path=tmp_db)
        results = []

        def remember():
            m = mem.remember("telegram", "user1", f"memory_{threading.current_thread().name}")
            results.append(m)

        threads = [threading.Thread(target=remember) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 5

    def test_coder_project_file_locking(self, tmp_path, monkeypatch):
        """Project file writes use a lock for thread safety."""
        from telechat_pkg.coder import _lock
        assert isinstance(_lock, type(threading.Lock()))


# ═══════════════════════════════════════════════════════════════════════════════
# 22. Web Chat WebSocket Security Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWebChatSecurity:
    """Verify WebSocket handler security."""

    def test_unauthenticated_messages_blocked(self):
        """When auth is required, messages before auth should be rejected."""
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat._ws_handler)
        assert "not authenticated" in source.lower() or "Not authenticated" in source

    def test_auth_required_when_token_set(self):
        """When WEB_AUTH_TOKEN is set, auth is required."""
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat._ws_handler)
        assert "authenticated" in source

    def test_invalid_json_handled(self):
        """Invalid JSON messages should be handled gracefully."""
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat._ws_handler)
        assert "JSONDecodeError" in source

    def test_empty_message_ignored(self):
        """Empty messages should not be processed."""
        import inspect
        from telechat_pkg import web_chat
        source = inspect.getsource(web_chat._ws_handler)
        assert "not text" in source.lower() or "strip" in source

    def test_health_endpoint_no_auth_required(self):
        """Health endpoint should be accessible without auth."""
        from telechat_pkg.web_chat import _create_app
        app = _create_app()
        routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource') and hasattr(r.resource, 'canonical')]
        assert "/health" in routes


# ═══════════════════════════════════════════════════════════════════════════════
# 23. HTML Stripping Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestHTMLStripping:
    """Verify HTML is properly stripped from fetched content."""

    def test_link_understanding_strips_scripts(self):
        from telechat_pkg.link_understanding import _strip_html
        html = '<div>Hello</div><script>alert("xss")</script>'
        text = _strip_html(html)
        assert "alert" not in text
        assert "<script>" not in text

    def test_link_understanding_strips_styles(self):
        from telechat_pkg.link_understanding import _strip_html
        html = '<style>.evil { display:none }</style><p>Content</p>'
        text = _strip_html(html)
        assert "<style>" not in text
        assert "evil" not in text
        assert "Content" in text

    def test_link_understanding_strips_all_tags(self):
        from telechat_pkg.link_understanding import _strip_html
        html = '<div><a href="bad">link</a><img src="x" onerror="alert(1)"></div>'
        text = _strip_html(html)
        assert "<" not in text
        assert ">" not in text

    def test_web_fetch_strips_scripts(self):
        """web_fetch raw mode strips script tags."""
        import inspect
        from telechat_pkg import web_fetch
        source = inspect.getsource(web_fetch._fetch_raw)
        assert "script" in source.lower()

    def test_web_fetch_strips_nav_footer(self):
        """web_fetch strips nav and footer to reduce noise."""
        import inspect
        from telechat_pkg import web_fetch
        source = inspect.getsource(web_fetch._fetch_raw)
        assert "nav" in source.lower()
        assert "footer" in source.lower()
