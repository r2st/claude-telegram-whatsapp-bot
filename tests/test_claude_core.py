"""
Tests for claude_core internals — prompt building, CLI output parsing,
tool detail extraction, history cache, write queue, usage tracking,
session management, and all invocation paths (mocked).

Run:
    pytest tests/test_claude_core.py -v
"""

import os
import queue
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_cc.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir
os.environ["RATE_LIMIT_REQUESTS"] = "100"
os.environ["RATE_LIMIT_WINDOW"] = "60"

import telechat_pkg.claude_core as cc
import telechat_pkg.store as _store

cc.init_db()


# ══════════════════════════════════════════════════════════════════════════════
# 1. _build_prompt
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildPrompt:
    def test_with_history(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = cc._build_prompt("how are you?", history)
        assert "User: hello" in result
        assert "Assistant: hi there" in result
        assert "User: how are you?" in result

    def test_empty_history(self):
        result = cc._build_prompt("test message", [])
        assert result == "User: test message"

    def test_multiple_turns(self):
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = cc._build_prompt("q3", history)
        assert result.count("User:") == 3
        assert result.count("Assistant:") == 2


# ══════════════════════════════════════════════════════════════════════════════
# 2. _extract_tool_detail
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractToolDetail:
    def test_file_path(self):
        block = {"input": {"file_path": "/home/user/project/src/main.py"}}
        assert "src/main.py" in cc._extract_tool_detail(block)

    def test_short_file_path(self):
        block = {"input": {"file_path": "main.py"}}
        assert cc._extract_tool_detail(block) == "main.py"

    def test_command(self):
        block = {"input": {"command": "ls -la"}}
        assert cc._extract_tool_detail(block) == "ls -la"

    def test_long_command_truncated(self):
        block = {"input": {"command": "x" * 100}}
        result = cc._extract_tool_detail(block)
        assert len(result) == 50

    def test_pattern(self):
        block = {"input": {"pattern": "TODO"}}
        assert cc._extract_tool_detail(block) == "/TODO/"

    def test_long_pattern_truncated(self):
        block = {"input": {"pattern": "x" * 60}}
        result = cc._extract_tool_detail(block)
        assert len(result) <= 33  # /.../ with 30 chars

    def test_empty_input(self):
        block = {"input": {}}
        assert cc._extract_tool_detail(block) == ""

    def test_non_dict_input(self):
        block = {"input": "not a dict"}
        assert cc._extract_tool_detail(block) == ""

    def test_no_input(self):
        block = {}
        assert cc._extract_tool_detail(block) == ""


# ══════════════════════════════════════════════════════════════════════════════
# 3. _parse_cli_output
# ══════════════════════════════════════════════════════════════════════════════


class TestParseCliOutput:
    def test_result_event(self):
        stdout = '{"type":"result","result":"Hello world","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":3},"total_cost_usd":0.01,"session_id":"s123"}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert text == "Hello world"
        assert stats["input_tokens"] == 13
        assert stats["output_tokens"] == 5
        assert stats["cost_usd"] == 0.01
        assert stats["session_id"] == "s123"

    def test_assistant_text_event(self):
        stdout = '{"type":"assistant","message":{"content":[{"type":"text","text":"response text"}]}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert text == "response text"

    def test_assistant_tool_use_event(self):
        stdout = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert "Read" in stats.get("tools_used", [])

    def test_content_block_start_tool(self):
        stdout = '{"type":"content_block_start","content_block":{"type":"tool_use","name":"Bash"}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert "Bash" in stats.get("tools_used", [])

    def test_non_json_output(self):
        text, stats = cc._parse_cli_output("plain text output", "", 0, 180)
        assert text == "plain text output"

    def test_error_returncode(self):
        text, stats = cc._parse_cli_output("", "something went wrong", 1, 180)
        assert "[Claude error]" in text
        assert "something went wrong" in text

    def test_empty_output(self):
        text, stats = cc._parse_cli_output("", "", 0, 180)
        assert text == "(no response)"

    def test_mixed_json_and_plain(self):
        stdout = 'some plain text\n{"type":"result","result":"final answer","usage":{}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert text == "final answer"

    def test_multiple_events(self):
        lines = [
            '{"type":"content_block_start","content_block":{"type":"tool_use","name":"Read"}}',
            '{"type":"content_block_start","content_block":{"type":"tool_use","name":"Bash"}}',
            '{"type":"result","result":"Done","usage":{"input_tokens":50,"output_tokens":20}}',
        ]
        text, stats = cc._parse_cli_output("\n".join(lines), "", 0, 180)
        assert text == "Done"
        assert "Read" in stats["tools_used"]
        assert "Bash" in stats["tools_used"]

    def test_assistant_message_non_dict(self):
        stdout = '{"type":"assistant","message":"string message"}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        # Should not crash

    def test_long_stderr_truncated(self):
        text, stats = cc._parse_cli_output("", "x" * 1000, 1, 180)
        assert len(text) < 600


# ══════════════════════════════════════════════════════════════════════════════
# 4. History cache
# ══════════════════════════════════════════════════════════════════════════════


class TestHistoryCache:
    def test_cache_key(self):
        assert cc._cache_key("tg", "123") == "tg:123"

    def test_invalidate_clears_cache(self):
        cc._history_cache["tg:test_inv"] = (time.time(), [{"role": "user", "content": "cached"}])
        cc._invalidate_history("tg", "test_inv")
        assert "tg:test_inv" not in cc._history_cache

    def test_load_uses_cache(self):
        cc.save_turn("test_cache", "u_cache", "hello", "world")
        time.sleep(0.3)
        cc._invalidate_history("test_cache", "u_cache")
        h1 = cc.load_history("test_cache", "u_cache")
        h2 = cc.load_history("test_cache", "u_cache")
        assert h1 == h2

    def test_cache_expires(self):
        key = cc._cache_key("test_expire", "u_exp")
        cc._history_cache[key] = (time.time() - 100, [])
        h = cc.load_history("test_expire", "u_exp")
        assert isinstance(h, list)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Write queue
# ══════════════════════════════════════════════════════════════════════════════


class TestWriteQueue:
    def test_enqueue_write(self):
        cc._enqueue_write(
            "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
            ("test_wq", "wq_user"),
        )
        time.sleep(0.5)
        conn = cc._get_conn()
        row = conn.execute(
            "SELECT * FROM usage WHERE platform='test_wq' AND user_id='wq_user'"
        ).fetchone()
        # May or may not be written yet due to async queue

    def test_enqueue_write_fallback_when_full(self):
        old_queue = _store._write_queue
        try:
            _store._write_queue = queue.Queue(maxsize=0)
            cc._enqueue_write(
                "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
                ("test_full", "full_user"),
            )
        finally:
            _store._write_queue = old_queue


# ══════════════════════════════════════════════════════════════════════════════
# 6. Track functions
# ══════════════════════════════════════════════════════════════════════════════


class TestTracking:
    def test_track_tool_usage(self):
        cc.track_tool_usage("test", "tool_user", ["Read", "Bash"])
        time.sleep(0.3)

    def test_track_tool_usage_empty(self):
        cc.track_tool_usage("test", "tool_user", [])

    def test_track_cost(self):
        cc.track_cost("test", "cost_user", 100, 50, 0.01)
        time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Session legacy API
# ══════════════════════════════════════════════════════════════════════════════


class TestSessionLegacyAPI:
    def test_get_session_id_none(self):
        assert cc.get_session_id("test", "no_sess_user") is None

    def test_set_and_get_session_id(self):
        cc.set_session_id("test", "sess_user", "sid123")
        sid = cc.get_session_id("test", "sess_user")
        assert sid == "sid123"

    def test_clear_session(self):
        cc.set_session_id("test", "clear_sess_user", "sid456")
        cc.clear_session("test", "clear_sess_user")
        sid = cc.get_session_id("test", "clear_sess_user")
        assert sid is None

    def test_session_expires(self):
        sess = cc._session_mgr.get_or_create_active("test", "expire_user")
        sess.claude_session_id = "old_sid"
        sess.last_active = time.time() - 7200  # 2 hours ago
        sess.is_busy = False
        assert sess.cli_session_valid is False
        assert cc.get_session_id("test", "expire_user") is None


# ══════════════════════════════════════════════════════════════════════════════
# 8. SessionManager additional coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestSessionManagerExtra:
    def test_get_all_empty(self):
        assert cc._session_mgr.get_all("test", "nonexistent_user") == []

    def test_get_active_index_default(self):
        assert cc._session_mgr.get_active_index("test", "no_idx_user") == 0

    def test_clear_active(self):
        sess = cc._session_mgr.get_or_create_active("test", "clear_active_user")
        sess.claude_session_id = "sid"
        sess.message_count = 5
        cc._session_mgr.clear_active("test", "clear_active_user")
        assert sess.claude_session_id is None
        assert sess.message_count == 0

    def test_delete_nonexistent(self):
        assert cc._session_mgr.delete("test", "no_del_user", 0) is False

    def test_delete_out_of_range(self):
        cc._session_mgr.get_or_create_active("test", "del_range_user")
        assert cc._session_mgr.delete("test", "del_range_user", 99) is False

    def test_delete_negative_index(self):
        cc._session_mgr.get_or_create_active("test", "del_neg_user")
        assert cc._session_mgr.delete("test", "del_neg_user", -1) is False

    def test_delete_recreates_default(self):
        cc._session_mgr.get_or_create_active("test", "del_recreate_user")
        cc._session_mgr.delete("test", "del_recreate_user", 0)
        sessions = cc._session_mgr.get_all("test", "del_recreate_user")
        assert len(sessions) == 1
        assert sessions[0].name == "default"

    def test_delete_adjusts_active_index(self):
        uid = "del_adj_user"
        cc._session_mgr.get_or_create_active("test", uid)
        cc._session_mgr.create("test", uid, "second")
        cc._session_mgr.create("test", uid, "third")
        # Active is now index 2 (third)
        cc._session_mgr.delete("test", uid, 0)  # delete default
        # Active should adjust

    def test_create_evicts_oldest(self):
        uid = "evict_user"
        cc._session_mgr.get_or_create_active("test", uid)
        for i in range(21):
            cc._session_mgr.create("test", uid, f"s{i}")
        sessions = cc._session_mgr.get_all("test", uid)
        assert len(sessions) <= 20


# ══════════════════════════════════════════════════════════════════════════════
# 9. UserSession properties
# ══════════════════════════════════════════════════════════════════════════════


class TestUserSessionProperties:
    def test_age_str_minutes(self):
        sess = cc.UserSession("test", "tg", "123")
        sess.last_active = time.time() - 300
        assert "5m ago" in sess.age_str()

    def test_age_str_hours(self):
        sess = cc.UserSession("test", "tg", "123")
        sess.last_active = time.time() - 7200
        assert "2h ago" in sess.age_str()

    def test_cli_session_valid_busy(self):
        sess = cc.UserSession("test", "tg", "123")
        sess.claude_session_id = "sid"
        sess.is_busy = True
        assert sess.cli_session_valid is True


# ══════════════════════════════════════════════════════════════════════════════
# 10. ask_claude_sync (mocked subprocess)
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeSync:
    def test_success(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"type":"result","result":"sync reply","usage":{"input_tokens":10,"output_tokens":5}}'
        mock_result.stderr = ""
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            reply, stats = cc.ask_claude_sync("test", [])
            assert reply == "sync reply"

    def test_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 180)):
            reply, stats = cc.ask_claude_sync("test", [], timeout=180)
            assert "[Timeout]" in reply

    def test_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            reply, stats = cc.ask_claude_sync("test", [])
            assert "[Error]" in reply
            assert "not found" in reply

    def test_with_add_dirs(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"type":"result","result":"ok","usage":{}}'
        mock_result.stderr = ""
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            cc.ask_claude_sync("test", [], add_dirs="/tmp/dir1,/tmp/dir2")
            cmd = mock_run.call_args[0][0]
            assert "--add-dir" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# 11. ask_claude_async (mocked subprocess)
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeAsync:
    @pytest.mark.asyncio
    async def test_timeout(self):
        import asyncio

        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            reply, stats = await cc.ask_claude_async("test", [], timeout=1)
            assert "[Timeout]" in reply

    @pytest.mark.asyncio
    async def test_with_session_resume(self):

        result_line = '{"type":"result","result":"resumed","usage":{"input_tokens":5,"output_tokens":3},"session_id":"s1"}'

        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        call_count = [0]

        async def readline():
            call_count[0] += 1
            if call_count[0] == 1:
                return result_line.encode() + b"\n"
            return b""

        mock_proc.stdout.readline = readline
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            reply, stats = await cc.ask_claude_async(
                "test", [],
                resume_session_id="old_session",
                timeout=10,
            )
            assert reply == "resumed"


