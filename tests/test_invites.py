"""
Tests for invite codes, access grants, and referral attribution.

The invariants that matter: a single-use code admits exactly one person even
under concurrency, a code cannot be stretched past its limits, and a grant
survives a restart because it lives in the database rather than in .env.

Run:
    pytest tests/test_invites.py -v
"""

import os
import threading
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import invites
from telechat_pkg.invites import Invite, InviteStore


@pytest.fixture
def store(tmp_path):
    s = InviteStore(str(tmp_path / "invites.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_chaining(monkeypatch):
    monkeypatch.delenv("INVITE_ALLOW_CHAINING", raising=False)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Code format
# ══════════════════════════════════════════════════════════════════════════════


class TestCodeFormat:
    def test_generated_code_has_expected_shape(self):
        code = invites.generate_code()
        assert len(code) == invites.CODE_LENGTH
        assert set(code) <= set(invites.CODE_ALPHABET)

    def test_codes_are_not_repeated(self):
        codes = {invites.generate_code() for _ in range(500)}
        assert len(codes) == 500

    def test_alphabet_excludes_confusable_characters(self):
        for ch in "IO01":
            assert ch not in invites.CODE_ALPHABET

    def test_alphabet_is_deep_link_safe(self):
        # Telegram's start payload allows A-Z a-z 0-9 _ and - only.
        assert all(c.isalnum() and c.isascii() for c in invites.CODE_ALPHABET)

    @pytest.mark.parametrize("raw", [
        "ABCDEFGHJK",
        "abcdefghjk",
        "  ABCDEFGHJK  ",
        "ABCDE-FGHJK",
        "inv_ABCDEFGHJK",
        "INV_abcdefghjk",
    ])
    def test_normalize_accepts_the_forms_people_send(self, raw):
        assert invites.normalize_code(raw) == "ABCDEFGHJK"

    @pytest.mark.parametrize("raw", ["", None, "short", "TOOLONGACODE12", "IIIIIIIIII", "!!!!!!!!!!"])
    def test_normalize_rejects_non_codes(self, raw):
        assert invites.normalize_code(raw) == ""

    def test_deep_link_shape(self):
        link = invites.deep_link("@mybot", "ABCDEFGHJK")
        assert link == "https://t.me/mybot?start=inv_ABCDEFGHJK"

    def test_deep_link_round_trips_through_normalize(self):
        code = invites.generate_code()
        payload = invites.deep_link("mybot", code).split("start=")[1]
        assert invites.normalize_code(payload) == code


# ══════════════════════════════════════════════════════════════════════════════
# 2. Creating
# ══════════════════════════════════════════════════════════════════════════════


class TestCreate:
    def test_defaults(self, store):
        inv = store.create("telegram", "1")
        assert inv.max_uses == invites.DEFAULT_MAX_USES
        assert inv.uses == 0
        assert not inv.revoked
        assert inv.expires_at is not None
        assert inv.is_usable()

    def test_unlimited_uses(self, store):
        inv = store.create("telegram", "1", max_uses=0)
        assert inv.unlimited
        assert inv.remaining() is None
        assert not inv.is_exhausted()

    def test_never_expires(self, store):
        inv = store.create("telegram", "1", ttl_days=None)
        assert inv.expires_at is None
        assert not inv.is_expired()

    def test_ttl_is_capped(self, store):
        inv = store.create("telegram", "1", ttl_days=99_999)
        assert inv.expires_at <= time.time() + invites.MAX_TTL_DAYS * 86400 + 5

    def test_uses_are_capped(self, store):
        inv = store.create("telegram", "1", max_uses=10 ** 9)
        assert inv.max_uses == invites.MAX_USES_CEILING

    def test_negative_uses_become_unlimited_floor(self, store):
        # Clamped to 0, which is the documented "unlimited" value.
        inv = store.create("telegram", "1", max_uses=-5)
        assert inv.max_uses == 0

    def test_note_is_truncated(self, store):
        inv = store.create("telegram", "1", note="x" * 500)
        assert len(inv.note) == 200

    def test_created_invite_is_readable_back(self, store):
        inv = store.create("telegram", "1", note="for ada")
        again = store.get(inv.code)
        assert again is not None
        assert again.code == inv.code
        assert again.note == "for ada"
        assert again.created_by == "1"

    def test_get_normalizes_input(self, store):
        inv = store.create("telegram", "1")
        assert store.get(inv.code.lower()) is not None
        assert store.get(f"inv_{inv.code}") is not None

    def test_get_unknown_returns_none(self, store):
        assert store.get("ZZZZZZZZZZ") is None
        assert store.get("garbage") is None

    def test_list_created_by_is_newest_first(self, store):
        first = store.create("telegram", "1")
        time.sleep(0.01)
        second = store.create("telegram", "1")
        store.create("telegram", "2")
        mine = store.list_created_by("telegram", "1")
        assert [i.code for i in mine] == [second.code, first.code]


# ══════════════════════════════════════════════════════════════════════════════
# 3. Redeeming
# ══════════════════════════════════════════════════════════════════════════════


class TestRedeem:
    def test_happy_path_grants_access(self, store):
        inv = store.create("telegram", "1")
        assert not store.is_granted("telegram", "2")
        result = store.redeem(inv.code, "telegram", "2", display_name="Ada")
        assert result.ok
        assert result.reason == "ok"
        assert store.is_granted("telegram", "2")
        assert result.grant.invited_by == "1"
        assert result.grant.display_name == "Ada"

    def test_use_counter_increments(self, store):
        inv = store.create("telegram", "1", max_uses=3)
        store.redeem(inv.code, "telegram", "2")
        store.redeem(inv.code, "telegram", "3")
        assert store.get(inv.code).uses == 2

    def test_single_use_code_admits_one_person(self, store):
        inv = store.create("telegram", "1", max_uses=1)
        assert store.redeem(inv.code, "telegram", "2").ok
        second = store.redeem(inv.code, "telegram", "3")
        assert not second.ok
        assert second.reason == "exhausted"
        assert not store.is_granted("telegram", "3")

    def test_unlimited_code_keeps_admitting(self, store):
        inv = store.create("telegram", "1", max_uses=0)
        for uid in range(2, 12):
            assert store.redeem(inv.code, "telegram", str(uid)).ok
        assert store.get(inv.code).uses == 10

    def test_unknown_code(self, store):
        result = store.redeem("ZZZZZZZZZZ", "telegram", "2")
        assert not result.ok
        assert result.reason == "unknown"

    def test_malformed_code(self, store):
        assert store.redeem("nope", "telegram", "2").reason == "unknown"
        assert store.redeem("", "telegram", "2").reason == "unknown"

    def test_revoked_code(self, store):
        inv = store.create("telegram", "1")
        store.revoke(inv.code)
        result = store.redeem(inv.code, "telegram", "2")
        assert not result.ok
        assert result.reason == "revoked"
        assert not store.is_granted("telegram", "2")

    def test_expired_code(self, store):
        inv = store.create("telegram", "1", ttl_days=0)
        time.sleep(0.01)
        result = store.redeem(inv.code, "telegram", "2")
        assert not result.ok
        assert result.reason == "expired"

    def test_cannot_redeem_own_code(self, store):
        inv = store.create("telegram", "1")
        result = store.redeem(inv.code, "telegram", "1")
        assert not result.ok
        assert result.reason == "self"
        assert store.get(inv.code).uses == 0

    def test_already_granted_does_not_burn_a_use(self, store):
        first = store.create("telegram", "1", max_uses=5)
        second = store.create("telegram", "1", max_uses=5)
        store.redeem(first.code, "telegram", "2")
        result = store.redeem(second.code, "telegram", "2")
        assert not result.ok
        assert result.reason == "already"
        assert store.get(second.code).uses == 0
        # …and they keep the access they already had.
        assert store.is_granted("telegram", "2")

    def test_code_is_scoped_to_its_platform(self, store):
        inv = store.create("telegram", "1")
        result = store.redeem(inv.code, "slack", "2")
        assert not result.ok
        # Reported as unknown rather than confirming it exists elsewhere.
        assert result.reason == "unknown"
        assert not store.is_granted("slack", "2")

    def test_messages_are_present_for_every_reason(self):
        from telechat_pkg.invites import RedeemResult
        for reason in RedeemResult.MESSAGES:
            assert RedeemResult(False, reason).message

    def test_unknown_reason_has_a_fallback_message(self):
        from telechat_pkg.invites import RedeemResult
        assert RedeemResult(False, "weird").message


class TestRedeemConcurrency:
    def test_single_use_code_under_a_thundering_herd(self, tmp_path):
        """Twenty threads, one use. Exactly one must get in."""
        path = str(tmp_path / "race.db")
        seed = InviteStore(path)
        inv = seed.create("telegram", "1", max_uses=1)
        seed.close()

        results = []
        barrier = threading.Barrier(20)

        def worker(n):
            s = InviteStore(path)
            barrier.wait()
            results.append(s.redeem(inv.code, "telegram", str(100 + n)))
            s.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r.ok) == 1
        check = InviteStore(path)
        assert check.get(inv.code).uses == 1
        assert len([u for u in check.granted_users("telegram")]) == 1
        check.close()

    def test_limited_code_never_overshoots(self, tmp_path):
        path = str(tmp_path / "race2.db")
        seed = InviteStore(path)
        inv = seed.create("telegram", "1", max_uses=5)
        seed.close()

        results = []
        barrier = threading.Barrier(20)

        def worker(n):
            s = InviteStore(path)
            barrier.wait()
            results.append(s.redeem(inv.code, "telegram", str(200 + n)))
            s.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r.ok) == 5
        check = InviteStore(path)
        assert check.get(inv.code).uses == 5
        check.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Revoking
