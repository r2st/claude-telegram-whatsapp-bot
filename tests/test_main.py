"""Comprehensive tests for telechat_pkg/main.py."""
from __future__ import annotations

import json
import os
import sys

# Ensure env vars are set before importing main
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

# Add the package directory to the path so `from main import ...` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telechat_pkg"))

import pytest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch, MagicMock

from main import (
    _resolve_workdir,
    _save_workdir,
    _find_env_file,
    _has_any_platform,
    _read_env,
    _set_env_var,
    _env_example_path,
    _parse_platforms,
    cli_entry,
    _print_setup_guidance,
    _sigint_handler,
)


# ─── _resolve_workdir ─────────────────────────────────────────────────────────


class TestResolveWorkdir:
    def test_returns_telechat_home_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELECHAT_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        result = _resolve_workdir()
        assert result == str(tmp_path)

    def test_chdir_to_telechat_home(self, tmp_path, monkeypatch):
        target = tmp_path / "custom_home"
        target.mkdir()
        monkeypatch.setenv("TELECHAT_HOME", str(target))
        _resolve_workdir()
        assert os.getcwd() == str(target)

    def test_falls_back_to_data_home_if_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        fake_data_home = tmp_path / ".telechat"
        fake_data_home.mkdir()
        monkeypatch.setattr("main._DATA_HOME", str(fake_data_home))
        result = _resolve_workdir()
        assert result == str(fake_data_home)

    def test_legacy_config_workdir_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        # _DATA_HOME does NOT exist → force fallback to config
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)

        legacy_wd = tmp_path / "legacy_workdir"
        legacy_wd.mkdir()

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"workdir": str(legacy_wd)}))
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))

        result = _resolve_workdir()
        assert result == str(legacy_wd)

    def test_legacy_config_claudeworkdir_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)

        legacy_wd = tmp_path / "old_workdir"
        legacy_wd.mkdir()

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"claudeWorkdir": str(legacy_wd)}))
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))

        result = _resolve_workdir()
        assert result == str(legacy_wd)

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)
        config_path = tmp_path / "missing_config.json"
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))

        result = _resolve_workdir()
        assert result is None

    def test_handles_invalid_json_in_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)

        config_path = tmp_path / "config.json"
        config_path.write_text("not valid json {{{")
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))

        result = _resolve_workdir()
        assert result is None

    def test_handles_missing_config_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)
        monkeypatch.setattr("main._CONFIG_FILE", str(tmp_path / "no_config.json"))

        result = _resolve_workdir()
        assert result is None

    def test_legacy_workdir_not_a_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        nonexistent_home = str(tmp_path / "nonexistent")
        monkeypatch.setattr("main._DATA_HOME", nonexistent_home)

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"workdir": "/does/not/exist/ever"}))
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))

        result = _resolve_workdir()
        assert result is None


# ─── _save_workdir ────────────────────────────────────────────────────────────


