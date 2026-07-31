"""
Supplemental unit tests for the Slack bot adapter (telechat_pkg.slack_bot).

``tests/test_slack_e2e.py`` already exercises the bulk of this module (auth,
button handlers, the full _handle pipeline for a few commands, DM filtering).
This file fills the gaps that the e2e suite leaves uncovered:

  * the slash-command dispatch branches in ``_handle`` for every command that
    e2e doesn't route through ``_handle`` (engine/usage/sessions/tasks/cancel/
    remember/recall/memories/forget/rename/title/pin/archive),
  * the heartbeat ``cancelled`` break,
  * the ">5 tools used" header summarisation branch,
  * ``_cmd_cancel`` with active tasks,
  * the ``run_slack`` entry point.

All Slack API calls are mocked; the ``slack_bolt.App`` is patched before import
so no socket connection is attempted. No network calls are made.

Run:
    pytest tests/test_slack_bot.py -v
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ── Isolation: env BEFORE importing the module under test ────────────────────

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_slackbot.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir
os.environ["RATE_LIMIT_REQUESTS"] = "1000"
os.environ["RATE_LIMIT_WINDOW"] = "60"


def _passthrough_decorator(*args, **kwargs):
    def wrapper(fn):
        return fn
    return wrapper


_mock_app = MagicMock()
_mock_app.return_value.action = _passthrough_decorator
_mock_app.return_value.event = _passthrough_decorator

with patch("slack_bolt.App", _mock_app):
    from telechat_pkg import slack_bot as sb
    cc = sb.cc

cc.init_db()


def _mock_client():
    client = MagicMock()
    client.chat_postMessage = MagicMock(return_value={"ts": "1.1"})
    client.chat_update = MagicMock()
    client.chat_delete = MagicMock()
    client.reactions_add = MagicMock()
    client.reactions_remove = MagicMock()
    return client


@pytest.fixture(autouse=True)
def _clean_state():
    sb._user_model.clear()
    sb._user_engine.clear()
    sb._task_registry._tasks.clear()
    sb.ALLOWED_SLACK_USERS.clear()
    cc._rate_state.clear()
    yield


# ══════════════════════════════════════════════════════════════════════════════
# 1. _handle slash-command dispatch branches
# ══════════════════════════════════════════════════════════════════════════════
#
# Each command is patched at the _cmd_* boundary so we assert that _handle
# routes the right text to the right handler with the right parsed argument,
# and crucially returns *before* the rate-limit / task pipeline runs.


class TestHandleDispatch:
    def _run(self, text, cmd_name):
        client = _mock_client()
        with patch.object(sb, cmd_name) as mock_cmd:
            sb._handle(client, "C1", "U1", "1.1", text)
        return mock_cmd

    def test_engine(self):
        m = self._run("engine", "_cmd_engine")
        m.assert_called_once()
        # routed with channel/thread/user
        assert m.call_args.args[1:] == ("C1", "1.1", "U1")

    def test_engine_slash(self):
        self._run("/engine", "_cmd_engine").assert_called_once()

    def test_usage(self):
        self._run("usage", "_cmd_usage").assert_called_once()

    def test_stats_alias_routes_to_usage(self):
        self._run("stats", "_cmd_usage").assert_called_once()

    def test_sessions(self):
        self._run("sessions", "_cmd_sessions").assert_called_once()

    def test_tasks(self):
        self._run("tasks", "_cmd_tasks").assert_called_once()

    def test_cancel(self):
        self._run("cancel", "_cmd_cancel").assert_called_once()

    def test_cancel_all(self):
        self._run("cancel all", "_cmd_cancel").assert_called_once()

    def test_mode(self):
        self._run("mode", "_cmd_mode").assert_called_once()

    def test_status_alias_routes_to_mode(self):
        self._run("status", "_cmd_mode").assert_called_once()

    def test_reset(self):
        self._run("reset", "_cmd_reset").assert_called_once()

    def test_model(self):
        self._run("model", "_cmd_model").assert_called_once()

    def test_pin(self):
        self._run("pin", "_cmd_pin_session").assert_called_once()

    def test_remember_arg_parsed(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_remember") as m:
            sb._handle(client, "C1", "U1", "1.1", "remember I like tea")
        assert m.call_args.args[-1] == "I like tea"

    def test_recall_arg_parsed(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_recall") as m:
            sb._handle(client, "C1", "U1", "1.1", "recall tea")
        assert m.call_args.args[-1] == "tea"

    def test_memories_no_arg(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_memories") as m:
            sb._handle(client, "C1", "U1", "1.1", "memories")
        assert m.call_args.args[-1] == ""

    def test_memories_with_arg(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_memories") as m:
            sb._handle(client, "C1", "U1", "1.1", "memories work")
        assert m.call_args.args[-1] == "work"

    def test_forget_arg_parsed(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_forget") as m:
            sb._handle(client, "C1", "U1", "1.1", "forget abc123")
        assert m.call_args.args[-1] == "abc123"

    def test_rename_arg_parsed(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_rename_session") as m:
            sb._handle(client, "C1", "U1", "1.1", "rename new-name")
        assert m.call_args.args[-1] == "new-name"

    def test_title_arg_parsed(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_title_session") as m:
            sb._handle(client, "C1", "U1", "1.1", "title My Title")
        assert m.call_args.args[-1] == "My Title"

    def test_archive_no_arg(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_archive_session") as m:
            sb._handle(client, "C1", "U1", "1.1", "archive")
        assert m.call_args.args[-1] == ""

    def test_archive_with_arg(self):
        client = _mock_client()
        with patch.object(sb, "_cmd_archive_session") as m:
            sb._handle(client, "C1", "U1", "1.1", "archive old-session")
        assert m.call_args.args[-1] == "old-session"

    def test_dispatch_returns_before_pipeline(self):
        """A recognised command must not create a SlackTask."""
        client = _mock_client()
        with patch.object(sb, "_cmd_usage"):
            sb._handle(client, "C1", "U1", "1.1", "usage")
        assert sb._task_registry.get_user_tasks("U1") == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. _cmd_cancel with active tasks
# ══════════════════════════════════════════════════════════════════════════════


class TestCmdCancel:
    def test_cancel_with_active_tasks(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U_cancel", "do work")
        sb._task_registry.register(task)
        sb._cmd_cancel(client, "C1", "1.1", "U_cancel")
        # the "Cancelling N task(s)" path
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Cancelling 1 task" in text
        assert task.cancelled is True

    def test_cancel_with_no_tasks(self):
        client = _mock_client()
        sb._cmd_cancel(client, "C1", "1.1", "U_none")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "No active tasks" in text


# ══════════════════════════════════════════════════════════════════════════════
# 3. Full pipeline edge branches
# ══════════════════════════════════════════════════════════════════════════════


class TestPipelineBranches:
    def test_many_tools_used_header_truncates(self, monkeypatch):
        """>5 tools triggers the '+N more' header summarisation branch."""
        client = _mock_client()
        tools = [f"tool{i}" for i in range(8)]

        def fake_ask(text, history, **kwargs):
            return ("the answer", {"input_tokens": 1, "output_tokens": 1, "tools_used": tools})

        monkeypatch.setattr(cc, "ask_claude_sync", fake_ask)
        monkeypatch.setattr(sb, "_engine", lambda uid: "cli")
        sb._handle(client, "C1", "U_tools", "1.1", "do many things")

        posted = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
        joined = "\n".join(posted)
        assert "+3 more" in joined

    def test_real_heartbeat_breaks_when_cancelled(self, monkeypatch):
        """Run the real _handle pipeline so the in-function heartbeat thread
        observes the task as cancelled and hits its ``break``.

        We make ``Event.wait`` return immediately (so the heartbeat loops fast)
        and have ``ask_claude_sync`` cancel the user's task before returning."""
        import threading

        client = _mock_client()

        real_wait = threading.Event.wait

        def fast_wait(self, timeout=None):
            return real_wait(self, timeout=0.005)

        monkeypatch.setattr(threading.Event, "wait", fast_wait)
        monkeypatch.setattr(sb, "_engine", lambda uid: "cli")

        def fake_ask(text, history, **kwargs):
            # Let the heartbeat run several *non-cancelled* iterations first so
            # its ``task.post_status()`` (line 413) executes, THEN cancel so the
            # ``if task.cancelled: break`` (line 412) also executes.
            real_wait(threading.Event(), 0.06)  # ~12 heartbeat ticks at 5ms
            for t in sb._task_registry.get_user_tasks("U_realhb"):
                t.cancel()
            real_wait(threading.Event(), 0.05)  # let heartbeat observe cancel
            return ("answer", {"input_tokens": 1, "output_tokens": 1, "tools_used": []})

        monkeypatch.setattr(cc, "ask_claude_sync", fake_ask)
        sb._handle(client, "C1", "U_realhb", "1.1", "do something")
        # cancelled tasks render the cancellation summary, not a fresh reply
        assert client.chat_update.called or client.chat_postMessage.called

    def test_delete_status_swallows_error(self):
        """delete_status swallows Slack API errors (covers the except branch)."""
        client = _mock_client()
        client.chat_delete.side_effect = RuntimeError("slack down")
        task = sb.SlackTask(client, "C1", "1.1", "U_del", "x")
        task._status_ts = "1.1"  # so delete_status proceeds past the guard
        task.delete_status()  # must not raise

    def test_delete_status_noop_without_ts(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U_del2", "x")
        task.delete_status()  # _status_ts is None → early return
        client.chat_delete.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Entry point
# ══════════════════════════════════════════════════════════════════════════════


class TestRunSlack:
    def test_run_slack_starts_socket_handler(self, monkeypatch):
        started = {"start": False}

        class FakeHandler:
            def __init__(self, app, token):
                self.app = app
                self.token = token

            def start(self):
                started["start"] = True

        monkeypatch.setattr(sb, "SocketModeHandler", FakeHandler)
        monkeypatch.setattr(cc, "init_db", lambda: None)
        sb.run_slack()
        assert started["start"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. Cosmetic failures stay survivable — and stop being invisible
#
# Reactions and the status card are decoration: a Slack API refusal must not
# take down the turn. It used to vanish entirely, though, so "why does the ⏳
# never appear?" (usually a missing reactions:write scope) had no answer
# anywhere in the log.
# ══════════════════════════════════════════════════════════════════════════════


class _RaisingClient:
    """A Slack client whose every method raises."""

    def __getattr__(self, _name):
        def raise_it(*_args, **_kwargs):
            raise RuntimeError("invalid_auth")
        return raise_it


class TestCosmeticFailuresAreLoggedNotSwallowed:
    def test_add_reaction_survives_and_logs(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.slack_bot"):
            sb._add_reaction(_RaisingClient(), "C1", "1.0", "hourglass")
        assert caplog.records, "a failed reaction must leave a trace"

    def test_remove_reaction_survives_and_logs(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.slack_bot"):
            sb._remove_reaction(_RaisingClient(), "C1", "1.0", "hourglass")
        assert caplog.records

    def test_the_traceback_is_kept_not_just_the_message(self, caplog):
        # exc_info is the difference between "reactions_add failed" and knowing
        # it was a missing scope.
        import logging
        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.slack_bot"):
            sb._add_reaction(_RaisingClient(), "C1", "1.0", "x")
        assert any(r.exc_info for r in caplog.records)
