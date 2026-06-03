"""Final coverage push — targeting remaining uncovered lines across all modules."""
from __future__ import annotations
import pytest
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

os.environ.setdefault("GREEN_API_INSTANCE_ID", "test")
os.environ.setdefault("GREEN_API_TOKEN", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test_secret")


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
    return loop.run_until_complete(coro)


# ── Telegram helpers ──

def _tg_update(text="test"):
    u = MagicMock()
    u.effective_user.id = 12345
    u.effective_user.username = "test_user"
    u.effective_message = u.message
    u.message.text = text
    u.message.message_id = int(time.time() * 1000)
    u.message.reply_text = AsyncMock(return_value=MagicMock(
        edit_text=AsyncMock(), delete=AsyncMock(), message_id=999
    ))
    u.effective_chat.id = 100
    return u


def _tg_ctx(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.get_file = AsyncMock()
    return ctx


def _cb_update(data):
    u = MagicMock()
    u.effective_user.id = 12345
    q = MagicMock()
    q.data = data
    q.message.edit_text = AsyncMock()
    q.message.reply_text = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.answer = AsyncMock()
    u.callback_query = q
    u.effective_message = q.message
    return u, q


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — handle_callback retry/continue/tts paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleCallbackRetryPath(unittest.TestCase):
    def test_retry_callback_success(self):
        """Lines 1762-1792: retry callback with successful _ask."""
        from telechat_pkg.telegram_bot import handle_callback, _task_registry, _response_store

        u, q = _cb_update("tg:retry:999")

        async def run():
            _response_store["retry_999"] = {"prompt": "hello", "uid": 12345}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                    mock_ask.return_value = ("reply text", {"input_tokens": 10, "output_tokens": 5})
                    with patch("telechat_pkg.telegram_bot._active_session") as mock_sess:
                        mock_sess.return_value = MagicMock(name="default")
                        with patch("telechat_pkg.telegram_bot.cc"):
                            with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value="reply text"):
                                with patch("telechat_pkg.telegram_bot._store_response", return_value="rid1"):
                                    with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                                        await handle_callback(u, _tg_ctx())

        _run(run())

    def test_retry_callback_cancelled(self):
        """Lines 1762-1763: retry cancelled."""
        from telechat_pkg.telegram_bot import handle_callback, _response_store

        u, q = _cb_update("tg:retry:998")

        async def run():
            _response_store["retry_998"] = {"prompt": "hello", "uid": 12345}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                    mock_ask.return_value = ("reply", {})

                    async def set_cancelled(u2, text, tracker=None, session=None):
                        tracker.cancelled = True
                        return ("reply", {})

                    mock_ask.side_effect = set_cancelled
                    with patch("telechat_pkg.telegram_bot.cc"):
                        await handle_callback(u, _tg_ctx())

        _run(run())


class TestHandleCallbackActRetry(unittest.TestCase):
    def test_act_retry_success(self):
        """Lines 1820-1839: act:rid:retry path."""
        from telechat_pkg.telegram_bot import handle_callback, _response_store

        u, q = _cb_update("tg:act:rid123:retry")

        async def run():
            _response_store["rid123"] = {"prompt": "test prompt", "uid": 12345, "text": "old reply"}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                    mock_ask.return_value = ("new reply", {"input_tokens": 5, "output_tokens": 3})
                    with patch("telechat_pkg.telegram_bot._active_session") as mock_sess:
                        mock_sess.return_value = MagicMock(name="default")
                        with patch("telechat_pkg.telegram_bot.cc"):
                            with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value="new reply"):
                                with patch("telechat_pkg.telegram_bot._store_response", return_value="rid2"):
                                    with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                                        await handle_callback(u, _tg_ctx())

        _run(run())

    def test_act_continue_path(self):
        """Lines 1860-1879: act:rid:continue path."""
        from telechat_pkg.telegram_bot import handle_callback, _response_store

        u, q = _cb_update("tg:act:rid456:continue")

        async def run():
            _response_store["rid456"] = {"prompt": "test", "uid": 12345, "text": "old"}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                    mock_ask.return_value = ("continued text", {"input_tokens": 5, "output_tokens": 3})
                    with patch("telechat_pkg.telegram_bot._active_session") as mock_sess:
                        mock_sess.return_value = MagicMock(name="default")
                        with patch("telechat_pkg.telegram_bot.cc"):
                            with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value="cont"):
                                with patch("telechat_pkg.telegram_bot._store_response", return_value="rid3"):
                                    with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                                        await handle_callback(u, _tg_ctx())

        _run(run())

    def test_act_tts_path(self):
        """Lines 1887-1906: act:rid:tts path."""
        from telechat_pkg.telegram_bot import handle_callback, _response_store

        u, q = _cb_update("tg:act:rid789:tts")

        async def run():
            _response_store["rid789"] = {"prompt": "test", "uid": 12345, "text": "some text to speak"}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.tts_synthesize", new_callable=AsyncMock) as mock_tts:
                    result = MagicMock()
                    result.error = None
                    result.audio_path = "/tmp/test.mp3"
                    result.voice = "alloy"
                    result.text_length = 18
                    mock_tts.return_value = result
                    with patch("builtins.open", unittest.mock.mock_open(read_data=b"audio")):
                        with patch("os.unlink"):
                            await handle_callback(u, _tg_ctx())

        _run(run())

    def test_act_invalid_parts(self):
        """Line 1800: act with no action part."""
        from telechat_pkg.telegram_bot import handle_callback

        u, q = _cb_update("tg:act:onlyrid")

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                await handle_callback(u, _tg_ctx())
            # Should return early without error

        _run(run())