# ══════════════════════════════════════════════════════════════════════════════


class TestRevoke:
    def test_creator_can_revoke(self, store):
        inv = store.create("telegram", "1")
        assert store.revoke(inv.code, requester="1")
        assert store.get(inv.code).revoked

    def test_someone_else_cannot_revoke(self, store):
        inv = store.create("telegram", "1")
        assert not store.revoke(inv.code, requester="2")
        assert not store.get(inv.code).revoked

    def test_operator_revoke_without_requester(self, store):
        inv = store.create("telegram", "1")
        assert store.revoke(inv.code)
        assert store.get(inv.code).revoked

    def test_revoke_unknown_code(self, store):
        assert not store.revoke("ZZZZZZZZZZ")
        assert not store.revoke("nonsense")

    def test_revoking_a_link_does_not_evict_existing_users(self, store):
        inv = store.create("telegram", "1", max_uses=5)
        store.redeem(inv.code, "telegram", "2")
        store.revoke(inv.code)
        assert store.is_granted("telegram", "2")

    def test_revoke_access_removes_a_user(self, store):
        inv = store.create("telegram", "1")
        store.redeem(inv.code, "telegram", "2")
        assert store.revoke_access("telegram", "2")
        assert not store.is_granted("telegram", "2")

    def test_revoke_access_for_a_stranger_is_false(self, store):
        assert not store.revoke_access("telegram", "999")

    def test_revoke_access_leaves_their_invitees_alone(self, store):
        a = store.create("telegram", "1")
        store.redeem(a.code, "telegram", "2")
        b = store.create("telegram", "2")
        store.redeem(b.code, "telegram", "3")
        store.revoke_access("telegram", "2")
        assert store.is_granted("telegram", "3")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Grants and permissions
