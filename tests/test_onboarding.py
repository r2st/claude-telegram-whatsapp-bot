"""
Tests for the first-run onboarding tour.

What matters here: a first-time user is recognised exactly once, the tour
advances and terminates, and every step renders without a KeyError in the
middle of someone's first minute with the bot.

Run:
    pytest tests/test_onboarding.py -v
"""

import os
import threading

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import onboarding as ob


@pytest.fixture
def store(tmp_path):
    s = ob.OnboardingStore(str(tmp_path / "onboarding.db"))
    yield s
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# 1. The tour content
# ══════════════════════════════════════════════════════════════════════════════


class TestTourContent:
    def test_tour_is_not_empty(self):
        assert ob.TOTAL_STEPS >= 3
        assert len(ob.TOUR) == ob.TOTAL_STEPS

    def test_step_keys_are_unique(self):
        keys = [s.key for s in ob.TOUR]
        assert len(set(keys)) == len(keys)

    def test_every_step_has_a_title_and_body(self):
        for step in ob.TOUR:
            assert step.title.strip()
            assert step.body.strip()

    def test_examples_are_runnable_looking(self):
        for step in ob.TOUR:
            if step.example:
                assert "\n" not in step.example

    def test_step_lookup(self):
        assert ob.tour_step(0) is ob.TOUR[0]
        assert ob.tour_step(ob.TOTAL_STEPS - 1) is ob.TOUR[-1]

    def test_step_lookup_out_of_range(self):
        assert ob.tour_step(-1) is None
        assert ob.tour_step(ob.TOTAL_STEPS) is None
        assert ob.tour_step(999) is None

    def test_render_shows_position(self):
        text = ob.TOUR[0].render(0, ob.TOTAL_STEPS)
        assert f"(1/{ob.TOTAL_STEPS})" in text

    def test_render_includes_the_example(self):
        step = next(s for s in ob.TOUR if s.example)
        assert step.example in step.render(0, ob.TOTAL_STEPS)

    def test_step_text_for_every_index(self):
        for i in range(ob.TOTAL_STEPS):
            assert ob.step_text(i).strip()

    def test_step_text_past_the_end_is_the_closing_line(self):
        assert ob.step_text(ob.TOTAL_STEPS) == ob.finish_text()

    def test_progress_bar_fills_up(self):
        assert ob.progress_bar(0) == "●" + "○" * (ob.TOTAL_STEPS - 1)
        assert ob.progress_bar(ob.TOTAL_STEPS - 1) == "●" * ob.TOTAL_STEPS

    def test_progress_bar_is_always_the_right_length(self):
        for i in range(-2, ob.TOTAL_STEPS + 3):
            assert len(ob.progress_bar(i)) == ob.TOTAL_STEPS

    def test_the_tour_ends_by_pointing_at_help(self):
        assert "/help" in ob.finish_text()

    def test_sharing_is_part_of_the_tour(self):
        # The invite step is the growth loop; losing it silently would be bad.
        assert any(s.key == "share" for s in ob.TOUR)
        assert any("/invite" in s.body or "/invite" in s.example for s in ob.TOUR)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Welcome text
# ══════════════════════════════════════════════════════════════════════════════


class TestWelcome:
    def test_first_time_offers_the_tour(self):
        text = ob.welcome_text("Ada", first_time=True)
        assert "Ada" in text
        assert "tour" in text.lower()

    def test_first_time_without_a_name(self):
        assert ob.welcome_text("", first_time=True).strip()

    def test_inviter_is_credited(self):
        text = ob.welcome_text("Ada", first_time=True, invited_by="Grace")
        assert "Grace" in text

    def test_returning_user_gets_a_short_greeting(self):
        text = ob.welcome_text("Ada", first_time=False)
        assert len(text) < len(ob.welcome_text("Ada", first_time=True))
        assert "Welcome back" in text

    def test_returning_user_is_told_how_to_replay(self):
        assert "/tour" in ob.welcome_text("Ada", first_time=False)