class TestSaveWorkdir:
    def test_creates_config_directory_if_needed(self, tmp_path, monkeypatch):
        config_path = tmp_path / "subdir" / "config.json"
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/some/path")
        assert config_path.parent.exists()

    def test_writes_workdir_to_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/my/workdir")
        data = json.loads(config_path.read_text())
        assert data["workdir"] == "/my/workdir"

    def test_updates_existing_config_preserving_other_keys(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"otherKey": "value", "workdir": "/old"}))
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/new/workdir")
        data = json.loads(config_path.read_text())
        assert data["workdir"] == "/new/workdir"
        assert data["otherKey"] == "value"

    def test_overwrites_invalid_json_in_existing_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text("INVALID JSON !!!")
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/fresh/path")
        data = json.loads(config_path.read_text())
        assert data["workdir"] == "/fresh/path"

    def test_handles_oserror_silently(self, tmp_path, monkeypatch):
        monkeypatch.setattr("main._CONFIG_FILE", "/root/no_permission/config.json")
        # Should not raise
        _save_workdir("/some/path")

    def test_config_file_ends_with_newline(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setattr("main._CONFIG_FILE", str(config_path))
        _save_workdir("/path")
        content = config_path.read_text()
        assert content.endswith("\n")


# ─── _find_env_file ───────────────────────────────────────────────────────────


class TestFindEnvFile:
    def test_returns_telechat_home_env_if_exists(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=VAL\n")
        monkeypatch.setenv("TELECHAT_HOME", str(tmp_path))
        result = _find_env_file()
        assert result == str(env_file)

    def test_returns_data_home_env_if_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        fake_home = tmp_path / ".telechat"
        fake_home.mkdir()
        env_file = fake_home / ".env"
        env_file.write_text("KEY=VAL\n")
        monkeypatch.setattr("main._DATA_HOME", str(fake_home))
        result = _find_env_file()
        assert result == str(env_file)

    def test_returns_cwd_env_if_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        # Make _DATA_HOME point somewhere without .env
        nonexistent = str(tmp_path / "no_home")
        monkeypatch.setattr("main._DATA_HOME", nonexistent)
        monkeypatch.chdir(tmp_path)
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=VAL\n")
        result = _find_env_file()
        assert result == str(env_file)

    def test_defaults_to_data_home_env_if_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELECHAT_HOME", raising=False)
        fake_home = tmp_path / ".telechat"
        fake_home.mkdir()
        monkeypatch.setattr("main._DATA_HOME", str(fake_home))
        monkeypatch.chdir(tmp_path)
        # No .env anywhere
        result = _find_env_file()
        assert result == str(fake_home / ".env")


# ─── _has_any_platform ────────────────────────────────────────────────────────


class TestHasAnyPlatform:
    def test_returns_false_for_empty_dict(self):
        assert _has_any_platform({}) is False

    def test_returns_true_if_telegram_bot_token_set(self):
        assert _has_any_platform({"TELEGRAM_BOT_TOKEN": "abc123"}) is True

    def test_returns_true_if_both_green_api_keys_set(self):
        env = {
            "GREEN_API_INSTANCE_ID": "123",
            "GREEN_API_TOKEN": "tok",
        }
        assert _has_any_platform(env) is True

    def test_returns_true_if_both_slack_tokens_set(self):
        env = {
            "SLACK_BOT_TOKEN": "xoxb-abc",
            "SLACK_APP_TOKEN": "xapp-abc",
        }
        assert _has_any_platform(env) is True

    def test_returns_false_if_only_green_api_instance_id(self):
        assert _has_any_platform({"GREEN_API_INSTANCE_ID": "123"}) is False

    def test_returns_false_if_only_green_api_token(self):
        assert _has_any_platform({"GREEN_API_TOKEN": "tok"}) is False

    def test_returns_false_if_only_slack_bot_token(self):
        assert _has_any_platform({"SLACK_BOT_TOKEN": "xoxb-abc"}) is False

    def test_returns_false_if_only_slack_app_token(self):
        assert _has_any_platform({"SLACK_APP_TOKEN": "xapp-abc"}) is False

    def test_returns_true_for_all_platforms_configured(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "tg-tok",
            "GREEN_API_INSTANCE_ID": "123",
            "GREEN_API_TOKEN": "tok",
            "SLACK_BOT_TOKEN": "xoxb-abc",
            "SLACK_APP_TOKEN": "xapp-abc",
        }
        assert _has_any_platform(env) is True


# ─── _read_env ────────────────────────────────────────────────────────────────


class TestReadEnv:
    def test_returns_empty_dict_for_nonexistent_file(self, tmp_path):
        result = _read_env(str(tmp_path / "nonexistent.env"))
        assert result == {}

    def test_parses_key_value_lines(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        result = _read_env(str(env_file))
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_ignores_comment_lines(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# this is a comment\nKEY=val\n")
        result = _read_env(str(env_file))
        assert "# this is a comment" not in result
        assert result == {"KEY": "val"}

    def test_ignores_blank_lines(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("\n\nKEY=val\n\n")
        result = _read_env(str(env_file))
        assert result == {"KEY": "val"}

    def test_ignores_lines_without_equals(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("NOEQUALSSIGN\nKEY=val\n")
        result = _read_env(str(env_file))
        assert "NOEQUALSSIGN" not in result
        assert result["KEY"] == "val"

    def test_value_with_equals_sign(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val=with=equals\n")
        result = _read_env(str(env_file))
        assert result["KEY"] == "val=with=equals"

    def test_strips_whitespace(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("  KEY  =  val  \n")
        result = _read_env(str(env_file))
        assert result["KEY"] == "val"


# ─── _set_env_var ─────────────────────────────────────────────────────────────


class TestSetEnvVar:
    def test_creates_file_if_not_exists(self, tmp_path):
        env_file = tmp_path / ".env"
        _set_env_var(str(env_file), "NEW_KEY", "new_value")
        assert env_file.exists()
        assert "NEW_KEY=new_value" in env_file.read_text()

    def test_updates_existing_key_in_place(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=old_value\nOTHER=keep\n")
        _set_env_var(str(env_file), "KEY", "new_value")
        content = env_file.read_text()
        assert "KEY=new_value" in content
        assert "KEY=old_value" not in content
        assert "OTHER=keep" in content

    def test_appends_new_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING=yes\n")
        _set_env_var(str(env_file), "BRAND_NEW", "value")
        content = env_file.read_text()
        assert "EXISTING=yes" in content
        assert "BRAND_NEW=value" in content

    def test_preserves_comments_in_file(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# My comment\nKEY=val\n")
        _set_env_var(str(env_file), "KEY", "updated")
        content = env_file.read_text()
        assert "# My comment" in content
        assert "KEY=updated" in content

    def test_does_not_duplicate_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=original\n")
        _set_env_var(str(env_file), "KEY", "changed")
        content = env_file.read_text()
        assert content.count("KEY=") == 1

    def test_empty_file_gets_key_appended(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        _set_env_var(str(env_file), "K", "v")
        assert "K=v" in env_file.read_text()


# ─── _env_example_path ───────────────────────────────────────────────────────


class TestEnvExamplePath:
    def test_returns_none_if_no_env_example_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Patch __file__ via pkg_dir to a tmp location with no .env.example
        with patch("main.os.path.abspath") as mock_abspath:
            mock_abspath.return_value = str(tmp_path / "main.py")
            result = _env_example_path()
        assert result is None

    def test_returns_path_if_found_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        example = tmp_path / ".env.example"
        example.write_text("KEY=example\n")
        result = _env_example_path()
        assert result == str(example)

    def test_returns_path_if_found_in_project_dir(self, tmp_path, monkeypatch):
        pkg_dir = tmp_path / "telechat_pkg"
        pkg_dir.mkdir()
        proj_dir = tmp_path
        example = proj_dir / ".env.example"
        example.write_text("KEY=example\n")
        # cwd has no .env.example
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)
        with patch("main.os.path.abspath") as mock_abspath:
            mock_abspath.return_value = str(pkg_dir / "main.py")
            result = _env_example_path()
        assert result == str(example)


# ─── _parse_platforms ─────────────────────────────────────────────────────────


class TestParsePlatforms:
    def test_both_returns_telegram_and_whatsapp(self):
        assert _parse_platforms("both") == {"telegram", "whatsapp"}

    def test_all_returns_all_platforms(self):
        result = _parse_platforms("all")
        assert {"telegram", "whatsapp", "slack"}.issubset(result)

    def test_telegram_only(self):
        assert _parse_platforms("telegram") == {"telegram"}

    def test_telegram_comma_whatsapp(self):
        assert _parse_platforms("telegram,whatsapp") == {"telegram", "whatsapp"}

    def test_telegram_comma_slack(self):
        assert _parse_platforms("telegram,slack") == {"telegram", "slack"}

    def test_handles_whitespace(self):
        assert _parse_platforms("  telegram , whatsapp  ") == {"telegram", "whatsapp"}

    def test_handles_uppercase(self):
        assert _parse_platforms("TELEGRAM") == {"telegram"}

    def test_handles_mixed_case(self):
        assert _parse_platforms("Telegram,Slack") == {"telegram", "slack"}

    def test_both_uppercase(self):
        assert _parse_platforms("BOTH") == {"telegram", "whatsapp"}

    def test_all_uppercase(self):
        result = _parse_platforms("ALL")
        assert {"telegram", "whatsapp", "slack"}.issubset(result)

    def test_slack_only(self):
        assert _parse_platforms("slack") == {"slack"}

    def test_whatsapp_only(self):
        assert _parse_platforms("whatsapp") == {"whatsapp"}


# ─── cli_entry ────────────────────────────────────────────────────────────────


class TestCliEntry:
    def test_help_command_prints_usage(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "help"])
        with patch("main._resolve_workdir"):
            cli_entry()
        out = capsys.readouterr().out
        assert "Usage:" in out
        assert "telechat" in out.lower()

    def test_double_dash_help_prints_usage(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "--help"])
        with patch("main._resolve_workdir"):
            cli_entry()
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_dash_h_prints_usage(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "-h"])
        with patch("main._resolve_workdir"):
            cli_entry()
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_version_flag_prints_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "--version"])
        with patch("main._resolve_workdir"):
            cli_entry()
        out = capsys.readouterr().out
        assert "telechat" in out

    def test_unknown_command_exits_with_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telechat", "unknown-cmd-xyz"])
        with patch("main._resolve_workdir"):
            with pytest.raises(SystemExit) as exc_info:
                cli_entry()
        assert exc_info.value.code == 1

    def test_unknown_command_prints_message(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "unknown-cmd-xyz"])
        with patch("main._resolve_workdir"):
            with pytest.raises(SystemExit):
                cli_entry()
        out = capsys.readouterr().out
        assert "Unknown command" in out

    def test_start_with_no_platform_config_prints_guidance_and_exits(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "argv", ["telechat", "start"])
        empty_env = tmp_path / ".env"
        empty_env.write_text("")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(empty_env)
        ):
            with pytest.raises(SystemExit) as exc_info:
                cli_entry()
        assert exc_info.value.code == 1

    def test_start_with_no_platform_config_prints_guidance(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys, "argv", ["telechat", "start"])
        empty_env = tmp_path / ".env"
        empty_env.write_text("")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(empty_env)
        ):
            with pytest.raises(SystemExit):
                cli_entry()
        out = capsys.readouterr().out
        assert "telechat" in out.lower()

    def test_start_command_calls_cmd_start_when_configured(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "argv", ["telechat", "start"])
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=real-token\n")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(env_file)
        ), patch("main._cmd_start") as mock_start:
            cli_entry()
            mock_start.assert_called_once()

    def test_run_command_calls_cmd_start_when_configured(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "argv", ["telechat", "run"])
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=real-token\n")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(env_file)
        ), patch("main._cmd_start") as mock_start:
            cli_entry()
            mock_start.assert_called_once()

    def test_init_command_calls_cmd_init(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telechat", "init"])
        with patch("main._resolve_workdir"), patch("main._cmd_init") as mock_init:
            cli_entry()
            mock_init.assert_called_once()

    def test_no_args_defaults_to_start_with_configured_env(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "argv", ["telechat"])
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=real-token\n")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(env_file)
        ), patch("main._cmd_start") as mock_start:
            cli_entry()
            mock_start.assert_called_once()

    def test_no_args_no_config_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telechat"])
        env_file = tmp_path / ".env"
        env_file.write_text("")
        with patch("main._resolve_workdir"), patch(
            "main._find_env_file", return_value=str(env_file)
        ):
            with pytest.raises(SystemExit) as exc_info:
                cli_entry()
        assert exc_info.value.code == 1

    def test_version_unknown_package_prints_unknown(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["telechat", "--version"])
        with patch("main._resolve_workdir"), patch(
            "importlib.metadata.version", side_effect=Exception("not found")
        ):
            cli_entry()
        out = capsys.readouterr().out
        assert "telechat" in out


