"""
Tests for group-chat policy.

The behaviour under test is the one that decides whether a bot is tolerable
in a group: it must answer when addressed and stay quiet otherwise, and it
must not mistake a passing "@" for being addressed.

Run:
    pytest tests/test_group_policy.py -v
"""

import os
import threading

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import group_policy as gp


@pytest.fixture
def settings(tmp_path):
    s = gp.GroupSettings(str(tmp_path / "groups.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GROUP_DEFAULT_MODE", raising=False)
    gp._mention_cache.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Mode names
# ══════════════════════════════════════════════════════════════════════════════


class TestModes:
    @pytest.mark.parametrize("raw,expected", [
        ("mention", "mention"), ("MENTION", "mention"), (" mentions ", "mention"),
        ("mention-only", "mention"), ("@", "mention"),
        ("all", "all"), ("always", "all"), ("every", "all"), ("on", "all"),
        ("off", "off"), ("none", "off"), ("mute", "off"), ("quiet", "off"),
    ])
    def test_normalize_accepts_synonyms(self, raw, expected):
        assert gp.normalize_mode(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "loud", "sometimes", "1"])
    def test_normalize_rejects_nonsense(self, raw):
        assert gp.normalize_mode(raw) is None

    def test_default_is_mention(self):
        assert gp.default_mode() == "mention"

    def test_default_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("GROUP_DEFAULT_MODE", "all")
        assert gp.default_mode() == "all"

    def test_bad_default_falls_back(self, monkeypatch):
        monkeypatch.setenv("GROUP_DEFAULT_MODE", "banana")
        assert gp.default_mode() == "mention"

    def test_every_mode_is_documented(self):
        assert set(gp.MODE_HELP) == set(gp.MODES)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Addressing detection
# ══════════════════════════════════════════════════════════════════════════════


class TestIsAddressed:
    def test_plain_mention(self):
        assert gp.is_addressed("@mybot hello", bot_username="mybot")

    def test_mention_is_case_insensitive(self):
        assert gp.is_addressed("@MyBot hello", bot_username="mybot")
        assert gp.is_addressed("@mybot hello", bot_username="MyBot")

    def test_mention_mid_sentence(self):
        assert gp.is_addressed("hey @mybot what do you think", bot_username="mybot")

    def test_no_mention(self):
        assert not gp.is_addressed("just chatting", bot_username="mybot")

    def test_another_bot_is_not_us(self):
        assert not gp.is_addressed("@otherbot hello", bot_username="mybot")

    def test_longer_handle_starting_with_ours(self):
        # @mybot2 is a different account and must not match.
        assert not gp.is_addressed("@mybot2 hello", bot_username="mybot")

    def test_email_address_is_not_a_mention(self):
        assert not gp.is_addressed("write to ada@mybot.com", bot_username="mybot")

    def test_reply_to_the_bot_counts(self):
        assert gp.is_addressed("what about this", bot_username="mybot", is_reply_to_bot=True)

    def test_reply_wins_even_with_no_username(self):
        assert gp.is_addressed("hi", is_reply_to_bot=True)

    def test_slack_style_mention(self):
        assert gp.is_addressed("<@U123> hello", bot_user_id="U123")

    def test_slack_mention_with_label(self):
        assert gp.is_addressed("<@U123|mybot> hello", bot_user_id="U123")

    def test_discord_style_mention(self):
        assert gp.is_addressed("<@!456> hello", bot_user_id="456")

    def test_someone_elses_platform_mention(self):
        assert not gp.is_addressed("<@U999> hello", bot_user_id="U123")

    def test_mention_ids_list(self):
        assert gp.is_addressed("hello", bot_user_id="U123", mention_ids=["U9", "U123"])

    def test_mention_ids_without_a_match(self):
        assert not gp.is_addressed("hello", bot_user_id="U123", mention_ids=["U9"])

    def test_bare_command_counts(self):
        assert gp.is_addressed("/help", bot_username="mybot")

    def test_command_aimed_at_us(self):
        assert gp.is_addressed("/help@mybot", bot_username="mybot")

    def test_command_aimed_at_another_bot(self):
        assert not gp.is_addressed("/help@otherbot", bot_username="mybot")

    def test_command_when_we_do_not_know_our_handle(self):
        assert gp.is_addressed("/help@whoever")

    def test_slash_mid_sentence_is_not_a_command(self):
        assert not gp.is_addressed("use the and/or operator", bot_username="mybot")

    def test_empty_text(self):
        assert not gp.is_addressed("", bot_username="mybot")
        assert not gp.is_addressed(None, bot_username="mybot")