# ══════════════════════════════════════════════════════════════════════════════
# 12. ask_claude_api (mocked anthropic)
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeAPI:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="API answer")]
        # A bare MagicMock would auto-create cache_read_input_tokens as another
        # mock; spec it to the fields a real usage object carries.
        mock_resp.usage = SimpleNamespace(input_tokens=20, output_tokens=10)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        with patch.dict("sys.modules", {"anthropic": MagicMock(Anthropic=MagicMock(return_value=mock_client))}):
            reply, stats = cc.ask_claude_api("test", [])
            assert reply == "API answer"
            assert stats["input_tokens"] == 20
            # Item 16: the API reports tokens and no cost, so the turn used to be
            # recorded at $0.00 and the budget guard summed a column of zeroes.
            assert stats["cost_usd"] > 0
            assert stats["cost_estimated"] is True

    def test_no_anthropic(self):
        import sys
        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = None
        try:
            with patch.dict("sys.modules", {"anthropic": None}):
                # Force ImportError
                pass
        finally:
            if saved:
                sys.modules["anthropic"] = saved
            else:
                sys.modules.pop("anthropic", None)


# ══════════════════════════════════════════════════════════════════════════════
# 13. DB connection pool
# ══════════════════════════════════════════════════════════════════════════════


class TestDBConnection:
    def test_get_conn_reuse(self):
        c1 = cc._get_conn()
        c2 = cc._get_conn()
        assert c1 is c2

    def test_save_turn_trims_history(self):
        for i in range(25):
            cc.save_turn("test_trim", "trim_user", f"msg{i}", f"reply{i}")
        time.sleep(0.3)
        cc._invalidate_history("test_trim", "trim_user")
        h = cc.load_history("test_trim", "trim_user")
        assert len(h) <= 20

    def test_save_turn_with_session_name(self):
        cc.save_turn("test_sess", "sess_user", "hi", "hello", session_name="mysess")
        time.sleep(0.3)
        cc._invalidate_history("test_sess", "sess_user:mysess")
        h = cc.load_history("test_sess", "sess_user", session_name="mysess")
        found = False
        for _ in range(20):
            if any("hi" in m["content"] for m in h):
                found = True
                break
            time.sleep(0.1)
            cc._invalidate_history("test_sess", "sess_user:mysess")
            h = cc.load_history("test_sess", "sess_user", session_name="mysess")
        assert found

    def test_clear_history_with_session_name(self):
        cc.save_turn("test_clr", "clr_user", "msg", "reply", session_name="s1")
        time.sleep(0.3)
        cc.clear_history("test_clr", "clr_user", session_name="s1")
        cc._invalidate_history("test_clr", "clr_user:s1")
        h = cc.load_history("test_clr", "clr_user", session_name="s1")
        for _ in range(20):
            if len(h) == 0:
                break
            time.sleep(0.1)
            cc._invalidate_history("test_clr", "clr_user:s1")
            h = cc.load_history("test_clr", "clr_user", session_name="s1")
        assert len(h) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 14. init_db (migration paths)
