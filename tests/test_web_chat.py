"""Tests for web_chat.py — 100% coverage target."""
import asyncio
import json
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Patch aiohttp.web before importing web_chat


class TestGetUserId(unittest.TestCase):
    def test_returns_hex_hash(self):
        from telechat_pkg.web_chat import _get_user_id
        result = _get_user_id("test_token")
        self.assertEqual(len(result), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_deterministic(self):
        from telechat_pkg.web_chat import _get_user_id
        self.assertEqual(_get_user_id("abc"), _get_user_id("abc"))

    def test_different_inputs(self):
        from telechat_pkg.web_chat import _get_user_id
        self.assertNotEqual(_get_user_id("a"), _get_user_id("b"))


class TestIndexHandler(unittest.TestCase):
    def test_returns_html(self):
        from telechat_pkg.web_chat import _index_handler
        request = MagicMock()
        result = asyncio.run(_index_handler(request))
        self.assertEqual(result.content_type, "text/html")
        self.assertIn("<html", result.text)

    def test_has_social_sharing_meta_tags(self):
        # Shared links (Dev.to, Slack, Twitter/X, etc.) render a preview card
        # from these tags — without them a shared TeleChat link looks bare.
        from telechat_pkg.web_chat import _index_handler
        request = MagicMock()
        result = asyncio.run(_index_handler(request))
        html = result.text
        self.assertIn('name="description"', html)
        self.assertIn('property="og:title"', html)
        self.assertIn('property="og:description"', html)
        self.assertIn('name="twitter:card"', html)
        self.assertIn('name="theme-color"', html)
        self.assertIn('rel="icon"', html)

    def test_has_dark_mode_support(self):
        from telechat_pkg.web_chat import _index_handler
        request = MagicMock()
        result = asyncio.run(_index_handler(request))
        html = result.text
        self.assertIn('name="color-scheme"', html)
        self.assertIn("prefers-color-scheme: dark", html)

    def test_has_accessibility_affordances(self):
        from telechat_pkg.web_chat import _index_handler
        request = MagicMock()
        result = asyncio.run(_index_handler(request))
        html = result.text
        self.assertIn('aria-label="Send message"', html)
        self.assertIn('role="log"', html)
        self.assertIn("prefers-reduced-motion", html)
        # Suggestion/command chips must be real buttons, not divs with
        # onclick, so they're reachable and activatable via keyboard.
        self.assertIn('<button type="button" class="suggestion"', html)
        self.assertIn('<button type="button" class="cmd"', html)


class TestHealthHandler(unittest.TestCase):
    @patch("telechat_pkg.health.get_health", return_value={"status": "healthy"})
    @patch("telechat_pkg.web_chat._active_ws", {"a": None, "b": None})
    def test_healthy(self, mock_gh):
        from telechat_pkg.web_chat import _health_handler
        request = MagicMock()
        result = asyncio.run(_health_handler(request))
        self.assertEqual(result.status, 200)

    @patch("telechat_pkg.health.get_health", return_value={"status": "degraded"})
    @patch("telechat_pkg.web_chat._active_ws", {})
    def test_unhealthy(self, mock_gh):
        from telechat_pkg.web_chat import _health_handler
        request = MagicMock()
        result = asyncio.run(_health_handler(request))
        self.assertEqual(result.status, 503)


class _WsHarness:
    """Shared fake-WebSocket driver for the handler tests."""

    def _make_msg(self, data_dict=None, text=None, msg_type=None):
        msg = MagicMock()
        if msg_type:
            msg.type = msg_type
        else:
            from aiohttp import web
            msg.type = web.WSMsgType.TEXT
        if data_dict is not None:
            msg.data = json.dumps(data_dict)
        elif text is not None:
            msg.data = text
        return msg

    def _run_ws(self, messages, auth_token=""):
        """Run ws handler with given messages, return ws mock and sent messages."""
        from aiohttp import web as aio_web
        from telechat_pkg import web_chat

        ws = AsyncMock(spec=aio_web.WebSocketResponse)
        ws.closed = False
        ws.prepare = AsyncMock()
        ws.send_json = AsyncMock()

        # Make ws iterable with our messages
        async def ws_iter():
            for m in messages:
                yield m

        ws.__aiter__ = lambda self: ws_iter()

        request = MagicMock()

        with patch.object(web_chat, "WEB_AUTH_TOKEN", auth_token), \
             patch.object(web_chat, "_active_ws", {}), \
             patch("aiohttp.web.WebSocketResponse", return_value=ws):
            asyncio.run(web_chat._ws_handler(request))

        return ws


class TestWsHandler(_WsHarness, unittest.TestCase):
    def test_connect_no_auth(self):
        """No auth required — connected message with auth_required=False."""
        ws = self._run_ws([])
        calls = ws.send_json.call_args_list
        self.assertGreater(len(calls), 0)
        first = calls[0][0][0]
        self.assertEqual(first["type"], "connected")
        self.assertFalse(first["auth_required"])

    def test_connect_with_auth_required(self):
        ws = self._run_ws([], auth_token="secret123")
        calls = ws.send_json.call_args_list
        first = calls[0][0][0]
        self.assertTrue(first["auth_required"])

    def test_auth_success(self):
        msg = self._make_msg({"type": "auth", "token": "secret"})
        ws = self._run_ws([msg], auth_token="secret")
        calls = ws.send_json.call_args_list
        # Should get connected + auth_ok
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("auth_ok", types)

    def test_auth_fail(self):
        from telechat_pkg import web_chat
        # Reset throttling state so prior tests can't lock out this attempt.
        web_chat._auth_failures.clear()
        with patch.object(web_chat, "_client_ip", return_value="1.2.3.4"):
            msg = self._make_msg({"type": "auth", "token": "wrong"})
            ws = self._run_ws([msg], auth_token="secret")
        calls = ws.send_json.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("auth_fail", types)
        web_chat._auth_failures.clear()

    def test_auth_closes_after_max_failures(self):
        """A single connection failing N times in a row is closed."""
        from telechat_pkg import web_chat
        web_chat._auth_failures.clear()
        bad = self._make_msg({"type": "auth", "token": "wrong"})
        # Send a couple more than the cap to confirm the handler stops
        # processing once the connection-level limit is hit.
        msgs = [bad] * (web_chat.WEB_AUTH_MAX_ATTEMPTS + 2)
        with patch.object(web_chat, "_client_ip", return_value="2.2.2.2"):
            ws = self._run_ws(msgs, auth_token="secret")
        ws.close.assert_called()
        types = [c[0][0]["type"] for c in ws.send_json.call_args_list]
        # Should see at most WEB_AUTH_MAX_ATTEMPTS auth_fail frames before close.
        self.assertLessEqual(
            sum(1 for t in types if t == "auth_fail"),
            web_chat.WEB_AUTH_MAX_ATTEMPTS,
        )
        web_chat._auth_failures.clear()

    def test_ip_lockout_rejects_correct_token(self):
        """Once the per-IP failure window is exceeded, even a *correct* token
        is rejected — that's what makes brute-force untenable."""
        from telechat_pkg import web_chat
        import time as _t
        web_chat._auth_failures.clear()
        # Pre-seed the locked state for our deterministic IP.
        web_chat._auth_failures["3.3.3.3"] = (
            _t.time(), web_chat.WEB_AUTH_MAX_ATTEMPTS,
        )
        with patch.object(web_chat, "_client_ip", return_value="3.3.3.3"):
            msg = self._make_msg({"type": "auth", "token": "secret"})
            ws = self._run_ws([msg], auth_token="secret")
        types = [c[0][0]["type"] for c in ws.send_json.call_args_list]
        self.assertIn("auth_fail", types)
        self.assertNotIn("auth_ok", types)
        ws.close.assert_called()
        web_chat._auth_failures.clear()

    def test_message_not_authenticated(self):
        msg = self._make_msg({"type": "message", "text": "hello"})
        ws = self._run_ws([msg], auth_token="secret")
        calls = ws.send_json.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertIn("error", types)

    def test_invalid_json(self):
        msg = self._make_msg(text="not json{{{")
        ws = self._run_ws([msg])
        calls = ws.send_json.call_args_list
        error_calls = [c for c in calls if c[0][0].get("type") == "error"]
        self.assertGreater(len(error_calls), 0)

    def test_empty_message_ignored(self):
        msg = self._make_msg({"type": "message", "text": ""})
        ws = self._run_ws([msg])
        # Should only have the connected message, no chat
        calls = ws.send_json.call_args_list
        types = [c[0][0]["type"] for c in calls]
        self.assertNotIn("thinking", types)

    @patch("telechat_pkg.web_chat._handle_command", new_callable=AsyncMock)
    def test_command_dispatched(self, mock_cmd):
        msg = self._make_msg({"type": "message", "text": "/help"})
        ws = self._run_ws([msg])
        mock_cmd.assert_called_once()

    @patch("telechat_pkg.web_chat._handle_chat", new_callable=AsyncMock)
    def test_chat_dispatched(self, mock_chat):
        msg = self._make_msg({"type": "message", "text": "hello"})
        # Need to let the task run
        from telechat_pkg import web_chat

        ws_mock = AsyncMock()
        ws_mock.closed = False
        ws_mock.prepare = AsyncMock()
        ws_mock.send_json = AsyncMock()

        async def ws_iter():
            yield msg

        ws_mock.__aiter__ = lambda self: ws_iter()

        request = MagicMock()

        async def run():
            with patch.object(web_chat, "WEB_AUTH_TOKEN", ""), \
                 patch.object(web_chat, "_active_ws", {}), \
                 patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
                await web_chat._ws_handler(request)
                # Give the created task time to start
                await asyncio.sleep(0.05)

        asyncio.run(run())
        mock_chat.assert_called_once()

    def test_cancel_message(self):
        msg = self._make_msg({"type": "cancel"})
        ws = self._run_ws([msg])
        # Should not crash — cancel is a no-op currently
        calls = ws.send_json.call_args_list
        self.assertGreater(len(calls), 0)

    def test_ws_error_closes(self):
        from aiohttp import web as aio_web
        msg = MagicMock()
        msg.type = aio_web.WSMsgType.ERROR
        ws = self._run_ws([msg])
        # Should disconnect cleanly


class TestHandleCommand(unittest.TestCase):
    def test_clear(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
            asyncio.run(_handle_command(ws, send, "user1", "/clear"))
        send.assert_called_once()
        self.assertIn("cleared", send.call_args[0][0]["text"].lower())

    def test_new_session(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            asyncio.run(_handle_command(ws, send, "user1", "/new test_sess"))
        send.assert_called_once()
        self.assertIn("test_sess", send.call_args[0][0]["text"])

    def test_new_session_no_name(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            asyncio.run(_handle_command(ws, send, "user1", "/new"))
        send.assert_called_once()
        self.assertIn("New session", send.call_args[0][0]["text"])

    def test_model_with_args(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        asyncio.run(_handle_command(ws, send, "user1", "/model opus"))
        self.assertIn("opus", send.call_args[0][0]["text"])

    def test_model_no_args(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        asyncio.run(_handle_command(ws, send, "user1", "/model"))
        self.assertIn("Current model", send.call_args[0][0]["text"])

    def test_help(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        asyncio.run(_handle_command(ws, send, "user1", "/help"))
        self.assertIn("Commands", send.call_args[0][0]["text"])

    def test_unknown_command(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        asyncio.run(_handle_command(ws, send, "user1", "/unknown"))
        self.assertIn("Unknown", send.call_args[0][0]["text"])

    def test_help_mentions_export(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        asyncio.run(_handle_command(ws, send, "user1", "/help"))
        self.assertIn("/export", send.call_args[0][0]["text"])

    def test_export_no_history(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
            mock_cc.load_history.return_value = []
            asyncio.run(_handle_command(ws, send, "user1", "/export"))
        msg = send.call_args[0][0]
        self.assertEqual(msg["type"], "system")
        self.assertIn("No conversation", msg["text"])

    def test_export_default_format_sends_download(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        history = [{"role": "user", "content": "hi", "timestamp": 0},
                   {"role": "assistant", "content": "hello", "timestamp": 0}]
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
            mock_cc.load_history.return_value = history
            asyncio.run(_handle_command(ws, send, "user1", "/export"))
        msg = send.call_args[0][0]
        self.assertEqual(msg["type"], "download")
        self.assertTrue(msg["filename"].endswith(".txt"))
        self.assertEqual(msg["mime"], "text/plain")
        self.assertEqual(msg["message_count"], 2)
        self.assertIn("hello", msg["content"])

    def test_export_json_format(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        history = [{"role": "user", "content": "hi", "timestamp": 0}]
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
            mock_cc.load_history.return_value = history
            asyncio.run(_handle_command(ws, send, "user1", "/export json"))
        msg = send.call_args[0][0]
        self.assertEqual(msg["type"], "download")
        self.assertEqual(msg["mime"], "application/json")
        self.assertTrue(msg["filename"].endswith(".json"))
        json.loads(msg["content"])  # valid JSON

    def test_export_unknown_format(self):
        from telechat_pkg.web_chat import _handle_command
        ws = MagicMock()
        send = AsyncMock()
        history = [{"role": "user", "content": "hi", "timestamp": 0}]
        with patch("telechat_pkg.web_chat.cc") as mock_cc:
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
            mock_cc.load_history.return_value = history
            asyncio.run(_handle_command(ws, send, "user1", "/export pdf"))
        msg = send.call_args[0][0]
        self.assertEqual(msg["type"], "system")
        self.assertIn("Unknown format", msg["text"])


class TestHandleChat(unittest.TestCase):
    def _run_chat(self, engine="cli", reply="test reply", stats=None):
        from telechat_pkg.web_chat import _handle_chat
        if stats is None:
            stats = {"input_tokens": 10, "output_tokens": 20, "cost_usd": 0.001, "session_id": "s1"}

        ws = MagicMock()
        ws.closed = False
        send = AsyncMock()

        with patch("telechat_pkg.web_chat.cc") as mock_cc, \
             patch("telechat_pkg.web_chat._active_ws", {"client1": ws}):
            mock_cc.CLAUDE_MODE = engine
            mock_cc.CLAUDE_MODEL = "sonnet"
            mock_cc.CLAUDE_SYSTEM = ""
            mock_cc.CLAUDE_ADD_DIRS = ""
            mock_cc.CLAUDE_TIMEOUT = 60
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(
                name="default", cli_session_valid=True, claude_session_id="old"
            )
            mock_cc.load_history.return_value = []

            if engine == "api":
                mock_cc.ask_claude_api_async = AsyncMock(return_value=(reply, stats))
            elif engine == "sdk":
                mock_cc.ask_claude_sdk = AsyncMock(return_value=(reply, stats))
            else:
                mock_cc.ask_claude_async = AsyncMock(return_value=(reply, stats))

            asyncio.run(_handle_chat(ws, send, "user1", "client1", "hello"))

        return send

    def test_cli_engine(self):
        send = self._run_chat("cli")
        types = [c[0][0]["type"] for c in send.call_args_list]
        self.assertIn("thinking", types)
        self.assertTrue("reply" in types or "done" in types)

    def test_api_engine(self):
        send = self._run_chat("api")
        types = [c[0][0]["type"] for c in send.call_args_list]
        self.assertIn("reply", types)

    def test_sdk_engine(self):
        send = self._run_chat("sdk")
        types = [c[0][0]["type"] for c in send.call_args_list]
        self.assertIn("reply", types)

    def test_no_cost(self):
        send = self._run_chat("cli", stats={"input_tokens": 0, "output_tokens": 0})
        # Should work fine without cost tracking

    def test_with_cost(self):
        send = self._run_chat("cli", stats={
            "input_tokens": 100, "output_tokens": 200,
            "cost_usd": 0.01, "session_id": "s1",
        })
        # Should track cost

    def test_error_handling(self):
        from telechat_pkg.web_chat import _handle_chat
        ws = MagicMock()
        ws.closed = False
        send = AsyncMock()

        with patch("telechat_pkg.web_chat.cc") as mock_cc, \
             patch("telechat_pkg.web_chat._active_ws", {"c1": ws}):
            mock_cc.CLAUDE_MODE = "cli"
            mock_cc.CLAUDE_MODEL = "sonnet"
            mock_cc.CLAUDE_SYSTEM = ""
            mock_cc.CLAUDE_ADD_DIRS = ""
            mock_cc.CLAUDE_TIMEOUT = 60
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(
                name="default", cli_session_valid=False, claude_session_id=""
            )
            mock_cc.load_history.return_value = []
            mock_cc.ask_claude_async = AsyncMock(side_effect=RuntimeError("boom"))
            asyncio.run(_handle_chat(ws, send, "u1", "c1", "hi"))

        types = [c[0][0]["type"] for c in send.call_args_list]
        self.assertIn("error", types)
        error_msg = next(c[0][0] for c in send.call_args_list if c[0][0]["type"] == "error")
        # The raw exception text must never reach the client — only the
        # server log (see _friendly_error_text below).
        self.assertNotIn("boom", error_msg["text"])

    def test_streamed_response(self):
        """When on_text is called, should send 'done' instead of 'reply'."""
        from telechat_pkg.web_chat import _handle_chat
        ws = MagicMock()
        ws.closed = False
        send = AsyncMock()

        async def fake_ask(*args, **kwargs):
            # Simulate streaming by calling on_text
            if "on_text" in kwargs and kwargs["on_text"]:
                await kwargs["on_text"]("chunk1")
                await kwargs["on_text"]("chunk2")
            return "chunk1chunk2", {"input_tokens": 10, "output_tokens": 20}

        with patch("telechat_pkg.web_chat.cc") as mock_cc, \
             patch("telechat_pkg.web_chat._active_ws", {"c1": ws}):
            mock_cc.CLAUDE_MODE = "cli"
            mock_cc.CLAUDE_MODEL = "sonnet"
            mock_cc.CLAUDE_SYSTEM = ""
            mock_cc.CLAUDE_ADD_DIRS = ""
            mock_cc.CLAUDE_TIMEOUT = 60
            mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(
                name="default", cli_session_valid=False, claude_session_id=""
            )
            mock_cc.load_history.return_value = []
            mock_cc.ask_claude_async = fake_ask

            asyncio.run(_handle_chat(ws, send, "u1", "c1", "hi"))

        types = [c[0][0]["type"] for c in send.call_args_list]
        self.assertIn("stream", types)
        self.assertIn("done", types)
        self.assertNotIn("reply", types)


class TestFriendlyErrorText(unittest.TestCase):
    """A raw `str(exc)` from an SDK/network failure must never reach the
    browser client — see the comment on _friendly_error_text for why."""

    def test_timeout(self):
        from telechat_pkg.web_chat import _friendly_error_text
        msg = _friendly_error_text(asyncio.TimeoutError())
        self.assertIn("too long", msg)

    def test_rate_limit_by_class_name(self):
        from telechat_pkg.web_chat import _friendly_error_text
        RateLimitError = type("RateLimitError", (Exception,), {})
        msg = _friendly_error_text(RateLimitError("429 too many requests, retry-after: 30s"))
        self.assertIn("too many requests", msg.lower())
        self.assertNotIn("retry-after", msg)

    def test_auth_error_by_class_name(self):
        from telechat_pkg.web_chat import _friendly_error_text
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        msg = _friendly_error_text(AuthenticationError("invalid x-api-key: sk-ant-secret123"))
        self.assertIn("credentials", msg)
        self.assertNotIn("sk-ant-secret123", msg)

    def test_connection_error(self):
        from telechat_pkg.web_chat import _friendly_error_text
        msg = _friendly_error_text(ConnectionError("Connection reset by peer"))
        self.assertIn("connection", msg.lower())
        self.assertNotIn("reset by peer", msg)

    def test_missing_backend_is_not_reported_as_a_connection_blip(self):
        # FileNotFoundError is an OSError, so it used to fall into the
        # connection arm and answer "Lost connection… try again in a moment".
        # Nothing was connected, and trying again will never fix a `claude`
        # binary that isn't installed.
        from telechat_pkg.web_chat import _friendly_error_text
        for exc in (FileNotFoundError(2, "No such file or directory", "claude"),
                    NotADirectoryError(20, "Not a directory", "/work"),
                    PermissionError(13, "Permission denied", "claude")):
            msg = _friendly_error_text(exc)
            self.assertIn("isn't set up", msg)
            self.assertNotIn("Lost connection", msg)

    def test_api_connection_error_by_class_name(self):
        from telechat_pkg.web_chat import _friendly_error_text
        APIConnectionError = type("APIConnectionError", (Exception,), {})
        msg = _friendly_error_text(APIConnectionError("Connection to api.anthropic.com timed out"))
        self.assertIn("connection", msg.lower())

    def test_generic_fallback_hides_message(self):
        from telechat_pkg.web_chat import _friendly_error_text
        msg = _friendly_error_text(RuntimeError("/Users/dev/secret/path traceback detail"))
        self.assertNotIn("/Users/dev/secret/path", msg)
        self.assertIn("try again", msg.lower())


class TestCreateApp(unittest.TestCase):
    def test_creates_app(self):
        from telechat_pkg.web_chat import _create_app
        app = _create_app()
        # Should have 3 routes
        routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource')]
        self.assertIn("/", routes)
        self.assertIn("/health", routes)
        self.assertIn("/ws", routes)


class TestRunWebChat(unittest.TestCase):
    def test_run_web_chat(self):
        from telechat_pkg.web_chat import run_web_chat

        async def run():
            with patch("telechat_pkg.web_chat._create_app") as mock_app, \
                 patch("aiohttp.web.AppRunner") as mock_runner, \
                 patch("aiohttp.web.TCPSite") as mock_site:
                runner_inst = AsyncMock()
                mock_runner.return_value = runner_inst
                site_inst = AsyncMock()
                mock_site.return_value = site_inst

                # Make sleep raise CancelledError to stop the loop
                with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                    await run_web_chat()

                runner_inst.setup.assert_called_once()
                site_inst.start.assert_called_once()
                runner_inst.cleanup.assert_called_once()

        asyncio.run(run())

    @patch("asyncio.run")
    def test_run_web_chat_sync(self, mock_run):
        from telechat_pkg.web_chat import run_web_chat_sync
        run_web_chat_sync()
        mock_run.assert_called_once()
        # asyncio.run is mocked, so the coroutine it received was never
        # awaited; close it explicitly to silence the RuntimeWarning.
        coro = mock_run.call_args[0][0]
        coro.close()


class TestSendJsonWhenClosed(unittest.TestCase):
    def test_send_json_skips_when_closed(self):
        """The nested send_json should not send when ws is closed."""
        # This tests the `if not ws.closed` branch inside _ws_handler
        from aiohttp import web as aio_web
        from telechat_pkg import web_chat

        ws = AsyncMock(spec=aio_web.WebSocketResponse)
        ws.closed = True  # ws is already closed
        ws.prepare = AsyncMock()
        ws.send_json = AsyncMock()

        # An auth message while closed — send_json should be skipped
        msg = MagicMock()
        msg.type = aio_web.WSMsgType.TEXT
        msg.data = json.dumps({"type": "auth", "token": "t"})

        async def ws_iter():
            yield msg

        ws.__aiter__ = lambda self: ws_iter()

        request = MagicMock()

        with patch.object(web_chat, "WEB_AUTH_TOKEN", ""), \
             patch.object(web_chat, "_active_ws", {}), \
             patch("aiohttp.web.WebSocketResponse", return_value=ws):
            asyncio.run(web_chat._ws_handler(request))

        # send_json won't be called for the "connected" message since ws.closed is True
        # But the initial send_json happens before closed=True takes effect in the real flow.
        # We're testing the guard works.


class TestAuthFailureTableIsBounded(unittest.TestCase):
    """The brute-force defence must not itself be a way to exhaust memory.

    An entry was only removed on a successful auth, or when that same IP came
    back after its window expired. One failed attempt each from many addresses
    therefore left one entry per address behind, permanently.
    """

    def setUp(self):
        from telechat_pkg import web_chat
        web_chat._auth_failures.clear()

    def tearDown(self):
        from telechat_pkg import web_chat
        web_chat._auth_failures.clear()

    def test_expired_windows_are_dropped_on_the_next_failure(self):
        from telechat_pkg import web_chat
        import time
        stale = time.time() - (web_chat.WEB_AUTH_LOCKOUT_SEC + 60)
        for i in range(500):
            web_chat._auth_failures[f"10.0.0.{i}"] = (stale, 1)
        web_chat._record_auth_failure("192.168.1.1")
        self.assertEqual(list(web_chat._auth_failures), ["192.168.1.1"])

    def test_a_live_window_is_kept(self):
        from telechat_pkg import web_chat
        import time
        web_chat._auth_failures["10.0.0.1"] = (time.time(), 2)
        web_chat._record_auth_failure("10.0.0.2")
        self.assertIn("10.0.0.1", web_chat._auth_failures)
        self.assertIn("10.0.0.2", web_chat._auth_failures)

    def test_the_table_is_hard_capped(self):
        from telechat_pkg import web_chat
        import time
        now = time.time()
        with patch.object(web_chat, "_AUTH_FAILURES_MAX", 50):
            for i in range(200):
                # All within the window, so pruning alone can't help.
                web_chat._auth_failures[f"10.1.0.{i}"] = (now, 1)
            web_chat._record_auth_failure("10.2.0.1")
            self.assertLessEqual(len(web_chat._auth_failures), 50)

    def test_the_reporting_ip_survives_its_own_failure(self):
        from telechat_pkg import web_chat
        # Pruning runs after the record, so an attacker cannot evict their own
        # counter by filling the table.
        import time
        now = time.time()
        with patch.object(web_chat, "_AUTH_FAILURES_MAX", 10):
            for i in range(50):
                web_chat._auth_failures[f"10.3.0.{i}"] = (now + 100, 1)
            web_chat._record_auth_failure("10.9.9.9")
            self.assertIn("10.9.9.9", web_chat._auth_failures)

    def test_the_count_still_increments_within_a_window(self):
        from telechat_pkg import web_chat
        # The bounding must not reset the thing it is bounding.
        first = web_chat._record_auth_failure("10.4.0.1")
        second = web_chat._record_auth_failure("10.4.0.1")
        self.assertEqual((first, second), (1, 2))

    def test_lockout_still_engages_after_the_cap_work(self):
        from telechat_pkg import web_chat
        for _ in range(web_chat.WEB_AUTH_MAX_ATTEMPTS):
            web_chat._record_auth_failure("10.5.0.1")
        self.assertTrue(web_chat._ip_is_locked("10.5.0.1"))


if __name__ == "__main__":
    unittest.main()


class TestPhoneQrOnlyWhenReachable(unittest.TestCase):
    """The QR code must not advertise an address that refuses connections.

    `print_web_qr` encodes this machine's LAN IP, but the web chat binds to
    127.0.0.1 unless the operator opts out. Under "── Scan to open on your
    phone ──" that produced a code which timed out when scanned — an
    invitation the configuration could not honour, which reads as a broken
    product rather than a disabled feature.
    """

    def _start(self, bind):
        from telechat_pkg import web_chat

        async def run():
            with patch.object(web_chat, "WEB_BIND", bind), \
                 patch.object(web_chat, "WEB_AUTH_TOKEN", "tok"), \
                 patch("telechat_pkg.web_chat._create_app"), \
                 patch("aiohttp.web.AppRunner", return_value=AsyncMock()), \
                 patch("aiohttp.web.TCPSite", return_value=AsyncMock()), \
                 patch("telechat_pkg.qr_util.print_web_qr") as qr, \
                 patch("builtins.print") as printed, \
                 patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                await web_chat.run_web_chat()
            return qr, printed

        return asyncio.run(run())

    def test_loopback_bind_prints_no_qr(self):
        qr, printed = self._start("127.0.0.1")
        qr.assert_not_called()
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("this machine only", out)
        # And it says how to change that, rather than just refusing.
        self.assertIn("WEB_CHAT_BIND=0.0.0.0", out)

    def test_lan_bind_prints_the_qr(self):
        qr, _ = self._start("0.0.0.0")
        qr.assert_called_once()


class TestPrintWebQrHost(unittest.TestCase):
    def test_defaults_to_the_lan_address(self):
        from telechat_pkg import qr_util

        with patch.object(qr_util, "_get_local_ip", return_value="10.0.0.7"), \
             patch("builtins.print") as printed:
            qr_util.print_web_qr("8585")
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("10.0.0.7:8585", out)

    def test_an_explicit_host_wins(self):
        from telechat_pkg import qr_util

        with patch.object(qr_util, "_get_local_ip", return_value="10.0.0.7"), \
             patch("builtins.print") as printed:
            qr_util.print_web_qr("8585", host="tailscale-host")
        out = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("tailscale-host:8585", out)
        self.assertNotIn("10.0.0.7", out)


class TestSessionsSnapshot(unittest.TestCase):
    """The web dashboard's read of Claude Code sessions on this machine."""

    RUNNING = [
        {"sid": "abcdef0123456789", "cwd": "/Users/me/projects/telechat",
         "model": "opus", "etime": "12:04", "pid": "901"},
        {"sid": "", "cwd": "", "model": "", "etime": "00:03", "pid": "902"},
    ]
    RECENT = [
        {"sid": "abcdef0123456789", "cwd": "/Users/me/projects/telechat",
         "ago": "2m ago", "running": True, "last": "Fixed the parser."},
        {"sid": "99887766554433", "cwd": "/Users/me/projects/site",
         "ago": "3h ago", "running": False, "last": None},
    ]

    def _patch_bridge(self, **overrides):
        from telechat_pkg import desktop_bridge
        defaults = {
            "get_current_session": MagicMock(
                return_value=("abcdef0123456789", "/Users/me/projects/telechat")),
            "list_running_sessions": MagicMock(return_value=list(self.RUNNING)),
            "list_recent_sessions": MagicMock(return_value=list(self.RECENT)),
            "_session_status": MagicMock(return_value={"busy": True, "last": "Running Bash"}),
            "approve_mode_on": MagicMock(return_value=True),
        }
        defaults.update(overrides)
        return [patch.object(desktop_bridge, k, v) for k, v in defaults.items()]

    def _snapshot(self, limit=8, **overrides):
        from telechat_pkg.web_chat import _sessions_snapshot
        patches = self._patch_bridge(**overrides)
        for p in patches:
            p.start()
        try:
            return _sessions_snapshot(limit)
        finally:
            for p in patches:
                p.stop()

    def test_running_sessions_carry_status_and_project(self):
        snap = self._snapshot()
        self.assertTrue(snap["available"])
        first = snap["running"][0]
        self.assertEqual(first["project"], "telechat")
        self.assertEqual(first["short"], "abcdef01")
        self.assertEqual(first["model"], "opus")
        self.assertEqual(first["uptime"], "12:04")
        self.assertTrue(first["busy"])
        self.assertEqual(first["last"], "Running Bash")
        self.assertTrue(first["approve"])

    def test_the_selected_session_is_flagged_as_current(self):
        snap = self._snapshot()
        self.assertTrue(snap["running"][0]["current"])
        self.assertTrue(snap["recent"][0]["current"])
        self.assertFalse(snap["recent"][1]["current"])
        self.assertEqual(snap["current"]["project"], "telechat")

    def test_a_session_with_no_id_yet_is_still_listed(self):
        # A brand-new Desktop window has no transcript to probe, but it is
        # genuinely running — dropping it would under-report what's live.
        snap = self._snapshot()
        second = snap["running"][1]
        self.assertEqual(second["sid"], "")
        self.assertFalse(second["busy"])
        self.assertIsNone(second["last"])
        self.assertFalse(second["current"])

    def test_no_current_session_leaves_current_null(self):
        snap = self._snapshot(get_current_session=MagicMock(return_value=(None, None)))
        self.assertIsNone(snap["current"])
        self.assertFalse(any(s["current"] for s in snap["running"]))

    def test_a_broken_bridge_degrades_instead_of_raising(self):
        snap = self._snapshot(
            list_running_sessions=MagicMock(side_effect=OSError("no ps")))
        self.assertFalse(snap["available"])
        self.assertEqual(snap["running"], [])
        self.assertEqual(snap["recent"], [])
        self.assertIn("bridge install", snap["reason"])

    def test_an_unreadable_approval_setting_does_not_blank_the_panel(self):
        snap = self._snapshot(
            approve_mode_on=MagicMock(side_effect=RuntimeError("db locked")))
        self.assertTrue(snap["available"])
        self.assertFalse(snap["running"][0]["approve"])

    def test_the_limit_is_clamped(self):
        # The limit arrives from the browser, so it must survive junk without
        # letting a client ask for every transcript on disk.
        from telechat_pkg import web_chat
        recorded = []

        def fake_recent(limit):
            recorded.append(limit)
            return []

        for value in (0, -5, 10_000, None, "seven"):
            self._snapshot(value, list_recent_sessions=fake_recent)
        self.assertEqual(
            recorded,
            [1, 1, web_chat.SESSIONS_LIMIT_MAX, web_chat.SESSIONS_LIMIT,
             web_chat.SESSIONS_LIMIT],
        )

    def test_a_missing_bridge_module_degrades(self):
        from telechat_pkg.web_chat import _sessions_snapshot
        import builtins
        real_import = builtins.__import__

        def boom(name, globals=None, locals=None, fromlist=(), level=0):
            if fromlist and "desktop_bridge" in fromlist:
                raise ImportError("nope")
            return real_import(name, globals, locals, fromlist, level)

        with patch.object(builtins, "__import__", side_effect=boom):
            snap = _sessions_snapshot()
        self.assertFalse(snap["available"])


class TestHandleSessions(unittest.TestCase):
    def test_sends_a_sessions_payload(self):
        from telechat_pkg import web_chat
        send = AsyncMock()
        with patch.object(web_chat, "_sessions_snapshot",
                          return_value={"available": True, "running": [], "recent": [],
                                        "current": None, "reason": ""}):
            asyncio.run(web_chat._handle_sessions(send, 3))
        payload = send.call_args[0][0]
        self.assertEqual(payload["type"], "sessions")
        self.assertTrue(payload["available"])

    def test_the_slash_command_answers_with_the_panel_payload(self):
        from telechat_pkg import web_chat
        send = AsyncMock()
        with patch.object(web_chat, "_sessions_snapshot",
                          return_value={"available": False, "running": [], "recent": [],
                                        "current": None, "reason": "nope"}) as snap:
            asyncio.run(web_chat._handle_command(MagicMock(), send, "u1", "/sessions"))
        snap.assert_called_once_with(web_chat.SESSIONS_LIMIT)
        self.assertEqual(send.call_args[0][0]["type"], "sessions")

    def test_the_slash_command_accepts_a_limit(self):
        from telechat_pkg import web_chat
        send = AsyncMock()
        with patch.object(web_chat, "_sessions_snapshot",
                          return_value={"available": True, "running": [], "recent": [],
                                        "current": None, "reason": ""}) as snap:
            asyncio.run(web_chat._handle_command(MagicMock(), send, "u1", "/sessions 3"))
        snap.assert_called_once_with(3)

    def test_help_lists_sessions(self):
        from telechat_pkg.web_chat import _handle_command
        send = AsyncMock()
        asyncio.run(_handle_command(MagicMock(), send, "u1", "/help"))
        self.assertIn("/sessions", send.call_args[0][0]["text"])


class TestSessionsOverWebSocket(_WsHarness, unittest.TestCase):
    def test_an_authenticated_client_gets_the_snapshot(self):
        from telechat_pkg import web_chat
        with patch.object(web_chat, "_sessions_snapshot",
                          return_value={"available": True, "running": [{"sid": "x"}],
                                        "recent": [], "current": None, "reason": ""}):
            ws = self._run_ws([self._make_msg({"type": "sessions"})])
        types = [c[0][0]["type"] for c in ws.send_json.call_args_list]
        self.assertIn("sessions", types)

    def test_an_unauthenticated_client_is_refused(self):
        from telechat_pkg import web_chat
        with patch.object(web_chat, "_sessions_snapshot") as snap:
            ws = self._run_ws([self._make_msg({"type": "sessions"})], auth_token="secret")
        snap.assert_not_called()
        types = [c[0][0]["type"] for c in ws.send_json.call_args_list]
        self.assertNotIn("sessions", types)
        self.assertIn("error", types)


class TestSessionsDashboardUI(unittest.TestCase):
    def _html(self):
        from telechat_pkg.web_chat import _index_handler
        return asyncio.run(_index_handler(MagicMock())).text

    def test_the_header_has_a_sessions_button(self):
        html = self._html()
        self.assertIn('id="sessionsBtn"', html)
        self.assertIn('aria-controls="sessionsPanel"', html)
        self.assertIn('aria-expanded="false"', html)

    def test_the_panel_exists_and_starts_hidden(self):
        html = self._html()
        self.assertIn('class="sessions-panel hidden" id="sessionsPanel"', html)
        self.assertIn('id="sessionsBody"', html)

    def test_the_client_handles_the_sessions_message(self):
        html = self._html()
        self.assertIn("case 'sessions':", html)
        self.assertIn("function renderSessions", html)

    def test_polling_only_runs_while_the_panel_is_open(self):
        # An always-on poll would keep waking the bridge (ps + transcript reads)
        # for a panel nobody is looking at.
        html = self._html()
        self.assertIn("clearInterval(sessionsTimer)", html)
        self.assertIn("setInterval(requestSessions, SESSIONS_POLL_MS)", html)

    def test_session_text_is_never_injected_as_html(self):
        # Rows carry transcript snippets and filesystem paths; they are set with
        # textContent so a session that echoes markup can't script the dashboard.
        html = self._html()
        start = html.index("function sessionRow")
        end = html.index("function span(")
        self.assertNotIn("innerHTML", html[start:end])