class TestStripAddressing:
    def test_leading_mention_removed(self):
        assert gp.strip_addressing("@mybot summarise this", bot_username="mybot") == "summarise this"

    def test_mid_sentence_mention_removed(self):
        out = gp.strip_addressing("hey @mybot look at this", bot_username="mybot")
        assert out == "hey look at this"

    def test_other_mentions_are_kept(self):
        out = gp.strip_addressing("@mybot ask @ada about it", bot_username="mybot")
        assert out == "ask @ada about it"

    def test_platform_mention_removed(self):
        assert gp.strip_addressing("<@U123> hi", bot_user_id="U123") == "hi"

    def test_other_platform_mention_kept(self):
        assert gp.strip_addressing("<@U999> hi", bot_user_id="U123") == "<@U999> hi"

    def test_command_suffix_removed(self):
        assert gp.strip_addressing("/help@mybot", bot_username="mybot") == "/help"

    def test_whitespace_is_collapsed(self):
        assert gp.strip_addressing("@mybot    lots   of  space", bot_username="mybot") == "lots of space"

    def test_only_a_mention_leaves_nothing(self):
        assert gp.strip_addressing("@mybot", bot_username="mybot") == ""

    def test_untouched_without_identity(self):
        assert gp.strip_addressing("plain text") == "plain text"

    def test_none_text(self):
        assert gp.strip_addressing(None, bot_username="mybot") == ""