# ─── _sigint_handler ─────────────────────────────────────────────────────────


class TestSigintHandler:
    def setup_method(self):
        # Reset the global counter before each test
        import main
        main._sigint_count = 0

    def test_first_call_raises_keyboard_interrupt(self):
        # First SIGINT now raises KeyboardInterrupt so the asyncio loop can
        # unwind cleanly and flush the DB writer thread, instead of calling
        # os._exit(0) which skipped that cleanup.
        with pytest.raises(KeyboardInterrupt):
            _sigint_handler(None, None)

    def test_second_call_exits_with_1(self):
        import main
        main._sigint_count = 1  # Simulate already called once
        with patch("os._exit") as mock_exit:
            _sigint_handler(None, None)
            mock_exit.assert_called_once_with(1)

    def test_first_call_prints_shutting_down(self, capsys):
        with pytest.raises(KeyboardInterrupt):
            _sigint_handler(None, None)
        out = capsys.readouterr().out
        assert "Shutting down" in out

    def test_increments_counter(self):
        import main
        with pytest.raises(KeyboardInterrupt):
            _sigint_handler(None, None)
        assert main._sigint_count == 1


# ─── _print_setup_guidance ───────────────────────────────────────────────────


class TestPrintSetupGuidance:
    def test_does_not_crash(self, capsys):
        _print_setup_guidance()
        out = capsys.readouterr().out
        assert len(out) > 0

    def test_mentions_telechat_init(self, capsys):
        _print_setup_guidance()
        out = capsys.readouterr().out
        assert "telechat init" in out

    def test_mentions_not_configured(self, capsys):
        _print_setup_guidance()
        out = capsys.readouterr().out
        assert "not configured" in out

    def test_mentions_platform_credentials(self, capsys):
        _print_setup_guidance()
        out = capsys.readouterr().out
        assert "credentials" in out.lower() or "platform" in out.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 12. __main__.py
# ══════════════════════════════════════════════════════════════════════════════

class TestDunderMain:
    def test_cli_entry_is_importable(self):
        # A second class of this name lower in the file (the one that actually
        # reloads telechat_pkg.__main__) used to shadow this one, so it never
        # ran. Renamed rather than deleted: it is the only check that the name
        # __main__.py imports still exists on the module.
        from main import cli_entry as ce
        assert callable(ce)


# ─── _cmd_init ────────────────────────────────────────────────────────────────


def _safe_inputs(inputs_list):
    """Return a side_effect callable that yields from inputs_list, then returns ''
    for any extra prompts (e.g. optional features, validation retries)."""
    it = iter(inputs_list)
    def _side_effect(prompt=""):
        return next(it, "")
    return _side_effect


# Shared validation mocks for _cmd_init tests — prevents real HTTP calls
_INIT_VALIDATION_PATCHES = [
    patch("main._validate_telegram_token", return_value="@testbot"),
    patch("main._validate_green_api", return_value="authorized"),
    patch("main._validate_slack_token", return_value="test-team"),
]