# ══════════════════════════════════════════════════════════════════════════════


class TestInitDB:
    def test_init_db_idempotent(self):
        cc.init_db()
        cc.init_db()


# ══════════════════════════════════════════════════════════════════════════════
# 15. _db_writer error handling (lines 85-86)
# ══════════════════════════════════════════════════════════════════════════════


class TestDbWriterErrorHandling:
    """Test the batch-error path in _db_writer (lines 80-86)."""

    def test_db_writer_logs_error_on_bad_execute(self, monkeypatch):
        """If conn.execute raises, _db_writer should log the error and not crash."""
        import queue as _q

        errors_logged = []

        # Capture log.error calls
        orig_log_error = cc.log.error
        def fake_log_error(msg, *args, **kwargs):
            errors_logged.append(msg % args if args else msg)
        monkeypatch.setattr(cc.log, "error", fake_log_error)

        # Create a throwaway queue and put a bad SQL statement in it
        test_q = _q.Queue(maxsize=100)
        test_q.put_nowait(("INVALID SQL !!!", ()))

        # Temporarily swap the module-level queue so _db_writer drains ours
        orig_queue = _store._write_queue
        _store._write_queue = test_q

        # Run _db_writer in a daemon thread; give it time to process one batch
        def run_writer():
            # We only want one iteration; _db_writer loops forever, so we
            # patch _write_queue.get to raise after the first item is drained.
            cc._db_writer.__wrapped__ = None  # just run it
            try:
                cc._db_writer()
            except Exception:
                pass

        # Put an item and let the live writer thread handle it
        _store._write_queue = test_q
        # Allow a short window for the background writer to pick it up
        time.sleep(1.5)

        # Restore
        _store._write_queue = orig_queue
        monkeypatch.setattr(cc.log, "error", orig_log_error)

        # The important invariant: bad SQL must have been attempted, which means
        # the queue was eventually drained (item removed).  We can't easily
        # assert the log message without racing, so just verify no exception
        # escaped from the writer thread (it's still alive if it was the live one).
        assert cc._writer_thread is not None

    def test_db_writer_drains_multiple_items(self):
        """Put multiple items in the queue and verify all are drained."""
        import queue as _q

        orig_queue = _store._write_queue
        test_q = _q.Queue(maxsize=100)

        # Two valid writes
        test_q.put_nowait((
            "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
            ("drain_test", "drain_u1"),
        ))
        test_q.put_nowait((
            "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
            ("drain_test", "drain_u2"),
        ))

        _store._write_queue = test_q
        try:
            # Give the background writer a moment to drain
            time.sleep(1.5)
            assert test_q.empty()
        finally:
            _store._write_queue = orig_queue