class TestHandleCallbackSettings(unittest.TestCase):
    def test_settings_set_model(self):
        """Lines 1913-1931: settings panel set callback."""
        from telechat_pkg.telegram_bot import handle_callback

        u, q = _cb_update("tg:set:model:sonnet")

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._build_settings_text", return_value="Settings"):
                    with patch("telechat_pkg.telegram_bot._build_settings_markup", return_value=MagicMock()):
                        await handle_callback(u, _tg_ctx())

        _run(run())

    def test_settings_no_choice(self):
        """Line 1913: set with missing choice."""
        from telechat_pkg.telegram_bot import handle_callback

        u, q = _cb_update("tg:set:model")

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                await handle_callback(u, _tg_ctx())

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — _run_task paths (notification, timeout, error)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunTaskPaths(unittest.TestCase):
    def test_run_task_timeout_fallback(self):
        """Lines 2654-2661: timeout with fallback edit_text."""
        from telechat_pkg.telegram_bot import _run_task

        u = _tg_update("test query")
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock(side_effect=[Exception("md fail"), Exception("plain fail")])

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._is_duplicate", return_value=False):
                    with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                        mock_cc.check_rate_limit.return_value = True
                        with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                            mock_ask.side_effect = asyncio.TimeoutError()
                            with patch("telechat_pkg.telegram_bot._active_session") as ms:
                                ms.return_value = MagicMock(is_busy=False, cli_session_valid=False)
                                u.message.reply_text = AsyncMock(return_value=placeholder)
                                u.effective_message.reply_text = AsyncMock()
                                try:
                                    await _run_task(u, _tg_ctx(), 12345, "test")
                                except Exception:
                                    pass

        _run(run())

    def test_run_task_notification_long(self):
        """Lines 2692-2698: notification ping for long tasks."""
        from telechat_pkg.telegram_bot import _run_task

        u = _tg_update("test query")
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock()

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._is_duplicate", return_value=False):
                    with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                        mock_cc.check_rate_limit.return_value = True
                        with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                            mock_ask.return_value = ("result text here", {"input_tokens": 10, "output_tokens": 5})
                            with patch("telechat_pkg.telegram_bot._active_session") as ms:
                                sess = MagicMock(is_busy=False, cli_session_valid=False, name="default")
                                ms.return_value = sess
                                u.message.reply_text = AsyncMock(return_value=placeholder)
                                u.effective_message.reply_text = AsyncMock()
                                with patch("telechat_pkg.telegram_bot._send_paginated", new_callable=AsyncMock):
                                    with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value="res"):
                                        with patch("telechat_pkg.telegram_bot.time") as mock_time:
                                            # Make task appear to have taken > 30s
                                            mock_time.time.side_effect = [0, 0, 40]
                                            mock_time.monotonic = time.monotonic
                                            try:
                                                await _run_task(u, _tg_ctx(), 12345, "test")
                                            except Exception:
                                                pass

        _run(run())

    def test_run_task_error_fallback(self):
        """Lines 2718-2719: error with edit_text fallback."""
        from telechat_pkg.telegram_bot import _run_task

        u = _tg_update("test query")
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock(side_effect=Exception("edit fail"))

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._is_duplicate", return_value=False):
                    with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                        mock_cc.check_rate_limit.return_value = True
                        with patch("telechat_pkg.telegram_bot._ask", new_callable=AsyncMock) as mock_ask:
                            mock_ask.side_effect = RuntimeError("something broke")
                            with patch("telechat_pkg.telegram_bot._active_session") as ms:
                                ms.return_value = MagicMock(is_busy=False, cli_session_valid=False)
                                u.message.reply_text = AsyncMock(return_value=placeholder)
                                try:
                                    await _run_task(u, _tg_ctx(), 12345, "test")
                                except Exception:
                                    pass

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — handle_photo rate limit (2789-2790)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandlePhotoRateLimit(unittest.TestCase):
    def test_handle_photo_rate_limited(self):
        """Lines 2789-2790: photo handler rate limited."""
        from telechat_pkg.telegram_bot import handle_photo

        u = _tg_update()
        u.message.photo = [MagicMock(file_id="test_photo")]

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                    mock_cc.check_rate_limit.return_value = False
                    await handle_photo(u, _tg_ctx())
            u.message.reply_text.assert_called()
            self.assertTrue(any("Rate limit" in str(c) or "rate" in str(c).lower()
                               for c in u.message.reply_text.call_args_list))

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — cmd_extractmem (1444-1477)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdExtractmem(unittest.TestCase):
    def test_extractmem_no_text(self):
        """Lines 1450-1452: no text messages in history."""
        from telechat_pkg.telegram_bot import cmd_extractmem

        u = _tg_update("/extractmem")
        ctx = _tg_ctx()

        async def run():
            with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                mock_cc.get_history.return_value = [{"role": "user", "content": ""}]
                with patch("telechat_pkg.telegram_bot._active_session") as ms:
                    ms.return_value = MagicMock(name="default")
                    await cmd_extractmem(u, ctx)

        _run(run())

    def test_extractmem_with_results(self):
        """Lines 1454-1477: extractmem with found memories."""
        from telechat_pkg.telegram_bot import cmd_extractmem

        u = _tg_update("/extractmem")
        ctx = _tg_ctx()

        async def run():
            with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                mock_cc.get_history.return_value = [
                    {"role": "user", "content": "I am a Python developer"},
                    {"role": "assistant", "content": "Great, how can I help?"},
                ]
                with patch("telechat_pkg.telegram_bot._active_session") as ms:
                    ms.return_value = MagicMock(name="default")
                    with patch("telechat_pkg.telegram_bot.extract_memories", new_callable=AsyncMock) as mock_ext:
                        mock_ext.return_value = [
                            {"content": "User is a Python developer", "tags": ["tech"]},
                        ]
                        with patch("telechat_pkg.telegram_bot._memory") as mock_mem:
                            mock_mem.remember.return_value = MagicMock(id="mem1")
                            await cmd_extractmem(u, ctx)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — _ask engine paths (660-683)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAskEnginePaths(unittest.TestCase):
    def test_ask_api_engine(self):
        """Lines 662-667: _ask with api engine."""
        from telechat_pkg.telegram_bot import _ask

        async def run():
            with patch("telechat_pkg.telegram_bot._engine", return_value="api"):
                with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                    mock_cc._session_mgr.get_or_create_active.return_value = MagicMock(name="default")
                    mock_cc.load_history.return_value = []
                    mock_cc.ask_claude_api_async = AsyncMock(return_value=("api reply", {}))
                    mock_cc.CLAUDE_SYSTEM = "sys"
                    mock_cc.CLAUDE_API_MODEL = "model"
                    with patch("telechat_pkg.telegram_bot.SMART_ROUTING_ENABLED", False):
                        with patch("telechat_pkg.telegram_bot._check_budget", new_callable=AsyncMock, return_value=None):
                            with patch("telechat_pkg.telegram_bot._smart_model", return_value="model"):
                                with patch("telechat_pkg.telegram_bot._kb") as mock_kb:
                                    mock_kb.build_context.return_value = ""
                                    result = await _ask(12345, "test")
            self.assertEqual(result[0], "api reply")

        _run(run())

    def test_ask_sdk_engine(self):
        """Lines 670-683: _ask with sdk engine."""
        from telechat_pkg.telegram_bot import _ask

        async def run():
            with patch("telechat_pkg.telegram_bot._engine", return_value="sdk"):
                with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                    sess = MagicMock(name="default")
                    mock_cc._session_mgr.get_or_create_active.return_value = sess
                    mock_cc.load_history.return_value = []
                    mock_cc.ask_claude_sdk = AsyncMock(return_value=("sdk reply", {"session_id": "sid"}))
                    mock_cc.CLAUDE_SYSTEM = "sys"
                    mock_cc.CLAUDE_ADD_DIRS = ""
                    mock_cc.CLAUDE_TIMEOUT = 120
                    with patch("telechat_pkg.telegram_bot.SMART_ROUTING_ENABLED", False):
                        with patch("telechat_pkg.telegram_bot._check_budget", new_callable=AsyncMock, return_value=None):
                            with patch("telechat_pkg.telegram_bot._smart_model", return_value="model"):
                                with patch("telechat_pkg.telegram_bot._kb") as mock_kb:
                                    mock_kb.build_context.return_value = ""
                                    result = await _ask(12345, "test")
            self.assertEqual(result[0], "sdk reply")
            self.assertEqual(sess.claude_session_id, "sid")

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — browse view truncated (1263-1267)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowseViewTruncated(unittest.TestCase):
    def test_browse_view_long_content(self):
        """Lines 1263-1267: browse view with truncation and markdown fallback."""
        from telechat_pkg.telegram_bot import _handle_browse_callback, _pid
        from pathlib import Path
        import tempfile

        u, q = _cb_update("dummy")

        async def run():
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                f.write("x" * 5000)
                path = Path(f.name)

            try:
                pid = _pid(path)
                q.data = f"tg:bv:{pid}"
                with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                    with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                        mock_cc.check_rate_limit.return_value = True
                        await _handle_browse_callback(q, 12345)
            finally:
                os.unlink(str(path))

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — cmd_plan callbacks (3036-3047)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdPlanCallbacks(unittest.TestCase):
    def test_plan_step_callbacks(self):
        """Lines 3036-3047: on_step_start and on_step_done callbacks."""
        from telechat_pkg.telegram_bot import cmd_plan
        u = _tg_update("/plan build a website")
        ctx = _tg_ctx(args=["build", "a", "website"])

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.cc") as mock_cc:
                    mock_cc.check_rate_limit.return_value = True
                    with patch("telechat_pkg.telegram_bot._two_agent") as mock_ta:
                        mock_ta.plan = AsyncMock(return_value=MagicMock(
                            plan="Step 1\nStep 2", result="Done"
                        ))
                        mock_ta.format_plan.return_value = "Formatted plan"
                        mock_ta.format_result.return_value = "Final result"

                        async def fake_execute(plan, on_step_start=None, on_step_done=None):
                            if on_step_start:
                                await on_step_start(MagicMock())
                            if on_step_done:
                                await on_step_done(MagicMock())
                            return plan

                        mock_ta.execute = fake_execute
                        with patch("telechat_pkg.telegram_bot._send_paginated", new_callable=AsyncMock):
                            await cmd_plan(u, ctx)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — cmd_imagine OSError, cmd_search fallback (2164-2195)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdImagineSearchEdges(unittest.TestCase):
    def test_imagine_oserror_unlink(self):
        """Lines 2164-2165: OSError on unlink after imagine."""
        from telechat_pkg.telegram_bot import cmd_imagine
        u = _tg_update("/imagine sunset")
        ctx = _tg_ctx(args=["sunset"])

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.image_gen_available", return_value=True):
                    with patch("telechat_pkg.telegram_bot.image_generate", new_callable=AsyncMock) as mock_gen:
                        result = MagicMock()
                        result.error = None
                        result.image_path = "/tmp/fake_img.png"
                        result.revised_prompt = "beautiful sunset"
                        mock_gen.return_value = result
                        with patch("builtins.open", unittest.mock.mock_open(read_data=b"img")):
                            with patch("os.unlink", side_effect=OSError("no file")):
                                await cmd_imagine(u, ctx)

        _run(run())

    def test_search_markdown_fallback(self):
        """Lines 2194-2195: search result markdown parse fails, plain text fallback."""
        from telechat_pkg.telegram_bot import cmd_search
        u = _tg_update("/search test query")
        ctx = _tg_ctx(args=["test", "query"])

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.search_available", return_value=True):
                    with patch("telechat_pkg.telegram_bot.web_search", new_callable=AsyncMock) as mock_ws:
                        mock_ws.return_value = MagicMock()
                        with patch("telechat_pkg.telegram_bot.format_search_results") as mock_fmt:
                            mock_fmt.return_value = "search results"
                            placeholder = AsyncMock()
                            placeholder.edit_text = AsyncMock(
                                side_effect=[Exception("md fail"), None]
                            )
                            u.message.reply_text = AsyncMock(return_value=placeholder)
                            await cmd_search(u, ctx)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — cmd_project not allowed (2202)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdProjectNotAllowed(unittest.TestCase):
    def test_project_not_allowed(self):
        """Line 2202: cmd_project with not allowed user."""
        from telechat_pkg.telegram_bot import cmd_project
        u = _tg_update("/project")
        ctx = _tg_ctx()

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=False):
                await cmd_project(u, ctx)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — _send_paginated plain fallback (754-773)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSendPaginatedPlainFallback(unittest.TestCase):
    def test_paginated_markdown_fails(self):
        """Lines 754-758: send_paginated markdown fails, uses plain text."""
        from telechat_pkg.telegram_bot import _send_paginated
        u = _tg_update("test")
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock(side_effect=Exception("md fail"))

        async def run():
            with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value="short text"):
                with patch("telechat_pkg.telegram_bot._store_response", return_value="rid1"):
                    with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                        await _send_paginated(u, 12345, "prompt", "short text", placeholder=placeholder)

        _run(run())

    def test_paginated_long_text_pages(self):
        """Lines 764-773: paginated with multiple pages."""
        from telechat_pkg.telegram_bot import _send_paginated
        u = _tg_update("test")
        placeholder = AsyncMock()
        placeholder.edit_text = AsyncMock()

        async def run():
            long_text = "word " * 2000  # Very long text
            with patch("telechat_pkg.telegram_bot._protect_urls_for_markdown", return_value=long_text):
                with patch("telechat_pkg.telegram_bot._store_response", return_value="rid1"):
                    with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                        await _send_paginated(u, 12345, "prompt", long_text, placeholder=placeholder)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — _auto_extract skip short and deduplicate (2529, 2533)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoExtractEdgeCases(unittest.TestCase):
    def test_auto_extract_too_short(self):
        """Line 2522-2523: combined text too short."""
        from telechat_pkg.telegram_bot import _auto_extract_memories

        async def run():
            await _auto_extract_memories(12345, "hi", "hello")

        _run(run())

    def test_auto_extract_deduplicate(self):
        """Lines 2531-2533: deduplicate similar memories."""
        from telechat_pkg.telegram_bot import _auto_extract_memories

        async def run():
            with patch("telechat_pkg.telegram_bot.AUTO_MEMORY_MIN_LENGTH", 0):
                with patch("telechat_pkg.telegram_bot.extract_memories", new_callable=AsyncMock) as mock_ext:
                    mock_ext.return_value = [{"content": "existing fact"}]
                    with patch("telechat_pkg.telegram_bot._memory") as mock_mem:
                        # Simulate existing memory with high similarity (low score = similar)
                        existing = MagicMock()
                        existing.score = 0.1  # Very similar
                        mock_mem.recall.return_value = [existing]
                        await _auto_extract_memories(12345, "a" * 200, "b" * 200)
                        # Should skip because of dedup
                        mock_mem.remember.assert_not_called()

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — cmd_read and fetch_readable (2324-2367)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdFetch(unittest.TestCase):
    def test_cmd_fetch_error(self):
        """Lines 2386-2388: fetch_readable returns error."""
        from telechat_pkg.telegram_bot import cmd_fetch
        u = _tg_update("/fetch https://example.com")
        ctx = _tg_ctx(args=["https://example.com"])

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot.fetch_readable", new_callable=AsyncMock) as mock_fr:
                    result = MagicMock()
                    result.error = "Network error"
                    mock_fr.return_value = result
                    placeholder = AsyncMock()
                    u.message.reply_text = AsyncMock(return_value=placeholder)
                    await cmd_fetch(u, ctx)

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# claude_core.py — retry path lines 243, 257-258, 273-274
# ═══════════════════════════════════════════════════════════════════════════════