class TestNamesCannotBreakTheMarkdownParse:
    """The welcome is sent with Markdown on, and both names come from the user.

    A handle like ``max_power`` carries an unbalanced ``_``; Telegram's legacy
    parser answers that with 400 and the caller has no plain-text fallback, so
    a brand-new user's first ``/start`` would return nothing at all.
    """

    @staticmethod
    def _balanced(text: str) -> bool:
        return all(text.count(c) % 2 == 0 for c in "_*`")

    def test_underscored_name_leaves_no_stray_delimiter(self):
        text = ob.welcome_text("max_power", first_time=True)
        assert "_" not in text
        assert "maxpower" in text

    def test_underscored_inviter_leaves_the_markup_balanced(self):
        text = ob.welcome_text("Ada", first_time=True, invited_by="max_power")
        assert self._balanced(text)
        assert "maxpower" in text

    def test_a_name_cannot_inject_markup(self):
        text = ob.welcome_text("*Ada*", first_time=True, invited_by="`Grace`")
        assert self._balanced(text)
        assert "Ada" in text and "Grace" in text

    def test_returning_greeting_is_sanitized_too(self):
        assert self._balanced(ob.welcome_text("max_power", first_time=False))

    def test_a_name_that_is_only_markup_degrades_to_no_name(self):
        # Must not leave a dangling "Hi  ." with a doubled space.
        text = ob.welcome_text("___", first_time=True)
        assert self._balanced(text)
        assert "Hi." in text

    def test_plain_name_is_idempotent(self):
        once = ob.plain_name("a_b*c")
        assert ob.plain_name(once) == once

    def test_plain_name_tolerates_none(self):
        assert ob.plain_name(None) == ""


class TestInvitePitch:
    def test_contains_the_link(self):
        text = ob.invite_pitch("https://t.me/mybot?start=inv_ABC", uses_left=1, expires_hours=24)
        assert "https://t.me/mybot?start=inv_ABC" in text

    def test_singular_and_plural_uses(self):
        assert "1 use." in ob.invite_pitch("L", uses_left=1)
        assert "5 uses" in ob.invite_pitch("L", uses_left=5)

    def test_unlimited(self):
        assert "unlimited" in ob.invite_pitch("L", uses_left=None)

    def test_hours_for_a_short_expiry(self):
        assert "12h" in ob.invite_pitch("L", uses_left=1, expires_hours=12)

    def test_days_for_a_long_expiry(self):
        assert "7d" in ob.invite_pitch("L", uses_left=1, expires_hours=168)

    def test_no_expiry_mentioned_when_there_is_none(self):
        text = ob.invite_pitch("L", uses_left=1)
        assert "expires" not in text

    def test_tells_the_inviter_how_to_undo_it(self):
        assert "/revoke" in ob.invite_pitch("L", uses_left=1)

    def test_a_realistic_bot_username_survives_intact(self):
        """Bot usernames must end in 'bot', so '..._bot' is the common shape.

        The pitch is sent as plain text precisely so these underscores stay
        literal. If markup ever creeps back in, the link is the thing that
        breaks — and it is the only part of the message that matters.
        """
        link = "https://t.me/telechat_demo_bot?start=inv_K7QW2M9XPT"
        text = ob.invite_pitch(link, uses_left=1, expires_hours=24)
        assert link in text

    def test_pitch_carries_no_markup_that_a_parser_could_trip_on(self):
        link = "https://t.me/telechat_demo_bot?start=inv_K7QW2M9XPT"
        text = ob.invite_pitch(link, uses_left=1, expires_hours=24)
        # Every underscore present belongs to the link itself, not to markup.
        assert "`" not in text and "*" not in text
        assert all(u in link for u in text.split() if "_" in u)


# ══════════════════════════════════════════════════════════════════════════════
# 3. First-run state
# ══════════════════════════════════════════════════════════════════════════════