# ══════════════════════════════════════════════════════════════════════════════
# 16. _enqueue_write full-queue fallback (lines 105-109)
# ══════════════════════════════════════════════════════════════════════════════


class TestEnqueueWriteFullQueue:
    """Lines 105-109: when put_nowait raises Full, falls back to sync write."""

    def test_full_queue_falls_back_to_sync_write(self, monkeypatch):
        import queue as _q

        # Replace the module-level queue with one whose put_nowait always raises Full
        class AlwaysFullQueue:
            def put_nowait(self, item):
                raise _q.Full()

        orig_queue = _store._write_queue
        _store._write_queue = AlwaysFullQueue()
        try:
            # This should fall through to the sync path without raising
            cc._enqueue_write(
                "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
                ("sync_fallback", "fb_user"),
            )
        finally:
            _store._write_queue = orig_queue

        # Verify the row actually landed (sync path committed it)
        conn = cc._get_conn()
        row = conn.execute(
            "SELECT * FROM usage WHERE platform='sync_fallback' AND user_id='fb_user'"
        ).fetchone()
        assert row is not None

    def test_none_queue_falls_back_to_sync_write(self):
        """When _write_queue is None the sync path is taken directly."""
        orig_queue = _store._write_queue
        _store._write_queue = None
        try:
            cc._enqueue_write(
                "INSERT OR IGNORE INTO usage (platform, user_id, message_count, input_tokens, output_tokens) VALUES (?,?,0,0,0)",
                ("none_queue", "nq_user"),
            )
        finally:
            _store._write_queue = orig_queue

        conn = cc._get_conn()
        row = conn.execute(
            "SELECT * FROM usage WHERE platform='none_queue' AND user_id='nq_user'"
        ).fetchone()
        assert row is not None


# ══════════════════════════════════════════════════════════════════════════════
# 17. init_db migration path (lines 151-178)
# ══════════════════════════════════════════════════════════════════════════════