# ══════════════════════════════════════════════════════════════════════════════


class TestGrants:
    def test_direct_grant_without_a_code(self, store):
        grant = store.grant_direct("telegram", "7", invited_by="1", display_name="Bob")
        assert grant.code is None
        assert store.is_granted("telegram", "7")
        assert store.get_grant("telegram", "7").display_name == "Bob"

    def test_direct_grant_is_idempotent(self, store):
        store.grant_direct("telegram", "7")
        store.grant_direct("telegram", "7")
        assert store.granted_users("telegram") == ["7"]

    def test_get_grant_for_stranger_is_none(self, store):
        assert store.get_grant("telegram", "404") is None

    def test_granted_users_is_per_platform(self, store):
        store.grant_direct("telegram", "1")
        store.grant_direct("slack", "U1")
        assert store.granted_users("telegram") == ["1"]
        assert store.granted_users("slack") == ["U1"]

    def test_operator_can_always_invite(self, store):
        assert store.can_invite("telegram", "1", is_operator=True)

    def test_invited_user_cannot_invite_by_default(self, store):
        store.grant_direct("telegram", "2")
        assert not store.can_invite("telegram", "2", is_operator=False)

    def test_stranger_cannot_invite(self, store):
        assert not store.can_invite("telegram", "999", is_operator=False)

    def test_chaining_env_opens_it_up(self, store, monkeypatch):
        store.grant_direct("telegram", "2")
        monkeypatch.setenv("INVITE_ALLOW_CHAINING", "true")
        assert store.can_invite("telegram", "2", is_operator=False)
        # …but still not for someone with no access at all.
        assert not store.can_invite("telegram", "999", is_operator=False)

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("maybe", False),
    ])
    def test_chaining_flag_parsing(self, monkeypatch, value, expected):
        monkeypatch.setenv("INVITE_ALLOW_CHAINING", value)
        assert invites._chaining_allowed() is expected


