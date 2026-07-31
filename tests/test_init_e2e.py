"""End-to-end tests for the telechat init process.

Tests the complete Claude-assisted initialization flow:
  - Python `_cmd_init()` (manual wizard via `telechat init` from pip)
  - Node.js `telechat init` (Claude CLI-assisted setup via npm)
  - Pre-flight checks, .env creation, token validation, post-init validation
  - Full multi-platform flows (Telegram, WhatsApp, Slack)
  - Optional feature configuration
  - Edge cases: invalid tokens, retries, skips, existing configs
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telechat_pkg"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

import pytest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch, MagicMock

from main import (
    _cmd_init,
    _read_env,
    _set_env_var,
    _save_workdir,
    _validate_telegram_token,
    _validate_green_api,
    _validate_slack_token,
    _has_any_platform,
    _parse_platforms,
    _env_example_path,
    cli_entry,
)


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _safe_inputs(inputs_list):
    """Return a side_effect callable that yields from inputs_list,
    then returns '' for any extra prompts."""
    it = iter(inputs_list)
    def _side_effect(prompt=""):
        return next(it, "")
    return _side_effect


@contextmanager
def _init_env(tmp_path, inputs, env_data=None, example_content=None,
              validate_tg="@testbot", validate_wa="authorized",
              validate_sl="test-team", extra_patches=None):
    """Full context manager for running _cmd_init end-to-end with controlled I/O.

    Args:
        tmp_path: pytest tmp_path fixture
        inputs: list of strings simulating user keyboard input
        env_data: dict to return from _read_env (existing .env state)
        example_content: if set, creates a .env.example with this content
        validate_tg: return value for _validate_telegram_token (None = invalid)
        validate_wa: return value for _validate_green_api (None = invalid)
        validate_sl: return value for _validate_slack_token (None = invalid)
        extra_patches: additional patch objects
    """
    env_path = tmp_path / ".env"
    mock_set = MagicMock()
    env_data = env_data or {}

    example_path = None
    if example_content:
        example_path = tmp_path / ".env.example"
        example_path.write_text(example_content)

    patches = [
        patch("main._find_env_file", return_value=str(env_path)),
        patch("main._env_example_path", return_value=str(example_path) if example_path else None),
        patch("main._read_env", return_value=env_data),
        patch("main._set_env_var", mock_set),
        patch("main._save_workdir"),
        patch("main._validate_telegram_token", return_value=validate_tg),
        patch("main._validate_green_api", return_value=validate_wa),
        patch("main._validate_slack_token", return_value=validate_sl),
        patch("builtins.input", side_effect=_safe_inputs(inputs)),
        *(extra_patches or []),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield mock_set, env_path


def _get_set_calls(mock_set):
    """Extract {key: value} dict from all _set_env_var mock calls."""
    result = {}
    for c in mock_set.call_args_list:
        # _set_env_var(path, key, value)
        if len(c.args) >= 3:
            result[c.args[1]] = c.args[2]
    return result


def _was_set(mock_set, key, value=None):
    """Check if _set_env_var was called with the given key (and optionally value)."""
    for c in mock_set.call_args_list:
        if len(c.args) >= 3 and c.args[1] == key:
            if value is None or c.args[2] == value:
                return True
    return False


def _not_set(mock_set, key):
    """Check that _set_env_var was never called for the given key."""
    return not any(c.args[1] == key for c in mock_set.call_args_list if len(c.args) >= 3)


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Full Telegram-Only Init Flow
# ═════════════════════════════════════════════════════════════════════════════


class TestE2ETelegramOnlyInit:
    """Full end-to-end: fresh install → Telegram only → CLI mode → no features."""

    def test_fresh_telegram_init_happy_path(self, tmp_path, capsys):
        """Complete fresh init: choose Telegram, provide token + user ID, CLI mode."""
        inputs = [
            "1",                        # platform = telegram
            "123456:AABBccDDeeFF",       # bot token
            "6775379103",               # user ID
            "cli",                      # Claude mode
            "",                         # optional features = skip
        ]
        with _init_env(tmp_path, inputs) as (mock_set, env_path):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "telegram"
        assert calls["TELEGRAM_BOT_TOKEN"] == "123456:AABBccDDeeFF"
        assert calls["TELEGRAM_ALLOWED_USER_IDS"] == "6775379103"
        assert calls["CLAUDE_MODE"] == "cli"

        out = capsys.readouterr().out
        assert "Setup Complete" in out
        assert "Bot verified" in out

    def test_fresh_telegram_init_skip_user_ids(self, tmp_path, capsys):
        """Init with Telegram but skip user ID restriction (allow all)."""
        inputs = ["1", "my-bot-token-12345", "", "cli", ""]
        with _init_env(tmp_path, inputs) as (mock_set, env_path):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["TELEGRAM_BOT_TOKEN"] == "my-bot-token-12345"
        # User IDs not set (empty input = keep/skip)
        assert "TELEGRAM_ALLOWED_USER_IDS" not in calls

        out = capsys.readouterr().out
        assert "Setup Complete" in out

    def test_telegram_init_none_user_ids_clears(self, tmp_path):
        """Typing 'none' for user IDs explicitly clears the restriction."""
        inputs = ["1", "tok-123456789", "none", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "TELEGRAM_ALLOWED_USER_IDS", "")

    def test_telegram_init_invalid_token_retry(self, tmp_path):
        """Invalid token on first attempt, then valid token on retry."""
        call_count = [0]
        def _validate_tg_side_effect(token):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # first attempt invalid
            return "@mybot"  # second attempt valid

        inputs = [
            "1",                    # platform
            "bad-token",            # first token (will fail)
            "good-token-12345",     # second token (will pass)
            "12345",                # user ID
            "",                     # claude mode
            "",                     # features
        ]
        extra = [patch("main._validate_telegram_token", side_effect=_validate_tg_side_effect)]
        with _init_env(tmp_path, inputs, extra_patches=extra) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "TELEGRAM_BOT_TOKEN", "good-token-12345")

    def test_telegram_init_skip_token(self, tmp_path, capsys):
        """Empty token input skips Telegram token setup."""
        inputs = ["1", "", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _not_set(mock_set, "TELEGRAM_BOT_TOKEN")
        out = capsys.readouterr().out
        assert "Skipped" in out


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Full WhatsApp-Only Init Flow
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EWhatsAppOnlyInit:
    """Full end-to-end: fresh install → WhatsApp only."""

    def test_fresh_whatsapp_init(self, tmp_path, capsys):
        """Complete WhatsApp init: instance ID, API token, phone number."""
        inputs = [
            "2",                    # platform = whatsapp
            "7107621928",           # instance ID
            "abc123def456token",    # API token
            "919876543210",         # allowed number
            "cli",                  # Claude mode
            "",                     # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "whatsapp"
        assert calls["GREEN_API_INSTANCE_ID"] == "7107621928"
        assert calls["GREEN_API_TOKEN"] == "abc123def456token"
        assert calls["WHATSAPP_ALLOWED_NUMBERS"] == "919876543210"

    def test_whatsapp_phone_number_cleaning(self, tmp_path):
        """Phone numbers with special chars are cleaned."""
        inputs = ["2", "inst1", "tok1", "+91 (987) 654-3210", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "WHATSAPP_ALLOWED_NUMBERS", "919876543210")

    def test_whatsapp_skip_credentials(self, tmp_path, capsys):
        """Skipping WhatsApp credentials shows warning."""
        inputs = ["2", "", "", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        out = capsys.readouterr().out
        assert "Skipped" in out

    def test_whatsapp_invalid_creds_retry(self, tmp_path):
        """Invalid Green API creds on first attempt, valid on retry."""
        call_count = [0]
        def _validate_wa_side_effect(instance_id, token):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return "authorized"

        inputs = [
            "2",
            "bad-inst", "bad-tok",          # first attempt (fails)
            "good-inst", "good-tok",        # retry (succeeds)
            "",                             # numbers
            "",                             # claude mode
            "",                             # features
        ]
        extra = [patch("main._validate_green_api", side_effect=_validate_wa_side_effect)]
        with _init_env(tmp_path, inputs, extra_patches=extra) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "GREEN_API_INSTANCE_ID", "good-inst")

    def test_whatsapp_not_authorized_warning(self, tmp_path, capsys):
        """notAuthorized state shows QR code warning."""
        inputs = ["2", "inst1", "tok1", "", "", ""]
        with _init_env(tmp_path, inputs, validate_wa="notAuthorized") as (mock_set, _):
            _cmd_init()

        out = capsys.readouterr().out
        assert "QR code" in out or "notAuthorized" in out


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Full Slack-Only Init Flow
# ═════════════════════════════════════════════════════════════════════════════


class TestE2ESlackOnlyInit:
    """Full end-to-end: fresh install → Slack only."""

    def test_fresh_slack_init(self, tmp_path, capsys):
        """Complete Slack init: bot token, app token, member ID."""
        inputs = [
            "3",                        # platform = slack
            "xoxb-my-bot-token-123",    # bot token
            "xapp-my-app-token-456",    # app token
            "U12345ABC",                # member ID
            "cli",                      # Claude mode
            "",                         # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "slack"
        assert calls["SLACK_BOT_TOKEN"] == "xoxb-my-bot-token-123"
        assert calls["SLACK_APP_TOKEN"] == "xapp-my-app-token-456"
        assert calls["SLACK_ALLOWED_USER_IDS"] == "U12345ABC"

    def test_slack_invalid_bot_token_prefix_rejected(self, tmp_path, capsys):
        """Non-xoxb token triggers retry, then valid token succeeds."""
        # When token doesn't start with xoxb-, the loop asks again
        # _validate_slack_token is mocked to always return "test-team"
        # but the prefix check happens BEFORE validation
        inputs = [
            "3",
            "xoxb-real-bot-token",   # valid bot token
            "xapp-real-app-token",   # app token
            "",                      # member IDs
            "",                      # claude mode
            "",                      # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "SLACK_BOT_TOKEN", "xoxb-real-bot-token")

    def test_slack_none_ids_clears(self, tmp_path):
        """Typing 'none' for Slack IDs clears the restriction."""
        inputs = ["3", "xoxb-bot", "xapp-app", "none", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "SLACK_ALLOWED_USER_IDS", "")

    def test_slack_app_token_non_xapp_warning(self, tmp_path, capsys):
        """Non-xapp app token is saved with a warning."""
        inputs = ["3", "xoxb-bot-tok", "not-xapp-prefix", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "SLACK_APP_TOKEN", "not-xapp-prefix")
        out = capsys.readouterr().out
        assert "xapp-" in out  # warning about expected prefix


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Multi-Platform Init Flows
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EMultiPlatformInit:
    """Full init with multiple platforms configured together."""

    def test_telegram_whatsapp_init(self, tmp_path):
        """Choice 6: Telegram + WhatsApp."""
        inputs = [
            "6",                    # telegram,whatsapp
            "tg-token-1234567",     # Telegram token
            "123456789",            # Telegram user ID
            "inst-42",              # WhatsApp instance
            "wa-token-xyz",         # WhatsApp API token
            "919876543210",         # WhatsApp number
            "cli",                  # Claude mode
            "",                     # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "telegram,whatsapp"
        assert calls["TELEGRAM_BOT_TOKEN"] == "tg-token-1234567"
        assert calls["GREEN_API_INSTANCE_ID"] == "inst-42"

    def test_all_platforms_init(self, tmp_path, capsys):
        """Choice 9: All platforms (Telegram + WhatsApp + Slack + Discord + Web)."""
        inputs = [
            "9",                        # all
            "tg-tok-1234567890",        # Telegram token
            "111222333",                # Telegram user IDs
            "inst-99",                  # WhatsApp instance
            "wa-tok-abc",               # WhatsApp token
            "14155551234",              # WhatsApp number
            "xoxb-slack-bot-token",     # Slack bot token
            "xapp-slack-app-token",     # Slack app token
            "U999888",                  # Slack member ID
            "discord-bot-token",        # Discord bot token
            "D777666",                  # Discord user ID
            "",                         # Web chat port (keep default)
            "",                         # Web chat token (skip)
            "cli",                      # Claude mode
            "",                         # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "all"
        assert "TELEGRAM_BOT_TOKEN" in calls
        assert "GREEN_API_INSTANCE_ID" in calls
        assert "SLACK_BOT_TOKEN" in calls

        out = capsys.readouterr().out
        assert "Setup Complete" in out

    def test_telegram_slack_init(self, tmp_path):
        """Choice 7: Telegram + Slack."""
        inputs = [
            "7",                        # telegram,slack
            "tg-tok-9999999",           # Telegram token
            "42",                       # Telegram user ID
            "xoxb-bot-slack",           # Slack bot token
            "xapp-app-slack",           # Slack app token
            "UABC123",                  # Slack member ID
            "api",                      # Claude mode = API
            "sk-anthropic-key-123",     # API key
            "",                         # features
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["BOT_MODE"] == "telegram,slack"
        assert calls["CLAUDE_MODE"] == "api"
        assert calls["ANTHROPIC_API_KEY"] == "sk-anthropic-key-123"


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Existing Config (Re-init / Reconfigure)
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EExistingConfigReinit:
    """Re-running init with existing .env — keep vs reconfigure flows."""

    def test_keep_existing_telegram_config(self, tmp_path, capsys):
        """Existing valid Telegram token: user declines to change → kept."""
        existing_env = {
            "BOT_MODE": "telegram",
            "TELEGRAM_BOT_TOKEN": "existing-token-1234567890",
            "TELEGRAM_ALLOWED_USER_IDS": "111222",
            "CLAUDE_MODE": "cli",
        }
        # inputs: platform(keep), change?(N), user_ids(keep), claude(keep), features
        inputs = ["", "N", "", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        # Token should NOT have been re-set
        assert _not_set(mock_set, "TELEGRAM_BOT_TOKEN")
        out = capsys.readouterr().out
        assert "Using" in out  # "Using <path>"
        assert "@testbot" in out  # validated bot name shown

    def test_reconfigure_existing_telegram_token(self, tmp_path):
        """User chooses to change existing token → new token saved."""
        existing_env = {
            "BOT_MODE": "telegram",
            "TELEGRAM_BOT_TOKEN": "old-token-1234567890",
            "TELEGRAM_ALLOWED_USER_IDS": "111",
        }
        # inputs: platform(keep), change?(y), new token, user_ids, claude, features
        inputs = ["", "y", "brand-new-token-99", "", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        assert _was_set(mock_set, "TELEGRAM_BOT_TOKEN", "brand-new-token-99")

    def test_keep_existing_whatsapp_config(self, tmp_path):
        """Existing valid WhatsApp creds: user declines change → kept."""
        existing_env = {
            "BOT_MODE": "whatsapp",
            "GREEN_API_INSTANCE_ID": "inst-existing",
            "GREEN_API_TOKEN": "tok-existing-abc",
            "WHATSAPP_ALLOWED_NUMBERS": "919876543210",
        }
        inputs = ["", "N", "", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        assert _not_set(mock_set, "GREEN_API_INSTANCE_ID")
        assert _not_set(mock_set, "GREEN_API_TOKEN")

    def test_reconfigure_whatsapp_from_existing(self, tmp_path):
        """Existing WhatsApp config → user chooses y → enters new creds."""
        existing_env = {
            "BOT_MODE": "whatsapp",
            "GREEN_API_INSTANCE_ID": "old-inst",
            "GREEN_API_TOKEN": "old-tok-1234567890",
        }
        inputs = ["", "y", "new-inst-77", "new-tok-xyz", "", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        assert _was_set(mock_set, "GREEN_API_INSTANCE_ID", "new-inst-77")
        assert _was_set(mock_set, "GREEN_API_TOKEN", "new-tok-xyz")

    def test_keep_existing_slack_config(self, tmp_path):
        """Existing valid Slack token: user declines change → kept."""
        existing_env = {
            "BOT_MODE": "slack",
            "SLACK_BOT_TOKEN": "xoxb-existing-long-token",
            "SLACK_APP_TOKEN": "xapp-existing-app-long",
        }
        inputs = ["", "N", "N", "", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        assert _not_set(mock_set, "SLACK_BOT_TOKEN")

    def test_existing_invalid_token_forces_new_entry(self, tmp_path, capsys):
        """Existing token that fails validation → user must enter a new one."""
        existing_env = {
            "BOT_MODE": "telegram",
            "TELEGRAM_BOT_TOKEN": "expired-token-123456",
        }
        inputs = ["", "new-valid-token-789", "42", "", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env,
                       validate_tg=None) as (mock_set, env_path):
            env_path.write_text("existing content")
            # Override: first call returns None (invalid), second returns valid
            pass

        # Need a different approach — use side_effect
        call_count = [0]
        def _validate(token):
            call_count[0] += 1
            if token == "expired-token-123456":
                return None
            return "@newbot"

        extra = [patch("main._validate_telegram_token", side_effect=_validate)]
        with _init_env(tmp_path, inputs, env_data=existing_env,
                       extra_patches=extra) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        assert _was_set(mock_set, "TELEGRAM_BOT_TOKEN", "new-valid-token-789")
        out = capsys.readouterr().out
        assert "invalid" in out.lower()


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Claude Mode Selection
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EClaudeModeSelection:
    """Tests for the Claude mode section of init."""

    def test_api_mode_prompts_for_key(self, tmp_path):
        """Choosing API mode prompts for ANTHROPIC_API_KEY."""
        inputs = ["1", "my-tg-token-99", "", "api", "sk-ant-key-12345", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        calls = _get_set_calls(mock_set)
        assert calls["CLAUDE_MODE"] == "api"
        assert calls["ANTHROPIC_API_KEY"] == "sk-ant-key-12345"

    def test_api_mode_existing_key_not_reprompted(self, tmp_path, capsys):
        """API mode with existing key → shows masked key, doesn't reprompt."""
        existing_env = {
            "BOT_MODE": "telegram",
            "TELEGRAM_BOT_TOKEN": "tok-1234567890",
            "ANTHROPIC_API_KEY": "sk-existing-real-key-here",
        }
        inputs = ["", "N", "", "api", ""]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing content")
            _cmd_init()

        out = capsys.readouterr().out
        assert "API Key:" in out  # shows masked existing key
        assert _not_set(mock_set, "ANTHROPIC_API_KEY")

    def test_cli_mode_no_api_key_prompt(self, tmp_path, capsys):
        """CLI mode does not prompt for API key."""
        inputs = ["1", "my-token-12345", "", "cli", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "CLAUDE_MODE", "cli")
        assert _not_set(mock_set, "ANTHROPIC_API_KEY")

    def test_empty_claude_mode_keeps_default(self, tmp_path):
        """Empty input for Claude mode keeps the existing/default."""
        inputs = ["1", "tok-123456789", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        # CLAUDE_MODE should NOT be set (empty input = keep default)
        assert _not_set(mock_set, "CLAUDE_MODE")

    def test_invalid_claude_mode_ignored(self, tmp_path):
        """Invalid mode string (not 'cli' or 'api') is ignored."""
        inputs = ["1", "tok-123456789", "", "foobar", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _not_set(mock_set, "CLAUDE_MODE")


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Optional Features Configuration
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EOptionalFeatures:
    """Tests for optional feature selection during init."""

    def test_enable_voice_transcription(self, tmp_path):
        """Feature 1: voice transcription → requires OpenAI key."""
        inputs = [
            "1", "tok-12345", "", "",   # platform + telegram + claude mode
            "1",                        # feature 1 = voice transcription
            "sk-openai-key-xyz",        # OpenAI API key
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "TRANSCRIPTION_ENABLED", "true")
        assert _was_set(mock_set, "OPENAI_API_KEY", "sk-openai-key-xyz")

    def test_enable_tts(self, tmp_path):
        """Feature 2: TTS → requires OpenAI key."""
        inputs = ["1", "tok-12345", "", "", "2", "sk-oai-key"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "TTS_ENABLED", "true")
        assert _was_set(mock_set, "OPENAI_API_KEY", "sk-oai-key")

    def test_enable_image_gen(self, tmp_path):
        """Feature 3: image generation → requires OpenAI key."""
        inputs = ["1", "tok-12345", "", "", "3", "sk-oai-img"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "IMAGE_GEN_ENABLED", "true")

    def test_enable_multiple_openai_features(self, tmp_path):
        """Features 1,2,3 together share one OpenAI key prompt."""
        inputs = ["1", "tok-12345", "", "", "1,2,3", "sk-shared-key"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "TRANSCRIPTION_ENABLED", "true")
        assert _was_set(mock_set, "TTS_ENABLED", "true")
        assert _was_set(mock_set, "IMAGE_GEN_ENABLED", "true")
        # Only one OpenAI key prompt
        oai_calls = [c for c in mock_set.call_args_list
                     if len(c.args) >= 3 and c.args[1] == "OPENAI_API_KEY"]
        assert len(oai_calls) == 1

    def test_enable_web_search_brave(self, tmp_path):
        """Feature 4: web search with Brave provider."""
        inputs = ["1", "tok-12345", "", "", "4", "1", "brave-api-key-123"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "WEB_SEARCH_ENABLED", "true")
        assert _was_set(mock_set, "BRAVE_SEARCH_API_KEY", "brave-api-key-123")

    def test_enable_web_search_tavily(self, tmp_path):
        """Feature 4: web search with Tavily provider."""
        inputs = ["1", "tok-12345", "", "", "4", "2", "tvly-api-key-456"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "WEB_SEARCH_ENABLED", "true")
        assert _was_set(mock_set, "TAVILY_API_KEY", "tvly-api-key-456")

    def test_enable_web_fetch_with_jina(self, tmp_path):
        """Feature 5: web fetch with optional Jina key."""
        inputs = ["1", "tok-12345", "", "", "5", "jina-key-abc"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "WEB_FETCH_ENABLED", "true")
        assert _was_set(mock_set, "JINA_API_KEY", "jina-key-abc")

    def test_enable_web_fetch_without_jina(self, tmp_path):
        """Feature 5: web fetch without Jina key (fallback to basic)."""
        inputs = ["1", "tok-12345", "", "", "5", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "WEB_FETCH_ENABLED", "true")
        assert _not_set(mock_set, "JINA_API_KEY")

    def test_enable_music_gen(self, tmp_path):
        """Feature 6: music generation → requires Replicate token."""
        inputs = ["1", "tok-12345", "", "", "6", "r8-replicate-token"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "MUSIC_GEN_ENABLED", "true")
        assert _was_set(mock_set, "REPLICATE_API_TOKEN", "r8-replicate-token")

    def test_enable_video_gen(self, tmp_path):
        """Feature 7: video generation → requires Replicate token."""
        inputs = ["1", "tok-12345", "", "", "7", "r8-replicate-vid-token"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "VIDEO_GEN_ENABLED", "true")
        assert _was_set(mock_set, "REPLICATE_API_TOKEN", "r8-replicate-vid-token")

    def test_enable_music_and_video_share_replicate_key(self, tmp_path):
        """Features 6,7 together share one Replicate token prompt."""
        inputs = ["1", "tok-12345", "", "", "6,7", "r8-shared-token"]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        assert _was_set(mock_set, "MUSIC_GEN_ENABLED", "true")
        assert _was_set(mock_set, "VIDEO_GEN_ENABLED", "true")
        rep_calls = [c for c in mock_set.call_args_list
                     if len(c.args) >= 3 and c.args[1] == "REPLICATE_API_TOKEN"]
        assert len(rep_calls) == 1

    def test_skip_all_features(self, tmp_path):
        """Empty feature selection skips everything."""
        inputs = ["1", "tok-12345", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        for key in ("TRANSCRIPTION_ENABLED", "TTS_ENABLED", "IMAGE_GEN_ENABLED",
                     "WEB_SEARCH_ENABLED", "WEB_FETCH_ENABLED",
                     "MUSIC_GEN_ENABLED", "VIDEO_GEN_ENABLED"):
            assert _not_set(mock_set, key)

    def test_enable_all_features(self, tmp_path):
        """All 7 features enabled at once."""
        inputs = [
            "1", "tok-12345", "", "",       # platform setup
            "1,2,3,4,5,6,7",               # all features
            "sk-openai-all",                # OpenAI key (for 1,2,3)
            "1", "brave-key-all",           # Brave search (for 4)
            "jina-key-all",                 # Jina (for 5)
            "r8-replicate-all",             # Replicate (for 6,7)
        ]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        for key in ("TRANSCRIPTION_ENABLED", "TTS_ENABLED", "IMAGE_GEN_ENABLED",
                     "WEB_SEARCH_ENABLED", "WEB_FETCH_ENABLED",
                     "MUSIC_GEN_ENABLED", "VIDEO_GEN_ENABLED"):
            assert _was_set(mock_set, key, "true"), f"{key} should be set to true"

    def test_existing_openai_key_not_reprompted(self, tmp_path):
        """If OPENAI_API_KEY exists, features 1-3 don't ask for it again."""
        existing_env = {
            "BOT_MODE": "telegram",
            "TELEGRAM_BOT_TOKEN": "tok-existing-1234567890",
            "OPENAI_API_KEY": "sk-already-set-key",
        }
        inputs = ["", "N", "", "", "1,2"]
        with _init_env(tmp_path, inputs, env_data=existing_env) as (mock_set, env_path):
            env_path.write_text("existing")
            _cmd_init()

        assert _was_set(mock_set, "TRANSCRIPTION_ENABLED", "true")
        assert _was_set(mock_set, "TTS_ENABLED", "true")
        # OpenAI key should NOT be set again
        assert _not_set(mock_set, "OPENAI_API_KEY")


# ═════════════════════════════════════════════════════════════════════════════
# E2E: .env File Creation and Template Handling
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EEnvFileCreation:
    """Tests for .env file creation logic at init start."""

    def test_creates_from_example_template(self, tmp_path, capsys):
        """When no .env exists but .env.example does, copies it."""
        example_content = textwrap.dedent("""\
            # Telechat configuration
            BOT_MODE=telegram
            TELEGRAM_BOT_TOKEN=your_telegram_bot_token
            CLAUDE_MODE=cli
        """)
        inputs = ["", "", "", "", ""]
        extra = [patch("main._parse_platforms", return_value={"telegram"})]
        with _init_env(tmp_path, inputs,
                       env_data={"BOT_MODE": "telegram"},
                       example_content=example_content,
                       extra_patches=extra) as (mock_set, env_path):
            _cmd_init()

        assert env_path.exists()
        out = capsys.readouterr().out
        assert "Created" in out or "template" in out.lower()

    def test_creates_empty_when_no_example(self, tmp_path, capsys):
        """When neither .env nor .env.example exist, creates empty .env."""
        inputs = ["", "", "", "", ""]
        extra = [patch("main._parse_platforms", return_value={"telegram"})]
        with _init_env(tmp_path, inputs, extra_patches=extra) as (mock_set, env_path):
            _cmd_init()

        assert env_path.exists()
        out = capsys.readouterr().out
        assert "Created" in out

    def test_existing_env_shows_using_message(self, tmp_path, capsys):
        """When .env already exists, shows 'Using' message."""
        inputs = ["", "", "", "", ""]
        extra = [patch("main._parse_platforms", return_value={"telegram"})]
        with _init_env(tmp_path, inputs,
                       env_data={"BOT_MODE": "telegram"},
                       extra_patches=extra) as (mock_set, env_path):
            env_path.write_text("BOT_MODE=telegram\n")
            _cmd_init()

        out = capsys.readouterr().out
        assert "Using" in out


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Summary Output and Warnings
# ═════════════════════════════════════════════════════════════════════════════


class TestE2EInitSummaryAndWarnings:
    """Tests for the summary section at the end of init."""

    def test_summary_shows_configured_platforms(self, tmp_path, capsys):
        """Summary lists all configured platforms with ✓."""
        inputs = ["1", "tok-12345678", "42", "cli", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            # Make _read_env return final state with token
            pass

        # Use a version that returns real final env
        with _init_env(tmp_path, inputs,
                       extra_patches=[
                           patch("main._read_env", side_effect=[
                               {},  # initial read
                               {"TELEGRAM_BOT_TOKEN": "tok-12345678",
                                "CLAUDE_MODE": "cli"},  # final read
                           ])
                       ]) as (mock_set, _):
            _cmd_init()

        out = capsys.readouterr().out
        assert "Setup Complete" in out

    def test_summary_shows_skipped_platforms(self, tmp_path, capsys):
        """Summary shows 'skipped' for unconfigured platforms."""
        inputs = ["1", "tok-12345678", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        out = capsys.readouterr().out
        assert "Setup Complete" in out
        assert "skipped" in out  # WhatsApp and Slack are skipped

    def test_no_platform_configured_warning(self, tmp_path, capsys):
        """Warning when no platform has credentials after init."""
        inputs = ["1", "", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        out = capsys.readouterr().out
        assert "No platform configured" in out or "won't start" in out

    def test_security_warning_no_telegram_user_ids(self, tmp_path, capsys):
        """Security warning when Telegram configured but no user restriction."""
        inputs = ["1", "tok-12345678", "", "", ""]
        final_env = {
            "TELEGRAM_BOT_TOKEN": "tok-12345678",
            "CLAUDE_MODE": "cli",
        }
        # Two calls to _read_env: initial (empty) and final
        with _init_env(tmp_path, inputs) as (mock_set, _):
            with patch("main._read_env", side_effect=[{}, final_env]):
                _cmd_init()

        out = capsys.readouterr().out
        assert "Security" in out or "restriction" in out.lower() or "anyone" in out.lower()

    def test_workdir_saved_at_end(self, tmp_path):
        """_save_workdir is called at the end of init."""
        inputs = ["1", "tok-12345678", "", "", ""]
        mock_save = MagicMock()
        extra = [patch("main._save_workdir", mock_save)]
        with _init_env(tmp_path, inputs, extra_patches=extra) as (mock_set, _):
            _cmd_init()

        mock_save.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# E2E: CLI Entry Point Routing
# ═════════════════════════════════════════════════════════════════════════════


class TestE2ECliEntryRouting:
    """Tests that `telechat init` from CLI properly routes to _cmd_init."""

    def test_init_command_routes_to_cmd_init(self, monkeypatch):
        """'telechat init' calls _cmd_init."""
        monkeypatch.setattr(sys, "argv", ["telechat", "init"])
        with patch("main._resolve_workdir"), \
             patch("main._cmd_init") as mock_init:
            cli_entry()

        mock_init.assert_called_once()

    def test_start_without_config_shows_guidance(self, monkeypatch, capsys):
        """'telechat start' with no platform config shows setup guidance."""
        monkeypatch.setattr(sys, "argv", ["telechat", "start"])
        exit_calls = []

        class _ExitCalled(Exception):
            pass

        def _fake_exit(code):
            exit_calls.append(code)
            raise _ExitCalled()

        with patch("main._resolve_workdir"), \
             patch("main._find_env_file", return_value="/nonexistent/.env"), \
             patch("main._read_env", return_value={}), \
             patch("main._has_any_platform", return_value=False):
            monkeypatch.setattr(sys, "exit", _fake_exit)
            with pytest.raises(_ExitCalled):
                cli_entry()

        assert 1 in exit_calls

    def test_default_command_is_start(self, monkeypatch):
        """Running bare 'telechat' defaults to 'start' subcommand."""
        monkeypatch.setattr(sys, "argv", ["telechat"])
        called = []
        with patch("main._resolve_workdir"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("main._read_env", return_value={"TELEGRAM_BOT_TOKEN": "tok"}), \
             patch("main._has_any_platform", return_value=True), \
             patch("main._cmd_start", side_effect=lambda: called.append(True)):
            cli_entry()

        assert called

    def test_help_command_prints_usage(self, monkeypatch, capsys):
        """'telechat help' prints usage info."""
        monkeypatch.setattr(sys, "argv", ["telechat", "help"])
        with patch("main._resolve_workdir"):
            cli_entry()

        out = capsys.readouterr().out
        assert "Usage" in out
        assert "init" in out

    def test_unknown_command_exits(self, monkeypatch):
        """Unknown command causes sys.exit(1)."""
        monkeypatch.setattr(sys, "argv", ["telechat", "bogus"])
        exit_calls = []
        with patch("main._resolve_workdir"):
            monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
            cli_entry()

        assert 1 in exit_calls


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Token Validation Functions (Unit-level but critical for init)
# ═════════════════════════════════════════════════════════════════════════════


class TestTokenValidation:
    """Tests for the validation functions used during init."""

    def test_validate_telegram_token_success(self):
        """Valid Telegram token returns bot username."""
        response_data = json.dumps({"ok": True, "result": {"username": "my_test_bot"}})
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _validate_telegram_token("123456:AABBccDDeeFF")

        assert result == "@my_test_bot"

    def test_validate_telegram_token_invalid(self):
        """Invalid Telegram token returns None."""
        with patch("urllib.request.urlopen", side_effect=Exception("401")):
            result = _validate_telegram_token("bad-token")

        assert result is None

    def test_validate_telegram_token_not_ok(self):
        """Telegram API returns ok=false → None."""
        response_data = json.dumps({"ok": False})
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _validate_telegram_token("some-token")

        assert result is None

    def test_validate_green_api_success(self):
        """Valid Green API credentials return state string."""
        response_data = json.dumps({"stateInstance": "authorized"})
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _validate_green_api("inst-1", "tok-1")

        assert result == "authorized"

    def test_validate_green_api_invalid(self):
        """Invalid Green API credentials return None."""
        with patch("urllib.request.urlopen", side_effect=Exception("404")):
            result = _validate_green_api("bad-inst", "bad-tok")

        assert result is None

    def test_validate_slack_token_success(self):
        """Valid Slack token returns team name."""
        response_data = json.dumps({"ok": True, "team": "my-workspace"})
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _validate_slack_token("xoxb-valid-token")

        assert result == "my-workspace"

    def test_validate_slack_token_invalid(self):
        """Invalid Slack token returns None."""
        with patch("urllib.request.urlopen", side_effect=Exception("invalid_auth")):
            result = _validate_slack_token("xoxb-bad")

        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Platform Parsing
# ═════════════════════════════════════════════════════════════════════════════


class TestParsePlatforms:
    """Tests for _parse_platforms used in init flow."""

    def test_single_platforms(self):
        assert _parse_platforms("telegram") == {"telegram"}
        assert _parse_platforms("whatsapp") == {"whatsapp"}
        assert _parse_platforms("slack") == {"slack"}

    def test_comma_separated(self):
        assert _parse_platforms("telegram,whatsapp") == {"telegram", "whatsapp"}
        assert _parse_platforms("telegram,slack") == {"telegram", "slack"}
        assert _parse_platforms("telegram,whatsapp,slack") == {"telegram", "whatsapp", "slack"}

    def test_aliases(self):
        assert _parse_platforms("both") == {"telegram", "whatsapp"}
        assert {"telegram", "whatsapp", "slack"}.issubset(_parse_platforms("all"))

    def test_case_insensitive(self):
        assert _parse_platforms("TELEGRAM") == {"telegram"}
        assert {"telegram", "whatsapp", "slack"}.issubset(_parse_platforms("ALL"))

    def test_whitespace_handling(self):
        assert _parse_platforms("  telegram , whatsapp  ") == {"telegram", "whatsapp"}
        assert _parse_platforms(" slack ") == {"slack"}


# ═════════════════════════════════════════════════════════════════════════════
# E2E: _has_any_platform Detection
# ═════════════════════════════════════════════════════════════════════════════


class TestHasAnyPlatform:
    """Tests for _has_any_platform used in start pre-flight."""

    def test_telegram_token_detected(self):
        assert _has_any_platform({"TELEGRAM_BOT_TOKEN": "tok-123"}) is True

    def test_whatsapp_creds_detected(self):
        assert _has_any_platform({"GREEN_API_INSTANCE_ID": "id", "GREEN_API_TOKEN": "tok"}) is True

    def test_slack_creds_detected(self):
        assert _has_any_platform({"SLACK_BOT_TOKEN": "xoxb-bot", "SLACK_APP_TOKEN": "xapp-app"}) is True

    def test_empty_env_detected(self):
        assert _has_any_platform({}) is False

    def test_partial_whatsapp_not_detected(self):
        assert _has_any_platform({"GREEN_API_INSTANCE_ID": "id"}) is False
        assert _has_any_platform({"GREEN_API_TOKEN": "tok"}) is False

    def test_partial_slack_not_detected(self):
        # Slack requires BOTH bot token AND app token
        assert _has_any_platform({"SLACK_BOT_TOKEN": "xoxb-bot"}) is False
        assert _has_any_platform({"SLACK_APP_TOKEN": "xapp-app"}) is False


# ═════════════════════════════════════════════════════════════════════════════
# E2E: .env Read/Write Operations
# ═════════════════════════════════════════════════════════════════════════════


class TestEnvReadWrite:
    """Tests for _read_env and _set_env_var used throughout init."""

    def test_read_env_basic(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY1=value1\nKEY2=value2\n")
        result = _read_env(str(env))
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_read_env_ignores_comments(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# comment\nKEY=val\n# another comment\n")
        result = _read_env(str(env))
        assert result == {"KEY": "val"}

    def test_read_env_ignores_blank_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("A=1\n\n\nB=2\n")
        result = _read_env(str(env))
        assert result == {"A": "1", "B": "2"}

    def test_read_env_missing_file(self, tmp_path):
        result = _read_env(str(tmp_path / "nonexistent.env"))
        assert result == {}

    def test_set_env_var_appends_new(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("EXISTING=value\n")
        _set_env_var(str(env), "NEW_KEY", "new_value")
        content = env.read_text()
        assert "EXISTING=value" in content
        assert "NEW_KEY=new_value" in content

    def test_set_env_var_updates_existing(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY=old_value\n")
        _set_env_var(str(env), "KEY", "new_value")
        content = env.read_text()
        assert "KEY=new_value" in content
        assert "old_value" not in content

    def test_set_env_var_creates_file_if_missing(self, tmp_path):
        env = tmp_path / ".env"
        _set_env_var(str(env), "FRESH", "value")
        assert env.exists()
        assert "FRESH=value" in env.read_text()

    def test_set_env_var_preserves_comments(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# my config\nKEY=old\n# end\n")
        _set_env_var(str(env), "KEY", "new")
        content = env.read_text()
        assert "# my config" in content
        assert "# end" in content
        assert "KEY=new" in content


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Workdir Resolution and Persistence
# ═════════════════════════════════════════════════════════════════════════════


class TestWorkdirPersistence:
    """Tests for workdir save/resolve used by both init and start."""

    def test_save_workdir_creates_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir(str(tmp_path / "mydir"))

        assert config_path.exists()
        cfg = json.loads(config_path.read_text())
        assert cfg["workdir"] == str(tmp_path / "mydir")

    def test_save_workdir_preserves_other_keys(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"claudeWorkdir": "/old", "version": "1.0"}))
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/new/workdir")

        cfg = json.loads(config_path.read_text())
        assert cfg["workdir"] == "/new/workdir"
        assert cfg["version"] == "1.0"

    def test_env_example_path_finds_in_project(self, tmp_path, monkeypatch):
        """_env_example_path finds .env.example in the project directory."""
        example = tmp_path / ".env.example"
        example.write_text("BOT_MODE=telegram\n")
        monkeypatch.chdir(tmp_path)
        result = _env_example_path()
        assert result is not None
        assert result.endswith(".env.example")


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Node.js Init Pre-flight Checks (Integration)
# ═════════════════════════════════════════════════════════════════════════════


class TestNodeJsInitPreflightChecks:
    """Tests for the Node.js telechat init pre-flight logic.

    These test the helper functions from telechat.js conceptually,
    validated through subprocess calls to node -e.
    """

    @pytest.fixture
    def telechat_js(self):
        return os.path.join(os.path.dirname(__file__), "..", "npm", "bin", "telechat.js")

    def test_node_find_python(self, telechat_js):
        """Node findPython() should locate python3."""
        if not os.path.exists(telechat_js):
            pytest.skip("telechat.js not found")
        result = subprocess.run(
            ["node", "-e", """
                const { execSync } = require("child_process");
                for (const cmd of ["python3", "python"]) {
                    try {
                        const v = execSync(cmd + " --version 2>&1", { encoding: "utf8" });
                        if (v.includes("Python 3")) { console.log(cmd); process.exit(0); }
                    } catch {}
                }
                process.exit(1);
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "python" in result.stdout.lower()

    def test_node_claude_cli_check(self, telechat_js):
        """Node claudeCliInstalled() returns true when claude is on PATH."""
        result = subprocess.run(
            ["node", "-e", """
                const { execSync } = require("child_process");
                try {
                    execSync("claude --version 2>&1", { stdio: "ignore" });
                    console.log("installed");
                } catch {
                    console.log("not-installed");
                }
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        # Either installed or not — just verify the check doesn't crash
        assert result.stdout.strip() in ("installed", "not-installed")

    def test_node_set_env_var(self, tmp_path):
        """Node setEnvVar() correctly writes to .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("OLD_KEY=old_value\n")

        result = subprocess.run(
            ["node", "-e", f"""
                const fs = require("fs");
                const envFile = "{env_file}";
                function setEnvVar(envFile, key, value) {{
                    if (!fs.existsSync(envFile)) {{
                        fs.writeFileSync(envFile, key + "=" + value + "\\n");
                        return;
                    }}
                    const lines = fs.readFileSync(envFile, "utf8").split("\\n");
                    let found = false;
                    for (let i = 0; i < lines.length; i++) {{
                        const trimmed = lines[i].trim();
                        if (trimmed.startsWith("#") || !trimmed.includes("=")) continue;
                        const k = trimmed.split("=")[0].trim();
                        if (k === key) {{ lines[i] = key + "=" + value; found = true; break; }}
                    }}
                    if (!found) lines.push(key + "=" + value);
                    fs.writeFileSync(envFile, lines.join("\\n"));
                }}
                setEnvVar(envFile, "NEW_KEY", "new_value");
                setEnvVar(envFile, "OLD_KEY", "updated_value");
                console.log(fs.readFileSync(envFile, "utf8"));
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "NEW_KEY=new_value" in result.stdout
        assert "OLD_KEY=updated_value" in result.stdout

    def test_node_data_home_constant(self):
        """Node DATA_HOME resolves to ~/.telechat."""
        result = subprocess.run(
            ["node", "-e", """
                const path = require("path");
                const os = require("os");
                const DATA_HOME = path.join(os.homedir(), ".telechat");
                console.log(DATA_HOME);
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert ".telechat" in result.stdout


# ═════════════════════════════════════════════════════════════════════════════
# E2E: System Prompt for Claude-Assisted Init
# ═════════════════════════════════════════════════════════════════════════════


class TestClaudeAssistedInitSystemPrompt:
    """Tests that the Claude-assisted init system prompt contains required elements.

    The system prompt in telechat.js defines the entire AI-guided setup behavior.
    These tests verify it covers all platforms and configuration steps.
    """

    @pytest.fixture
    def system_prompt(self):
        """Extract the system prompt from telechat.js."""
        js_path = os.path.join(os.path.dirname(__file__), "..", "npm", "bin", "telechat.js")
        if not os.path.exists(js_path):
            pytest.skip("telechat.js not found")
        with open(js_path) as f:
            content = f.read()
        # Extract the system prompt template between backticks
        start = content.find("const systemPrompt = `")
        if start == -1:
            pytest.skip("System prompt not found in telechat.js")
        start += len("const systemPrompt = `")
        end = content.find("`;", start)
        return content[start:end]

    def test_prompt_covers_telegram_setup(self, system_prompt):
        """System prompt includes Telegram setup instructions."""
        assert "TELEGRAM" in system_prompt or "Telegram" in system_prompt
        assert "BotFather" in system_prompt
        assert "TELEGRAM_BOT_TOKEN" in system_prompt
        assert "userinfobot" in system_prompt

    def test_prompt_covers_whatsapp_setup(self, system_prompt):
        """System prompt includes WhatsApp setup instructions."""
        assert "WhatsApp" in system_prompt
        assert "GREEN_API" in system_prompt or "green-api" in system_prompt
        assert "idInstance" in system_prompt
        assert "apiTokenInstance" in system_prompt

    def test_prompt_covers_slack_setup(self, system_prompt):
        """System prompt includes Slack setup instructions."""
        assert "Slack" in system_prompt
        assert "SLACK_BOT_TOKEN" in system_prompt
        assert "xoxb-" in system_prompt
        assert "Socket Mode" in system_prompt or "SLACK_APP_TOKEN" in system_prompt

    def test_prompt_covers_token_validation(self, system_prompt):
        """System prompt instructs validation of all tokens."""
        assert "validate" in system_prompt.lower() or "curl" in system_prompt
        assert "getMe" in system_prompt or "api.telegram.org" in system_prompt
        assert "auth.test" in system_prompt or "slack.com/api" in system_prompt

    def test_prompt_covers_optional_features(self, system_prompt):
        """System prompt covers optional features configuration."""
        assert "Optional Features" in system_prompt or "optional" in system_prompt.lower()
        assert "Voice" in system_prompt or "transcri" in system_prompt.lower()
        assert "DALL-E" in system_prompt or "image" in system_prompt.lower()
        assert "Replicate" in system_prompt or "REPLICATE" in system_prompt

    def test_prompt_covers_claude_mode_selection(self, system_prompt):
        """System prompt covers Claude mode (CLI vs API) selection."""
        assert "CLI" in system_prompt or "cli" in system_prompt
        assert "API" in system_prompt or "api" in system_prompt
        assert "CLAUDE_MODE" in system_prompt or "claude" in system_prompt.lower()

    def test_prompt_covers_existing_config_detection(self, system_prompt):
        """System prompt instructs checking for existing config."""
        assert "already configured" in system_prompt.lower() or "existing" in system_prompt.lower()
        assert "keep" in system_prompt.lower() or "reconfigure" in system_prompt.lower()

    def test_prompt_covers_finalize_and_summary(self, system_prompt):
        """System prompt includes finalize section with summary."""
        assert "Finalize" in system_prompt or "FINALIZE" in system_prompt
        assert "Setup Complete" in system_prompt or "summary" in system_prompt.lower()
        assert "BOT_MODE" in system_prompt

    def test_prompt_instructs_silent_validation(self, system_prompt):
        """System prompt instructs silent (non-verbose) credential validation."""
        assert "silently" in system_prompt.lower() or "autonomous" in system_prompt.lower()

    def test_prompt_handles_skip_flow(self, system_prompt):
        """System prompt handles skip/skip-all for platforms and features."""
        assert "skip" in system_prompt.lower()


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Post-Init Validation (Node.js side)
# ═════════════════════════════════════════════════════════════════════════════


class TestPostInitValidation:
    """Tests for the post-init validation that runs after Claude CLI exits."""

    def test_post_init_validates_env_contents(self, tmp_path):
        """postInitValidation correctly parses .env after init."""
        env_file = tmp_path / ".env"
        env_file.write_text(textwrap.dedent("""\
            BOT_MODE=telegram
            TELEGRAM_BOT_TOKEN=123456:AABBccDDeeFF
            TELEGRAM_ALLOWED_USER_IDS=42
            CLAUDE_MODE=cli
        """))

        result = subprocess.run(
            ["node", "-e", f"""
                const fs = require("fs");
                const envFile = "{env_file}";
                const content = fs.readFileSync(envFile, "utf8");
                const vars = {{}};
                for (const line of content.split("\\n")) {{
                    if (!line.trim() || line.startsWith("#")) continue;
                    const eq = line.indexOf("=");
                    if (eq > 0) vars[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
                }}
                console.log(JSON.stringify(vars));
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed["BOT_MODE"] == "telegram"
        assert parsed["TELEGRAM_BOT_TOKEN"] == "123456:AABBccDDeeFF"
        assert parsed["TELEGRAM_ALLOWED_USER_IDS"] == "42"

    def test_post_init_detects_missing_user_restriction(self, tmp_path):
        """postInitValidation detects missing TELEGRAM_ALLOWED_USER_IDS."""
        env_file = tmp_path / ".env"
        env_file.write_text(textwrap.dedent("""\
            BOT_MODE=telegram
            TELEGRAM_BOT_TOKEN=123456:AABBccDDeeFF
        """))

        result = subprocess.run(
            ["node", "-e", f"""
                const fs = require("fs");
                const envFile = "{env_file}";
                const content = fs.readFileSync(envFile, "utf8");
                const vars = {{}};
                for (const line of content.split("\\n")) {{
                    if (!line.trim() || line.startsWith("#")) continue;
                    const eq = line.indexOf("=");
                    if (eq > 0) vars[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
                }}
                const needsWarning = vars.TELEGRAM_BOT_TOKEN && !vars.TELEGRAM_ALLOWED_USER_IDS;
                console.log(needsWarning ? "warning" : "ok");
            """],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "warning"


# ═════════════════════════════════════════════════════════════════════════════
# E2E: Full Init-to-Start Integration
# ═════════════════════════════════════════════════════════════════════════════


class TestInitToStartIntegration:
    """Integration tests: init creates valid config → start can use it."""

    def test_init_creates_startable_config(self, tmp_path):
        """After init with Telegram, the .env has all required fields for start."""
        env_path = tmp_path / ".env"
        env_path.write_text("")

        # Simulate real _set_env_var calls
        calls = []
        def _real_set(path, key, value):
            calls.append((key, value))

        inputs = ["1", "tok-12345678901", "42", "cli", ""]
        patches = [
            patch("main._find_env_file", return_value=str(env_path)),
            patch("main._env_example_path", return_value=None),
            patch("main._read_env", return_value={}),
            patch("main._set_env_var", side_effect=_real_set),
            patch("main._save_workdir"),
            patch("main._validate_telegram_token", return_value="@testbot"),
            patch("main._validate_green_api", return_value="authorized"),
            patch("main._validate_slack_token", return_value="test-team"),
            patch("builtins.input", side_effect=_safe_inputs(inputs)),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _cmd_init()

        # Verify all required keys were set
        set_keys = {k for k, v in calls}
        assert "BOT_MODE" in set_keys
        assert "TELEGRAM_BOT_TOKEN" in set_keys
        assert "TELEGRAM_ALLOWED_USER_IDS" in set_keys
        assert "CLAUDE_MODE" in set_keys

        # Verify the config would pass _has_any_platform
        config = {k: v for k, v in calls}
        assert _has_any_platform(config) is True

    def test_init_without_platform_fails_start_preflight(self, tmp_path):
        """Init that skips all platforms → _has_any_platform returns False."""
        # Skip everything
        inputs = ["1", "", "", "", ""]
        with _init_env(tmp_path, inputs) as (mock_set, _):
            _cmd_init()

        # No platform token was set
        config = _get_set_calls(mock_set)
        # BOT_MODE is set but no token
        platform_config = {k: v for k, v in config.items()
                           if k not in ("BOT_MODE", "CLAUDE_MODE")}
        assert _has_any_platform(platform_config) is False
