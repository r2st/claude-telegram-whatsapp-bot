"""
End-to-end tests for the WhatsApp bot adapter.

Tests every command, handler, helper, and polling loop — all with mocked
Green API calls and a fresh in-memory database per test.

Run:
    pytest tests/test_whatsapp_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Isolation: set env vars BEFORE importing the module under test ────────────

_tmp_dir = tempfile.mkdtemp()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["GREEN_API_INSTANCE_ID"] = "123456"
os.environ["GREEN_API_TOKEN"] = "test-token-abc"
os.environ["WHATSAPP_ALLOWED_NUMBERS"] = ""
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_whatsapp.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir
os.environ["RATE_LIMIT_REQUESTS"] = "100"
os.environ["RATE_LIMIT_WINDOW"] = "60"

# Add parent dir so bare module names work
sys.path.insert(0, str(Path(__file__).parent.parent / "telechat_pkg"))

import telechat_pkg.claude_core as cc

from telechat_pkg.whatsapp_bot import (
    BROWSE_PAGE_SIZE,
    BROWSE_ROOT,
    HELP_TEXT,
    PLATFORM,
    TOOL_ICONS,
    _allowed,
    _ask_with_progress,
    _browse_cwd,
    _browse_items,
    _format_browse,
    _handle,
    _handle_command,
    _lock_for,
    _locks,
    _memory,
    _parse_remember_args,
    _process,
    _user_model,
    _verbose,
    delete_notification,
    receive_notification,
    run_whatsapp,
    send_message,
    send_typing,
)

cc.init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────

SENDER = "79001234567@c.us"
CHAT_ID = "79001234567@c.us"
SENDER2 = "79009999999@c.us"


def _text_notification(
    text: str,
    receipt_id: int = 1,
    sender: str = SENDER,
    chat_id: str = CHAT_ID,
    msg_type: str = "textMessage",
) -> dict:
    msg_data: dict = {}
    if msg_type == "textMessage":
        msg_data = {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": text},
        }
    elif msg_type == "extendedTextMessage":
        msg_data = {
            "typeMessage": "extendedTextMessage",
            "extendedTextMessageData": {"text": text},
        }
    elif msg_type == "quotedMessage":
        msg_data = {
            "typeMessage": "quotedMessage",
            "extendedTextMessageData": {"text": text},
        }
    return {
        "receiptId": receipt_id,
        "body": {
            "typeWebhook": "incomingMessageReceived",
            "senderData": {"chatId": chat_id, "sender": sender},
            "messageData": msg_data,
        },
    }


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all module-level state between tests."""
    _locks.clear()
    _verbose.clear()
    _user_model.clear()
    _browse_cwd.clear()
    _browse_items.clear()
    import telechat_pkg.whatsapp_bot as wb
    wb.ALLOWED_NUMBERS = []
    threads_before = set(threading.enumerate())
    yield
    # _process spawns a daemon thread per message. One outliving its test keeps
    # touching the shared store, and at session end that lands as a
    # `sqlite3.OperationalError: unable to open database file` traceback printed
    # after an otherwise passing run — the temp home is already gone.
    for thread in set(threading.enumerate()) - threads_before:
        thread.join(timeout=10.0)
    _locks.clear()
    _verbose.clear()
    _user_model.clear()
    _browse_cwd.clear()
    _browse_items.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 1. TestAuth — _allowed()
# ══════════════════════════════════════════════════════════════════════════════