# ══════════════════════════════════════════════════════════════════════════════
# 6. Referral graph
# ══════════════════════════════════════════════════════════════════════════════


class TestReferralGraph:
    def _chain(self, store):
        """1 invites 2 and 3; 2 invites 4; 4 invites 5."""
        for inviter, invitee in [("1", "2"), ("1", "3"), ("2", "4"), ("4", "5")]:
            inv = store.create("telegram", inviter)
            assert store.redeem(inv.code, "telegram", invitee).ok

    def test_direct_invitees(self, store):
        self._chain(store)
        assert {g.user_id for g in store.invitees("telegram", "1")} == {"2", "3"}
        assert {g.user_id for g in store.invitees("telegram", "2")} == {"4"}
        assert store.invitees("telegram", "5") == []

    def test_network_counts_the_whole_downstream(self, store):
        self._chain(store)
        assert store.network_size("telegram", "1") == 4    # 2, 3, 4, 5
        assert store.network_size("telegram", "2") == 2    # 4, 5
        assert store.network_size("telegram", "5") == 0

    def test_network_respects_the_depth_cap(self, store):
        self._chain(store)
        assert store.network_size("telegram", "1", max_depth=1) == 2

    def test_network_terminates_on_a_self_referencing_row(self, store):
        # A corrupt row should not hang the bot.
        store.grant_direct("telegram", "9", invited_by="9")
        assert store.network_size("telegram", "9") == 0

    def test_stats(self, store):
        self._chain(store)
        stats = store.stats("telegram", "1")
        assert stats.created == 2
        assert stats.redemptions == 2
        assert stats.direct == 2
        assert stats.network == 4

    def test_stats_active_excludes_spent_codes(self, store):
        spent = store.create("telegram", "1", max_uses=1)
        store.redeem(spent.code, "telegram", "2")
        store.create("telegram", "1", max_uses=1)
        stats = store.stats("telegram", "1")
        assert stats.created == 2
        assert stats.active == 1

    def test_stats_for_an_untouched_user(self, store):
        stats = store.stats("telegram", "42")
        assert (stats.created, stats.direct, stats.network) == (0, 0, 0)

    def test_leaderboard_ranks_by_invites(self, store):
        self._chain(store)
        board = store.leaderboard("telegram")
        assert board[0] == ("1", 2)
        assert ("2", 1) in board
        assert ("4", 1) in board

    def test_leaderboard_ignores_direct_grants(self, store):
        store.grant_direct("telegram", "8")     # invited_by is NULL
        assert store.leaderboard("telegram") == []


# ══════════════════════════════════════════════════════════════════════════════
# 7. Housekeeping and persistence
# ══════════════════════════════════════════════════════════════════════════════