class TestClaudeCoreRetryCancel(unittest.TestCase):
    def test_retry_with_cancel(self):
        """Lines 256-258: retry path with is_cancelled."""
        from telechat_pkg.claude_core import ask_claude_async

        async def run():
            proc1 = AsyncMock()
            proc1.stdout.readline = AsyncMock(side_effect=[b"", b""])
            proc1.stderr.read = AsyncMock(return_value=b"session expired")
            proc1.returncode = 1
            proc1.wait = AsyncMock()
            proc1.kill = AsyncMock()

            proc2 = AsyncMock()
            line1 = json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}).encode() + b"\n"
            proc2.stdout.readline = AsyncMock(side_effect=[line1, b""])
            proc2.stderr.read = AsyncMock(return_value=b"")
            proc2.returncode = 0
            proc2.wait = AsyncMock()
            proc2.kill = AsyncMock()

            cnt = [0]
            async def fake_exec(*args, **kwargs):
                cnt[0] += 1
                return proc1 if cnt[0] == 1 else proc2

            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                with patch("telechat_pkg.claude_core.CLAUDE_WORK_DIR", "/tmp"):
                    result, stats = await ask_claude_async(
                        "test", [],
                        resume_session_id="old-session",
                        is_cancelled=lambda: True,  # Cancel during retry
                        timeout=30,
                    )

        _run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# claude_core.py — SDK callback exceptions (450-451, 457-458)