class TestMentionPatternCache:
    def test_pattern_is_reused(self):
        assert gp.mention_pattern("mybot") is gp.mention_pattern("mybot")

    def test_cache_is_case_insensitive_on_the_key(self):
        assert gp.mention_pattern("MyBot") is gp.mention_pattern("mybot")

    def test_cache_is_bounded(self):
        for i in range(gp._MENTION_CACHE_MAX + 20):
            gp.mention_pattern(f"bot{i}")
        assert len(gp._mention_cache) <= gp._MENTION_CACHE_MAX

    def test_regex_metacharacters_in_a_handle_are_escaped(self):
        # Telegram handles cannot contain these, but a Slack/Discord caller
        # passing a display name must not be able to inject a pattern.
        pat = gp.mention_pattern("a.b*c")
        assert pat.search("@a.b*c hi")
        assert not pat.search("@axbxc hi")

    def test_concurrent_use_does_not_blow_up(self):
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    gp.mention_pattern(f"bot{(n * 50 + i) % 90}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ══════════════════════════════════════════════════════════════════════════════
# 3. The decision
# ══════════════════════════════════════════════════════════════════════════════


class TestDecide:
    def test_direct_message_always_answered(self):
        d = gp.decide(text="hello", is_direct=True, mode="off", bot_username="mybot")
        assert d.respond
        assert d.reason == "direct"

    def test_direct_message_still_strips_the_mention(self):
        d = gp.decide(text="@mybot hello", is_direct=True, bot_username="mybot")
        assert d.text == "hello"

    def test_group_mention_mode_answers_when_addressed(self):
        d = gp.decide(text="@mybot hello", is_direct=False, mode="mention", bot_username="mybot")
        assert d.respond
        assert d.reason == "addressed"
        assert d.text == "hello"

    def test_group_mention_mode_stays_quiet_otherwise(self):
        d = gp.decide(text="people talking", is_direct=False, mode="mention",
                      bot_username="mybot")
        assert not d.respond
        assert d.reason == "not_addressed"

    def test_group_all_mode_answers_everything(self):
        d = gp.decide(text="people talking", is_direct=False, mode="all", bot_username="mybot")
        assert d.respond
        assert d.reason == "mode_all"

    def test_group_off_mode_stays_quiet_even_when_mentioned(self):
        d = gp.decide(text="@mybot hello", is_direct=False, mode="off", bot_username="mybot")
        assert not d.respond
        assert d.reason == "mode_off"
        # …but it still records that it was spoken to, so a caller can decide
        # whether an explicit command deserves an answer.
        assert d.addressed

    def test_bare_mention_has_nothing_to_run(self):
        d = gp.decide(text="@mybot", is_direct=False, mode="mention", bot_username="mybot")
        assert not d.respond
        assert d.reason == "empty"
        assert d.addressed

    def test_whitespace_in_all_mode_is_not_a_prompt(self):
        d = gp.decide(text="   ", is_direct=False, mode="all", bot_username="mybot")
        assert not d.respond
        assert d.reason == "empty"

    def test_reply_to_bot_is_enough_in_mention_mode(self):
        d = gp.decide(text="and the other one?", is_direct=False, mode="mention",
                      bot_username="mybot", is_reply_to_bot=True)
        assert d.respond

    def test_unknown_mode_falls_back_to_the_default(self):
        d = gp.decide(text="chatter", is_direct=False, mode="banana", bot_username="mybot")
        assert not d.respond

    def test_missing_mode_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("GROUP_DEFAULT_MODE", "all")
        d = gp.decide(text="chatter", is_direct=False, mode=None, bot_username="mybot")
        assert d.respond

    def test_platform_mention_in_a_group(self):
        d = gp.decide(text="<@U123> deploy it", is_direct=False, mode="mention",
                      bot_user_id="U123")
        assert d.respond
        assert d.text == "deploy it"

    def test_unknown_handle_in_mention_mode_stays_quiet(self):
        # With no identity the bot cannot tell it was addressed, and guessing
        # yes would mean answering every message in the room.
        d = gp.decide(text="hello there", is_direct=False, mode="mention")
        assert not d.respond


# ══════════════════════════════════════════════════════════════════════════════
# 4. Persisted per-chat settings
# ══════════════════════════════════════════════════════════════════════════════


class TestGroupSettings:
    def test_unset_chat_uses_the_default(self, settings):
        assert settings.get_mode("telegram", "-100") == "mention"

    def test_set_and_read_back(self, settings):
        assert settings.set_mode("telegram", "-100", "all") == "all"
        assert settings.get_mode("telegram", "-100") == "all"

    def test_set_normalizes(self, settings):
        assert settings.set_mode("telegram", "-100", "ALWAYS") == "all"

    def test_set_rejects_nonsense(self, settings):
        with pytest.raises(ValueError):
            settings.set_mode("telegram", "-100", "banana")

    def test_chats_are_independent(self, settings):
        settings.set_mode("telegram", "-100", "all")
        settings.set_mode("telegram", "-200", "off")
        assert settings.get_mode("telegram", "-100") == "all"
        assert settings.get_mode("telegram", "-200") == "off"

    def test_platforms_are_independent(self, settings):
        settings.set_mode("telegram", "X", "all")
        settings.set_mode("discord", "X", "off")
        assert settings.get_mode("telegram", "X") == "all"
        assert settings.get_mode("discord", "X") == "off"

    def test_chat_id_type_does_not_matter(self, settings):
        settings.set_mode("telegram", -100, "all")
        assert settings.get_mode("telegram", "-100") == "all"

    def test_update_overwrites(self, settings):
        settings.set_mode("telegram", "-100", "all")
        settings.set_mode("telegram", "-100", "off")
        assert settings.get_mode("telegram", "-100") == "off"

    def test_title_is_kept_when_a_later_update_omits_it(self, settings):
        settings.set_mode("telegram", "-100", "all", title="Team Room")
        settings.set_mode("telegram", "-100", "off")
        rows = settings.all_for("telegram")
        assert rows[0].title == "Team Room"

    def test_clear_restores_the_default(self, settings):
        settings.set_mode("telegram", "-100", "all")
        assert settings.clear("telegram", "-100")
        assert settings.get_mode("telegram", "-100") == "mention"

    def test_clear_unknown_chat(self, settings):
        assert not settings.clear("telegram", "-999")

    def test_all_for_lists_configured_chats(self, settings):
        settings.set_mode("telegram", "-100", "all", title="A")
        settings.set_mode("telegram", "-200", "off")
        rows = settings.all_for("telegram")
        assert {r.chat_id for r in rows} == {"-100", "-200"}
        assert settings.all_for("slack") == []

    def test_setting_survives_a_new_store(self, tmp_path):
        path = str(tmp_path / "persist.db")
        a = gp.GroupSettings(path)
        a.set_mode("telegram", "-100", "all")
        a.close()
        b = gp.GroupSettings(path)
        assert b.get_mode("telegram", "-100") == "all"
        b.close()

    def test_cache_is_used_and_invalidatable(self, settings):
        settings.set_mode("telegram", "-100", "all")
        assert settings.get_mode("telegram", "-100") == "all"
        # Write behind the store's back, as another process would.
        settings._conn().execute(
            "UPDATE group_settings SET mode = 'off' WHERE chat_id = '-100'"
        )
        settings._conn().commit()
        assert settings.get_mode("telegram", "-100") == "all"   # cached
        settings.invalidate()
        assert settings.get_mode("telegram", "-100") == "off"

    def test_mode_of_helper_survives_a_broken_store(self, monkeypatch):
        def boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(gp, "get_settings", boom)
        assert gp.mode_of("telegram", "-100") == "mention"

    def test_module_settings_is_a_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gp, "_settings", None)
        from telechat_pkg import store as store_mod
        monkeypatch.setattr(store_mod, "DB_PATH", str(tmp_path / "singleton.db"))
        assert gp.get_settings() is gp.get_settings()
        gp.reset_settings()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Presentation
# ══════════════════════════════════════════════════════════════════════════════


class TestPresentation:
    def test_describe_each_mode(self):
        for mode in gp.MODES:
            assert gp.MODE_HELP[mode] in gp.describe(mode)

    def test_describe_names_the_chat(self):
        assert "Team Room" in gp.describe("all", chat_title="Team Room")

    def test_describe_handles_a_bad_mode(self):
        assert gp.describe("banana")

    def test_mode_options_marks_the_current(self):
        opts = gp.mode_options("all")
        current = [label for mode, label in opts if mode == "all"][0]
        assert current.endswith("✓")
        assert sum(1 for _, label in opts if label.endswith("✓")) == 1

    def test_mode_options_covers_every_mode(self):
        assert {m for m, _ in gp.mode_options("mention")} == set(gp.MODES)

    def test_summarize_empty(self):
        assert "default" in gp.summarize_settings([])

    def test_summarize_lists_chats(self, settings):
        settings.set_mode("telegram", "-100", "all", title="Team")
        text = gp.summarize_settings(settings.all_for("telegram"))
        assert "-100" in text and "all" in text and "Team" in text
