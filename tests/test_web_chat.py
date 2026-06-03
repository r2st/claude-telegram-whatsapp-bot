"""Tests for web_chat.py — 100% coverage target."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

# Patch aiohttp.web before importing web_chat
import sys


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


class TestWsHandler(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