class TestFirstRun:
    def test_unknown_user_is_new(self, store):
        assert store.is_new("telegram", "1")
        assert store.get("telegram", "1") is None

    def test_start_returns_true_only_once(self, store):
        assert store.start("telegram", "1") is True
        assert store.start("telegram", "1") is False

    def test_start_records_the_source(self, store):
        store.start("telegram", "1", source="invite")
        assert store.get("telegram", "1").source == "invite"

    def test_source_is_truncated(self, store):
        store.start("telegram", "1", source="x" * 200)
        assert len(store.get("telegram", "1").source) == 40

    def test_platforms_are_separate(self, store):
        store.start("telegram", "1")
        assert store.is_new("discord", "1")

    def test_user_id_type_does_not_matter(self, store):
        store.start("telegram", 1)
        assert not store.is_new("telegram", "1")

    def test_double_tap_on_start_welcomes_once(self, store):
        """A user tapping /start twice quickly must be first-time exactly once."""
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(store.start("telegram", "42"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for r in results if r) == 1


class TestTourProgress:
    def test_new_user_starts_at_zero(self, store):
        store.start("telegram", "1")
        assert store.get("telegram", "1").step == 0
        assert not store.get("telegram", "1").completed

    def test_set_step(self, store):
        store.set_step("telegram", "1", 2)
        assert store.get("telegram", "1").step == 2

    def test_set_step_on_an_unseen_user_creates_the_row(self, store):
        store.set_step("telegram", "9", 1)
        assert store.get("telegram", "9") is not None

    def test_step_is_clamped(self, store):
        assert store.set_step("telegram", "1", -5) == 0
        assert store.set_step("telegram", "1", 999) == ob.TOTAL_STEPS

    def test_reaching_the_end_marks_completion(self, store):
        store.set_step("telegram", "1", ob.TOTAL_STEPS)
        assert store.get("telegram", "1").completed

    def test_completion_is_not_undone_by_a_replay(self, store):
        store.complete("telegram", "1")
        store.set_step("telegram", "1", 0)
        assert store.get("telegram", "1").completed
        assert store.get("telegram", "1").step == 0

    def test_advance_walks_the_tour_and_stops(self, store):
        store.start("telegram", "1")
        seen = []
        for _ in range(ob.TOTAL_STEPS + 2):
            step = store.advance("telegram", "1")
            seen.append(step)
        assert seen[: ob.TOTAL_STEPS - 1] == list(ob.TOUR[1:])
        assert seen[-1] is None
        assert store.get("telegram", "1").completed

    def test_advance_from_nothing(self, store):
        assert store.advance("telegram", "unseen") is ob.TOUR[1]

    def test_reset_forgets_the_user(self, store):
        store.start("telegram", "1")
        store.complete("telegram", "1")
        store.reset("telegram", "1")
        assert store.is_new("telegram", "1")
        assert store.start("telegram", "1") is True

    def test_reset_of_an_unknown_user_is_harmless(self, store):
        store.reset("telegram", "nobody")


class TestFunnel:
    def test_counts_on_an_empty_store(self, store):
        assert store.counts("telegram") == {"started": 0, "completed": 0, "in_progress": 0}

    def test_counts_track_activation(self, store):
        store.start("telegram", "1")
        store.start("telegram", "2")
        store.start("telegram", "3")
        store.complete("telegram", "1")
        assert store.counts("telegram") == {"started": 3, "completed": 1, "in_progress": 2}

    def test_counts_are_per_platform(self, store):
        store.start("telegram", "1")
        store.start("discord", "2")
        assert store.counts("telegram")["started"] == 1
        assert store.counts("discord")["started"] == 1


class TestPersistence:
    def test_state_survives_a_new_store(self, tmp_path):
        path = str(tmp_path / "persist.db")
        a = ob.OnboardingStore(path)
        a.start("telegram", "1", source="invite")
        a.set_step("telegram", "1", 2)
        a.close()

        b = ob.OnboardingStore(path)
        assert not b.is_new("telegram", "1")
        assert b.get("telegram", "1").step == 2
        assert b.get("telegram", "1").source == "invite"
        b.close()

    def test_schema_init_is_idempotent(self, tmp_path):
        path = str(tmp_path / "twice.db")
        a, b = ob.OnboardingStore(path), ob.OnboardingStore(path)
        a.start("telegram", "1")
        assert not b.is_new("telegram", "1")
        a.close()
        b.close()

    def test_module_store_is_a_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ob, "_store", None)
        from telechat_pkg import store as store_mod
        monkeypatch.setattr(store_mod, "DB_PATH", str(tmp_path / "singleton.db"))
        assert ob.get_store() is ob.get_store()
        ob.reset_store()

    def test_is_first_run_helper_never_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("db is gone")
        monkeypatch.setattr(ob, "get_store", boom)
        assert ob.is_first_run("telegram", "1") is False