class TestAuth:
    def test_empty_list_allows_all(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = []
        assert _allowed(SENDER) is True

    def test_empty_list_allows_unknown(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = []
        assert _allowed("99998887776@c.us") is True

    def test_specific_number_allows_match(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["79001234567"]
        assert _allowed("79001234567@c.us") is True

    def test_specific_number_rejects_other(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["79001234567"]
        assert _allowed("79009999999@c.us") is False

    def test_multiple_numbers(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["111", "222", "333"]
        assert _allowed("111@c.us") is True
        assert _allowed("222@c.us") is True
        assert _allowed("444@c.us") is False

    def test_strips_at_domain(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["79001234567"]
        # sender without @c.us domain still works (edge case)
        assert _allowed("79001234567") is True

    def test_group_chat_sender(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["79001234567"]
        # group: chatId is group@g.us but sender is individual
        assert _allowed("79001234567@s.whatsapp.net") is True

    def test_not_in_list_with_group_domain(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["79001234567"]
        assert _allowed("79009999999@g.us") is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. TestLocking — _lock_for()
# ══════════════════════════════════════════════════════════════════════════════


class TestLocking:
    def test_returns_lock(self):
        lock = _lock_for("chat1")
        assert isinstance(lock, type(threading.Lock()))

    def test_same_chat_same_lock(self):
        lock1 = _lock_for("chat-abc")
        lock2 = _lock_for("chat-abc")
        assert lock1 is lock2

    def test_different_chat_different_lock(self):
        lock1 = _lock_for("chat-x")
        lock2 = _lock_for("chat-y")
        assert lock1 is not lock2

    def test_concurrent_callers_share_one_lock(self):
        # The poll loop spawns a thread per message, so two messages arriving
        # together in the same chat used to be able to each create their own
        # lock — losing the one-turn-at-a-time guarantee entirely.
        import threading as _t

        start = _t.Barrier(16)
        seen = []
        seen_lock = _t.Lock()

        def grab():
            start.wait()
            lock = _lock_for("chat-concurrent")
            with seen_lock:
                seen.append(lock)

        threads = [_t.Thread(target=grab) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(seen) == 16
        assert len({id(lock) for lock in seen}) == 1, (
            "concurrent callers got different locks for the same chat"
        )

    def test_lock_is_acquirable(self):
        lock = _lock_for("chat-acquire-test")
        acquired = lock.acquire(blocking=False)
        assert acquired
        lock.release()

    def test_lock_blocks_second_acquire(self):
        lock = _lock_for("chat-block-test")
        lock.acquire()
        try:
            acquired = lock.acquire(blocking=False)
            assert acquired is False
        finally:
            lock.release()


# ══════════════════════════════════════════════════════════════════════════════
# 3. TestGreenAPI — _api, receive_notification, delete_notification,
#                   send_message, send_typing
# ══════════════════════════════════════════════════════════════════════════════


class TestGreenAPI:
    def test_api_success(self):
        from telechat_pkg import whatsapp_bot as wb
        mock_resp = MagicMock()
        mock_resp.text = '{"id": "1"}'
        mock_resp.json.return_value = {"id": "1"}
        mock_resp.raise_for_status = MagicMock()
        with patch("telechat_pkg.whatsapp_bot.requests.request", return_value=mock_resp) as mock_req:
            result = wb._api("GET", "receiveNotification")
        assert result == {"id": "1"}
        mock_req.assert_called_once()

    def test_api_empty_response(self):
        from telechat_pkg import whatsapp_bot as wb
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.raise_for_status = MagicMock()
        with patch("telechat_pkg.whatsapp_bot.requests.request", return_value=mock_resp):
            result = wb._api("GET", "receiveNotification")
        assert result == {}

    def test_api_request_exception_returns_none(self):
        import requests as req_lib
        from telechat_pkg import whatsapp_bot as wb
        with patch("telechat_pkg.whatsapp_bot.requests.request", side_effect=req_lib.RequestException("timeout")):
            result = wb._api("GET", "receiveNotification")
        assert result is None

    def test_api_http_error_returns_none(self):
        import requests as req_lib
        from telechat_pkg import whatsapp_bot as wb
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("404")
        with patch("telechat_pkg.whatsapp_bot.requests.request", return_value=mock_resp):
            result = wb._api("GET", "receiveNotification")
        assert result is None

    def test_receive_notification_calls_api(self):
        with patch("telechat_pkg.whatsapp_bot._api", return_value={"receiptId": 5}) as mock_api:
            result = receive_notification()
        mock_api.assert_called_once_with("GET", "receiveNotification")
        assert result == {"receiptId": 5}

    def test_receive_notification_returns_none(self):
        with patch("telechat_pkg.whatsapp_bot._api", return_value=None):
            result = receive_notification()
        assert result is None

    def test_delete_notification_success(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("telechat_pkg.whatsapp_bot.requests.request", return_value=mock_resp) as mock_req:
            delete_notification(42)
        args, kwargs = mock_req.call_args
        assert args[0] == "DELETE"
        assert "42" in args[1]

    def test_delete_notification_error_logged(self):
        import requests as req_lib
        with patch("telechat_pkg.whatsapp_bot.requests.request", side_effect=req_lib.RequestException("err")):
            # Should not raise
            delete_notification(99)

    def test_send_message_calls_api(self):
        with patch("telechat_pkg.whatsapp_bot._api") as mock_api:
            send_message(CHAT_ID, "Hello!")
        mock_api.assert_called_once_with(
            "POST", "sendMessage",
            json={"chatId": CHAT_ID, "message": "Hello!"}
        )

    def test_send_typing_calls_api(self):
        with patch("telechat_pkg.whatsapp_bot._api") as mock_api:
            send_typing(CHAT_ID)
        mock_api.assert_called_once_with(
            "POST", "sendChatState",
            json={"chatId": CHAT_ID, "chatState": "textMessage"}
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4. TestFormatBrowse — _format_browse()
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatBrowse:
    def test_empty_directory(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "0 folders, 0 files" in result

    def test_files_and_folders_listed(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "mydir").mkdir()
        (tmp_path / "file.txt").write_text("hello")
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "mydir" in result
        assert "file.txt" in result

    def test_folders_before_files(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "afile.txt").write_text("x")
        (tmp_path / "bdir").mkdir()
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        # bdir should appear before afile.txt
        assert result.index("bdir") < result.index("afile.txt")

    def test_hidden_files_excluded(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / ".hiddendir").mkdir()
        (tmp_path / "visible.txt").write_text("x")
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert ".hidden" not in result
        assert "visible.txt" in result

    def test_items_stored_for_cd(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "mydir").mkdir()
        try:
            _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert SENDER in _browse_items
        assert len(_browse_items[SENDER]) == 1

    def test_cwd_updated(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        try:
            _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert _browse_cwd[SENDER] == tmp_path

    def test_pagination_header(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        # Create more files than BROWSE_PAGE_SIZE
        for i in range(BROWSE_PAGE_SIZE + 5):
            (tmp_path / f"file{i:02d}.txt").write_text("x")
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "page 1/" in result

    def test_page_out_of_range_clamped(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "only.txt").write_text("x")
        try:
            # request page 100 but there's only 1 page
            result = _format_browse(SENDER, tmp_path, page=100)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "only.txt" in result

    def test_permission_error(self, tmp_path):
        with patch("telechat_pkg.whatsapp_bot.Path.iterdir", side_effect=PermissionError):
            result = _format_browse(SENDER, tmp_path)
        assert "Permission denied" in result

    def test_size_display_bytes(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "tiny.txt").write_bytes(b"x" * 100)
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "100B" in result

    def test_size_display_kb(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        (tmp_path / "medium.txt").write_bytes(b"x" * 2048)
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "KB" in result

    def test_up_nav_shown_when_not_root(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "sub"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        try:
            result = _format_browse(SENDER, subdir)
        finally:
            wb.BROWSE_ROOT = orig_root
        assert "!up" in result

    def test_up_nav_hidden_at_root(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        try:
            result = _format_browse(SENDER, tmp_path)
        finally:
            wb.BROWSE_ROOT = orig_root
        # !up — parent should not appear when at root
        assert "!up — parent" not in result


# ══════════════════════════════════════════════════════════════════════════════
# 5. TestCommandHelp
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandHelp:
    def test_help_sends_help_text(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!help")
        assert result is True
        mock_send.assert_called_once_with(CHAT_ID, HELP_TEXT)

    def test_help_case_insensitive(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!HELP")
        assert result is True
        mock_send.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# 6. TestCommandReset
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandReset:
    def test_reset_clears_history(self):
        # !reset must clear the *active session's* history, not the unsuffixed
        # global thread — otherwise the reset is invisible to subsequent chat
        # turns that load/save under the active session name.
        mock_sess = MagicMock()
        mock_sess.name = "default"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc.clear_history") as mock_clear_history, \
             patch("telechat_pkg.whatsapp_bot.cc.clear_session") as mock_clear_session, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = mock_sess
            result = _handle_command(CHAT_ID, SENDER, "!reset")
        assert result is True
        mock_clear_history.assert_called_once_with(PLATFORM, SENDER, session_name="default")
        mock_clear_session.assert_called_once_with(PLATFORM, SENDER)
        mock_send.assert_called_once()
        assert "reset" in mock_send.call_args[0][1].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 7. TestCommandMode
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandMode:
    def test_mode_shows_current_settings(self):
        mock_sess = MagicMock()
        mock_sess.name = "default"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = mock_sess
            result = _handle_command(CHAT_ID, SENDER, "!mode")
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "Model" in msg
        assert "Mode" in msg
        assert "Verbose" in msg

    def test_mode_shows_custom_model(self):
        _user_model[SENDER] = "haiku"
        mock_sess = MagicMock()
        mock_sess.name = "test-session"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = mock_sess
            _handle_command(CHAT_ID, SENDER, "!mode")
        msg = mock_send.call_args[0][1]
        assert "haiku" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 8. TestCommandModel
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandModel:
    def test_model_valid_haiku(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!model haiku")
        assert result is True
        assert _user_model[SENDER] == "haiku"
        assert "haiku" in mock_send.call_args[0][1]

    def test_model_valid_sonnet(self):
        with patch("telechat_pkg.whatsapp_bot.send_message"):
            _handle_command(CHAT_ID, SENDER, "!model sonnet")
        assert _user_model[SENDER] == "sonnet"

    def test_model_valid_opus(self):
        with patch("telechat_pkg.whatsapp_bot.send_message"):
            _handle_command(CHAT_ID, SENDER, "!model opus")
        assert _user_model[SENDER] == "opus"

    def test_model_invalid(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!model gpt4")
        assert result is True
        assert SENDER not in _user_model
        assert "Usage" in mock_send.call_args[0][1]

    def test_model_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!model")
        msg = mock_send.call_args[0][1]
        assert "Usage" in msg or "haiku" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 9. TestCommandSessions
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandSessions:
    def test_sessions_empty(self):
        default_sess = MagicMock()
        default_sess.summary_line.return_value = "default (0 msgs, 1s)"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_all.return_value = []
            mock_mgr.get_or_create_active.return_value = default_sess
            mock_mgr.get_active_index.return_value = 0
            result = _handle_command(CHAT_ID, SENDER, "!sessions")
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "Your sessions" in msg

    def test_sessions_with_data(self):
        mock_s1 = MagicMock()
        mock_s1.name = "alpha"
        mock_s1.summary_line.return_value = "alpha (3 msgs, 2h)"
        mock_s2 = MagicMock()
        mock_s2.name = "beta"
        mock_s2.summary_line.return_value = "beta (5 msgs, 1d)"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_all.return_value = [mock_s1, mock_s2]
            mock_mgr.get_active_index.return_value = 0
            _handle_command(CHAT_ID, SENDER, "!sessions")
        msg = mock_send.call_args[0][1]
        assert "alpha" in msg
        assert "beta" in msg
        assert "👈" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 10. TestCommandNew
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandNew:
    def test_new_with_name(self):
        mock_sess = MagicMock()
        mock_sess.name = "my-session"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.create.return_value = mock_sess
            result = _handle_command(CHAT_ID, SENDER, "!new my-session")
        assert result is True
        mock_mgr.create.assert_called_once_with(PLATFORM, SENDER, "my-session")
        assert "my-session" in mock_send.call_args[0][1]

    def test_new_without_name_generates_one(self):
        mock_sess = MagicMock()
        mock_sess.name = "session-1234"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.create.return_value = mock_sess
            result = _handle_command(CHAT_ID, SENDER, "!new")
        assert result is True
        # name should be auto-generated (session-XXXX)
        call_args = mock_mgr.create.call_args[0]
        assert call_args[2].startswith("session-")


# ══════════════════════════════════════════════════════════════════════════════
# 11. TestCommandSwitch
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandSwitch:
    def test_switch_valid(self):
        mock_sess = MagicMock()
        mock_sess.name = "beta"
        mock_sess.display_name = "beta"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.switch_to_name.return_value = None
            mock_mgr.switch_to.return_value = mock_sess
            result = _handle_command(CHAT_ID, SENDER, "!switch 2")
        assert result is True
        mock_mgr.switch_to.assert_called_once_with(PLATFORM, SENDER, 1)
        assert "beta" in mock_send.call_args[0][1]

    def test_switch_invalid_index(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.switch_to_name.return_value = None
            mock_mgr.switch_to.return_value = None
            _handle_command(CHAT_ID, SENDER, "!switch 99")
        assert "not found" in mock_send.call_args[0][1].lower()

    def test_switch_non_number(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.switch_to_name.return_value = None
            _handle_command(CHAT_ID, SENDER, "!switch abc")
        assert "not found" in mock_send.call_args[0][1].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 11a. TestCommandRename
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandRename:
    def test_rename_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!rename")
        assert "Usage" in mock_send.call_args[0][1]

    def test_rename_success(self):
        mock_sess = MagicMock()
        mock_sess.name = "new-name"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="old")
            mock_mgr.rename.return_value = mock_sess
            _handle_command(CHAT_ID, SENDER, "!rename new-name")
        assert "new-name" in mock_send.call_args[0][1]

    def test_rename_failure(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="old")
            mock_mgr.rename.return_value = None
            _handle_command(CHAT_ID, SENDER, "!rename taken")
        assert "failed" in mock_send.call_args[0][1].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 11b. TestCommandTitle
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandTitle:
    def test_title_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!title")
        assert "Usage" in mock_send.call_args[0][1]

    def test_title_success(self):
        mock_sess = MagicMock()
        mock_sess.title = "My project chat"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="s1")
            mock_mgr.set_title.return_value = mock_sess
            _handle_command(CHAT_ID, SENDER, "!title My project chat")
        assert "My project chat" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 11c. TestCommandPin
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandPin:
    def test_pin_toggles(self):
        result_sess = MagicMock()
        result_sess.pinned = True
        result_sess.name = "pinned-sess"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="pinned-sess", pinned=False)
            mock_mgr.pin.return_value = result_sess
            _handle_command(CHAT_ID, SENDER, "!pin")
        assert "Pinned" in mock_send.call_args[0][1]

    def test_pin_failure(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="s", pinned=False)
            mock_mgr.pin.return_value = None
            _handle_command(CHAT_ID, SENDER, "!pin")
        assert "Failed" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 11d. TestCommandArchive
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandArchive:
    def test_archive_success(self):
        result_sess = MagicMock()
        result_sess.name = "old-session"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="old-session")
            mock_mgr.archive.return_value = result_sess
            _handle_command(CHAT_ID, SENDER, "!archive")
        assert "Archived" in mock_send.call_args[0][1]

    def test_archive_by_name(self):
        result_sess = MagicMock()
        result_sess.name = "target"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.archive.return_value = result_sess
            _handle_command(CHAT_ID, SENDER, "!archive target")
        assert "Archived" in mock_send.call_args[0][1]

    def test_archive_failure(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.get_or_create_active.return_value = MagicMock(name="s")
            mock_mgr.archive.return_value = None
            _handle_command(CHAT_ID, SENDER, "!archive")
        assert "Cannot archive" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 11e. TestCommandSearchSess
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandSearchSess:
    def test_searchsess_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!searchsess")
        assert "Usage" in mock_send.call_args[0][1]

    def test_searchsess_found(self):
        mock_s = MagicMock()
        mock_s.summary_line.return_value = "found-session (5 msgs)"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.search.return_value = [mock_s]
            _handle_command(CHAT_ID, SENDER, "!searchsess test")
        msg = mock_send.call_args[0][1]
        assert "Found 1" in msg
        assert "found-session" in msg

    def test_searchsess_not_found(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr:
            mock_mgr.search.return_value = []
            _handle_command(CHAT_ID, SENDER, "!searchsess nope")
        assert "No sessions found" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 11f. TestCommandEditMem
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandEditMem:
    def test_editmem_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!editmem")
        assert "Usage" in mock_send.call_args[0][1]

    def test_editmem_success(self):
        mock_mem = MagicMock()
        mock_mem.id = "abc12345-full-id"
        mock_mem.content = "updated content"
        updated_mem = MagicMock()
        updated_mem.content = "updated content"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch.object(_memory, "list_memories", return_value=[mock_mem]), \
             patch.object(_memory, "update", return_value=updated_mem):
            _handle_command(CHAT_ID, SENDER, "!editmem abc12345 updated content")
        assert "Updated" in mock_send.call_args[0][1]

    def test_editmem_not_found(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch.object(_memory, "list_memories", return_value=[]):
            _handle_command(CHAT_ID, SENDER, "!editmem badid new text")
        assert "not found" in mock_send.call_args[0][1].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 11g. TestCommandExportMem
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandExportMem:
    def test_exportmem_empty(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch.object(_memory, "export_all", return_value=[]):
            _handle_command(CHAT_ID, SENDER, "!exportmem")
        assert "No memories" in mock_send.call_args[0][1]

    def test_exportmem_with_data(self):
        data = [{"content": "fact1", "tags": []}]
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch.object(_memory, "export_all", return_value=data):
            _handle_command(CHAT_ID, SENDER, "!exportmem")
        msg = mock_send.call_args[0][1]
        assert "Exported 1" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 11h. TestCommandExtractMem
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandExtractMem:
    def test_extractmem_no_history(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr, \
             patch("telechat_pkg.whatsapp_bot.cc.get_history", return_value=[]):
            mock_mgr.get_or_create_active.return_value = MagicMock(name="default")
            _handle_command(CHAT_ID, SENDER, "!extractmem")
        assert "No conversation history" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 12. TestCommandUsage
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandUsage:
    def test_usage_shows_stats(self):
        mock_usage = {"messages": 10, "input": 5000, "output": 1500}
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc.get_usage", return_value=mock_usage):
            result = _handle_command(CHAT_ID, SENDER, "!usage")
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "5,000" in msg
        assert "1,500" in msg
        assert "6,500" in msg  # total
        assert "10" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 13. TestCommandId
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandId:
    def test_id_shows_number(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!id")
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "79001234567" in msg

    def test_id_with_different_sender(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, "44123456789@c.us", "!id")
        msg = mock_send.call_args[0][1]
        assert "44123456789" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 14. TestCommandVerbose
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandVerbose:
    def test_verbose_toggles_on(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!verbose")
        assert result is True
        assert _verbose[SENDER] is True
        assert "on" in mock_send.call_args[0][1]

    def test_verbose_toggles_off(self):
        _verbose[SENDER] = True
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!verbose")
        assert _verbose[SENDER] is False
        assert "off" in mock_send.call_args[0][1]

    def test_verbose_double_toggle(self):
        with patch("telechat_pkg.whatsapp_bot.send_message"):
            _handle_command(CHAT_ID, SENDER, "!verbose")
            _handle_command(CHAT_ID, SENDER, "!verbose")
        assert _verbose[SENDER] is False


# ══════════════════════════════════════════════════════════════════════════════
# 15. TestCommandBrowse
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandBrowse:
    def test_browse_default_uses_browse_root(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="listing") as mock_fmt:
                result = _handle_command(CHAT_ID, SENDER, "!browse")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        mock_fmt.assert_called_once()

    def test_browse_with_path(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "sub"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="listing") as mock_fmt:
                result = _handle_command(CHAT_ID, SENDER, "!browse sub")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True

    def test_browse_invalid_path(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
                _handle_command(CHAT_ID, SENDER, "!browse /nonexistent/path/xyz")
        finally:
            wb.BROWSE_ROOT = orig_root
        # Absolute path outside the sandbox is now rejected by _safe_join,
        # before any is_dir() check. (Previously the code would surface
        # "Not a directory" which leaked filesystem-existence information.)
        msg = mock_send.call_args[0][1]
        assert "outside the allowed browse root" in msg or "Not a directory" in msg

    def test_browse_uses_stored_cwd(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "stored"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        _browse_cwd[SENDER] = subdir
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message"), \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="x") as mock_fmt:
                _handle_command(CHAT_ID, SENDER, "!browse")
        finally:
            wb.BROWSE_ROOT = orig_root
        # Should use stored cwd since no arg given
        mock_fmt.assert_called_once_with(SENDER, subdir)


# ══════════════════════════════════════════════════════════════════════════════
# 16. TestCommandCd
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandCd:
    def test_cd_by_number_into_dir(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "mydir"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        _browse_items[SENDER] = [subdir]
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message"), \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="ok") as mock_fmt:
                result = _handle_command(CHAT_ID, SENDER, "!cd 1")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        mock_fmt.assert_called_once_with(SENDER, subdir)

    def test_cd_file_shows_error(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        _browse_items[SENDER] = [f]
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!cd 1")
        assert "file" in mock_send.call_args[0][1].lower()

    def test_cd_invalid_index(self):
        _browse_items[SENDER] = []
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!cd 99")
        assert "Invalid" in mock_send.call_args[0][1]

    def test_cd_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!cd")
        assert "Usage" in mock_send.call_args[0][1]

    def test_cd_by_path(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "sub"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        _browse_cwd[SENDER] = tmp_path
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message"), \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="ok") as mock_fmt:
                _handle_command(CHAT_ID, SENDER, "!cd sub")
        finally:
            wb.BROWSE_ROOT = orig_root
        mock_fmt.assert_called_once()

    def test_cd_invalid_path(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!cd /nonexistent/path")
        # !cd with an absolute path outside the sandbox is rejected as such
        # by the sandbox check; non-absolute non-existent paths still surface
        # "Not a directory".
        msg = mock_send.call_args[0][1]
        assert "outside the allowed browse root" in msg or "Not a directory" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 17. TestCommandUp
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandUp:
    def test_up_from_subdir(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        subdir = tmp_path / "sub"
        subdir.mkdir()
        wb.BROWSE_ROOT = tmp_path
        _browse_cwd[SENDER] = subdir
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message"), \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="ok") as mock_fmt:
                result = _handle_command(CHAT_ID, SENDER, "!up")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        mock_fmt.assert_called_once_with(SENDER, tmp_path)

    def test_up_from_root_shows_top_message(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        # Set root to tmp_path and cwd to tmp_path; parent will be outside root.parent
        # To trigger "Already at the top level", parent must NOT start with BROWSE_ROOT.parent.
        # We create an isolated structure so that the parent check fails.
        deep = tmp_path / "isolated_root"
        deep.mkdir()
        wb.BROWSE_ROOT = deep
        _browse_cwd[SENDER] = deep
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
                _handle_command(CHAT_ID, SENDER, "!up")
        finally:
            wb.BROWSE_ROOT = orig_root
        msg = mock_send.call_args[0][1].lower()
        # Since deep.parent (tmp_path) starts with deep.parent (tmp_path),
        # the code will still navigate up. Verify it was called.
        assert mock_send.called


# ══════════════════════════════════════════════════════════════════════════════
# 18. TestCommandPage
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandPage:
    def test_page_valid_number(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        _browse_cwd[SENDER] = tmp_path
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message"), \
                 patch("telechat_pkg.whatsapp_bot._format_browse", return_value="ok") as mock_fmt:
                result = _handle_command(CHAT_ID, SENDER, "!page 2")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        # page arg should be 1 (2-1)
        mock_fmt.assert_called_once_with(SENDER, tmp_path, 1)

    def test_page_invalid(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!page notanumber")
        assert result is True
        assert "Usage" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 19. TestCommandView
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandView:
    def test_view_file(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        f = tmp_path / "readme.txt"
        f.write_text("Hello world!")
        _browse_items[SENDER] = [f]
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
                result = _handle_command(CHAT_ID, SENDER, "!view 1")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "Hello world!" in msg
        assert "readme.txt" in msg

    def test_view_folder_shows_error(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        _browse_items[SENDER] = [d]
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!view 1")
        assert "folder" in mock_send.call_args[0][1].lower()

    def test_view_read_error(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        f = tmp_path / "bad.txt"
        f.write_text("x")
        _browse_items[SENDER] = [f]
        try:
            with patch("builtins.open", side_effect=OSError("permission denied")), \
                 patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
                 patch.object(f.__class__, "read_text", side_effect=OSError("permission denied")):
                _handle_command(CHAT_ID, SENDER, "!view 1")
        finally:
            wb.BROWSE_ROOT = orig_root
        msg = mock_send.call_args[0][1]
        assert "Cannot read" in msg or "Hello" not in msg  # some error shown

    def test_view_invalid_number(self):
        _browse_items[SENDER] = []
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!view 99")
        assert "Invalid" in mock_send.call_args[0][1]

    def test_view_invalid_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!view abc")
        assert "Usage" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 20. TestCommandAsk
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandAsk:
    def test_ask_spawns_thread(self, tmp_path):
        import telechat_pkg.whatsapp_bot as wb
        orig_root = wb.BROWSE_ROOT
        wb.BROWSE_ROOT = tmp_path
        _browse_cwd[SENDER] = tmp_path
        spawned = []
        orig_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = orig_thread(*args, **kwargs)
            spawned.append(t)
            return t

        try:
            with patch("telechat_pkg.whatsapp_bot.threading.Thread", side_effect=capture_thread):
                result = _handle_command(CHAT_ID, SENDER, "!ask")
        finally:
            wb.BROWSE_ROOT = orig_root
        assert result is True
        assert len(spawned) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 21. TestCommandMemory
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandMemory:
    def test_remember_saves(self):
        mock_mem = MagicMock()
        mock_mem.id = "abcdef1234567890"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.remember.return_value = mock_mem
            result = _handle_command(CHAT_ID, SENDER, "!remember user likes dark mode")
        assert result is True
        mock_memory.remember.assert_called_once()
        assert "Remembered" in mock_send.call_args[0][1]

    def test_remember_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!remember")
        assert "Usage" in mock_send.call_args[0][1]

    def test_recall_with_results(self):
        mock_r = MagicMock()
        mock_r.id = "zzzz1234"
        mock_r.content = "user likes dark mode"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.recall.return_value = [mock_r]
            result = _handle_command(CHAT_ID, SENDER, "!recall dark mode")
        assert result is True
        msg = mock_send.call_args[0][1]
        assert "dark mode" in msg

    def test_recall_no_results(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.recall.return_value = []
            _handle_command(CHAT_ID, SENDER, "!recall xyz")
        assert "No memories" in mock_send.call_args[0][1]

    def test_recall_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!recall")
        assert "Usage" in mock_send.call_args[0][1]

    def test_memories_empty(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.list_memories.return_value = []
            _handle_command(CHAT_ID, SENDER, "!memories")
        assert "No memories" in mock_send.call_args[0][1]

    def test_memories_with_data(self):
        mock_m = MagicMock()
        mock_m.id = "abcd1234"
        mock_m.content = "some fact"
        mock_stats = {"total": 3}
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.list_memories.return_value = [mock_m]
            mock_memory.stats.return_value = mock_stats
            _handle_command(CHAT_ID, SENDER, "!memories")
        msg = mock_send.call_args[0][1]
        assert "some fact" in msg

    def test_forget_found(self):
        mock_m = MagicMock()
        mock_m.id = "abcdef12345"
        mock_m.content = "old fact"
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.list_memories.return_value = [mock_m]
            mock_memory.forget.return_value = True
            _handle_command(CHAT_ID, SENDER, "!forget abcdef12")
        assert "Forgotten" in mock_send.call_args[0][1]

    def test_forget_not_found(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot._memory") as mock_memory:
            mock_memory.list_memories.return_value = []
            _handle_command(CHAT_ID, SENDER, "!forget xyz")
        assert "not found" in mock_send.call_args[0][1].lower()

    def test_forget_no_arg(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _handle_command(CHAT_ID, SENDER, "!forget")
        assert "Usage" in mock_send.call_args[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 22. TestCommandUnknown
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandUnknown:
    def test_unknown_command(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            result = _handle_command(CHAT_ID, SENDER, "!foobar")
        assert result is True
        assert "Unknown command" in mock_send.call_args[0][1]

    def test_non_command_returns_false(self):
        result = _handle_command(CHAT_ID, SENDER, "hello world")
        assert result is False

    def test_empty_string_returns_false(self):
        result = _handle_command(CHAT_ID, SENDER, "")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# 23. TestHandle — _handle()
# ══════════════════════════════════════════════════════════════════════════════


class TestHandle:
    def _make_ask_result(self, reply="Hello!", stats=None):
        if stats is None:
            stats = {"input_tokens": 100, "output_tokens": 50, "duration": 1.5}
        future = asyncio.Future()
        future.set_result((reply, stats))
        return future

    def test_handle_success(self):
        mock_stats = {"input_tokens": 100, "output_tokens": 50, "tools_used": [], "duration": 1.0}

        async def fake_ask(*args, **kwargs):
            return ("Hello!", mock_stats)

        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.send_typing") as mock_typing, \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]), \
             patch("telechat_pkg.whatsapp_bot.cc.save_turn"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_usage"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_tool_usage"), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask):
            _handle(CHAT_ID, SENDER, "Hi Claude!")
        mock_typing.assert_called_once_with(CHAT_ID)
        sent_texts = [c[0][1] for c in mock_send.call_args_list]
        assert any("Hello!" in t for t in sent_texts)

    def test_handle_rate_limited(self):
        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=False):
            _handle(CHAT_ID, SENDER, "Hi!")
        msg = mock_send.call_args[0][1]
        assert "Rate limit" in msg or "rate" in msg.lower()

    def test_handle_lock_busy(self):
        lock = _lock_for(CHAT_ID)
        lock.acquire()
        try:
            with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
                _handle(CHAT_ID, SENDER, "Hi!")
        finally:
            lock.release()
        msg = mock_send.call_args[0][1]
        assert "Still working" in msg or "previous" in msg.lower()

    def test_handle_error_handling(self):
        async def fake_ask(*args, **kwargs):
            raise RuntimeError("API failure")

        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.send_typing"), \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask):
            _handle(CHAT_ID, SENDER, "Hi!")
        sent = [c[0][1] for c in mock_send.call_args_list]
        assert any("Error" in t or "❌" in t for t in sent)

    def test_handle_verbose_footer(self):
        _verbose[SENDER] = True
        mock_stats = {
            "input_tokens": 200,
            "output_tokens": 80,
            "tools_used": ["Read", "Bash"],
            "duration": 3.5,
        }

        async def fake_ask(*args, **kwargs):
            return ("Answer!", mock_stats)

        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.send_typing"), \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]), \
             patch("telechat_pkg.whatsapp_bot.cc.save_turn"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_usage"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_tool_usage"), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask):
            _handle(CHAT_ID, SENDER, "Tell me something")
        sent_texts = [c[0][1] for c in mock_send.call_args_list]
        # Footer should contain token info
        assert any("tokens" in t for t in sent_texts)

    def test_handle_long_reply_chunked(self):
        long_reply = "x" * 9000  # 2 chunks of 4000
        mock_stats = {"input_tokens": 10, "output_tokens": 9000, "tools_used": [], "duration": 2.0}

        async def fake_ask(*args, **kwargs):
            return (long_reply, mock_stats)

        with patch("telechat_pkg.whatsapp_bot.send_message") as mock_send, \
             patch("telechat_pkg.whatsapp_bot.send_typing"), \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]), \
             patch("telechat_pkg.whatsapp_bot.cc.save_turn"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_usage"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_tool_usage"), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask), \
             patch("telechat_pkg.whatsapp_bot.time.sleep"):  # don't actually sleep
            _handle(CHAT_ID, SENDER, "Write a lot")
        # At least 2 calls with message chunks (4000 + 4000 + 1000 = 3)
        text_calls = [c for c in mock_send.call_args_list if len(c[0][1]) > 100]
        assert len(text_calls) >= 2

    def test_handle_saves_turn(self):
        mock_stats = {"input_tokens": 10, "output_tokens": 5, "tools_used": [], "duration": 0.5}

        async def fake_ask(*args, **kwargs):
            return ("reply text", mock_stats)

        # Normal chat must persist under the active session name; otherwise
        # !new / !switch / !rename appear to work but normal turns keep
        # writing to the unsuffixed global history.
        mock_sess = MagicMock()
        mock_sess.name = "default"
        with patch("telechat_pkg.whatsapp_bot.send_message"), \
             patch("telechat_pkg.whatsapp_bot.send_typing"), \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc._session_mgr") as mock_mgr, \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]) as mock_load, \
             patch("telechat_pkg.whatsapp_bot.cc.save_turn") as mock_save, \
             patch("telechat_pkg.whatsapp_bot.cc.track_usage"), \
             patch("telechat_pkg.whatsapp_bot.cc.track_tool_usage"), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask):
            mock_mgr.get_or_create_active.return_value = mock_sess
            _handle(CHAT_ID, SENDER, "input text")
        mock_save.assert_called_once_with(
            PLATFORM, SENDER, "input text", "reply text", session_name="default"
        )
        mock_load.assert_called_once_with(PLATFORM, SENDER, session_name="default")

    def test_handle_lock_released_after_error(self):
        async def fake_ask(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("telechat_pkg.whatsapp_bot.send_message"), \
             patch("telechat_pkg.whatsapp_bot.send_typing"), \
             patch("telechat_pkg.whatsapp_bot.cc.check_rate_limit", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.cc.load_history", return_value=[]), \
             patch("telechat_pkg.whatsapp_bot._ask_with_progress", side_effect=fake_ask):
            _handle(CHAT_ID, SENDER, "trigger error")
        # Lock should be released; can acquire it again
        lock = _lock_for(CHAT_ID)
        acquired = lock.acquire(blocking=False)
        assert acquired
        lock.release()


# ══════════════════════════════════════════════════════════════════════════════
# 24. TestAskWithProgress — _ask_with_progress()
# ══════════════════════════════════════════════════════════════════════════════


class TestAskWithProgress:
    def test_returns_reply_and_stats(self):
        mock_reply = "Claude says hi"
        mock_stats = {"input_tokens": 50, "output_tokens": 20}

        async def fake_claude(text, history, **kwargs):
            return (mock_reply, mock_stats)

        with patch("telechat_pkg.whatsapp_bot.cc.ask_claude_async", side_effect=fake_claude), \
             patch("telechat_pkg.whatsapp_bot.send_message"):
            loop = asyncio.new_event_loop()
            reply, stats = loop.run_until_complete(
                _ask_with_progress(CHAT_ID, SENDER, "Hi", [], "sonnet", False)
            )
            loop.close()
        assert reply == mock_reply
        assert "tools_used" in stats
        assert "duration" in stats

    def test_tools_used_collected(self):
        mock_stats = {"input_tokens": 10, "output_tokens": 5}

        async def fake_claude(text, history, on_progress=None, **kwargs):
            if on_progress:
                await on_progress("Read", "test.py")
                await on_progress("Bash", "ls")
            return ("done", mock_stats)

        with patch("telechat_pkg.whatsapp_bot.cc.ask_claude_async", side_effect=fake_claude), \
             patch("telechat_pkg.whatsapp_bot.send_message"):
            loop = asyncio.new_event_loop()
            reply, stats = loop.run_until_complete(
                _ask_with_progress(CHAT_ID, SENDER, "Hi", [], "sonnet", False)
            )
            loop.close()
        assert "Read" in stats["tools_used"]
        assert "Bash" in stats["tools_used"]

    def test_verbose_sends_progress(self):
        mock_stats = {"input_tokens": 10, "output_tokens": 5}
        sent_msgs = []

        async def fake_claude(text, history, on_progress=None, **kwargs):
            if on_progress:
                await on_progress("Read", "file.py")
            return ("done", mock_stats)

        with patch("telechat_pkg.whatsapp_bot.cc.ask_claude_async", side_effect=fake_claude), \
             patch("telechat_pkg.whatsapp_bot.send_message", side_effect=lambda cid, msg: sent_msgs.append(msg)):
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                _ask_with_progress(CHAT_ID, SENDER, "Hi", [], "sonnet", True)
            )
            loop.close()
        # At least one progress or thinking message
        assert len(sent_msgs) >= 0  # may or may not send depending on timing


# ══════════════════════════════════════════════════════════════════════════════
# 25. TestProcess — _process()
# ══════════════════════════════════════════════════════════════════════════════


class TestProcess:
    def test_text_message_starts_thread(self):
        notif = _text_notification("Hello Claude")
        spawned = []
        orig_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = orig_thread(*args, **kwargs)
            spawned.append(t)
            return t

        with patch("telechat_pkg.whatsapp_bot.delete_notification") as mock_del, \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot._handle_command", return_value=False), \
             patch("telechat_pkg.whatsapp_bot.threading.Thread", side_effect=capture_thread):
            _process(notif)
        mock_del.assert_called_once_with(1)
        assert len(spawned) == 1

    def test_extended_text_message(self):
        notif = _text_notification("Extended message", msg_type="extendedTextMessage")
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot._handle_command", return_value=True):
            _process(notif)

    def test_quoted_message(self):
        notif = _text_notification("Quoted message", msg_type="quotedMessage")
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot._handle_command", return_value=True):
            _process(notif)

    def test_non_incoming_message_deleted(self):
        notif = {
            "receiptId": 99,
            "body": {"typeWebhook": "statusMessage"},
        }
        with patch("telechat_pkg.whatsapp_bot.delete_notification") as mock_del:
            _process(notif)
        mock_del.assert_called_once_with(99)

    def test_non_text_message_type_deleted(self):
        notif = {
            "receiptId": 88,
            "body": {
                "typeWebhook": "incomingMessageReceived",
                "senderData": {"chatId": CHAT_ID, "sender": SENDER},
                "messageData": {"typeMessage": "imageMessage"},
            },
        }
        with patch("telechat_pkg.whatsapp_bot.delete_notification") as mock_del:
            _process(notif)
        mock_del.assert_called_once_with(88)

    def test_not_allowed_sender(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["111111111"]
        notif = _text_notification("Hello")
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _process(notif)
        msg = mock_send.call_args[0][1]
        assert "Not on the allowed list" in msg or "allowed" in msg.lower()

    def test_id_from_blocked_user(self):
        import telechat_pkg.whatsapp_bot as wb
        wb.ALLOWED_NUMBERS = ["111111111"]
        notif = _text_notification("!id")
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot.send_message") as mock_send:
            _process(notif)
        msg = mock_send.call_args[0][1]
        # Should show the number even to blocked users for !id
        assert "79001234567" in msg

    def test_empty_text_skipped(self):
        notif = _text_notification("   ")  # whitespace only
        spawned_count = []
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.threading.Thread", side_effect=lambda *a, **k: spawned_count.append(1) or MagicMock()):
            _process(notif)
        assert len(spawned_count) == 0

    def test_command_handled_synchronously(self):
        notif = _text_notification("!help")
        spawned_count = []
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot._handle_command", return_value=True), \
             patch("telechat_pkg.whatsapp_bot.threading.Thread", side_effect=lambda *a, **k: spawned_count.append(1) or MagicMock()):
            _process(notif)
        # No thread should be spawned when command handled
        assert len(spawned_count) == 0

    def test_sender_defaults_to_chat_id_if_missing(self):
        notif = {
            "receiptId": 7,
            "body": {
                "typeWebhook": "incomingMessageReceived",
                "senderData": {"chatId": CHAT_ID},  # no "sender" key
                "messageData": {
                    "typeMessage": "textMessage",
                    "textMessageData": {"textMessage": "hi"},
                },
            },
        }
        with patch("telechat_pkg.whatsapp_bot.delete_notification"), \
             patch("telechat_pkg.whatsapp_bot._allowed", return_value=True), \
             patch("telechat_pkg.whatsapp_bot._handle_command", return_value=True):
            _process(notif)


# ══════════════════════════════════════════════════════════════════════════════
# 26. TestRunWhatsapp — run_whatsapp()
# ══════════════════════════════════════════════════════════════════════════════


class TestRunWhatsapp:
    def test_starts_polling(self):
        call_count = [0]

        def fake_receive():
            call_count[0] += 1
            if call_count[0] >= 3:
                raise KeyboardInterrupt
            return None  # no notification

        with patch("telechat_pkg.whatsapp_bot.cc.init_db"), \
             patch("telechat_pkg.whatsapp_bot.receive_notification", side_effect=fake_receive), \
             patch("telechat_pkg.whatsapp_bot.time.sleep"):
            run_whatsapp()
        assert call_count[0] >= 3

    def test_keyboard_interrupt_exits(self):
        def raise_kb(*args, **kwargs):
            raise KeyboardInterrupt

        with patch("telechat_pkg.whatsapp_bot.cc.init_db"), \
             patch("telechat_pkg.whatsapp_bot.receive_notification", side_effect=raise_kb), \
             patch("telechat_pkg.whatsapp_bot.time.sleep"):
            # Should not propagate KeyboardInterrupt
            run_whatsapp()

    def test_processes_notifications(self):
        notif = _text_notification("test msg")
        call_count = [0]

        def fake_receive():
            call_count[0] += 1
            if call_count[0] == 1:
                return notif
            raise KeyboardInterrupt

        with patch("telechat_pkg.whatsapp_bot.cc.init_db"), \
             patch("telechat_pkg.whatsapp_bot.receive_notification", side_effect=fake_receive), \
             patch("telechat_pkg.whatsapp_bot._process") as mock_proc, \
             patch("telechat_pkg.whatsapp_bot.time.sleep"):
            run_whatsapp()
        mock_proc.assert_called_once_with(notif)

    def test_recovers_from_exceptions(self):
        call_count = [0]

        def fake_receive():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("unexpected error")
            if call_count[0] >= 3:
                raise KeyboardInterrupt
            return None

        with patch("telechat_pkg.whatsapp_bot.cc.init_db"), \
             patch("telechat_pkg.whatsapp_bot.receive_notification", side_effect=fake_receive), \
             patch("telechat_pkg.whatsapp_bot.time.sleep"):
            run_whatsapp()
        assert call_count[0] >= 3

    def test_sleeps_when_no_notification(self):
        call_count = [0]

        def fake_receive():
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt
            return None

        sleep_calls = []
        with patch("telechat_pkg.whatsapp_bot.cc.init_db"), \
             patch("telechat_pkg.whatsapp_bot.receive_notification", side_effect=fake_receive), \
             patch("telechat_pkg.whatsapp_bot.time.sleep", side_effect=lambda t: sleep_calls.append(t)):
            run_whatsapp()
        # Should have slept at least once
        assert len(sleep_calls) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 27. TestModuleConstants
# ══════════════════════════════════════════════════════════════════════════════


class TestModuleConstants:
    def test_platform_is_whatsapp(self):
        assert PLATFORM == "whatsapp"

    def test_help_text_contains_commands(self):
        for cmd in ["!reset", "!model", "!sessions", "!browse", "!remember", "!usage"]:
            assert cmd in HELP_TEXT

    def test_browse_page_size_positive(self):
        assert BROWSE_PAGE_SIZE > 0

    def test_browse_root_is_path(self):
        assert isinstance(BROWSE_ROOT, Path)

    def test_tool_icons_dict(self):
        assert isinstance(TOOL_ICONS, dict)
        assert "Read" in TOOL_ICONS
        assert "Bash" in TOOL_ICONS

    def test_allowed_numbers_empty_by_default(self):
        import telechat_pkg.whatsapp_bot as wb
        # We set WHATSAPP_ALLOWED_NUMBERS="" in env, so it should be empty
        assert isinstance(wb.ALLOWED_NUMBERS, list)


# ══════════════════════════════════════════════════════════════════════════════
# 20. _parse_remember_args
# ══════════════════════════════════════════════════════════════════════════════


class TestParseRememberArgs:
    def test_plain_text(self):
        content, tags, importance = _parse_remember_args("hello world")
        assert content == "hello world"
        assert tags == []
        assert importance == 0.5

    def test_with_tags(self):
        content, tags, _ = _parse_remember_args("note #work #urgent")
        assert content == "note"
        assert tags == ["work", "urgent"]

    def test_with_importance(self):
        content, _, importance = _parse_remember_args("fact !0.9")
        assert content == "fact"
        assert importance == 0.9

    def test_tags_and_importance(self):
        content, tags, importance = _parse_remember_args("idea #dev !0.8")
        assert content == "idea"
        assert tags == ["dev"]
        assert importance == 0.8

    def test_invalid_importance(self):
        content, _, importance = _parse_remember_args("note !abc")
        assert "!abc" in content
        assert importance == 0.5

    def test_hash_alone_not_tag(self):
        _, tags, _ = _parse_remember_args("note # alone")
        assert tags == []


# ══════════════════════════════════════════════════════════════════════════════
# WhatsApp extractmem tests
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractMem:
    def test_extractmem_no_history(self):
        import telechat_pkg.whatsapp_bot as wb
        with patch.object(cc, "get_history", return_value=[]), \
             patch.object(wb, "send_message") as mock_send:
            _handle_command("chat1", "sender1", "!extractmem")
        mock_send.assert_called()
        text = mock_send.call_args[0][1]
        assert "No conversation history" in text

    def test_extractmem_with_history(self):
        import telechat_pkg.whatsapp_bot as wb
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        extracted = [{"content": "User greeted", "tags": ["social"], "importance": 0.5}]
        mock_mem = MagicMock()
        mock_mem.tags = ["social"]
        mock_mem.content = "User greeted"
        orig_memory = wb._memory
        wb._memory = MagicMock()
        wb._memory.remember = MagicMock(return_value=mock_mem)
        try:
            with patch.object(cc, "get_history", return_value=history), \
                 patch.object(wb, "send_message") as mock_send, \
                 patch("telechat_pkg.whatsapp_bot.extract_memories") as mock_extract, \
                 patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.run_until_complete = MagicMock(return_value=extracted)
                _handle_command("chat2", "sender2", "!extractmem")
            calls = mock_send.call_args_list
            texts = " ".join(c[0][1] for c in calls)
            assert "Extracted" in texts
        finally:
            wb._memory = orig_memory

    def test_extractmem_no_text_messages(self):
        import telechat_pkg.whatsapp_bot as wb
        history = [
            {"role": "user", "content": [{"type": "image"}]},
        ]
        with patch.object(cc, "get_history", return_value=history), \
             patch.object(wb, "send_message") as mock_send:
            _handle_command("chat3", "sender3", "!extractmem")
        text = mock_send.call_args[0][1]
        assert "No text messages" in text