class TestHousekeeping:
    def test_purge_removes_long_expired_invites(self, store):
        old = store.create("telegram", "1", ttl_days=1)
        # Backdate it well past the purge window.
        store._conn().execute(
            "UPDATE invites SET expires_at = ? WHERE code = ?",
            (time.time() - 60 * 86400, old.code),
        )
        fresh = store.create("telegram", "1", ttl_days=30)
        assert store.purge_expired(older_than_days=30) == 1
        assert store.get(old.code) is None
        assert store.get(fresh.code) is not None

    def test_purge_never_touches_grants(self, store):
        inv = store.create("telegram", "1", ttl_days=1)
        store.redeem(inv.code, "telegram", "2")
        store._conn().execute(
            "UPDATE invites SET expires_at = ? WHERE code = ?",
            (time.time() - 60 * 86400, inv.code),
        )
        store.purge_expired(older_than_days=1)
        assert store.is_granted("telegram", "2")

    def test_purge_leaves_never_expiring_invites(self, store):
        inv = store.create("telegram", "1", ttl_days=None)
        assert store.purge_expired(older_than_days=0) == 0
        assert store.get(inv.code) is not None

    def test_grant_survives_a_new_store_over_the_same_file(self, tmp_path):
        path = str(tmp_path / "persist.db")
        first = InviteStore(path)
        inv = first.create("telegram", "1")
        first.redeem(inv.code, "telegram", "2")
        first.close()

        second = InviteStore(path)
        assert second.is_granted("telegram", "2")
        assert second.get(inv.code).uses == 1
        second.close()

    def test_schema_init_is_idempotent(self, tmp_path):
        path = str(tmp_path / "twice.db")
        a, b = InviteStore(path), InviteStore(path)
        inv = a.create("telegram", "1")
        assert b.get(inv.code) is not None
        a.close()
        b.close()

    def test_module_store_is_a_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setattr(invites, "_store", None)
        from telechat_pkg import store as store_mod
        monkeypatch.setattr(store_mod, "DB_PATH", str(tmp_path / "singleton.db"))
        assert invites.get_store() is invites.get_store()
        invites.reset_store()

    def test_is_granted_helper_never_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("db is gone")
        monkeypatch.setattr(invites, "get_store", boom)
        assert invites.is_granted("telegram", "1") is False


# ══════════════════════════════════════════════════════════════════════════════
# 8. Presentation helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatting:
    def test_status_words(self):
        now = time.time()
        assert Invite("C", "telegram", "1", now, revoked=True).status(now) == "revoked"
        assert Invite("C", "telegram", "1", now, expires_at=now - 1).status(now) == "expired"
        assert Invite("C", "telegram", "1", now, max_uses=1, uses=1).status(now) == "used"
        assert Invite("C", "telegram", "1", now, max_uses=2, uses=1).status(now) == "active"

    def test_remaining(self):
        now = time.time()
        assert Invite("C", "telegram", "1", now, max_uses=3, uses=1).remaining() == 2
        assert Invite("C", "telegram", "1", now, max_uses=0).remaining() is None
        # Never negative, even if the counter somehow overshot.
        assert Invite("C", "telegram", "1", now, max_uses=1, uses=4).remaining() == 0

    def test_format_line_shows_code_and_usage(self, store):
        inv = store.create("telegram", "1", max_uses=3, note="team")
        line = invites.format_invite_line(inv)
        assert inv.code in line
        assert "0/3" in line
        assert "team" in line

    def test_format_line_marks_a_dead_invite(self, store):
        inv = store.create("telegram", "1")
        store.revoke(inv.code)
        assert "revoked" in invites.format_invite_line(store.get(inv.code))

    def test_format_line_shows_days_for_a_long_ttl(self, store):
        inv = store.create("telegram", "1", ttl_days=30)
        assert "d left" in invites.format_invite_line(inv)

    def test_format_line_shows_hours_for_a_short_ttl(self, store):
        inv = store.create("telegram", "1", ttl_days=1)
        assert "h left" in invites.format_invite_line(inv)

    def test_summarize_mentions_the_numbers(self, store):
        inv = store.create("telegram", "1")
        store.redeem(inv.code, "telegram", "2", display_name="Ada")
        text = invites.summarize(store.stats("telegram", "1"))
        assert "Ada" in text
        assert "1" in text

    def test_summarize_for_an_empty_record(self, store):
        text = invites.summarize(store.stats("telegram", "1"))
        assert "0" in text