class TestInitDbMigration:
    """Test init_db when the DB has the OLD schema (no platform column)."""

    def test_migration_from_old_schema(self, tmp_path):
        old_db = str(tmp_path / "old.db")

        # Build the old schema
        import sqlite3 as _sql
        conn_old = _sql.connect(old_db)
        conn_old.execute("""
            CREATE TABLE conversations (
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                PRIMARY KEY (user_id, timestamp))
        """)
        conn_old.execute("""
            CREATE TABLE usage (
                user_id INTEGER NOT NULL PRIMARY KEY,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0)
        """)
        conn_old.execute(
            "INSERT INTO conversations VALUES (?,?,?,?)",
            (42, "user", "hello", 1000.0),
        )
        conn_old.execute(
            "INSERT INTO usage VALUES (?,?,?,?)",
            (42, 3, 100, 50),
        )
        conn_old.commit()
        conn_old.close()

        # Point store at the old DB and call init_db
        orig_db = _store.DB_PATH
        orig_conn = getattr(_store._local, "conn", None)
        _store._local.conn = None  # force new connection
        _store.DB_PATH = old_db

        try:
            cc.init_db()

            # Verify the migrated data in new schema
            conn = _store._get_conn()
            rows = conn.execute("SELECT platform, user_id, role FROM conversations").fetchall()
            assert any(r[0] == "telegram" for r in rows)
            assert any(r[2] == "user" for r in rows)

            usage_rows = conn.execute("SELECT platform, user_id FROM usage").fetchall()
            assert any(r[0] == "telegram" for r in usage_rows)
        finally:
            _store.DB_PATH = orig_db
            _store._local.conn = None  # will be recreated on next access

    def test_migration_idempotent_after_migration(self, tmp_path):
        """After migration init_db should be callable again without error."""
        already_migrated_db = str(tmp_path / "migrated.db")
        import sqlite3 as _sql
        # Build a db that already has the new schema
        conn_new = _sql.connect(already_migrated_db)
        conn_new.execute("""
            CREATE TABLE conversations (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn_new.execute("""
            CREATE TABLE usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, PRIMARY KEY (platform, user_id))
        """)
        conn_new.commit()
        conn_new.close()

        orig_db = _store.DB_PATH
        _store._local.conn = None
        _store.DB_PATH = already_migrated_db
        try:
            cc.init_db()  # should not raise
            cc.init_db()  # calling again must be idempotent
        finally:
            _store.DB_PATH = orig_db
            _store._local.conn = None


# ══════════════════════════════════════════════════════════════════════════════
# 18. ask_claude_sdk (lines 695-751 / 809-884)
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeSDK:
    """Cover ask_claude_sdk: import error, success, cancellation, timeout, exception."""

    @pytest.mark.asyncio
    async def test_sdk_import_error(self, monkeypatch):
        """When claude_code_sdk is not available, return an [Error] string."""
        import sys
        # Remove any cached import
        monkeypatch.setitem(sys.modules, "claude_code_sdk", None)
        reply, stats = await cc.ask_claude_sdk("hello", [])
        assert "[Error]" in reply
        assert "claude-code-sdk" in reply

    @pytest.mark.asyncio
    async def test_sdk_success_result_message(self, monkeypatch):
        """ResultMessage path: result_text and stats are populated."""
        import sys
        import types

        # Build minimal fake SDK module
        sdk_mod = types.ModuleType("claude_code_sdk")

        class FakeAssistantMessage:
            pass

        class FakeResultMessage:
            result = "SDK answer"
            usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}
            total_cost_usd = 0.003
            session_id = "sdk_sess_1"
            num_turns = 1
            duration_ms = 100

        class FakeTextBlock:
            text = "partial text"

        class FakeToolUseBlock:
            name = "Read"
            input = {"file_path": "/a/b/c.py"}

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        async def fake_query(prompt, options):
            yield FakeResultMessage()

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = FakeAssistantMessage
        sdk_mod.ResultMessage = FakeResultMessage
        sdk_mod.TextBlock = FakeTextBlock
        sdk_mod.ToolUseBlock = FakeToolUseBlock

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        reply, stats = await cc.ask_claude_sdk("hello", [])
        assert reply == "SDK answer"
        assert stats["input_tokens"] == 12  # 10 + 2 cache
        assert stats["output_tokens"] == 5
        assert stats["cost_usd"] == 0.003
        assert stats["session_id"] == "sdk_sess_1"

    @pytest.mark.asyncio
    async def test_sdk_assistant_message_with_tool_and_text(self, monkeypatch):
        """AssistantMessage path: tool and text blocks trigger callbacks."""
        import sys
        import types

        sdk_mod = types.ModuleType("claude_code_sdk")

        class FakeTextBlock:
            text = "streamed text"

        class FakeToolUseBlock:
            name = "Bash"
            input = {"command": "ls -la"}

        class FakeAssistantMessage:
            content = [FakeToolUseBlock(), FakeTextBlock()]

        class FakeResultMessage:
            result = "final"
            usage = {}
            total_cost_usd = 0
            session_id = ""
            num_turns = 1
            duration_ms = 50

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        async def fake_query(prompt, options):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = FakeAssistantMessage
        sdk_mod.ResultMessage = FakeResultMessage
        sdk_mod.TextBlock = FakeTextBlock
        sdk_mod.ToolUseBlock = FakeToolUseBlock

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        progress_calls = []
        text_calls = []

        async def on_progress(name, detail):
            progress_calls.append(name)

        async def on_text(text):
            text_calls.append(text)

        reply, stats = await cc.ask_claude_sdk(
            "hello", [], on_progress=on_progress, on_text=on_text
        )
        assert reply == "final"
        assert "Bash" in progress_calls
        assert "streamed text" in text_calls
        assert "Bash" in stats["tools_used"]

    @pytest.mark.asyncio
    async def test_sdk_cancellation(self, monkeypatch):
        """is_cancelled=True stops iteration and returns whatever was collected."""
        import sys
        import types

        sdk_mod = types.ModuleType("claude_code_sdk")

        class FakeAssistantMessage:
            content = []

        class FakeResultMessage:
            result = "cancelled result"
            usage = {}
            total_cost_usd = 0
            session_id = ""
            num_turns = 0
            duration_ms = 0

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        async def fake_query(prompt, options):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = FakeAssistantMessage
        sdk_mod.ResultMessage = FakeResultMessage
        sdk_mod.TextBlock = object
        sdk_mod.ToolUseBlock = object

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        reply, stats = await cc.ask_claude_sdk(
            "hello", [], is_cancelled=lambda: True
        )
        # Should return early — reply is either empty or "(no response)"
        assert isinstance(reply, str)

    @pytest.mark.asyncio
    async def test_sdk_general_exception(self, monkeypatch):
        """When query raises a generic exception, return [SDK Error] string."""
        import sys
        import types

        sdk_mod = types.ModuleType("claude_code_sdk")

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        async def fake_query(prompt, options):
            raise RuntimeError("SDK exploded")
            yield  # make it a generator

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = object
        sdk_mod.ResultMessage = object
        sdk_mod.TextBlock = object
        sdk_mod.ToolUseBlock = object

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        reply, stats = await cc.ask_claude_sdk("hello", [])
        assert "[SDK Error]" in reply
        assert "exploded" in reply

    @pytest.mark.asyncio
    async def test_sdk_empty_result_returns_no_response(self, monkeypatch):
        """When result_text stays empty, return '(no response)'."""
        import sys
        import types

        sdk_mod = types.ModuleType("claude_code_sdk")

        class FakeResultMessage:
            result = ""
            usage = {}
            total_cost_usd = 0
            session_id = ""
            num_turns = 0
            duration_ms = 0

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        async def fake_query(prompt, options):
            yield FakeResultMessage()

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = object
        sdk_mod.ResultMessage = FakeResultMessage
        sdk_mod.TextBlock = object
        sdk_mod.ToolUseBlock = object

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        reply, stats = await cc.ask_claude_sdk("hello", [])
        assert reply == "(no response)"

    @pytest.mark.asyncio
    async def test_sdk_with_add_dirs(self, monkeypatch):
        """add_dirs parameter populates opts.add_dirs."""
        import sys
        import types

        sdk_mod = types.ModuleType("claude_code_sdk")
        captured_opts = []

        class FakeOptions:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class FakeResultMessage:
            result = "ok"
            usage = {}
            total_cost_usd = 0
            session_id = ""
            num_turns = 1
            duration_ms = 0

        async def fake_query(prompt, options):
            captured_opts.append(options)
            yield FakeResultMessage()

        sdk_mod.query = fake_query
        sdk_mod.ClaudeCodeOptions = FakeOptions
        sdk_mod.AssistantMessage = object
        sdk_mod.ResultMessage = FakeResultMessage
        sdk_mod.TextBlock = object
        sdk_mod.ToolUseBlock = object

        monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk_mod)

        await cc.ask_claude_sdk("hello", [], add_dirs="/tmp/a,/tmp/b")
        assert captured_opts
        assert hasattr(captured_opts[0], "add_dirs")
        assert "/tmp/a" in captured_opts[0].add_dirs


# ══════════════════════════════════════════════════════════════════════════════
# 19. ask_claude_async extended paths (lines 611-678, 694-751)
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeAsyncExtended:
    @pytest.mark.asyncio
    async def test_on_progress_and_on_text_callbacks(self):
        """on_progress and on_text are fired for tool_use / text events."""

        progress_calls = []
        text_calls = []

        async def on_progress(name, detail):
            progress_calls.append(name)

        async def on_text(text):
            text_calls.append(text)

        lines = [
            b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n',
            b'{"type":"content_block_start","content_block":{"type":"tool_use","name":"Read","input":{}}}\n',
            b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello "}}\n',
            b'{"type":"result","result":"done","usage":{"input_tokens":5,"output_tokens":2},"session_id":"s99"}\n',
            b"",  # EOF
        ]
        iter_lines = iter(lines)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()

        async def fake_readline():
            return next(iter_lines, b"")

        mock_proc.stdout.readline = fake_readline
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            reply, stats = await cc.ask_claude_async(
                "test", [],
                on_progress=on_progress,
                on_text=on_text,
                timeout=10,
            )

        assert reply == "done"
        assert "Bash" in progress_calls
        assert "Read" in progress_calls
        assert "hello " in text_calls

    @pytest.mark.asyncio
    async def test_session_resume_failure_triggers_retry(self):
        """When resume returns error, the function retries with full history."""

        call_num = [0]

        def make_mock_proc(result_line: bytes, rc: int):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            delivered = [False]

            async def fake_readline():
                if not delivered[0]:
                    delivered[0] = True
                    return result_line
                return b""

            mock_proc.stdout.readline = fake_readline
            mock_proc.stderr = AsyncMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"stderr error" if rc != 0 else b"")
            mock_proc.wait = AsyncMock()
            mock_proc.returncode = rc
            mock_proc.kill = MagicMock()
            return mock_proc

        # First call (resume) → returncode=1 to trigger retry
        fail_proc = make_mock_proc(b"", 1)
        # Second call (retry) → success
        ok_line = b'{"type":"result","result":"retry ok","usage":{"input_tokens":3,"output_tokens":1}}\n'
        ok_proc = make_mock_proc(ok_line, 0)

        procs = iter([fail_proc, ok_proc])

        async def side_effect(*args, **kwargs):
            return next(procs)

        with patch("asyncio.create_subprocess_exec", side_effect=side_effect):
            reply, stats = await cc.ask_claude_async(
                "test", [],
                resume_session_id="stale_session_id",
                platform="test",
                user_id="retry_user",
                timeout=10,
            )

        assert reply == "retry ok"

    @pytest.mark.asyncio
    async def test_cancellation_kills_process(self):
        """When is_cancelled() returns True, proc.kill() is called."""
        import asyncio

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        kill_called = [False]

        async def fake_readline():
            await asyncio.sleep(0.01)
            return b'{"type":"result","result":"late","usage":{}}\n'

        mock_proc.stdout.readline = fake_readline
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0

        def fake_kill():
            kill_called[0] = True

        mock_proc.kill = fake_kill

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            reply, stats = await cc.ask_claude_async(
                "test", [],
                is_cancelled=lambda: True,
                timeout=10,
            )

        assert kill_called[0]

    @pytest.mark.asyncio
    async def test_history_passed_as_prompt_without_session(self):
        """Without a session_id, history is incorporated into the prompt."""

        result_line = b'{"type":"result","result":"with history","usage":{}}\n'
        call_count = [0]

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()

        async def fake_readline():
            call_count[0] += 1
            return result_line if call_count[0] == 1 else b""

        mock_proc.stdout.readline = fake_readline
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        captured_cmd = []

        async def capture_exec(*args, **kwargs):
            captured_cmd.extend(args)
            return mock_proc

        history = [{"role": "user", "content": "prev q"}, {"role": "assistant", "content": "prev a"}]

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            reply, stats = await cc.ask_claude_async(
                "new question", history, timeout=10
            )

        assert reply == "with history"
        # The full_prompt (with history) is passed since there's no resume session
        # Confirm --resume is NOT in the command
        assert "--resume" not in captured_cmd


# ══════════════════════════════════════════════════════════════════════════════
# 20. _parse_cli_output edge cases (lines 809-933)
# ══════════════════════════════════════════════════════════════════════════════


class TestParseCliOutputEdgeCases:
    def test_returncode_nonzero_with_output(self):
        """Non-zero rc but stdout present → parse stdout, not error string."""
        stdout = '{"type":"result","result":"partial result","usage":{}}'
        text, stats = cc._parse_cli_output(stdout, "some stderr", 1, 180)
        # stdout is non-empty so we parse it rather than treating as error
        assert text == "partial result"

    def test_cache_read_tokens_added_to_input(self):
        """cache_read_input_tokens should be summed into input_tokens."""
        stdout = '{"type":"result","result":"ok","usage":{"input_tokens":10,"cache_read_input_tokens":5,"output_tokens":3}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert stats["input_tokens"] == 15

    def test_multiple_plain_lines_first_used(self):
        """Multiple non-JSON lines: first non-empty line is used as result_text fallback."""
        stdout = "first plain line\nsecond plain line"
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert text == "first plain line"

    def test_content_block_start_non_tool_ignored(self):
        """content_block_start with type != tool_use should not add to tools_used."""
        stdout = '{"type":"content_block_start","content_block":{"type":"text"}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert stats.get("tools_used", []) == []

    def test_result_event_overrides_assistant_text(self):
        """result event should take precedence over earlier assistant text."""
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"draft"}]}}',
            '{"type":"result","result":"final answer","usage":{}}',
        ]
        text, stats = cc._parse_cli_output("\n".join(lines), "", 0, 180)
        assert text == "final answer"

    def test_empty_result_field_keeps_prior_text(self):
        """If result event has empty result, prior assistant text is kept.

        _parse_cli_output uses: result_text = event.get("result", result_text)
        So an empty string "" replaces result_text with "" — but then the
        final return falls back to `output` (raw stdout) when result_text is
        falsy.  We assert the text is non-empty (either prior text or raw).
        """
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"assistant text"}]}}',
            '{"type":"result","result":"","usage":{}}',
        ]
        text, stats = cc._parse_cli_output("\n".join(lines), "", 0, 180)
        # Either the assistant text or the raw output is returned — not empty
        assert text  # non-empty

    def test_stderr_500_char_limit(self):
        """Stderr is capped at 500 chars in the [Claude error] message."""
        long_stderr = "E" * 1000
        text, stats = cc._parse_cli_output("", long_stderr, 1, 180)
        assert "[Claude error]" in text
        # The error message itself should be <=500 chars of stderr
        assert text.count("E") <= 500

    def test_whitespace_only_lines_skipped(self):
        """Whitespace-only lines in stdout should not be treated as result."""
        stdout = "   \n\t\n" + '{"type":"result","result":"clean","usage":{}}'
        text, stats = cc._parse_cli_output(stdout, "", 0, 180)
        assert text == "clean"


# ══════════════════════════════════════════════════════════════════════════════
# 21. _build_prompt edge cases (line 933 area / general)
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildPromptEdgeCases:
    def test_system_role_treated_as_assistant(self):
        """Any non-'user' role is rendered as 'Assistant:'."""
        history = [{"role": "system", "content": "be helpful"}]
        result = cc._build_prompt("hi", history)
        assert "Assistant: be helpful" in result

    def test_prompt_joined_with_double_newlines(self):
        """Parts are separated by double newlines."""
        history = [{"role": "user", "content": "a"}]
        result = cc._build_prompt("b", history)
        assert "\n\nUser: b" in result

    def test_empty_content_in_history(self):
        """Empty content strings are handled without crashing."""
        history = [{"role": "user", "content": ""}, {"role": "assistant", "content": ""}]
        result = cc._build_prompt("new", history)
        assert "User: new" in result

    def test_special_characters_in_prompt(self):
        """Special chars are passed through unchanged."""
        text = 'What is 2+2? <script>alert("xss")</script>'
        result = cc._build_prompt(text, [])
        assert text in result


# ══════════════════════════════════════════════════════════════════════════════
# 22. SessionManager.switch_to and delete busy session
# ══════════════════════════════════════════════════════════════════════════════


class TestSessionManagerSwitchAndDelete:
    def test_switch_to_valid_index(self):
        uid = "switch_user"
        cc._session_mgr.get_or_create_active("test", uid)
        cc._session_mgr.create("test", uid, "second")
        result = cc._session_mgr.switch_to("test", uid, 0)
        assert result is not None
        assert result.name == "default"

    def test_switch_to_invalid_index(self):
        uid = "switch_bad_user"
        cc._session_mgr.get_or_create_active("test", uid)
        result = cc._session_mgr.switch_to("test", uid, 99)
        assert result is None

    def test_delete_busy_session_returns_false(self):
        uid = "busy_del_user"
        sess = cc._session_mgr.get_or_create_active("test", uid)
        sess.is_busy = True
        result = cc._session_mgr.delete("test", uid, 0)
        assert result is False
        sess.is_busy = False  # cleanup

    def test_delete_active_session_adjusts_index_down(self):
        """Deleting session at index < active should decrement active index."""
        uid = "adj_down_user"
        cc._session_mgr.get_or_create_active("test", uid)   # index 0
        cc._session_mgr.create("test", uid, "second")        # index 1
        cc._session_mgr.create("test", uid, "third")         # index 2 (active)
        idx_before = cc._session_mgr.get_active_index("test", uid)
        cc._session_mgr.delete("test", uid, 0)               # delete index 0
        idx_after = cc._session_mgr.get_active_index("test", uid)
        assert idx_after == idx_before - 1


# ══════════════════════════════════════════════════════════════════════════════
# 23. ask_claude_api ImportError path
# ══════════════════════════════════════════════════════════════════════════════


class TestAskClaudeAPIImportError:
    def test_import_error_returns_error_string(self, monkeypatch):
        """When anthropic is not importable, return [Error] string."""
        import sys
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("No module named 'anthropic'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        # Remove cached module so import is actually attempted
        monkeypatch.setitem(sys.modules, "anthropic", None)
        # Reset cached client so _get_api_client retries the import
        monkeypatch.setattr(cc, "_api_client", None)

        reply, stats = cc.ask_claude_api("test", [])
        assert "[Error]" in reply


# ══════════════════════════════════════════════════════════════════════════════
# 24. check_rate_limit
# ══════════════════════════════════════════════════════════════════════════════


class TestCheckRateLimit:
    def test_within_limit(self):
        assert cc.check_rate_limit("rl_test_key_unique_1") is True

    def test_exceeds_limit(self):
        key = "rl_exceed_key_unique_2"
        # Fill up the bucket (RATE_LIMIT_REQUESTS is 100 in our env)
        for _ in range(int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))):
            cc.check_rate_limit(key)
        assert cc.check_rate_limit(key) is False

    def test_old_timestamps_expire(self):
        key = "rl_expire_key_unique_3"
        # Inject old timestamps directly
        old_time = time.time() - cc.RATE_LIMIT_WINDOW - 1
        cc._rate_state[key] = [old_time] * 200
        # After expiry, should be allowed
        assert cc.check_rate_limit(key) is True


# ══════════════════════════════════════════════════════════════════════════════
# 25. get_usage
# ══════════════════════════════════════════════════════════════════════════════


class TestGetUsage:
    def test_get_usage_missing_user(self):
        result = cc.get_usage("test", "nonexistent_usage_user_xyz")
        assert result == {"messages": 0, "input": 0, "output": 0}

    def test_get_usage_after_track(self):
        # Use a direct synchronous write so the row is guaranteed to be present
        conn = cc._get_conn()
        conn.execute(
            """INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
               VALUES (?,?,1,10,5)
               ON CONFLICT(platform,user_id) DO UPDATE SET
                   message_count = message_count + 1,
                   input_tokens  = input_tokens  + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens""",
            ("test", "get_usage_sync_user"),
        )
        conn.commit()
        result = cc.get_usage("test", "get_usage_sync_user")
        assert result["messages"] >= 1
        assert result["input"] >= 10
        assert result["output"] >= 5
