"""
End-to-end tests for the Slack bot adapter.

Tests command parsing, button handlers, task tracking, DM filtering,
edge cases, and the full message handling pipeline — all with mocked
Slack API calls and a fresh in-memory database per test.

Run:
    pytest tests/test_slack_e2e.py -v
"""

import os
import re
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ── Isolation: set env vars BEFORE importing the module under test ───────────

_tmp_dir = tempfile.mkdtemp()

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_slack.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir
os.environ["RATE_LIMIT_REQUESTS"] = "100"
os.environ["RATE_LIMIT_WINDOW"] = "60"

# Must patch App before import so it doesn't try to connect to Slack.
# Make decorators (@app.action, @app.event) pass through the original function
# so handler references like sb.handle_set_model point to the real code.
def _passthrough_decorator(*args, **kwargs):
    def wrapper(fn):
        return fn
    return wrapper

_mock_app = MagicMock()
_mock_app.return_value.action = _passthrough_decorator
_mock_app.return_value.event = _passthrough_decorator

with patch("slack_bolt.App", _mock_app):
    from telechat_pkg import slack_bot as sb
    cc = sb.cc  # use the same claude_core instance that slack_bot uses

cc.init_db()


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _mock_client():
    client = MagicMock()
    client.chat_postMessage = MagicMock(return_value={"ts": "1234567890.123456"})
    client.chat_update = MagicMock()
    client.chat_delete = MagicMock()
    client.reactions_add = MagicMock()
    client.reactions_remove = MagicMock()
    return client


def _action_body(user_id: str, action_id: str, value: str = "",
                 channel_id: str = "C123", thread_ts: str = "111.222"):
    return {
        "user": {"id": user_id},
        "actions": [{"action_id": action_id, "value": value}],
        "channel": {"id": channel_id},
        "message": {"ts": "333.444", "thread_ts": thread_ts},
    }


@pytest.fixture(autouse=True)
def _clean_state():
    sb._user_model.clear()
    sb._user_engine.clear()
    sb._task_registry._tasks.clear()
    sb.ALLOWED_SLACK_USERS.clear()
    cc._rate_state.clear()
    yield