@contextmanager
def _init_patches(env_path, inputs_list, env_data=None, extra_patches=None):
    """Context manager for running _cmd_init with mocked I/O and validation."""
    mock_set = MagicMock()
    env_data = env_data or {}
    if hasattr(env_path, 'write_text') and not env_path.exists():
        env_path.write_text("")

    patches = [
        patch("main._find_env_file", return_value=str(env_path)),
        patch("main._env_example_path", return_value=None),
        patch("main._read_env", return_value=env_data),
        patch("main._set_env_var", mock_set),
        patch("main._save_workdir"),
        *_INIT_VALIDATION_PATCHES,
        patch("builtins.input", side_effect=_safe_inputs(inputs_list)),
        *(extra_patches or []),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield mock_set


class TestCmdInitEnvCreation:
    """Tests for .env file creation at the start of _cmd_init."""

    def test_creates_env_from_example_when_not_exists(self, tmp_path, monkeypatch):
        """When no .env exists and .env.example is found, copy it."""
        env_path = tmp_path / ".env"
        example_path = tmp_path / ".env.example"
        example_path.write_text("BOT_MODE=telegram\n")

        extra = [
            patch("main._env_example_path", return_value=str(example_path)),
            patch("main._parse_platforms", return_value={"telegram"}),
        ]
        with _init_patches(env_path, ["", "", "", "", ""],
                           env_data={"BOT_MODE": "telegram"},
                           extra_patches=extra):
            from main import _cmd_init
            _cmd_init()
        assert env_path.exists()

    def test_creates_empty_env_when_no_example(self, tmp_path, monkeypatch):
        """When no .env and no .env.example, create an empty file."""
        env_path = tmp_path / ".env"
        extra = [patch("main._parse_platforms", return_value={"telegram"})]
        with _init_patches(env_path, ["", "", "", "", ""],
                           extra_patches=extra):
            from main import _cmd_init
            _cmd_init()
        assert env_path.exists()

    def test_uses_existing_env_file(self, tmp_path, capsys):
        """When .env already exists, prints 'Using' message."""
        env_path = tmp_path / ".env"
        env_path.write_text("BOT_MODE=telegram\n")
        extra = [patch("main._parse_platforms", return_value={"telegram"})]
        with _init_patches(env_path, ["", "", "", "", ""],
                           env_data={"BOT_MODE": "telegram"},
                           extra_patches=extra):
            from main import _cmd_init
            _cmd_init()

        out = capsys.readouterr().out
        assert "Using" in out


class TestCmdInitPlatformSelection:
    """Tests for platform selection in _cmd_init."""

    def _run_init_with_inputs(self, env_path, inputs_list, env_data=None):
        """Helper to run _cmd_init with mock inputs."""
        env_path.write_text("")
        with _init_patches(env_path, inputs_list, env_data) as mock_set:
            from main import _cmd_init
            _cmd_init()
        return mock_set

    def test_choice_1_sets_telegram(self, tmp_path):
        """Choice '1' sets BOT_MODE=telegram."""
        env_path = tmp_path / ".env"
        # inputs: platform=1, token(skip), user_ids, claude_mode, features(skip)
        mock_set = self._run_init_with_inputs(
            env_path, ["1", "", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "telegram" in c for c in calls)

    def test_choice_2_sets_whatsapp(self, tmp_path):
        """Choice '2' sets BOT_MODE=whatsapp."""
        env_path = tmp_path / ".env"
        # inputs: platform=2, instance_id, api_token, wa_numbers, claude_mode, features(skip)
        mock_set = self._run_init_with_inputs(
            env_path, ["2", "inst123", "tok456", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "whatsapp" in c for c in calls)

    def test_choice_3_sets_slack(self, tmp_path):
        """Choice '3' sets BOT_MODE=slack."""
        env_path = tmp_path / ".env"
        # inputs: platform=3, bot_token, app_token, slack_ids, claude_mode, features(skip)
        mock_set = self._run_init_with_inputs(
            env_path, ["3", "xoxb-bot", "xapp-app", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "slack" in c for c in calls)

    def test_choice_5_sets_telegram_whatsapp(self, tmp_path):
        """Choice '5' sets BOT_MODE=telegram,whatsapp."""
        env_path = tmp_path / ".env"
        mock_set = self._run_init_with_inputs(
            env_path, ["5", "", "", "inst", "tok", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "telegram,whatsapp" in c for c in calls)

    def test_choice_6_sets_telegram_slack(self, tmp_path):
        """Choice '6' sets BOT_MODE=telegram,slack."""
        env_path = tmp_path / ".env"
        mock_set = self._run_init_with_inputs(
            env_path, ["6", "", "", "xoxb-bot", "xapp-app", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "telegram,slack" in c for c in calls)

    def test_choice_8_sets_all(self, tmp_path):
        """Choice '8' sets BOT_MODE=all."""
        env_path = tmp_path / ".env"
        # inputs: platform=8, tg_token(skip), user_ids, inst, tok, wa_nums,
        #         bot_token, app_token, slack_ids, web_port, web_token, claude_mode, features(skip)
        mock_set = self._run_init_with_inputs(
            env_path, ["8", "", "", "inst", "tok", "", "xoxb-b", "xapp-a", "", "", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "all" in c for c in calls)

    def test_custom_mode_string_saved(self, tmp_path):
        """Entering a custom mode string saves it as BOT_MODE."""
        env_path = tmp_path / ".env"
        # inputs: platform=whatsapp,slack, inst, tok, wa_nums, bot_token, app_token, slack_ids, claude_mode, features(skip)
        mock_set = self._run_init_with_inputs(
            env_path, ["whatsapp,slack", "inst", "tok", "", "xoxb-b", "xapp-a", "", "", ""]
        )
        calls = [str(c) for c in mock_set.call_args_list]
        assert any("BOT_MODE" in c and "whatsapp,slack" in c for c in calls)

    def test_empty_choice_keeps_current_mode(self, tmp_path, capsys):
        """Pressing enter (empty input) keeps the existing BOT_MODE."""
        env_path = tmp_path / ".env"
        # Start with existing token to skip token prompt; add N to skip change, then user_ids, claude_mode, features
        mock_set = self._run_init_with_inputs(
            env_path,
            ["", "N", "", "", ""],  # empty platform choice → keep telegram
            env_data={"BOT_MODE": "telegram", "TELEGRAM_BOT_TOKEN": "existing-tok-long"}
        )
        # BOT_MODE should NOT have been set (empty choice → keep current)
        bot_mode_calls = [c for c in mock_set.call_args_list
                          if c.args[1] == "BOT_MODE"]
        assert len(bot_mode_calls) == 0


class TestCmdInitTelegramSetup:
    """Tests for the Telegram section of _cmd_init."""

    def test_saves_new_telegram_token(self, tmp_path):
        """Token entered by user is saved via _set_env_var."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # inputs: platform=1, new token, user IDs, claude mode, features
        with _init_patches(env_path, ["1", "new-bot-token-123", "", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("TELEGRAM_BOT_TOKEN" in c and "new-bot-token-123" in c for c in calls)

    def test_keeps_existing_token_when_user_declines_change(self, tmp_path):
        """Existing token is kept when user answers 'N' to 'Change?'."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        existing_token = "existing-long-token-abc"
        # inputs: platform=keep, change=N, user IDs, claude mode, features
        with _init_patches(env_path, ["", "N", "", ""],
                           env_data={"BOT_MODE": "telegram",
                                     "TELEGRAM_BOT_TOKEN": existing_token}) as mock_set:
            from main import _cmd_init
            _cmd_init()

        token_set_calls = [c for c in mock_set.call_args_list
                           if c.args[1] == "TELEGRAM_BOT_TOKEN"]
        assert len(token_set_calls) == 0

    def test_saves_allowed_user_ids(self, tmp_path):
        """User IDs entered are saved."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # inputs: platform=1, token, user_ids, claude_mode, features
        with _init_patches(env_path, ["1", "my-token", "123,456", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("TELEGRAM_ALLOWED_USER_IDS" in c and "123,456" in c for c in calls)

    def test_none_user_ids_clears_them(self, tmp_path):
        """Typing 'none' for user IDs sets TELEGRAM_ALLOWED_USER_IDS to empty string."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # inputs: platform=1, token, none, claude_mode, features
        with _init_patches(env_path, ["1", "my-token", "none", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        id_calls = [c for c in mock_set.call_args_list
                    if c.args[1] == "TELEGRAM_ALLOWED_USER_IDS"]
        assert len(id_calls) == 1
        assert id_calls[0].args[2] == ""


class TestCmdInitWhatsAppSetup:
    """Tests for the WhatsApp section of _cmd_init."""

    def test_saves_instance_id_and_token(self, tmp_path):
        """Instance ID and API token are saved when entered."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # inputs: platform=2, instance_id, api_token, wa_numbers, claude_mode, features
        with _init_patches(env_path, ["2", "inst-99", "tok-abc", "", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("GREEN_API_INSTANCE_ID" in c and "inst-99" in c for c in calls)
        assert any("GREEN_API_TOKEN" in c and "tok-abc" in c for c in calls)

    def test_allowed_numbers_cleaned_of_special_chars(self, tmp_path):
        """Phone numbers have +, spaces, dashes stripped."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # inputs: platform=2, instance_id, api_token, wa_numbers, claude_mode, features
        with _init_patches(env_path, ["2", "inst-1", "tok-1", "+91 987-654-3210", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        num_calls = [c for c in mock_set.call_args_list
                     if c.args[1] == "WHATSAPP_ALLOWED_NUMBERS"]
        assert len(num_calls) == 1
        assert num_calls[0].args[2] == "919876543210"

    def test_none_wa_numbers_clears_restriction(self, tmp_path):
        """Typing 'none' for WA numbers sets WHATSAPP_ALLOWED_NUMBERS to empty."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with _init_patches(env_path, ["2", "inst-1", "tok-1", "none", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        num_calls = [c for c in mock_set.call_args_list
                     if c.args[1] == "WHATSAPP_ALLOWED_NUMBERS"]
        assert len(num_calls) == 1
        assert num_calls[0].args[2] == ""

    def test_existing_instance_id_kept_when_user_declines(self, tmp_path):
        """Existing instance ID is kept when user answers 'N' to 'Change?'."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # platform=2, change=N, wa_numbers=enter, claude_mode, features
        with _init_patches(env_path, ["2", "N", "", ""],
                           env_data={"GREEN_API_INSTANCE_ID": "existing-inst",
                                     "GREEN_API_TOKEN": "existing-tok"}) as mock_set:
            from main import _cmd_init
            _cmd_init()

        inst_calls = [c for c in mock_set.call_args_list
                      if c.args[1] == "GREEN_API_INSTANCE_ID"]
        assert len(inst_calls) == 0


class TestCmdInitSlackSetup:
    """Tests for the Slack section of _cmd_init."""

    def test_saves_bot_and_app_tokens(self, tmp_path):
        """Slack bot token and app token are saved when entered."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with _init_patches(env_path, ["3", "xoxb-my-bot-token", "xapp-my-app-token", "", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("SLACK_BOT_TOKEN" in c and "xoxb-my-bot-token" in c for c in calls)
        assert any("SLACK_APP_TOKEN" in c and "xapp-my-app-token" in c for c in calls)

    def test_invalid_bot_token_prefix_treated_as_missing(self, tmp_path):
        """Bot token not starting with 'xoxb-' is treated as missing/empty."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with _init_patches(env_path, ["3", "xoxb-valid-now", "xapp-valid", "", ""],
                           env_data={"SLACK_BOT_TOKEN": "bad-prefix-token"}) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("SLACK_BOT_TOKEN" in c and "xoxb-valid-now" in c for c in calls)

    def test_saves_slack_allowed_user_ids(self, tmp_path):
        """Slack allowed user IDs are saved when entered."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with _init_patches(env_path, ["3", "xoxb-bot", "xapp-app", "U12345,U67890", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("SLACK_ALLOWED_USER_IDS" in c and "U12345,U67890" in c for c in calls)

    def test_none_slack_ids_clears_them(self, tmp_path):
        """Typing 'none' for Slack IDs sets SLACK_ALLOWED_USER_IDS to empty."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with _init_patches(env_path, ["3", "xoxb-bot", "xapp-app", "none", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        id_calls = [c for c in mock_set.call_args_list
                    if c.args[1] == "SLACK_ALLOWED_USER_IDS"]
        assert len(id_calls) == 1
        assert id_calls[0].args[2] == ""


class TestCmdInitClaudeSettings:
    """Tests for the Claude settings section of _cmd_init."""

    def test_sets_claude_mode_cli(self, tmp_path):
        """Entering 'cli' sets CLAUDE_MODE=cli."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # platform=keep, change=N, user_ids, claude_mode=cli, features
        with _init_patches(env_path, ["", "N", "", "cli", ""],
                           env_data={"BOT_MODE": "telegram",
                                     "TELEGRAM_BOT_TOKEN": "tok-abc-1234567890"}) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("CLAUDE_MODE" in c and "cli" in c for c in calls)

    def test_sets_claude_mode_api_and_prompts_for_key(self, tmp_path):
        """Setting mode to 'api' and no existing key prompts for API key."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # platform=keep, new token, user_ids, claude_mode=api, api_key, features
        with _init_patches(env_path, ["", "new-bot-token", "", "api", "sk-my-api-key", ""]) as mock_set:
            from main import _cmd_init
            _cmd_init()

        calls = [str(c) for c in mock_set.call_args_list]
        assert any("ANTHROPIC_API_KEY" in c and "sk-my-api-key" in c for c in calls)

    def test_api_mode_with_existing_key_does_not_prompt(self, tmp_path, capsys):
        """If API key already set (not placeholder), does not prompt for new one."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # platform=keep, change=N(keep token), user_ids, claude_mode=api, features
        with _init_patches(env_path, ["", "N", "", "api", ""],
                           env_data={"BOT_MODE": "telegram",
                                     "TELEGRAM_BOT_TOKEN": "tok-existing-xyz-long",
                                     "CLAUDE_MODE": "cli",
                                     "ANTHROPIC_API_KEY": "sk-real-key-here"}) as mock_set:
            from main import _cmd_init
            _cmd_init()

        out = capsys.readouterr().out
        assert "API Key:" in out

    def test_saves_workdir_at_end(self, tmp_path):
        """_save_workdir is called at the end of _cmd_init."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        mock_save = MagicMock()
        patches = _INIT_VALIDATION_PATCHES + [
            patch("main._find_env_file", return_value=str(env_path)),
            patch("main._env_example_path", return_value=None),
            patch("main._read_env", return_value={"BOT_MODE": "telegram"}),
            patch("main._set_env_var"),
            patch("main._save_workdir", mock_save),
            patch("builtins.input", side_effect=_safe_inputs(["", "", "", ""])),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            from main import _cmd_init
            _cmd_init()

        mock_save.assert_called_once()

    def test_prints_setup_complete_at_end(self, tmp_path, capsys):
        """'Setup Complete' is printed at the end of a successful init."""
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # platform=keep(telegram), token, user_ids, claude_mode, features
        with _init_patches(env_path, ["", "my-token", "", "", ""]):
            from main import _cmd_init
            _cmd_init()

        out = capsys.readouterr().out
        assert "Setup Complete" in out


# ─── _cmd_start ───────────────────────────────────────────────────────────────


class TestCmdStartBotModeAliases:
    """Tests for BOT_MODE parsing in _cmd_start."""

    def _run_start(self, env_vars, monkeypatch):
        """Run _cmd_start with given env vars, mocking heavy imports."""
        import subprocess

        monkeypatch.setattr(os, "environ", {**os.environ, **env_vars})

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run"):
            from main import _cmd_start
            _cmd_start()

    def test_both_alias_is_accepted(self, monkeypatch):
        """BOT_MODE=both is a valid alias (no sys.exit)."""
        import subprocess
        env_vars = {"BOT_MODE": "both"}
        called = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=lambda coro: called.append(True)):
            monkeypatch.setenv("BOT_MODE", "both")
            monkeypatch.setenv("GREEN_API_INSTANCE_ID", "inst-1")
            monkeypatch.setenv("GREEN_API_TOKEN", "tok-1")
            monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "1234567890")
            from main import _cmd_start
            _cmd_start()

        assert called  # asyncio.run was called → no sys.exit

    def test_all_alias_is_accepted(self, monkeypatch):
        """BOT_MODE=all is a valid alias (no sys.exit)."""
        import subprocess
        called = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=lambda coro: called.append(True)):
            monkeypatch.setenv("BOT_MODE", "all")
            monkeypatch.setenv("GREEN_API_INSTANCE_ID", "inst-1")
            monkeypatch.setenv("GREEN_API_TOKEN", "tok-1")
            monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "1234567890")
            from main import _cmd_start
            _cmd_start()

        assert called


class TestCmdStartUnknownPlatform:
    """Tests for unknown platform validation in _cmd_start."""

    def test_unknown_platform_exits_1(self, monkeypatch):
        """An unknown platform name in BOT_MODE causes sys.exit(1)."""
        import subprocess
        exit_calls = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")):
            monkeypatch.setenv("BOT_MODE", "fakeplatform")
            monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
            from main import _cmd_start
            try:
                _cmd_start()
            except Exception:
                pass

        assert 1 in exit_calls

    def test_unknown_platform_prints_error_message(self, monkeypatch, capsys):
        """Error message names the unknown platform."""
        import subprocess
        exit_calls = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")):
            monkeypatch.setenv("BOT_MODE", "badplatform")
            monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
            from main import _cmd_start
            try:
                _cmd_start()
            except Exception:
                pass

        out = capsys.readouterr().out
        assert "ERROR" in out or "Unknown" in out


class TestCmdStartWhatsAppPreFlight:
    """Tests for WhatsApp credential pre-flight check in _cmd_start."""

    def test_missing_green_api_creds_exits_1(self, monkeypatch):
        """Missing GREEN_API_INSTANCE_ID or TOKEN causes sys.exit(1)."""
        import subprocess
        exit_calls = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")):
            monkeypatch.setenv("BOT_MODE", "whatsapp")
            monkeypatch.delenv("GREEN_API_INSTANCE_ID", raising=False)
            monkeypatch.delenv("GREEN_API_TOKEN", raising=False)
            monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
            from main import _cmd_start
            try:
                _cmd_start()
            except Exception:
                pass

        assert 1 in exit_calls

    def test_missing_green_api_creds_prints_error(self, monkeypatch, capsys):
        """Error message mentions WhatsApp and missing credentials."""
        import subprocess
        exit_calls = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")):
            monkeypatch.setenv("BOT_MODE", "whatsapp")
            monkeypatch.delenv("GREEN_API_INSTANCE_ID", raising=False)
            monkeypatch.delenv("GREEN_API_TOKEN", raising=False)
            monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
            from main import _cmd_start
            try:
                _cmd_start()
            except Exception:
                pass

        out = capsys.readouterr().out
        assert "WhatsApp" in out or "GREEN_API" in out

    def test_empty_wa_numbers_prints_note(self, monkeypatch, capsys):
        """Empty WHATSAPP_ALLOWED_NUMBERS prints a warning note (not exit)."""
        import subprocess
        called = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=lambda coro: called.append(True)):
            monkeypatch.setenv("BOT_MODE", "whatsapp")
            monkeypatch.setenv("GREEN_API_INSTANCE_ID", "inst-123")
            monkeypatch.setenv("GREEN_API_TOKEN", "tok-abc")
            monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
            from main import _cmd_start
            _cmd_start()

        out = capsys.readouterr().out
        assert "WHATSAPP_ALLOWED_NUMBERS" in out or "everyone" in out
        assert called  # continued to asyncio.run, did not exit


class TestCmdStartKillExisting:
    """Tests for the kill-existing-instances logic in _cmd_start."""

    def test_pgrep_failure_does_not_crash(self, monkeypatch):
        """CalledProcessError from pgrep is silently swallowed."""
        import subprocess
        called = []

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=lambda coro: called.append(True)):
            monkeypatch.setenv("BOT_MODE", "telegram")
            from main import _cmd_start
            _cmd_start()

        assert called  # reached asyncio.run

    def test_lsof_failure_does_not_crash(self, monkeypatch):
        """CalledProcessError from lsof is silently swallowed."""
        import subprocess
        called = []

        def fake_check_output(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd[0])

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("asyncio.run", side_effect=lambda coro: called.append(True)):
            monkeypatch.setenv("BOT_MODE", "telegram")
            from main import _cmd_start
            _cmd_start()

        assert called


class TestCmdStartErrorHandling:
    """Tests for error handling in _cmd_start's asyncio.run wrapper."""

    def test_keyboard_interrupt_calls_os_exit_0(self, monkeypatch):
        """KeyboardInterrupt is caught and results in sys.exit(0)."""
        import subprocess
        exit_calls = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=KeyboardInterrupt()):
            monkeypatch.setenv("BOT_MODE", "telegram")
            monkeypatch.setattr(sys, "exit", fake_exit)
            from main import _cmd_start
            with pytest.raises(SystemExit):
                _cmd_start()

        assert 0 in exit_calls

    def test_runtime_error_with_token_exits_1(self, monkeypatch):
        """RuntimeError containing 'TOKEN' triggers sys.exit(1) via guidance."""
        import subprocess
        exit_calls = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run", side_effect=RuntimeError("TOKEN not set")), \
             patch("main._print_setup_guidance"):
            monkeypatch.setenv("BOT_MODE", "telegram")
            monkeypatch.setattr(sys, "exit", fake_exit)
            from main import _cmd_start
            with pytest.raises(SystemExit):
                _cmd_start()

        assert 1 in exit_calls

    def test_runtime_error_with_not_set_message_exits_1(self, monkeypatch):
        """RuntimeError with 'not set' in message triggers clean exit."""
        import subprocess
        exit_calls = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run",
                   side_effect=RuntimeError("TELEGRAM_BOT_TOKEN is not set")), \
             patch("main._print_setup_guidance"):
            monkeypatch.setenv("BOT_MODE", "telegram")
            monkeypatch.setattr(sys, "exit", fake_exit)
            from main import _cmd_start
            with pytest.raises(SystemExit):
                _cmd_start()

        assert 1 in exit_calls

    def test_unrelated_runtime_error_is_reraised(self, monkeypatch):
        """RuntimeError unrelated to TOKEN/not-set is re-raised."""
        import subprocess

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch("asyncio.run",
                   side_effect=RuntimeError("something completely unrelated")):
            monkeypatch.setenv("BOT_MODE", "telegram")
            from main import _cmd_start
            with pytest.raises(RuntimeError, match="something completely unrelated"):
                _cmd_start()


# ─── __main__ guard ───────────────────────────────────────────────────────────


class TestMainGuard:
    def test_cli_entry_is_callable(self):
        """The cli_entry function used in the __main__ guard is callable."""
        from main import cli_entry
        assert callable(cli_entry)

    def test_module_has_main_guard(self):
        """The module source contains the __main__ guard."""
        import inspect
        import main
        source = inspect.getsource(main)
        assert 'if __name__ == "__main__"' in source
        assert "cli_entry()" in source


class TestDunderMainModule:
    """Tests for telechat_pkg/__main__.py."""

    def test_dunder_main_imports_cli_entry(self):
        with patch("telechat_pkg.main.cli_entry") as mock_entry:
            import importlib
            import telechat_pkg.__main__
            mock_entry.reset_mock()
            importlib.reload(telechat_pkg.__main__)
        mock_entry.assert_called_once()


class TestCmdInitWhatsAppChangeY:
    """Cover the WhatsApp 'Change? → y' paths."""

    def test_change_instance_id(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # Flow: platform=2(whatsapp), Change? y, new instance+token pair, numbers=keep, claude_mode, features
        with _init_patches(env_path, ["2", "y", "new-inst-id", "new-tok-val", "", ""],
                           env_data={
                               "BOT_MODE": "whatsapp",
                               "GREEN_API_INSTANCE_ID": "old-inst",
                               "GREEN_API_TOKEN": "old-token-1234567890",
                           }) as mock_set:
            from main import _cmd_init
            _cmd_init()

        inst_calls = [c for c in mock_set.call_args_list
                      if c.args[1] == "GREEN_API_INSTANCE_ID"]
        assert len(inst_calls) >= 1
        assert inst_calls[-1].args[2] == "new-inst-id"

        tok_calls = [c for c in mock_set.call_args_list
                     if c.args[1] == "GREEN_API_TOKEN"]
        assert len(tok_calls) >= 1
        assert tok_calls[-1].args[2] == "new-tok-val"


class TestCmdInitSlackChangeY:
    """Cover the Slack 'Change? → y' paths."""

    def test_change_slack_tokens(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("")
        # Flow: platform=3(slack), Change bot? y, new bot token, Change app? y, new app token, ids, claude, features
        with _init_patches(env_path, ["3", "y", "xoxb-new-bot-token", "y", "xapp-new-app-token", "", ""],
                           env_data={
                               "BOT_MODE": "slack",
                               "SLACK_BOT_TOKEN": "xoxb-old-bot-token-long",
                               "SLACK_APP_TOKEN": "xapp-old-app-token-long",
                           }) as mock_set:
            from main import _cmd_init
            _cmd_init()

        bot_calls = [c for c in mock_set.call_args_list
                     if c.args[1] == "SLACK_BOT_TOKEN"]
        assert len(bot_calls) >= 1
        assert bot_calls[-1].args[2] == "xoxb-new-bot-token"

        app_calls = [c for c in mock_set.call_args_list
                     if c.args[1] == "SLACK_APP_TOKEN"]
        assert len(app_calls) >= 1
        assert app_calls[-1].args[2] == "xapp-new-app-token"


class TestCmdStartKillSuccess:
    """The replace-existing-instance path, end to end through _cmd_start."""

    def test_kills_other_telechat_pids(self, monkeypatch):
        import subprocess as sp
        killed = []

        def fake_check_output(cmd, **kwargs):
            if "pgrep" in cmd:
                return "1234\n5678\n"
            if "lsof" in cmd:
                return "9999\n"
            if "ps" in cmd:
                # Every candidate here really is a bot process.
                return "python3 -m telechat_pkg.main\n"
            raise sp.CalledProcessError(1, cmd[0])

        monkeypatch.setenv("BOT_MODE", "telegram")
        monkeypatch.setattr(os, "getpid", lambda: 5678)

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("os.kill", side_effect=lambda pid, sig: killed.append(pid)), \
             patch("asyncio.run"):
            from main import _cmd_start
            _cmd_start()

        assert 1234 in killed
        assert 5678 not in killed  # never signal yourself
        assert 9999 in killed      # health-port holder, and it is telechat

    def test_spares_processes_that_only_mention_telechat(self, monkeypatch):
        # THE bug: `pgrep -f telechat_pkg.main` matched an editor with the file
        # open, a grep for the string, and the test suite itself.
        import subprocess as sp
        killed = []

        def fake_check_output(cmd, **kwargs):
            if "pgrep" in cmd:
                return "1234\n"
            if "ps" in cmd:
                return "vim telechat_pkg/main.py\n"
            raise sp.CalledProcessError(1, cmd[0])

        monkeypatch.setenv("BOT_MODE", "telegram")
        monkeypatch.setattr(os, "getpid", lambda: 5678)

        with patch("dotenv.load_dotenv"), \
             patch("main._find_env_file", return_value="/fake/.env"), \
             patch("subprocess.check_output", side_effect=fake_check_output), \
             patch("os.kill", side_effect=lambda pid, sig: killed.append(pid)), \
             patch("asyncio.run"):
            from main import _cmd_start
            _cmd_start()

        assert killed == []


class TestIsTelechatCmdline:
    """Which command lines count as 'the bot is already running'."""

    @pytest.mark.parametrize("cmdline", [
        "telechat",
        "telechat start",
        "/opt/homebrew/bin/telechat start",
        "python3 -m telechat_pkg.main",
        "python -m telechat_pkg",
        "/usr/bin/python3.12 -m telechat_pkg.main --debug",
        "python3 /Users/x/telechat/telechat_pkg/main.py",
    ])
    def test_real_bot_processes_match(self, cmdline):
        from main import _is_telechat_cmdline
        assert _is_telechat_cmdline(cmdline) is True

    @pytest.mark.parametrize("cmdline", [
        "",
        "   ",
        "grep -rn telechat_pkg.main .",
        "vim telechat_pkg/main.py",
        "tail -f bot.log",
        "python3 -m pytest tests/test_main.py",
        "rg telechat_pkg.main",
        "less telechat_pkg/main.py",
        # An editor invoked *through* python still isn't the bot.
        "python3 -m idlelib telechat_pkg/main.py",
    ])
    def test_mentions_do_not_match(self, cmdline):
        from main import _is_telechat_cmdline
        assert _is_telechat_cmdline(cmdline) is False

    def test_unbalanced_quotes_fall_back_to_whitespace_split(self):
        from main import _is_telechat_cmdline
        # shlex.split raises on an unterminated quote; the caller must not.
        assert _is_telechat_cmdline('python3 -m telechat_pkg.main "unclosed') is True


class TestPidFile:
    """The PID file that replaced substring matching as the source of truth."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELECHAT_HOME", str(tmp_path))
        return tmp_path

    def test_write_then_read_round_trip(self, home):
        from main import _pid_file_path, _read_pid_file, _write_pid_file
        _write_pid_file()
        assert _read_pid_file() == os.getpid()
        assert _pid_file_path() == str(home / ".telechat.pid")

    def test_missing_file_reads_as_none(self, home):
        from main import _read_pid_file
        assert _read_pid_file() is None

    def test_garbage_file_reads_as_none(self, home):
        from main import _read_pid_file
        (home / ".telechat.pid").write_text("not-a-pid\n")
        assert _read_pid_file() is None

    def test_write_creates_a_missing_data_home(self, tmp_path, monkeypatch):
        from main import _read_pid_file, _write_pid_file
        monkeypatch.setenv("TELECHAT_HOME", str(tmp_path / "brand" / "new"))
        _write_pid_file()
        assert _read_pid_file() == os.getpid()

    def test_remove_only_deletes_our_own_pid(self, home):
        from main import _pid_file_path, _remove_pid_file
        # A successor has already claimed the file — removing it on our way out
        # would leave the new instance unfindable.
        (home / ".telechat.pid").write_text("999999\n")
        _remove_pid_file()
        assert os.path.exists(_pid_file_path())

    def test_remove_deletes_our_own_pid(self, home):
        from main import _pid_file_path, _remove_pid_file, _write_pid_file
        _write_pid_file()
        _remove_pid_file()
        assert not os.path.exists(_pid_file_path())

    def test_unwritable_home_is_survivable(self, tmp_path, monkeypatch):
        from main import _write_pid_file
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setenv("TELECHAT_HOME", str(blocker / "home"))
        _write_pid_file()  # must not raise


class TestTerminatePreviousInstances:
    """Only real, other, same-user telechat processes get signalled."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELECHAT_HOME", str(tmp_path))
        return tmp_path

    def _patched(self, monkeypatch, *, pgrep="", lsof="", cmdlines=None):
        import subprocess as sp
        killed = []
        cmdlines = cmdlines or {}

        def fake_check_output(cmd, **kwargs):
            if "pgrep" in cmd:
                if not pgrep:
                    raise sp.CalledProcessError(1, "pgrep")
                return pgrep
            if "lsof" in cmd:
                if not lsof:
                    raise sp.CalledProcessError(1, "lsof")
                return lsof
            if "ps" in cmd:
                pid = cmd[-1]
                if pid not in cmdlines:
                    raise sp.CalledProcessError(1, "ps")
                return cmdlines[pid] + "\n"
            raise sp.CalledProcessError(1, cmd[0])

        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
        return killed

    def test_pid_file_instance_is_stopped(self, home, monkeypatch):
        from main import _terminate_previous_instances
        (home / ".telechat.pid").write_text("4242\n")
        killed = self._patched(monkeypatch, cmdlines={"4242": "telechat start"})
        assert _terminate_previous_instances() == [4242]
        assert killed == [4242]

    def test_pid_file_pointing_at_a_recycled_pid_is_ignored(self, home, monkeypatch):
        # The PID was reused by an unrelated program after our crash.
        from main import _terminate_previous_instances
        (home / ".telechat.pid").write_text("4242\n")
        killed = self._patched(monkeypatch, cmdlines={"4242": "/usr/sbin/cupsd -l"})
        assert _terminate_previous_instances() == []
        assert killed == []

    def test_dead_pid_file_entry_is_ignored(self, home, monkeypatch):
        from main import _terminate_previous_instances
        (home / ".telechat.pid").write_text("4242\n")
        killed = self._patched(monkeypatch)  # `ps` finds nothing
        assert _terminate_previous_instances() == []
        assert killed == []

    def test_pgrep_fallback_finds_instances_without_a_pid_file(self, home, monkeypatch):
        from main import _terminate_previous_instances
        killed = self._patched(
            monkeypatch, pgrep="777\n",
            cmdlines={"777": "python3 -m telechat_pkg.main"},
        )
        assert _terminate_previous_instances() == [777]
        assert killed == [777]

    def test_a_pid_is_never_signalled_twice(self, home, monkeypatch):
        from main import _terminate_previous_instances
        (home / ".telechat.pid").write_text("777\n")
        killed = self._patched(
            monkeypatch, pgrep="777\n", lsof="777\n",
            cmdlines={"777": "python3 -m telechat_pkg.main"},
        )
        assert _terminate_previous_instances(health_port=8484) == [777]
        assert killed == [777]

    def test_health_port_held_by_another_service_is_left_alone(self, home, monkeypatch):
        from main import _terminate_previous_instances
        killed = self._patched(
            monkeypatch, lsof="555\n",
            cmdlines={"555": "node /usr/local/lib/some-dev-server.js"},
        )
        assert _terminate_previous_instances(health_port=8484) == []
        assert killed == []

    def test_health_port_held_by_telechat_is_reclaimed(self, home, monkeypatch):
        from main import _terminate_previous_instances
        killed = self._patched(
            monkeypatch, lsof="555\n", cmdlines={"555": "telechat start"},
        )
        assert _terminate_previous_instances(health_port=8484) == [555]
        assert killed == [555]

    def test_own_pid_is_never_signalled(self, home, monkeypatch):
        from main import _terminate_previous_instances
        killed = self._patched(
            monkeypatch, pgrep=f"{os.getpid()}\n",
            cmdlines={str(os.getpid()): "python3 -m telechat_pkg.main"},
        )
        assert _terminate_previous_instances() == []
        assert killed == []

    def test_non_numeric_pgrep_output_is_skipped(self, home, monkeypatch):
        from main import _terminate_previous_instances
        killed = self._patched(monkeypatch, pgrep="not-a-pid\n\n")
        assert _terminate_previous_instances() == []
        assert killed == []

    def test_kill_permission_error_is_survivable(self, home, monkeypatch):
        from main import _terminate_previous_instances
        self._patched(
            monkeypatch, pgrep="777\n",
            cmdlines={"777": "python3 -m telechat_pkg.main"},
        )

        def denied(pid, sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(os, "kill", denied)
        assert _terminate_previous_instances() == []