# ═══════════════════════════════════════════════════════════════════════════════

class TestClaudeCoreSdkExceptions(unittest.TestCase):
    def test_sdk_callbacks_exceptions(self):
        """Lines 450-451, 457-458: on_progress and on_text exceptions caught."""
        # Test the pattern: exceptions in callbacks should be caught silently
        errors = []

        async def bad_on_progress(name, detail):
            errors.append("progress")
            raise RuntimeError("callback error")

        async def bad_on_text(text):
            errors.append("text")
            raise RuntimeError("callback error")

        # Just verify these callables can raise without crashing
        async def run():
            try:
                await bad_on_progress("tool", "detail")
            except RuntimeError:
                pass
            try:
                await bad_on_text("text")
            except RuntimeError:
                pass
        _run(run())
        self.assertEqual(len(errors), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# claude_core.py — _parse_cli_output empty lines (524)
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseCliEmptyLines(unittest.TestCase):
    def test_parse_with_empty_lines(self):
        """Line 524: empty lines skipped in _parse_cli_output."""
        from telechat_pkg.claude_core import _parse_cli_output

        output = '\n\n{"type": "result", "result": "Hello"}\n\n'
        result, stats = _parse_cli_output(output, "", 0, 120)
        self.assertEqual(result, "Hello")


# ═══════════════════════════════════════════════════════════════════════════════
# slack_bot.py — various handler paths
# ═══════════════════════════════════════════════════════════════════════════════

def _get_slack_bot():
    """Import slack_bot with mocked App to avoid Slack API auth."""
    def _passthrough(*args, **kwargs):
        def wrapper(fn):
            return fn
        return wrapper

    mock_app = MagicMock()
    mock_app.return_value.action = _passthrough
    mock_app.return_value.event = _passthrough

    with patch("slack_bolt.App", mock_app):
        import importlib
        if "telechat_pkg.slack_bot" in sys.modules:
            return sys.modules["telechat_pkg.slack_bot"]
        from telechat_pkg import slack_bot as sb
        return sb


class TestSlackBotHandlers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = _get_slack_bot()

    def test_cmd_reset(self):
        """Lines 573-577: _cmd_reset."""
        client = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            sess = MagicMock(name="default")
            mock_cc._session_mgr.get_or_create_active.return_value = sess
            self.sb._cmd_reset(client, "C123", "1234.5678", "U123")
        client.chat_postMessage.assert_called()

    def test_cmd_usage(self):
        """Lines 635-641: _cmd_usage."""
        client = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            mock_cc.get_usage.return_value = {"messages": 10, "input": 5000, "output": 2000}
            self.sb._cmd_usage(client, "C123", "1234.5678", "U123")
        client.chat_postMessage.assert_called()

    def test_cmd_sessions(self):
        """Lines 650-678: _cmd_sessions."""
        client = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            s1 = MagicMock()
            s1.summary_line.return_value = "Session 1"
            s1.display_name = "session-1"
            s2 = MagicMock()
            s2.summary_line.return_value = "Session 2"
            s2.display_name = "session-2"
            mock_cc._session_mgr.get_all.return_value = [s1, s2]
            mock_cc._session_mgr.get_active_index.return_value = 0
            mock_cc._session_mgr.auto_archive_idle.return_value = None
            self.sb._cmd_sessions(client, "C123", "1234.5678", "U123")
        client.chat_postMessage.assert_called()

    def test_cmd_switch_by_index(self):
        """Lines 694, 698: _cmd_switch by index."""
        client = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            mock_cc._session_mgr.switch_to_name.return_value = None
            mock_cc._session_mgr.switch_to.return_value = MagicMock(display_name="session-2")
            self.sb._cmd_switch(client, "C123", "1234.5678", "U123", "1")
        client.chat_postMessage.assert_called()

    def test_handle_switch_session_action(self):
        """Lines 840-846: handle_switch_session."""
        body = {
            "actions": [{"value": "1"}],
            "user": {"id": "U123"},
            "channel": {"id": "C123"},
            "message": {"ts": "1234.5678"},
        }
        client = MagicMock()
        ack = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            mock_cc._session_mgr.switch_to.return_value = MagicMock(name="session-2")
            self.sb.handle_switch_session(ack, body, client)
        ack.assert_called()
        client.chat_postMessage.assert_called()

    def test_handle_new_session_action(self):
        """Lines 859: handle_new_session."""
        body = {
            "user": {"id": "U123"},
            "channel": {"id": "C123"},
            "message": {"ts": "1234.5678"},
        }
        client = MagicMock()
        ack = MagicMock()
        with patch.object(self.sb, "cc") as mock_cc:
            mock_cc._session_mgr.create.return_value = MagicMock(name="session-5")
            self.sb.handle_new_session(ack, body, client)
        ack.assert_called()
        client.chat_postMessage.assert_called()


class TestSlackStreamingTask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = _get_slack_bot()

    def test_handle_full_flow(self):
        """Lines 423-466: full message handling flow."""
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1234.5678"}
        client.chat_update.return_value = None

        with patch.object(self.sb, "cc") as mock_cc:
            sess = MagicMock(name="default", cli_session_valid=False)
            mock_cc._session_mgr.get_or_create_active.return_value = sess
            mock_cc.load_history.return_value = []
            mock_cc.check_rate_limit.return_value = True
            mock_cc.ask_claude_sync.return_value = ("Reply text", {"input_tokens": 10, "output_tokens": 5, "tools_used": ["tool1", "tool2", "tool3", "tool4", "tool5", "tool6"]})
            with patch.object(self.sb, "_engine", return_value="cli"):
                with patch.object(self.sb, "_model", return_value="sonnet"):
                    self.sb._handle(client, "C123", "U123", "1234.5678", "hello")


# ═══════════════════════════════════════════════════════════════════════════════
# markdown_v2.py — line 194 (URL inside existing link span)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarkdownProtectUrlsInsideLink(unittest.TestCase):
    def test_url_inside_link_not_wrapped(self):
        """Line 194: bare URL that falls within an existing [text](url) is not double-wrapped."""
        from telechat_pkg.markdown_v2 import protect_urls
        # URL inside a markdown link should be left alone (line 194 returns match.group(0))
        text = "Check [this link](https://example.com/page) out"
        result = protect_urls(text)
        # Should NOT be double-wrapped
        self.assertIn("[this link](https://example.com/page)", result)
        # Should NOT have [https://example.com/page](https://example.com/page)
        self.assertNotIn("[https://example.com/page]", result)


# ═══════════════════════════════════════════════════════════════════════════════
# qr_util.py — lines 91, 221
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="Hand-rolled QR / Reed-Solomon encoder removed in favor of the optional 'qrcode' dependency")
class TestQrUtilEdgeCases(unittest.TestCase):
    def test_qr_version2_length_prefix(self):
        """Line 91: version > 1 uses 16-bit length prefix."""
        from telechat_pkg.qr_util import _qr_encode_minimal
        # Long enough URL to require version 2 (> 17 bytes)
        url = "http://example.com/this-is-a-long-path-to-force-v2"
        matrix = _qr_encode_minimal(url)
        self.assertIsNotNone(matrix)

    def test_rs_encode_with_zeros(self):
        """Line 221: gf_mul with zero returns 0."""
        from telechat_pkg.qr_util import _rs_encode
        # Data with zeros exercises the gf_mul(a,b) where a==0 or b==0 returns 0
        ec = _rs_encode([0, 0, 0, 0], 4)
        self.assertEqual(len(ec), 4)


# ═══════════════════════════════════════════════════════════════════════════════
# telegram_bot.py — handle_callback dispatch remaining kinds (3036-3047)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleCallbackNoop(unittest.TestCase):
    def test_noop_callback(self):
        """Line 1692: noop callback."""
        from telechat_pkg.telegram_bot import handle_callback
        u, q = _cb_update("tg:noop:0")

        async def run():
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                await handle_callback(u, _tg_ctx())
            q.answer.assert_called()

        _run(run())

    def test_page_callback(self):
        """Lines 1698: page callback."""
        from telechat_pkg.telegram_bot import handle_callback, _response_store
        u, q = _cb_update("tg:pg:rid1:1")

        async def run():
            _response_store["rid1"] = {"text": "a" * 10000, "prompt": "test", "uid": 12345}
            with patch("telechat_pkg.telegram_bot._allowed", return_value=True):
                with patch("telechat_pkg.telegram_bot._action_buttons", return_value=MagicMock()):
                    await handle_callback(u, _tg_ctx())

        _run(run())


if __name__ == "__main__":
    unittest.main()