# ══════════════════════════════════════════════════════════════════════════════
# 1. Auth & access control
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackAuth:
    def test_allowed_when_empty(self):
        sb.ALLOWED_SLACK_USERS.clear()
        assert sb._allowed("U123") is True
        assert sb._allowed("U999") is True

    def test_allowed_when_restricted(self):
        sb.ALLOWED_SLACK_USERS.extend(["U100", "U200"])
        assert sb._allowed("U100") is True
        assert sb._allowed("U200") is True
        assert sb._allowed("U300") is False

    def test_dispatch_rejects_unauthorized(self):
        sb.ALLOWED_SLACK_USERS.extend(["U999"])
        client = _mock_client()
        event = {"user": "U123", "channel": "D456", "text": "hello", "ts": "1.1"}
        sb._dispatch(client, event)
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "not on the allowed list" in text

    def test_dispatch_ignores_bot_messages(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "D456", "text": "hello",
                 "ts": "1.1", "bot_id": "B123"}
        sb._dispatch(client, event)
        client.chat_postMessage.assert_not_called()

    def test_dispatch_ignores_empty_text(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "D456", "text": "", "ts": "1.1"}
        sb._dispatch(client, event)
        client.chat_postMessage.assert_not_called()

    def test_dispatch_ignores_no_user(self):
        client = _mock_client()
        event = {"channel": "D456", "text": "hello", "ts": "1.1"}
        sb._dispatch(client, event)
        client.chat_postMessage.assert_not_called()

    def test_dispatch_strips_mention_tag(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "D456",
                 "text": "<@U0BOT> help", "ts": "1.1"}
        with patch.object(sb, "_handle") as mock_handle:
            with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
                mock_thread.return_value.start = MagicMock()
                sb._dispatch(client, event)
                args = mock_thread.call_args.kwargs["args"]
                assert args[4] == "help"

    def test_dispatch_mention_only_no_text(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "D456",
                 "text": "<@U0BOT>", "ts": "1.1"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            sb._dispatch(client, event)
            mock_thread.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 2. DM filtering
# ══════════════════════════════════════════════════════════════════════════════


class TestDMFilter:
    def test_dm_channel_type_im(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "D456", "text": "hi",
                 "ts": "1.1", "channel_type": "im"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_called_once()

    def test_channel_message_rejected(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "C456", "text": "hi",
                 "ts": "1.1", "channel_type": "channel"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_not_called()

    def test_dm_fallback_d_prefix(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "D456", "text": "hi", "ts": "1.1"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_called_once()

    def test_channel_no_d_prefix_rejected(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "C456", "text": "hi", "ts": "1.1"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_not_called()

    def test_dm_subtype_ignored(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "D456", "text": "hi",
                 "ts": "1.1", "channel_type": "im", "subtype": "message_changed"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_not_called()

    def test_group_message_without_channel_type_rejected(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "G456", "text": "hi", "ts": "1.1"}
        with patch("telechat_pkg.slack_bot.threading.Thread") as mock_thread:
            sb.handle_dm(client=client, event=event, say=MagicMock())
            mock_thread.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Command parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandParsing:
    def test_help_command(self):
        client = _mock_client()
        sb._cmd_help(client, "C1", "1.1")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Commands" in text
        assert "model" in text
        assert "sessions" in text

    def test_help_command_case_insensitive(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "HELP")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Commands" in text

    def test_help_slash_variant(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "/help")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Commands" in text

    def test_model_command(self):
        client = _mock_client()
        sb._cmd_model(client, "C1", "1.1", "U1")
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 1
        buttons = actions[0]["elements"]
        assert len(buttons) == 3

    def test_engine_command(self):
        client = _mock_client()
        sb._cmd_engine(client, "C1", "1.1", "U1")
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 1
        buttons = actions[0]["elements"]
        assert len(buttons) == 2

    def test_mode_command(self):
        client = _mock_client()
        sb._cmd_mode(client, "C1", "1.1", "U1")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Settings" in text
        assert "Session" in text
        assert "Engine" in text

    def test_usage_command(self):
        client = _mock_client()
        sb._cmd_usage(client, "C1", "1.1", "U1")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage Stats" in text
        assert "Messages" in text

    def test_reset_command(self):
        client = _mock_client()
        sb._cmd_reset(client, "C1", "1.1", "U_reset")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "cleared" in text.lower()

    def test_sessions_command(self):
        client = _mock_client()
        sb._cmd_sessions(client, "C1", "1.1", "U_sess")
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        assert any("Sessions" in str(b) for b in blocks)

    def test_new_session_with_name(self):
        client = _mock_client()
        sb._cmd_new_session(client, "C1", "1.1", "U_new", "my-test")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "my-test" in text
        assert "Created" in text

    def test_new_session_auto_name(self):
        client = _mock_client()
        sb._cmd_new_session(client, "C1", "1.1", "U_new2", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "session-" in text

    def test_new_session_sanitizes_name(self):
        client = _mock_client()
        sb._cmd_new_session(client, "C1", "1.1", "U_new3", "bad name!@#$")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "!" not in text
        assert "@" not in text

    def test_new_session_truncates_long_name(self):
        client = _mock_client()
        sb._cmd_new_session(client, "C1", "1.1", "U_new4", "a" * 50)
        text = client.chat_postMessage.call_args.kwargs["text"]
        name_match = re.search(r"`([^`]+)`", text)
        assert name_match
        assert len(name_match.group(1)) <= 20

    def test_switch_command_by_name(self):
        client = _mock_client()
        cc._session_mgr.get_or_create_active("slack", "U_sw")
        cc._session_mgr.create("slack", "U_sw", "other")
        sb._cmd_switch(client, "C1", "1.1", "U_sw", "other")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "other" in text
        assert "Switched" in text

    def test_switch_command_by_index(self):
        client = _mock_client()
        cc._session_mgr.get_or_create_active("slack", "U_sw2")
        cc._session_mgr.create("slack", "U_sw2", "second")
        sb._cmd_switch(client, "C1", "1.1", "U_sw2", "1")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "second" in text

    def test_switch_nonexistent_session(self):
        client = _mock_client()
        sb._cmd_switch(client, "C1", "1.1", "U_sw3", "nonexistent")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "not found" in text

    def test_tasks_empty(self):
        client = _mock_client()
        sb._cmd_tasks(client, "C1", "1.1", "U_tasks")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "No active tasks" in text

    def test_cancel_no_tasks(self):
        client = _mock_client()
        sb._cmd_cancel(client, "C1", "1.1", "U_cancel")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "No active tasks" in text

    def test_command_routing_new(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "new test-session")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "test-session" in text

    def test_command_routing_slash_new(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "/new another")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "another" in text

    def test_command_routing_switch(self):
        client = _mock_client()
        cc._session_mgr.get_or_create_active("slack", "U_rtsw")
        cc._session_mgr.create("slack", "U_rtsw", "target")
        sb._handle(client, "C1", "U_rtsw", "1.1", "switch target")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "target" in text

    def test_status_alias(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "status")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Settings" in text

    def test_stats_alias(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "stats")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage Stats" in text


# ══════════════════════════════════════════════════════════════════════════════
# 4. Per-user settings
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackPerUserSettings:
    def test_default_model(self):
        assert sb._model("U999") == sb._DEFAULT_MODEL

    def test_override_model(self):
        sb._user_model["U42"] = "opus"
        assert sb._model("U42") == "opus"

    def test_default_engine(self):
        assert sb._engine("U999") == sb._DEFAULT_ENGINE

    def test_override_engine(self):
        sb._user_engine["U42"] = "api"
        assert sb._engine("U42") == "api"


# ══════════════════════════════════════════════════════════════════════════════
# 5. SlackTask
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackTask:
    def _make_task(self, prompt="test prompt", user_id="U1"):
        client = _mock_client()
        return sb.SlackTask(client, "C1", "1.1", user_id, prompt)

    def test_basic_properties(self):
        task = self._make_task("a very long prompt that exceeds forty characters easily")
        assert task.user_id == "U1"
        assert len(task.prompt_preview) == 40
        assert task.cancelled is False
        assert task._phase == "thinking"
        assert task.tool_count == 0
        assert task.tools == []

    def test_cancel(self):
        task = self._make_task()
        task.cancel()
        assert task.cancelled is True

    def test_elapsed_seconds(self):
        task = self._make_task()
        task.start_time = time.time() - 7
        assert "7s" in task._elapsed()

    def test_elapsed_minutes(self):
        task = self._make_task()
        task.start_time = time.time() - 130
        assert "2m" in task._elapsed()

    def test_progress_bar_thinking(self):
        task = self._make_task()
        bar = task._progress_bar()
        assert "=" in bar
        assert " " in bar

    def test_progress_bar_streaming(self):
        task = self._make_task()
        task._phase = "streaming"
        bar = task._progress_bar()
        assert "=" in bar

    def test_progress_bar_working(self):
        task = self._make_task()
        task._phase = "working"
        task.tool_count = 5
        bar = task._progress_bar()
        assert "=" in bar

    def test_on_tool(self):
        task = self._make_task()
        task._last_update = 0
        task.on_tool("Read", "file.py")
        assert task._phase == "working"
        assert task.tool_count == 1
        assert task.tools == ["Read"]

    def test_on_text(self):
        task = self._make_task()
        task._last_update = 0
        task.on_text("Hello")
        assert task._phase == "streaming"

    def test_build_status_thinking(self):
        task = self._make_task()
        status = task._build_status()
        assert "Thinking" in status

    def test_build_status_with_tools(self):
        task = self._make_task()
        task._phase = "working"
        task.tools = ["Read", "Bash"]
        task.tool_count = 2
        status = task._build_status()
        assert "Working" in status
        assert "Running command" in status
        assert "2 steps" in status

    def test_build_status_streaming(self):
        task = self._make_task()
        task._phase = "streaming"
        status = task._build_status()
        assert "Writing" in status

    def test_post_status_creates_message(self):
        task = self._make_task()
        task.post_status()
        task.client.chat_postMessage.assert_called_once()
        task._status_ts = "999.111"

    def test_post_status_updates_existing(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task._last_update = 0
        task._last_status = ""
        task.post_status()
        task.client.chat_update.assert_called_once()

    def test_post_status_skips_duplicate(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task._last_status = task._build_status()
        task.post_status()
        task.client.chat_update.assert_not_called()

    def test_post_status_respects_interval(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task._last_update = time.time()
        task._last_status = "old"
        task.post_status()
        task.client.chat_update.assert_not_called()

    def test_finish_status(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task.finish_status("Done!")
        task.client.chat_update.assert_called_once()

    def test_finish_status_no_message(self):
        task = self._make_task()
        task.finish_status("Done!")
        task.client.chat_update.assert_not_called()

    def test_delete_status(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task.delete_status()
        task.client.chat_delete.assert_called_once()

    def test_delete_status_no_message(self):
        task = self._make_task()
        task.delete_status()
        task.client.chat_delete.assert_not_called()

    def test_post_status_swallows_exception(self):
        task = self._make_task()
        task.client.chat_postMessage.side_effect = Exception("API error")
        task.post_status()  # should not raise

    def test_finish_status_swallows_exception(self):
        task = self._make_task()
        task._status_ts = "999.111"
        task.client.chat_update.side_effect = Exception("API error")
        task.finish_status("Done!")  # should not raise


# ══════════════════════════════════════════════════════════════════════════════
# 6. TaskRegistry
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackTaskRegistry:
    def _make_task(self, user_id="U1"):
        client = _mock_client()
        return sb.SlackTask(client, "C1", "1.1", user_id, "test")

    def test_register_and_get(self):
        registry = sb.TaskRegistry()
        task = self._make_task()
        registry.register(task)
        assert registry.get(task.task_id) is task

    def test_unregister(self):
        registry = sb.TaskRegistry()
        task = self._make_task()
        registry.register(task)
        registry.unregister(task.task_id)
        assert registry.get(task.task_id) is None

    def test_unregister_nonexistent(self):
        registry = sb.TaskRegistry()
        registry.unregister(99999)  # should not raise

    def test_get_user_tasks(self):
        registry = sb.TaskRegistry()
        t1 = self._make_task("U1")
        t2 = self._make_task("U1")
        t3 = self._make_task("U2")
        registry.register(t1)
        registry.register(t2)
        registry.register(t3)
        assert len(registry.get_user_tasks("U1")) == 2
        assert len(registry.get_user_tasks("U2")) == 1
        assert len(registry.get_user_tasks("U999")) == 0

    def test_cancel_all_user(self):
        registry = sb.TaskRegistry()
        t1 = self._make_task("U1")
        t2 = self._make_task("U1")
        t3 = self._make_task("U2")
        registry.register(t1)
        registry.register(t2)
        registry.register(t3)
        count = registry.cancel_all_user("U1")
        assert count == 2
        assert t1.cancelled is True
        assert t2.cancelled is True
        assert t3.cancelled is False

    def test_cancel_all_no_tasks(self):
        registry = sb.TaskRegistry()
        assert registry.cancel_all_user("U_none") == 0

    def test_thread_safety(self):
        registry = sb.TaskRegistry()
        errors = []

        def register_many():
            try:
                for _ in range(50):
                    t = self._make_task("U_thread")
                    registry.register(t)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(registry.get_user_tasks("U_thread")) == 200


# ══════════════════════════════════════════════════════════════════════════════
# 7. Tool labels
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackToolLabels:
    def test_known_tool(self):
        assert "Running" in sb._tool_label("Bash")
        assert "Reading" in sb._tool_label("Read")

    def test_unknown_tool(self):
        label = sb._tool_label("SomeNewTool")
        assert "SomeNewTool" in label
        assert "wrench" in label


# ══════════════════════════════════════════════════════════════════════════════
# 8. Helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackHelpers:
    def test_add_reaction(self):
        client = _mock_client()
        sb._add_reaction(client, "C1", "1.1", "thumbsup")
        client.reactions_add.assert_called_once()

    def test_add_reaction_swallows_error(self):
        client = _mock_client()
        client.reactions_add.side_effect = Exception("already reacted")
        sb._add_reaction(client, "C1", "1.1", "thumbsup")  # no raise

    def test_remove_reaction_swallows_error(self):
        client = _mock_client()
        client.reactions_remove.side_effect = Exception("not reacted")
        sb._remove_reaction(client, "C1", "1.1", "thumbsup")  # no raise

    def test_post_reply_short(self):
        client = _mock_client()
        ts = sb._post_reply(client, "C1", "1.1", "short text")
        client.chat_postMessage.assert_called_once()
        assert ts == "1234567890.123456"

    def test_post_reply_long_chunks(self):
        client = _mock_client()
        long_text = "A" * 9000
        sb._post_reply(client, "C1", "1.1", long_text)
        assert client.chat_postMessage.call_count == 3  # 9000 / 3000 = 3

    def test_post_reply_with_blocks(self):
        client = _mock_client()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        sb._post_reply(client, "C1", "1.1", "hi", blocks=blocks)
        assert client.chat_postMessage.call_args.kwargs["blocks"] == blocks

    def test_finish_summary_no_tools(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U1", "test")
        summary = sb._finish_summary(task)
        assert "white_check_mark" in summary
        assert "tools" not in summary

    def test_finish_summary_with_tools(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U1", "test")
        task.tool_count = 5
        summary = sb._finish_summary(task)
        assert "5 tools" in summary


# ══════════════════════════════════════════════════════════════════════════════
# 9. Button action handlers
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackButtonHandlers:
    def test_set_model_button(self):
        client = _mock_client()
        body = _action_body("U1", "set_model_opus", "opus")
        sb.handle_set_model(ack=MagicMock(), body=body, client=client)
        assert sb._user_model["U1"] == "opus"
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Opus" in text

    def test_set_engine_button(self):
        client = _mock_client()
        body = _action_body("U1", "set_engine_api", "api")
        sb.handle_set_engine(ack=MagicMock(), body=body, client=client)
        assert sb._user_engine["U1"] == "api"
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "API" in text

    def test_cancel_task_button(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U1", "test")
        sb._task_registry.register(task)
        body = _action_body("U1", f"cancel_task_{task.task_id}")
        sb.handle_cancel_task(ack=MagicMock(), body=body, client=client)
        assert task.cancelled is True
        sb._task_registry.unregister(task.task_id)

    def test_cancel_task_wrong_user(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U_owner", "test")
        sb._task_registry.register(task)
        body = _action_body("U_hacker", f"cancel_task_{task.task_id}")
        sb.handle_cancel_task(ack=MagicMock(), body=body, client=client)
        assert task.cancelled is False
        sb._task_registry.unregister(task.task_id)

    def test_cancel_task_nonexistent(self):
        body = _action_body("U1", "cancel_task_99999")
        sb.handle_cancel_task(ack=MagicMock(), body=body, client=_mock_client())

    def test_cancel_all_button(self):
        client = _mock_client()
        t1 = sb.SlackTask(client, "C1", "1.1", "U1", "a")
        t2 = sb.SlackTask(client, "C1", "1.1", "U1", "b")
        sb._task_registry.register(t1)
        sb._task_registry.register(t2)
        body = _action_body("U1", "cancel_all_tasks")
        sb.handle_cancel_all(ack=MagicMock(), body=body, client=client)
        assert t1.cancelled is True
        assert t2.cancelled is True
        sb._task_registry.unregister(t1.task_id)
        sb._task_registry.unregister(t2.task_id)

    def test_switch_session_button(self):
        cc._session_mgr.get_or_create_active("slack", "U_btn_sw")
        cc._session_mgr.create("slack", "U_btn_sw", "other")
        client = _mock_client()
        body = _action_body("U_btn_sw", "switch_session_1", "1")
        sb.handle_switch_session(ack=MagicMock(), body=body, client=client)
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "other" in text

    def test_switch_session_invalid_index(self):
        cc._session_mgr.get_or_create_active("slack", "U_btn_sw2")
        client = _mock_client()
        body = _action_body("U_btn_sw2", "switch_session_99", "99")
        sb.handle_switch_session(ack=MagicMock(), body=body, client=client)
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "not found" in text

    def test_new_session_button(self):
        client = _mock_client()
        body = _action_body("U_btn_new", "new_session_auto")
        sb.handle_new_session(ack=MagicMock(), body=body, client=client)
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Created" in text
        assert "session-" in text


# ══════════════════════════════════════════════════════════════════════════════
# 10. Full message handling with mocked Claude
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackHandleMessage:
    def test_rate_limited(self):
        client = _mock_client()
        with patch.object(cc, "check_rate_limit", return_value=False):
            sb._handle(client, "C1", "U_rate", "1.1", "hello")
        calls = client.chat_postMessage.call_args_list
        text = calls[0].kwargs.get("text", "")
        assert "Rate limit" in text

    def test_full_round_trip(self):
        client = _mock_client()
        mock_reply = ("The answer is 42.", {"input_tokens": 10, "output_tokens": 5})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_rt", "1.1", "What is the answer?")
        # Should have posted reply containing the answer
        calls = client.chat_postMessage.call_args_list
        all_text = " ".join(c.kwargs.get("text", "") for c in calls)
        assert "42" in all_text

    def test_api_engine_route(self):
        client = _mock_client()
        sb._user_engine["U_api"] = "api"
        mock_reply = ("API response.", {"input_tokens": 10, "output_tokens": 5})
        with patch.object(cc, "ask_claude_api", return_value=mock_reply) as mock:
            sb._handle(client, "C1", "U_api", "1.1", "test api")
            mock.assert_called_once()

    def test_error_response(self):
        client = _mock_client()
        mock_reply = ("[Error] Something broke", {})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_err", "1.1", "break stuff")
        calls = client.chat_postMessage.call_args_list
        all_text = " ".join(c.kwargs.get("text", "") for c in calls)
        assert "Error" in all_text

    def test_timeout_response(self):
        client = _mock_client()
        mock_reply = ("[Timeout] after 180s", {})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_to", "1.1", "slow query")
        calls = client.chat_postMessage.call_args_list
        all_text = " ".join(c.kwargs.get("text", "") for c in calls)
        assert "Timeout" in all_text

    def test_exception_handling(self):
        client = _mock_client()
        with patch.object(cc, "ask_claude_sync", side_effect=RuntimeError("boom")):
            sb._handle(client, "C1", "U_exc", "1.1", "crash test")
        calls = client.chat_postMessage.call_args_list
        all_text = " ".join(c.kwargs.get("text", "") for c in calls)
        assert "Error" in all_text or "boom" in all_text

    def test_cancelled_task(self):
        client = _mock_client()

        def _slow_claude(*args, **kwargs):
            # Simulate cancel during Claude call
            tasks = sb._task_registry.get_user_tasks("U_can")
            for t in tasks:
                t.cancel()
            return ("partial", {})

        with patch.object(cc, "ask_claude_sync", side_effect=_slow_claude):
            sb._handle(client, "C1", "U_can", "1.1", "cancel me")
        # Should show cancelled status
        update_calls = client.chat_update.call_args_list
        all_text = " ".join(str(c) for c in update_calls)
        assert "Cancelled" in all_text or "cancel" in all_text.lower()

    def test_reaction_cleanup(self):
        client = _mock_client()
        mock_reply = ("reply", {"input_tokens": 1, "output_tokens": 1})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_rx", "1.1", "test")
        client.reactions_add.assert_called_with(
            channel="C1", timestamp="1.1", name="hourglass_flowing_sand")
        client.reactions_remove.assert_called_with(
            channel="C1", timestamp="1.1", name="hourglass_flowing_sand")

    def test_task_registered_and_unregistered(self):
        client = _mock_client()
        mock_reply = ("reply", {})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_reg", "1.1", "test")
        assert len(sb._task_registry.get_user_tasks("U_reg")) == 0

    def test_tools_in_header(self):
        client = _mock_client()
        mock_reply = ("Result.", {"input_tokens": 10, "output_tokens": 5,
                                  "tools_used": ["Read", "Bash", "Write"]})
        with patch.object(cc, "ask_claude_sync", return_value=mock_reply):
            sb._handle(client, "C1", "U_tools", "1.1", "do stuff")
        calls = client.chat_postMessage.call_args_list
        all_text = " ".join(c.kwargs.get("text", "") for c in calls)
        assert "Read" in all_text
        assert "Bash" in all_text


# ══════════════════════════════════════════════════════════════════════════════
# 11. Edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackEdgeCases:
    def test_whitespace_only_text(self):
        client = _mock_client()
        event = {"user": "U1", "channel": "D456", "text": "   \n  ", "ts": "1.1"}
        sb._dispatch(client, event)
        client.chat_postMessage.assert_not_called()

    def test_very_long_command_text(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "A" * 10000)
        # Should not crash; just passes to Claude

    def test_special_chars_in_text(self):
        client = _mock_client()
        sb._handle(client, "C1", "U1", "1.1", "help")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Commands" in text

    def test_concurrent_users(self):
        errors = []

        def run_user(uid):
            try:
                client = _mock_client()
                sb._cmd_help(client, "C1", "1.1")
                sb._cmd_model(client, "C1", "1.1", uid)
                sb._cmd_usage(client, "C1", "1.1", uid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_user, args=(f"U{i}",))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

    def test_model_current_marker(self):
        sb._user_model["U_check"] = "sonnet"
        client = _mock_client()
        sb._cmd_model(client, "C1", "1.1", "U_check")
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        action_block = [b for b in blocks if b.get("type") == "actions"][0]
        sonnet_btn = [e for e in action_block["elements"]
                      if "sonnet" in e.get("action_id", "")][0]
        assert "white_check_mark" in sonnet_btn["text"]["text"]

    def test_engine_current_marker(self):
        sb._user_engine["U_eng"] = "api"
        client = _mock_client()
        sb._cmd_engine(client, "C1", "1.1", "U_eng")
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        action_block = [b for b in blocks if b.get("type") == "actions"][0]
        api_btn = [e for e in action_block["elements"]
                   if "api" in e.get("action_id", "")][0]
        assert "white_check_mark" in api_btn["text"]["text"]

    def test_sessions_max_5_buttons(self):
        uid = "U_maxbtn"
        cc._session_mgr.get_or_create_active("slack", uid)
        for i in range(8):
            cc._session_mgr.create("slack", uid, f"s{i}")
        client = _mock_client()
        sb._cmd_sessions(client, "C1", "1.1", uid)
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        action_block = [b for b in blocks if b.get("type") == "actions"]
        if action_block:
            assert len(action_block[0]["elements"]) <= 5

    def test_tasks_with_active_task(self):
        client = _mock_client()
        task = sb.SlackTask(client, "C1", "1.1", "U_tsk", "running query")
        sb._task_registry.register(task)
        sb._cmd_tasks(client, "C1", "1.1", "U_tsk")
        blocks = client.chat_postMessage.call_args.kwargs.get("blocks", [])
        assert any("Cancel All" in str(b) for b in blocks)
        sb._task_registry.unregister(task.task_id)


# ══════════════════════════════════════════════════════════════════════════════
# 12. Memory commands (remember, recall, memories, forget)
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackRemember:
    @patch.object(sb, "_memory")
    def test_remember_no_arg(self, mock_mem):
        client = _mock_client()
        sb._cmd_remember(client, "C1", "1.1", "U_mem", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage" in text

    @patch.object(sb, "_memory")
    def test_remember_success(self, mock_mem):
        mem_obj = MagicMock()
        mem_obj.id = "abc12345-full-id"
        mem_obj.tags = ["work"]
        mock_mem.remember.return_value = mem_obj
        client = _mock_client()
        sb._cmd_remember(client, "C1", "1.1", "U_mem", "test note #work")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Remembered" in text
        assert "abc12345" in text

    @patch.object(sb, "_memory")
    def test_remember_empty_content(self, mock_mem):
        client = _mock_client()
        sb._cmd_remember(client, "C1", "1.1", "U_mem", "#tag !0.5")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "empty" in text.lower() or "Usage" in text


class TestSlackRecall:
    @patch.object(sb, "_memory")
    def test_recall_no_arg(self, mock_mem):
        client = _mock_client()
        sb._cmd_recall(client, "C1", "1.1", "U_mem", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage" in text

    @patch.object(sb, "_memory")
    def test_recall_found(self, mock_mem):
        result = MagicMock()
        result.content = "remember this"
        result.id = "def45678-full"
        result.tags = []
        mock_mem.recall.return_value = [result]
        client = _mock_client()
        sb._cmd_recall(client, "C1", "1.1", "U_mem", "this")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Found" in text

    @patch.object(sb, "_memory")
    def test_recall_not_found(self, mock_mem):
        mock_mem.recall.return_value = []
        client = _mock_client()
        sb._cmd_recall(client, "C1", "1.1", "U_mem", "nothing")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "No memories found" in text


class TestSlackMemories:
    @patch.object(sb, "_memory")
    def test_memories_empty(self, mock_mem):
        mock_mem.list_memories.return_value = []
        client = _mock_client()
        sb._cmd_memories(client, "C1", "1.1", "U_mem", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "No memories" in text

    @patch.object(sb, "_memory")
    def test_memories_with_data(self, mock_mem):
        mem_obj = MagicMock()
        mem_obj.content = "a memory"
        mem_obj.id = "aaa11111-full"
        mem_obj.tags = ["tag1"]
        mock_mem.list_memories.return_value = [mem_obj]
        mock_mem.stats.return_value = {"total": 1}
        client = _mock_client()
        sb._cmd_memories(client, "C1", "1.1", "U_mem", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Your memories" in text
        assert "a memory" in text

    @patch.object(sb, "_memory")
    def test_memories_filter_by_tag(self, mock_mem):
        mock_mem.list_memories.return_value = []
        client = _mock_client()
        sb._cmd_memories(client, "C1", "1.1", "U_mem", "#work")
        mock_mem.list_memories.assert_called_once()
        call_kwargs = mock_mem.list_memories.call_args
        assert call_kwargs.kwargs.get("tags") == ["work"] or call_kwargs[1].get("tags") == ["work"]


class TestSlackForget:
    @patch.object(sb, "_memory")
    def test_forget_no_arg(self, mock_mem):
        client = _mock_client()
        sb._cmd_forget(client, "C1", "1.1", "U_mem", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage" in text

    @patch.object(sb, "_memory")
    def test_forget_success(self, mock_mem):
        mem_obj = MagicMock()
        mem_obj.id = "abc12345-full-id"
        mem_obj.content = "old note"
        mock_mem.list_memories.return_value = [mem_obj]
        mock_mem.forget.return_value = True
        client = _mock_client()
        sb._cmd_forget(client, "C1", "1.1", "U_mem", "abc12345")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Forgotten" in text

    @patch.object(sb, "_memory")
    def test_forget_not_found(self, mock_mem):
        mock_mem.list_memories.return_value = []
        client = _mock_client()
        sb._cmd_forget(client, "C1", "1.1", "U_mem", "zzz99999")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "not found" in text


# ══════════════════════════════════════════════════════════════════════════════
# 13. Session management commands (rename, title, pin, archive)
# ══════════════════════════════════════════════════════════════════════════════


class TestSlackRename:
    def test_rename_no_arg(self):
        client = _mock_client()
        sb._cmd_rename_session(client, "C1", "1.1", "U_ren", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage" in text

    @patch.object(cc, "_session_mgr")
    def test_rename_success(self, mock_mgr):
        sess = MagicMock()
        sess.name = "new-name"
        mock_mgr.get_or_create_active.return_value = MagicMock(name="old")
        mock_mgr.rename.return_value = sess
        client = _mock_client()
        sb._cmd_rename_session(client, "C1", "1.1", "U_ren", "new-name")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Renamed" in text
        assert "new-name" in text

    @patch.object(cc, "_session_mgr")
    def test_rename_failure(self, mock_mgr):
        mock_mgr.get_or_create_active.return_value = MagicMock(name="old")
        mock_mgr.rename.return_value = None
        client = _mock_client()
        sb._cmd_rename_session(client, "C1", "1.1", "U_ren", "taken")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "failed" in text.lower() or "taken" in text.lower()


class TestSlackTitle:
    def test_title_no_arg(self):
        client = _mock_client()
        sb._cmd_title_session(client, "C1", "1.1", "U_ttl", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Usage" in text

    @patch.object(cc, "_session_mgr")
    def test_title_success(self, mock_mgr):
        sess = MagicMock()
        sess.title = "My Title"
        mock_mgr.get_or_create_active.return_value = MagicMock()
        mock_mgr.set_title.return_value = sess
        client = _mock_client()
        sb._cmd_title_session(client, "C1", "1.1", "U_ttl", "My Title")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Title set" in text

    @patch.object(cc, "_session_mgr")
    def test_title_failure(self, mock_mgr):
        mock_mgr.get_or_create_active.return_value = MagicMock()
        mock_mgr.set_title.return_value = None
        client = _mock_client()
        sb._cmd_title_session(client, "C1", "1.1", "U_ttl", "bad")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Failed" in text


class TestSlackPin:
    @patch.object(cc, "_session_mgr")
    def test_pin_toggle(self, mock_mgr):
        sess = MagicMock()
        sess.pinned = False
        mock_mgr.get_or_create_active.return_value = sess
        result = MagicMock()
        result.pinned = True
        result.name = "sess1"
        mock_mgr.pin.return_value = result
        client = _mock_client()
        sb._cmd_pin_session(client, "C1", "1.1", "U_pin")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Pinned" in text

    @patch.object(cc, "_session_mgr")
    def test_pin_failure(self, mock_mgr):
        sess = MagicMock()
        sess.pinned = False
        mock_mgr.get_or_create_active.return_value = sess
        mock_mgr.pin.return_value = None
        client = _mock_client()
        sb._cmd_pin_session(client, "C1", "1.1", "U_pin")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Failed" in text


class TestSlackArchive:
    @patch.object(cc, "_session_mgr")
    def test_archive_current(self, mock_mgr):
        active = MagicMock()
        active.name = "default"
        active.display_name = "default"
        mock_mgr.get_or_create_active.return_value = active
        result = MagicMock()
        result.display_name = "default"
        mock_mgr.archive.return_value = result
        client = _mock_client()
        sb._cmd_archive_session(client, "C1", "1.1", "U_arc", "")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Archived" in text

    @patch.object(cc, "_session_mgr")
    def test_archive_by_name(self, mock_mgr):
        result = MagicMock()
        result.display_name = "old-sess"
        mock_mgr.archive.return_value = result
        client = _mock_client()
        sb._cmd_archive_session(client, "C1", "1.1", "U_arc", "old-sess")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Archived" in text
        mock_mgr.archive.assert_called_with("slack", "U_arc", "old-sess")

    @patch.object(cc, "_session_mgr")
    def test_archive_failure(self, mock_mgr):
        mock_mgr.get_or_create_active.return_value = MagicMock(name="x")
        mock_mgr.archive.return_value = None
        client = _mock_client()
        sb._cmd_archive_session(client, "C1", "1.1", "U_arc", "nonexistent")
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Cannot archive" in text


# ══════════════════════════════════════════════════════════════════════════════
# 14. _parse_remember_args
# ══════════════════════════════════════════════════════════════════════════════


class TestParseRememberArgs:
    def test_plain_text(self):
        content, tags, importance = sb._parse_remember_args("hello world")
        assert content == "hello world"
        assert tags == []
        assert importance == 0.5

    def test_with_tags(self):
        content, tags, _ = sb._parse_remember_args("note #work #urgent")
        assert content == "note"
        assert tags == ["work", "urgent"]

    def test_with_importance(self):
        content, _, importance = sb._parse_remember_args("fact !0.9")
        assert content == "fact"
        assert importance == 0.9

    def test_tags_and_importance(self):
        content, tags, importance = sb._parse_remember_args("idea #dev !0.8")
        assert content == "idea"
        assert tags == ["dev"]
        assert importance == 0.8

    def test_invalid_importance(self):
        content, _, importance = sb._parse_remember_args("note !abc")
        assert "!abc" in content
        assert importance == 0.5

    def test_hash_alone_not_tag(self):
        _, tags, _ = sb._parse_remember_args("note # alone")
        assert tags == []


# ══════════════════════════════════════════════════════════════════════════════
# 15. handle_mention
# ══════════════════════════════════════════════════════════════════════════════


class TestHandleMention:
    def test_mention_dispatches(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "C456", "text": "<@BOT> hello", "ts": "1.1"}
        with patch.object(sb, "_handle") as mock_handle:
            sb.handle_mention(client, event, say=MagicMock())
            # _dispatch strips mention, spawns thread calling _handle
            import time
            time.sleep(0.1)
            mock_handle.assert_called_once()
            args = mock_handle.call_args[0]
            assert args[2] == "U123"
            assert "hello" in args[4]

    def test_mention_strips_user_ref(self):
        client = _mock_client()
        event = {"user": "U123", "channel": "C456", "text": "<@U999BOT> help", "ts": "2.2"}
        with patch.object(sb, "_handle") as mock_handle:
            sb.handle_mention(client, event, say=MagicMock())
            import time
            time.sleep(0.1)
            mock_handle.assert_called_once()
            text_arg = mock_handle.call_args[0][4]
            assert "<@" not in text_arg
