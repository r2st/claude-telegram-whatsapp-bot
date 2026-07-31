"""Tests for the Discord adapter.

None of these need discord.py installed: the library is only touched inside
``_build_client``/``run_discord``, and everything worth testing — the command
surface, the allowlist, when to answer, how a reply is split — is deliberately
kept free of gateway objects.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import discord_bot as db  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_user_settings():
    db._user_model.clear()
    db._user_engine.clear()
    yield
    db._user_model.clear()
    db._user_engine.clear()


# ─── Mentions and message splitting ──────────────────────────────────────────

class TestStripMentions:
    def test_removes_the_bot_mention(self):
        assert db.strip_mentions("<@1234> hello there") == "hello there"

    def test_removes_the_nickname_form(self):
        assert db.strip_mentions("<@!1234> hi") == "hi"

    def test_leaves_ordinary_text_alone(self):
        assert db.strip_mentions("what does <@ mean") == "what does <@ mean"

    def test_empty_and_none_are_safe(self):
        assert db.strip_mentions("") == ""
        assert db.strip_mentions(None) == ""


class TestSplitForDiscord:
    def test_short_reply_is_one_message_with_no_counter(self):
        assert db.split_for_discord("hi") == ["hi"]

    def test_empty_reply_sends_nothing(self):
        assert db.split_for_discord("") == []

    def test_every_part_fits_discords_limit(self):
        text = "\n\n".join(f"Paragraph number {i} " + "x" * 200 for i in range(60))
        parts = db.split_for_discord(text)
        assert len(parts) > 1
        assert all(len(p) <= db.DISCORD_MESSAGE_LIMIT for p in parts)

    def test_code_fences_are_not_split_open(self):
        # A fixed-width slice lands inside the fence and Discord renders an
        # unterminated block, with a stray ``` opening the next message.
        body = "\n".join(f"line {i} of code" for i in range(200))
        text = f"Here you go:\n\n```python\n{body}\n```\n\nThat's it."
        for part in db.split_for_discord(text):
            assert part.count("```") % 2 == 0, "a fence was left open"

    def test_parts_are_numbered_when_there_is_more_than_one(self):
        text = "\n\n".join("word " * 100 for _ in range(20))
        parts = db.split_for_discord(text)
        assert len(parts) > 1
        assert f"(1/{len(parts)})" in parts[0]


# ─── When to answer ──────────────────────────────────────────────────────────

class TestShouldRespond:
    def test_answers_a_dm(self):
        assert db.should_respond(is_dm=True, is_own_message=False, was_mentioned=False)

    def test_answers_a_mention_in_a_server(self):
        assert db.should_respond(is_dm=False, is_own_message=False, was_mentioned=True)

    def test_ignores_unaddressed_server_chatter(self):
        assert not db.should_respond(is_dm=False, is_own_message=False, was_mentioned=False)

    def test_never_answers_itself(self):
        # Without this the bot replies to its own reply, forever, and gets the
        # application rate-limited off the gateway.
        assert not db.should_respond(is_dm=True, is_own_message=True, was_mentioned=True)
        assert not db.should_respond(is_dm=False, is_own_message=True, was_mentioned=True)


# ─── Allowlist ───────────────────────────────────────────────────────────────

class TestAllowlist:
    def test_empty_allowlist_permits_everyone(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}):
            assert db.is_allowed("999")

    def test_listed_user_is_permitted(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "111,222"}):
            assert db.is_allowed("222")

    def test_unlisted_user_is_refused(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "111,222"}):
            assert not db.is_allowed("333")

    def test_whitespace_and_int_ids_still_match(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": " 111 , 222 "}):
            assert db.is_allowed(111)
            assert db.is_allowed("111")

    def test_allowlist_is_read_per_call(self):
        # Read at import time it would ignore anything `telechat init` wrote
        # after the process started.
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "111"}):
            assert not db.is_allowed("777")
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "777"}):
            assert db.is_allowed("777")


# ─── Commands ────────────────────────────────────────────────────────────────

class TestCommands:
    def test_plain_text_is_not_a_command(self):
        assert db.handle_command("u1", "what is a monad") is None

    def test_help(self):
        assert "Claude on Discord" in db.handle_command("u1", "!help")

    def test_unknown_command_is_named(self):
        out = db.handle_command("u1", "!wharrgarbl")
        assert "wharrgarbl" in out and "!help" in out

    def test_model_shows_then_sets(self):
        assert "haiku" in db.handle_command("u1", "!model")
        assert "sonnet" in db.handle_command("u1", "!model sonnet")
        assert db._model("u1") == "sonnet"

    def test_model_rejects_an_unknown_name(self):
        db.handle_command("u1", "!model wat")
        assert "wat" not in db._model("u1")

    def test_engine_sets_per_user(self):
        db.handle_command("u1", "!engine api")
        assert db._engine("u1") == "api"
        assert db._engine("u2") != "api"

    def test_commands_are_case_insensitive(self):
        assert db.handle_command("u1", "!HELP") is not None

    def test_remember_and_recall_round_trip(self):
        out = db.handle_command("u1", "!remember the deploy key is in 1Password #ops")
        assert "Remembered" in out
        assert "deploy key" in db.handle_command("u1", "!recall deploy key")

    def test_remember_without_text_explains_itself(self):
        assert "remember" in db.handle_command("u1", "!remember").lower()

    def test_memories_are_scoped_per_user(self):
        db.handle_command("user-a", "!remember alpha secret")
        listing = db.handle_command("user-b", "!memories")
        assert "alpha secret" not in listing

    def test_status_reports_the_current_settings(self):
        db.handle_command("u1", "!model opus")
        out = db.handle_command("u1", "!status")
        assert "opus" in out

    def test_usage_reports_counters(self):
        assert "Tokens" in db.handle_command("u1", "!usage")

    def test_sessions_new_and_switch(self):
        assert "name" in db.handle_command("u1", "!new").lower()
        assert "refactor" in db.handle_command("u1", "!new refactor")
        assert "refactor" in db.handle_command("u1", "!sessions")
        assert "refactor" in db.handle_command("u1", "!switch refactor")

    def test_switch_to_a_session_that_does_not_exist(self):
        assert "No session" in db.handle_command("u1", "!switch nope-not-here")


# ─── The turn itself ─────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


class TestRunTurn:
    def test_unlisted_user_never_reaches_claude(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "111"}), \
             patch.object(db.cc, "ask_claude_async") as ask:
            out = _run(db.run_turn("999", "hello"))
        assert "allowlist" in out
        ask.assert_not_called()

    def test_a_normal_message_reaches_claude_and_is_saved(self):
        async def fake_ask(*a, **k):
            return "the answer", {"input_tokens": 5, "output_tokens": 7}

        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}), \
             patch.object(db.cc, "ask_claude_async", fake_ask), \
             patch.object(db.cc, "save_turn") as save:
            out = _run(db.run_turn("u-save", "hello"))

        assert out == "the answer"
        save.assert_called_once()

    def test_a_failed_turn_is_not_saved_as_history(self):
        # Storing it would feed the error back to Claude as context next time.
        async def fake_ask(*a, **k):
            return "[Claude error] signed out", {}

        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}), \
             patch.object(db.cc, "ask_claude_async", fake_ask), \
             patch.object(db.cc, "save_turn") as save:
            out = _run(db.run_turn("u-err", "hello"))

        assert out.startswith("[Claude error]")
        save.assert_not_called()

    def test_commands_short_circuit_before_claude(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}), \
             patch.object(db.cc, "ask_claude_async") as ask:
            out = _run(db.run_turn("u1", "!help"))
        assert "Claude on Discord" in out
        ask.assert_not_called()

    def test_rate_limit_is_reported_not_silently_dropped(self):
        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}), \
             patch.object(db.cc, "check_rate_limit", return_value=False), \
             patch.object(db.cc, "ask_claude_async") as ask:
            out = _run(db.run_turn("u1", "hello"))
        assert "Rate limit" in out
        ask.assert_not_called()

    def test_api_engine_uses_the_api_path(self):
        async def fake_api(*a, **k):
            return "api answer", {}

        with patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": ""}), \
             patch.object(db.cc, "ask_claude_api_async", fake_api), \
             patch.object(db.cc, "ask_claude_async") as cli:
            db.handle_command("u-api", "!engine api")
            out = _run(db.run_turn("u-api", "hello"))

        assert out == "api answer"
        cli.assert_not_called()


# ─── Entry point guards ──────────────────────────────────────────────────────

class TestRunDiscordGuards:
    def test_missing_token_is_reported_not_raised(self, capsys):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            db.run_discord()
        assert "DISCORD_BOT_TOKEN" in capsys.readouterr().out

    def test_missing_library_says_what_to_install(self, capsys):
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def no_discord(name, *args, **kwargs):
            if name == "discord":
                raise ImportError("No module named 'discord'")
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "tok"}), \
             patch("builtins.__import__", no_discord):
            db.run_discord()
        assert "pip install" in capsys.readouterr().out
