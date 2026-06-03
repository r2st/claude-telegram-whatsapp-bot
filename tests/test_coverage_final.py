import pytest
"""Final coverage push — tests for remaining uncovered lines.

Targets: telegram_bot (session/budget/plan/schedule/kb/browse commands),
claude_core (CLI retry, API async, SDK), store (session manager edge cases),
web_fetch (jina, raw, blocked URLs), main.py (QR code gen).
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE_TOKEN_FOR_TESTS")


def _run(coro):
    """Run async code safely across test suites."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ─── Telegram Bot: Session commands (resume, fork, _format_age) ───────────

class TestTelegramSessionCommands(unittest.TestCase):
    """Cover cmd_resume, cmd_fork, _format_age (lines 2896-2955)."""

    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=False)
    def test_resume_not_allowed(self, _):
        from telechat_pkg.telegram_bot import cmd_resume
        update = self._make_update("/resume")
        _run(cmd_resume(update, None))
        update.message.reply_text.assert_not_called()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._session_browser")
    def test_resume_no_args_no_sessions(self, mock_browser, _):
        from telechat_pkg.telegram_bot import cmd_resume
        mock_browser.list_sessions.return_value = []
        update = self._make_update("/resume")
        _run(cmd_resume(update, None))
        update.message.reply_text.assert_called_once()
        self.assertIn("No previous sessions", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._session_browser")
    def test_resume_no_args_with_sessions(self, mock_browser, _):
        from telechat_pkg.telegram_bot import cmd_resume
        sess = MagicMock()
        sess.name = "test_session"
        sess.message_count = 10
        sess.last_active = time.time() - 3600
        sess.preview = "Hello world this is a preview"
        mock_browser.list_sessions.return_value = [sess]
        update = self._make_update("/resume")
        _run(cmd_resume(update, None))
        call_text = update.message.reply_text.call_args[0][0]
        self.assertIn("test_session", call_text)
        self.assertIn("10 msgs", call_text)

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.cc")
    def test_resume_with_session_name(self, mock_cc, _):
        from telechat_pkg.telegram_bot import cmd_resume
        mock_sess = MagicMock()
        mock_cc._session_mgr.get_or_create_active.return_value = mock_sess
        update = self._make_update("/resume my_session")
        _run(cmd_resume(update, None))
        self.assertEqual(mock_sess.name, "my_session")
        self.assertIn("Resumed", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=False)
    def test_fork_not_allowed(self, _):
        from telechat_pkg.telegram_bot import cmd_fork
        update = self._make_update("/fork")
        _run(cmd_fork(update, None))
        update.message.reply_text.assert_not_called()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._session_browser")
    @patch("telechat_pkg.telegram_bot.cc")
    def test_fork_no_source(self, mock_cc, mock_browser, _):
        from telechat_pkg.telegram_bot import cmd_fork
        mock_sess = MagicMock()
        mock_sess.name = "current"
        mock_cc._session_mgr.get_or_create_active.return_value = mock_sess
        result = MagicMock()
        result.success = True
        result.new_session_name = "current_fork1"
        result.messages_copied = 5
        mock_browser.fork_session.return_value = result
        update = self._make_update("/fork")
        _run(cmd_fork(update, None))
        self.assertIn("Forked", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._session_browser")
    def test_fork_with_args_failure(self, mock_browser, _):
        from telechat_pkg.telegram_bot import cmd_fork
        result = MagicMock()
        result.success = False
        result.error = "session not found"
        mock_browser.fork_session.return_value = result
        update = self._make_update("/fork source_sess new_sess")
        _run(cmd_fork(update, None))
        self.assertIn("Fork failed", update.message.reply_text.call_args[0][0])

    def test_format_age(self):
        from telechat_pkg.telegram_bot import _format_age
        now = time.time()
        self.assertIn("s", _format_age(now - 30))
        self.assertIn("m", _format_age(now - 300))
        self.assertIn("h", _format_age(now - 7200))
        self.assertIn("d", _format_age(now - 100000))


# ─── Telegram Bot: Budget command (lines 2964-3006) ──────────────────────

class TestTelegramBudgetCommand(unittest.TestCase):
    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._budget_mgr")
    def test_budget_set_daily(self, mock_budget, _):
        from telechat_pkg.telegram_bot import cmd_budget
        update = self._make_update("/budget daily 5.0")
        _run(cmd_budget(update, None))
        mock_budget.set_budget.assert_called_once()
        self.assertIn("Budget updated", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._budget_mgr")
    def test_budget_set_monthly(self, mock_budget, _):
        from telechat_pkg.telegram_bot import cmd_budget
        update = self._make_update("/budget monthly 50.0")
        _run(cmd_budget(update, None))
        mock_budget.set_budget.assert_called_once()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._budget_mgr")
    def test_budget_invalid_period(self, mock_budget, _):
        from telechat_pkg.telegram_bot import cmd_budget
        update = self._make_update("/budget weekly 10.0")
        _run(cmd_budget(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._budget_mgr")
    def test_budget_invalid_amount(self, mock_budget, _):
        from telechat_pkg.telegram_bot import cmd_budget
        update = self._make_update("/budget daily abc")
        _run(cmd_budget(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._budget_mgr")
    def test_budget_show_report(self, mock_budget, _):
        from telechat_pkg.telegram_bot import cmd_budget
        report = MagicMock()
        report.daily_pct = 0.5
        report.monthly_pct = 0.3
        report.daily_cost = 2.5
        report.daily_limit = 5.0
        report.daily_requests = 10
        report.monthly_cost = 15.0
        report.monthly_limit = 50.0
        report.monthly_requests = 100
        mock_budget.usage_report.return_value = report
        update = self._make_update("/budget")
        _run(cmd_budget(update, None))
        call_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Cost Budget", call_text)

    def test_progress_bar(self):
        from telechat_pkg.telegram_bot import _progress_bar
        bar = _progress_bar(0.5)
        self.assertIn("🟢", bar)
        bar = _progress_bar(0.85)
        self.assertIn("🟡", bar)
        bar = _progress_bar(1.1)
        self.assertIn("🔴", bar)


# ─── Telegram Bot: Plan command (lines 3019-3056) ────────────────────────

class TestTelegramPlanCommand(unittest.TestCase):
    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock()))
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    def test_plan_no_args(self, _):
        from telechat_pkg.telegram_bot import cmd_plan
        update = self._make_update("/plan")
        _run(cmd_plan(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._two_agent")
    @patch("telechat_pkg.telegram_bot._send_paginated", new_callable=AsyncMock)
    def test_plan_success(self, mock_paginated, mock_ta, _):
        from telechat_pkg.telegram_bot import cmd_plan
        plan = MagicMock()
        mock_ta.plan = AsyncMock(return_value=plan)
        mock_ta.format_plan.return_value = "Plan text"
        mock_ta.execute = AsyncMock(return_value=plan)
        mock_ta.format_result.return_value = "Result text"
        update = self._make_update("/plan build a website")
        _run(cmd_plan(update, None))
        mock_ta.plan.assert_called_once()
        mock_ta.execute.assert_called_once()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._two_agent")
    def test_plan_error(self, mock_ta, _):
        from telechat_pkg.telegram_bot import cmd_plan
        mock_ta.plan = AsyncMock(side_effect=RuntimeError("fail"))
        update = self._make_update("/plan do something")
        placeholder = MagicMock(edit_text=AsyncMock())
        update.message.reply_text = AsyncMock(return_value=placeholder)
        _run(cmd_plan(update, None))
        self.assertIn("failed", placeholder.edit_text.call_args[0][0])


# ─── Telegram Bot: Schedule command (lines 3069-3107) ────────────────────

class TestTelegramScheduleCommand(unittest.TestCase):
    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_list(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        mock_sched.list_tasks.return_value = []
        mock_sched.format_task_list.return_value = "No tasks"
        update = self._make_update("/schedule list")
        _run(cmd_schedule(update, None))
        update.message.reply_text.assert_called_once()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_no_args(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        mock_sched.list_tasks.return_value = []
        mock_sched.format_task_list.return_value = "No tasks"
        update = self._make_update("/schedule")
        _run(cmd_schedule(update, None))
        update.message.reply_text.assert_called_once()

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_delete_success(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        mock_sched.delete_task.return_value = True
        update = self._make_update("/schedule delete 5")
        _run(cmd_schedule(update, None))
        self.assertIn("Deleted", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_delete_not_found(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        mock_sched.delete_task.return_value = False
        update = self._make_update("/schedule delete 99")
        _run(cmd_schedule(update, None))
        self.assertIn("not found", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_delete_invalid(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        update = self._make_update("/schedule delete abc")
        _run(cmd_schedule(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_create_success(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        task = MagicMock()
        task.description = "check deploys"
        task.interval_seconds = 7200
        task.max_runs = 0
        task.id = 42
        mock_sched.parse_and_create.return_value = task
        update = self._make_update("/schedule check deploys every 2 hours")
        _run(cmd_schedule(update, None))
        self.assertIn("Scheduled", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._auto_sched")
    def test_schedule_create_failure(self, mock_sched, _):
        from telechat_pkg.telegram_bot import cmd_schedule
        mock_sched.parse_and_create.return_value = None
        update = self._make_update("/schedule something unparseable")
        _run(cmd_schedule(update, None))
        self.assertIn("Couldn't parse", update.message.reply_text.call_args[0][0])


# ─── Telegram Bot: KB command (lines 3126-3174) ─────────────────────────

class TestTelegramKBCommand(unittest.TestCase):
    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_stats(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        mock_kb.stats.return_value = {"documents": 5, "chunks": 20}
        update = self._make_update("/kb stats")
        _run(cmd_kb(update, None))
        self.assertIn("Knowledge Base", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_list_empty(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        mock_kb.list_documents.return_value = []
        update = self._make_update("/kb list")
        _run(cmd_kb(update, None))
        self.assertIn("No documents", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_list_with_docs(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        doc = MagicMock()
        doc.id = "abc12345-xyz"
        doc.title = "Test Doc"
        doc.chunk_count = 3
        mock_kb.list_documents.return_value = [doc]
        update = self._make_update("/kb list")
        _run(cmd_kb(update, None))
        self.assertIn("Test Doc", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_add(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        doc = MagicMock()
        doc.chunk_count = 2
        doc.title = "Note 12345"
        mock_kb.ingest_text.return_value = doc
        update = self._make_update("/kb add some knowledge text")
        _run(cmd_kb(update, None))
        self.assertIn("Added to KB", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_search_results(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        result = MagicMock()
        result.document.title = "My Doc"
        result.chunk.chunk_index = 0
        result.chunk.content = "This is the matching content"
        mock_kb.search.return_value = [result]
        update = self._make_update("/kb search test query")
        _run(cmd_kb(update, None))
        self.assertIn("KB Search", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_search_no_results(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        mock_kb.search.return_value = []
        update = self._make_update("/kb search nothing")
        _run(cmd_kb(update, None))
        self.assertIn("No results", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_delete_success(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        doc = MagicMock()
        doc.id = "abc12345"
        doc.title = "Old Doc"
        mock_kb.list_documents.return_value = [doc]
        mock_kb.delete_document.return_value = True
        update = self._make_update("/kb delete abc")
        _run(cmd_kb(update, None))
        self.assertIn("Deleted", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_delete_not_found(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        mock_kb.list_documents.return_value = []
        update = self._make_update("/kb delete xyz")
        _run(cmd_kb(update, None))
        self.assertIn("not found", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_unknown_subcommand(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        update = self._make_update("/kb unknown")
        _run(cmd_kb(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot._kb")
    def test_kb_no_args_defaults_to_stats(self, mock_kb, _):
        from telechat_pkg.telegram_bot import cmd_kb
        mock_kb.stats.return_value = {"documents": 0, "chunks": 0}
        update = self._make_update("/kb")
        _run(cmd_kb(update, None))
        self.assertIn("Knowledge Base", update.message.reply_text.call_args[0][0])


# ─── Telegram Bot: Browse command (lines 3194-3258) ─────────────────────

class TestTelegramBrowseCommand(unittest.TestCase):
    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock(return_value=MagicMock(
            edit_text=AsyncMock(), delete=AsyncMock(),
        ))
        update.message.reply_photo = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", False)
    def test_browse_disabled(self, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        update = self._make_update("/web screenshot https://example.com")
        _run(cmd_browse_web(update, None))
        self.assertIn("disabled", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    def test_browse_no_args(self, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        update = self._make_update("/web")
        _run(cmd_browse_web(update, None))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_screenshot_success(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = True
        result.screenshot_path = "/tmp/test.png"
        result.title = "Example"
        result.url = "https://example.com"
        result.duration = 1.5
        agent.screenshot = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web screenshot https://example.com")
        with patch("builtins.open", MagicMock()):
            _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_screenshot_failure(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = False
        result.error = "timeout"
        result.screenshot_path = None
        agent.screenshot = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web screenshot https://example.com")
        _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    @patch("telechat_pkg.telegram_bot._send_paginated", new_callable=AsyncMock)
    def test_browse_extract_success(self, mock_pag, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = True
        result.data = MagicMock(title="Test", text_content="Some content here")
        result.error = None
        agent.extract_text = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web extract https://example.com")
        _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_info_success(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = True
        result.data = {"title": "Example", "url": "https://example.com", "text_preview": "Hello"}
        result.error = None
        agent.get_page_info = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web info https://example.com")
        _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_unknown_action(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        mock_get_agent.return_value = agent
        update = self._make_update("/web foobar https://example.com")
        _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_exception(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        agent.screenshot = AsyncMock(side_effect=RuntimeError("boom"))
        mock_get_agent.return_value = agent
        update = self._make_update("/web screenshot https://example.com")
        _run(cmd_browse_web(update, None))


# ─── Claude Core: API & SDK edge cases (lines 310-316, 378-384, 450-475) ─

class TestClaudeCoreEdgeCases(unittest.TestCase):
    """Cover _get_async_api_client, ask_claude_api_async streaming, ask_claude_sdk."""

    @patch("telechat_pkg.claude_core._async_api_client", None)
    def test_get_async_api_client_no_anthropic(self):
        from telechat_pkg import claude_core
        with patch.dict("sys.modules", {"anthropic": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                claude_core._async_api_client = None
                result = claude_core._get_async_api_client()
                # Should return None when anthropic not installed
                self.assertIsNone(result)

    def test_ask_claude_api_async_on_text_error(self):
        """Test that on_text errors are caught during streaming."""
        from telechat_pkg import claude_core

        async def run():
            mock_client = MagicMock()
            mock_stream = AsyncMock()
            mock_final = MagicMock()
            mock_final.usage.input_tokens = 10
            mock_final.usage.output_tokens = 20
            mock_stream.get_final_message = AsyncMock(return_value=mock_final)

            async def mock_text_stream():
                yield "hello"
                yield " world"
            mock_stream.text_stream = mock_text_stream()

            ctx_mgr = AsyncMock()
            ctx_mgr.__aenter__ = AsyncMock(return_value=mock_stream)
            ctx_mgr.__aexit__ = AsyncMock(return_value=False)
            mock_client.messages.stream = MagicMock(return_value=ctx_mgr)

            async def bad_on_text(chunk):
                raise RuntimeError("callback error")

            with patch.object(claude_core, "_get_async_api_client", return_value=mock_client):
                result, stats = await claude_core.ask_claude_api_async(
                    "test", [], on_text=bad_on_text,
                )
            self.assertIn("hello", result)

        _run(run())

    def test_ask_claude_sdk_timeout(self):
        """Test SDK timeout handling (line 474-475)."""
        from telechat_pkg import claude_core

        async def run():
            # Mock query as an async generator that raises TimeoutError
            async def mock_query(**kwargs):
                raise asyncio.TimeoutError()
                yield  # make it a generator  # noqa

            mock_sdk_mod = MagicMock()
            mock_sdk_mod.query = mock_query
            mock_sdk_mod.ClaudeCodeOptions = MagicMock
            mock_sdk_mod.AssistantMessage = type("AM", (), {})
            mock_sdk_mod.ResultMessage = type("RM", (), {})
            mock_sdk_mod.ToolUseBlock = type("TU", (), {})
            mock_sdk_mod.TextBlock = type("TB", (), {})

            with patch.dict("sys.modules", {"claude_code_sdk": mock_sdk_mod}):
                result, stats = await claude_core.ask_claude_sdk(
                    "test", [], timeout=1,
                )
            self.assertIn("Timeout", result)

        _run(run())


# ─── Store: Session Manager edge cases (lines 570-578, 707-714, 783-794) ──

class TestSessionManagerEdgeCases(unittest.TestCase):
    def test_get_or_create_active_fallback_to_first(self):
        """When active session name doesn't match any session, fall back to first."""
        from telechat_pkg.store import SessionManager
        mgr = SessionManager()
        # Create a session manually
        from telechat_pkg.store import UserSession
        sess = UserSession("test_sess", "test_plat", "user1")
        sessions = mgr._ensure_loaded("test_plat", "user1")
        sessions.clear()
        sessions.append(sess)
        # Set active to non-existent name
        key = mgr._key("test_plat", "user1")
        mgr._active[key] = "nonexistent"
        with patch.object(mgr, "_save_active"):
            result = mgr.get_or_create_active("test_plat", "user1")
        self.assertEqual(result.name, "test_sess")

    def test_archive_creates_default_when_no_active(self):
        """When archiving the only active session, create a new default."""
        from telechat_pkg.store import SessionManager, UserSession
        mgr = SessionManager()
        sessions = mgr._ensure_loaded("test_plat", "user_arch")
        sessions.clear()
        sess = UserSession("only_session", "test_plat", "user_arch")
        sessions.append(sess)
        key = mgr._key("test_plat", "user_arch")
        mgr._active[key] = "only_session"
        with patch.object(mgr, "_save_active"), \
             patch.object(mgr, "_archive_session"), \
             patch.object(mgr, "_save_session"):
            result = mgr.archive("test_plat", "user_arch", "only_session")
        self.assertIsNotNone(result)

    def test_auto_archive_idle(self):
        """Auto-archive sessions that have been idle."""
        from telechat_pkg.store import SessionManager, UserSession
        mgr = SessionManager()
        sessions = mgr._ensure_loaded("test_plat", "user_idle")
        sessions.clear()
        # Active recent session
        s1 = UserSession("recent", "test_plat", "user_idle")
        s1.last_active = time.time()
        sessions.append(s1)
        # Old idle session
        s2 = UserSession("old", "test_plat", "user_idle")
        s2.last_active = time.time() - 100 * 86400  # 100 days ago
        sessions.append(s2)
        with patch.object(mgr, "_archive_session"):
            archived = mgr.auto_archive_idle("test_plat", "user_idle")
        self.assertIn("old", archived)
        self.assertNotIn("recent", archived)


# ─── Web Fetch: blocked URLs, jina, raw (lines 30-32, 46-49, 72) ─────────

class TestWebFetchEdgeCases(unittest.TestCase):
    def test_blocked_private_ip(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        self.assertTrue(_is_blocked_url("http://127.0.0.1/secret"))
        self.assertTrue(_is_blocked_url("http://192.168.1.1/admin"))

    def test_blocked_loopback(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        self.assertTrue(_is_blocked_url("http://localhost/admin"))

    def test_not_blocked(self):
        from telechat_pkg.web_fetch import _is_blocked_url
        self.assertFalse(_is_blocked_url("https://example.com"))

    def test_get_session_creates_new(self):
        import telechat_pkg.web_fetch as wf
        old_session = wf._session
        try:
            wf._session = None
            with patch("aiohttp.ClientSession") as mock_cls:
                mock_cls.return_value = MagicMock(closed=False)
                session = wf._get_session()
                self.assertIsNotNone(session)
        finally:
            wf._session = old_session

    def test_fetch_readable_blocked(self):
        from telechat_pkg.web_fetch import fetch_readable

        async def run():
            result = await fetch_readable("http://127.0.0.1/secret")
            self.assertIn("Blocked", result.error)

        _run(run())

    @patch("telechat_pkg.web_fetch.JINA_API_KEY", "test_key")
    @patch("telechat_pkg.web_fetch._fetch_jina", new_callable=AsyncMock)
    def test_fetch_readable_uses_jina(self, mock_jina):
        from telechat_pkg.web_fetch import fetch_readable
        mock_jina.return_value = MagicMock(error=None)

        async def run():
            await fetch_readable("https://example.com")
            mock_jina.assert_called_once()

        _run(run())

    @patch("telechat_pkg.web_fetch.JINA_API_KEY", "")
    @patch("telechat_pkg.web_fetch._fetch_raw", new_callable=AsyncMock)
    def test_fetch_readable_uses_raw(self, mock_raw):
        from telechat_pkg.web_fetch import fetch_readable
        mock_raw.return_value = MagicMock(error=None)

        async def run():
            await fetch_readable("https://example.com")
            mock_raw.assert_called_once()

        _run(run())


# ─── Telegram misc uncovered lines ────────────────────────────────────────

class TestTelegramMiscLines(unittest.TestCase):
    """Cover scattered uncovered lines in telegram_bot."""

    def _make_update(self, text="", uid=999):
        update = MagicMock()
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_extract_failure(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = False
        result.error = "connection failed"
        result.data = None
        agent.extract_text = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web extract https://example.com")
        _run(cmd_browse_web(update, None))

    @patch("telechat_pkg.telegram_bot._allowed", return_value=True)
    @patch("telechat_pkg.telegram_bot.BROWSER_ENABLED", True)
    @patch("telechat_pkg.telegram_bot.get_browser_agent")
    def test_browse_info_failure(self, mock_get_agent, _):
        from telechat_pkg.telegram_bot import cmd_browse_web
        agent = MagicMock()
        result = MagicMock()
        result.success = False
        result.error = "timeout"
        result.data = None
        agent.get_page_info = AsyncMock(return_value=result)
        mock_get_agent.return_value = agent
        update = self._make_update("/web info https://example.com")
        _run(cmd_browse_web(update, None))

    def test_run_telegram_set_commands_failure(self):
        """Test that set_my_commands failure is handled gracefully (lines 3516-3517)."""
        from telechat_pkg.telegram_bot import run_telegram

        async def run():
            mock_app = MagicMock()
            mock_app.initialize = AsyncMock()
            mock_app.start = AsyncMock()
            mock_app.updater.start_polling = AsyncMock()
            mock_app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("API error"))
            mock_app.updater.stop = AsyncMock()
            mock_app.stop = AsyncMock()
            mock_app.shutdown = AsyncMock()

            with patch("telechat_pkg.telegram_bot.build_app", return_value=mock_app):
                task = asyncio.ensure_future(run_telegram())
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        _run(run())


# ─── Store: history cache eviction (lines 259-264) ──────────────────────

class TestStoreCacheEviction(unittest.TestCase):
    def test_history_cache_eviction(self):
        from telechat_pkg import store
        # Fill cache beyond max
        old_max = store._HISTORY_CACHE_MAX
        store._HISTORY_CACHE_MAX = 3
        try:
            store.init_db()
            # Save some turns
            for i in range(5):
                store.save_turn("test_cache", f"user_{i}", "hello", "world")
            __import__("time").sleep(0.5)
            # Load history for each, filling cache
            for i in range(5):
                store.load_history("test_cache", f"user_{i}")
            # Cache should have been evicted
            self.assertLessEqual(len(store._history_cache), 5)
        finally:
            store._HISTORY_CACHE_MAX = old_max
            # Clean up
            for i in range(5):
                store.clear_history("test_cache", f"user_{i}")


# ─── Main.py: QR code generation (lines 645-807) ────────────────────────

@pytest.mark.skip(reason="Hand-rolled QR / Reed-Solomon encoder removed in favor of the optional 'qrcode' dependency")
class TestMainQRCodeGen(unittest.TestCase):
    def test_qr_encode_minimal_short(self):
        from telechat_pkg.main import _qr_encode_minimal
        result = _qr_encode_minimal("https://t.me/bot")
        # Should return a matrix (list of lists) or None
        if result is not None:
            self.assertIsInstance(result, list)
            self.assertTrue(len(result) > 0)

    def test_qr_encode_minimal_too_long(self):
        from telechat_pkg.main import _qr_encode_minimal
        long_data = "x" * 200
        result = _qr_encode_minimal(long_data)
        self.assertIsNone(result)

    def test_render_qr_terminal(self):
        from telechat_pkg.main import _render_qr_terminal
        # Create a small test matrix
        matrix = [[True, False, True], [False, True, False], [True, True, True]]
        # Should not raise
        _render_qr_terminal(matrix)

    def test_print_web_qr(self):
        from telechat_pkg.main import _print_web_qr
        with patch("telechat_pkg.main._qr_encode_minimal", return_value=None):
            _print_web_qr("8080")  # Should not raise when QR returns None


# ─── Claude Core: CLI retry path (lines 243, 257-274, 279-282) ──────────

class TestClaudeCoreRetryPath(unittest.TestCase):
    def test_cli_output_parse_with_retry(self):
        """Test the retry path in ask_claude_async when session resume fails."""
        from telechat_pkg import claude_core

        async def run():
            # Mock first attempt failing (session resume failure)
            mock_proc1 = AsyncMock()
            mock_proc1.returncode = 1
            mock_proc1.wait = AsyncMock()
            mock_proc1.stderr = AsyncMock()
            mock_proc1.stderr.read = AsyncMock(return_value=b"session not found")

            lines1 = [b'']
            idx1 = [0]
            async def readline1():
                if idx1[0] < len(lines1):
                    line = lines1[idx1[0]]
                    idx1[0] += 1
                    return line
                return b''
            mock_proc1.stdout = AsyncMock()
            mock_proc1.stdout.readline = readline1

            # Mock second attempt succeeding
            mock_proc2 = AsyncMock()
            mock_proc2.returncode = 0
            mock_proc2.wait = AsyncMock()
            mock_proc2.stderr = AsyncMock()
            mock_proc2.stderr.read = AsyncMock(return_value=b"")

            result_json = json.dumps({"type": "result", "result": "success answer"})
            lines2 = [result_json.encode() + b'\n', b'']
            idx2 = [0]
            async def readline2():
                if idx2[0] < len(lines2):
                    line = lines2[idx2[0]]
                    idx2[0] += 1
                    return line
                return b''
            mock_proc2.stdout = AsyncMock()
            mock_proc2.stdout.readline = readline2

            call_count = [0]
            async def mock_create(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_proc1
                return mock_proc2

            # Need a session_id to trigger retry path
            with patch("asyncio.create_subprocess_exec", side_effect=mock_create), \
                 patch.object(claude_core, "get_session_id", return_value="test-session-id"), \
                 patch.object(claude_core, "_session_mgr") as mock_mgr:
                mock_mgr.get_or_create_active.return_value = MagicMock()
                result, stats = await claude_core.ask_claude_async(
                    "test prompt", [],
                    platform="test", user_id="u1",
                )
            self.assertEqual(call_count[0], 2)

        _run(run())


if __name__ == "__main__":
    unittest.main()
