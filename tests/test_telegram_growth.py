"""
End-to-end tests for the Telegram wiring of invites and onboarding.

The unit tests for the two modules live next door; this file covers the part
that actually changes user-visible behaviour — that a deep link admits
someone, that the welcome only fires once, and that only an operator can
mint an invite.

Run:
    pytest tests/test_telegram_growth.py -v
"""

import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Isolation: set env vars BEFORE importing the module under test ───────────

_tmp_dir = tempfile.mkdtemp()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "growth.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir
os.environ["RATE_LIMIT_REQUESTS"] = "1000"
os.environ["RATE_LIMIT_WINDOW"] = "60"

import telechat_pkg.claude_core as cc
from telechat_pkg import invites
from telechat_pkg import onboarding as ob
from telechat_pkg import telegram_bot as tb

cc.init_db()


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_update(
    uid: int = 12345,
    text: str = "hello",
    chat_id: int = 99,
    chat_type: str = "private",
    first_name: str = "Ada",
    reply_from_bot: bool = False,
    chat_title: str = "",
):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = uid
    update.effective_user.first_name = first_name
    update.effective_user.username = "ada"

    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_chat.title = chat_title

    update.message = AsyncMock()
    update.message.message_id = int(time.time() * 1_000_000) % 2_000_000_000
    update.message.text = text
    update.message.caption = None
    update.message.reply_text = AsyncMock(return_value=MagicMock())

    if reply_from_bot:
        reply = MagicMock()
        reply.from_user = MagicMock()
        reply.from_user.is_bot = True
        reply.from_user.id = 777
        update.message.reply_to_message = reply
    else:
        update.message.reply_to_message = None

    update.effective_message = update.message
    update.callback_query = None
    return update


def _make_ctx(args=None, username="mybot", bot_id=777):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = AsyncMock()
    ctx.bot.username = username
    ctx.bot.id = bot_id
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _make_callback_update(uid: int, data: str, chat_id: int = 99, chat_type="private"):
    update = _make_update(uid=uid, chat_id=chat_id, chat_type=chat_type)
    q = AsyncMock()
    q.from_user = MagicMock()
    q.from_user.id = uid
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.chat = MagicMock()
    q.message.chat.id = chat_id
    q.message.chat.title = "Team Room"
    update.callback_query = q
    return update


def _replies(update):
    return [c.args[0] if c.args else c.kwargs.get("text", "")
            for c in update.message.reply_text.call_args_list]


def _last_reply(update):
    return _replies(update)[-1] if _replies(update) else ""


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Point every store at a fresh database and reset module globals."""
    db = str(tmp_path / "growth.db")
    from telechat_pkg import store as store_mod
    monkeypatch.setattr(store_mod, "DB_PATH", db)

    invites.reset_store()
    ob.reset_store()

    tb._processed_msgs.clear()
    tb._response_store.clear()
    tb._task_registry._tasks.clear()
    tb.ALLOWED_USER_IDS = set()
    cc._session_mgr._cache.clear()
    cc._session_mgr._active.clear()
    monkeypatch.delenv("INVITE_ALLOW_CHAINING", raising=False)
    yield
    invites.reset_store()
    ob.reset_store()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Access control now includes invite grants
# ══════════════════════════════════════════════════════════════════════════════


class TestAllowed:
    def test_open_bot_allows_everyone(self):
        assert tb._allowed(1)
        assert tb._is_operator(1)

    def test_allowlisted_user_is_an_operator(self):
        tb.ALLOWED_USER_IDS = {1}
        assert tb._allowed(1)
        assert tb._is_operator(1)

    def test_stranger_is_refused(self):
        tb.ALLOWED_USER_IDS = {1}
        assert not tb._allowed(2)
        assert not tb._is_operator(2)

    def test_invited_user_is_allowed_but_is_not_an_operator(self):
        tb.ALLOWED_USER_IDS = {1}
        invites.get_store().grant_direct("telegram", "2")
        assert tb._allowed(2)
        assert not tb._is_operator(2)

    def test_grant_on_another_platform_does_not_admit(self):
        tb.ALLOWED_USER_IDS = {1}
        invites.get_store().grant_direct("discord", "2")
        assert not tb._allowed(2)

    def test_revoked_access_locks_the_door_again(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        store.grant_direct("telegram", "2")
        store.revoke_access("telegram", "2")
        assert not tb._allowed(2)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_bot_username(self):
        assert tb._bot_username(_make_ctx(username="mybot")) == "mybot"
        assert tb._bot_username(_make_ctx(username="@mybot")) == "mybot"

    def test_bot_username_unknown(self):
        ctx = _make_ctx()
        ctx.bot.username = None
        assert tb._bot_username(ctx) == ""

    def test_bot_username_from_a_mock_is_empty(self):
        ctx = MagicMock()
        assert tb._bot_username(ctx) == ""

    def test_display_name_prefers_the_first_name(self):
        assert tb._display_name(_make_update(first_name="Ada")) == "Ada"

    def test_display_name_falls_back_to_the_handle(self):
        update = _make_update()
        update.effective_user.first_name = None
        assert tb._display_name(update) == "ada"

    @pytest.mark.parametrize("raw,expected", [
        ("", (1, 7.0, "")),
        ("5", (5, 7.0, "")),
        ("5 30", (5, 30.0, "")),
        ("5 30 for the team", (5, 30.0, "for the team")),
        ("unlimited", (0, 7.0, "")),
        ("unlimited never", (0, None, "")),
        ("3 never launch week", (3, None, "launch week")),
        ("just a note", (1, 7.0, "just a note")),
        ("5 14d", (5, 14.0, "")),
    ])
    def test_invite_arg_parsing(self, raw, expected):
        assert tb._parse_invite_args(raw) == expected


# ══════════════════════════════════════════════════════════════════════════════
# 3. /start — deep links and onboarding
# ══════════════════════════════════════════════════════════════════════════════


class TestStart:
    @pytest.mark.asyncio
    async def test_first_time_user_is_offered_the_tour(self):
        update, ctx = _make_update(uid=1), _make_ctx()
        await tb.cmd_start(update, ctx)
        assert "tour" in _last_reply(update).lower()
        kwargs = update.message.reply_text.call_args.kwargs
        assert kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_returning_user_gets_the_short_greeting(self):
        update, ctx = _make_update(uid=1), _make_ctx()
        await tb.cmd_start(update, ctx)
        again = _make_update(uid=1)
        await tb.cmd_start(again, ctx)
        assert "Welcome back" in _last_reply(again)

    @pytest.mark.asyncio
    async def test_deep_link_admits_a_stranger(self):
        tb.ALLOWED_USER_IDS = {1}
        invite = invites.get_store().create("telegram", "1")

        update = _make_update(uid=2, first_name="Bob")
        await tb.cmd_start(update, _make_ctx(args=[f"inv_{invite.code}"]))

        assert tb._allowed(2)
        assert "tour" in _last_reply(update).lower()

    @pytest.mark.asyncio
    async def test_deep_link_credits_the_inviter(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        store.grant_direct("telegram", "1", display_name="Grace")
        invite = store.create("telegram", "1")

        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=[f"inv_{invite.code}"]))
        assert "Grace" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_bare_code_without_the_prefix_works(self):
        tb.ALLOWED_USER_IDS = {1}
        invite = invites.get_store().create("telegram", "1")
        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=[invite.code]))
        assert tb._allowed(2)

    @pytest.mark.asyncio
    async def test_spent_code_explains_itself(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        invite = store.create("telegram", "1", max_uses=1)
        store.redeem(invite.code, "telegram", "3")

        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=[invite.code]))
        assert "maximum" in _last_reply(update)
        assert not tb._allowed(2)

    @pytest.mark.asyncio
    async def test_revoked_code_is_refused(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        invite = store.create("telegram", "1")
        store.revoke(invite.code)
        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=[invite.code]))
        assert "revoked" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_garbage_payload_from_a_stranger(self):
        tb.ALLOWED_USER_IDS = {1}
        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=["not-a-code"]))
        assert "isn't valid" in _last_reply(update)
        assert not tb._allowed(2)

    @pytest.mark.asyncio
    async def test_stranger_without_a_code_is_told_their_id(self):
        tb.ALLOWED_USER_IDS = {1}
        update = _make_update(uid=4242)
        await tb.cmd_start(update, _make_ctx())
        assert "4242" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_redeeming_your_own_code_still_greets_you(self):
        invite = invites.get_store().create("telegram", "1")
        update = _make_update(uid=1)
        await tb.cmd_start(update, _make_ctx(args=[invite.code]))
        assert "Hi" in _last_reply(update) or "Welcome" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_already_admitted_user_redeeming_again_is_greeted(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        store.grant_direct("telegram", "2")
        invite = store.create("telegram", "1")
        update = _make_update(uid=2)
        await tb.cmd_start(update, _make_ctx(args=[invite.code]))
        assert "not valid" not in _last_reply(update)


class TestTour:
    @pytest.mark.asyncio
    async def test_tour_command_starts_at_the_beginning(self):
        update = _make_update(uid=1)
        await tb.cmd_tour(update, _make_ctx())
        assert ob.TOUR[0].title in _last_reply(update)

    @pytest.mark.asyncio
    async def test_tour_callback_advances(self):
        update = _make_callback_update(1, "tg:tour:1")
        await tb.handle_callback(update, _make_ctx())
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert ob.TOUR[1].title in text

    @pytest.mark.asyncio
    async def test_tour_callback_past_the_end_finishes(self):
        update = _make_callback_update(1, f"tg:tour:{ob.TOTAL_STEPS}")
        await tb.handle_callback(update, _make_ctx())
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "/help" in text

    @pytest.mark.asyncio
    async def test_tour_done_marks_completion(self):
        update = _make_callback_update(1, "tg:tour:done")
        await tb.handle_callback(update, _make_ctx())
        assert ob.get_store().get("telegram", "1").completed

    @pytest.mark.asyncio
    async def test_tour_callback_with_a_bad_index_is_ignored(self):
        update = _make_callback_update(1, "tg:tour:banana")
        await tb.handle_callback(update, _make_ctx())
        update.callback_query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_progress_is_remembered(self):
        await tb.handle_callback(_make_callback_update(1, "tg:tour:2"), _make_ctx())
        assert ob.get_store().get("telegram", "1").step == 2

    def test_keyboard_offers_next_until_the_end(self):
        assert len(tb._tour_keyboard(0).inline_keyboard[0]) == 2
        assert len(tb._tour_keyboard(ob.TOTAL_STEPS - 1).inline_keyboard[0]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Invite commands
# ══════════════════════════════════════════════════════════════════════════════


class TestInviteCommands:
    @pytest.mark.asyncio
    async def test_invite_returns_a_tappable_link(self):
        update = _make_update(uid=1, text="/invite")
        await tb.cmd_invite(update, _make_ctx(username="mybot"))
        reply = _last_reply(update)
        assert "https://t.me/mybot?start=inv_" in reply
        assert len(invites.get_store().list_created_by("telegram", "1")) == 1

    @pytest.mark.asyncio
    async def test_invite_without_a_known_handle_falls_back_to_the_code(self):
        ctx = _make_ctx()
        ctx.bot.username = None
        update = _make_update(uid=1, text="/invite")
        await tb.cmd_invite(update, ctx)
        code = invites.get_store().list_created_by("telegram", "1")[0].code
        assert code in _last_reply(update)

    @pytest.mark.asyncio
    async def test_invite_honours_arguments(self):
        update = _make_update(uid=1, text="/invite 5 30 launch week")
        await tb.cmd_invite(update, _make_ctx())
        invite = invites.get_store().list_created_by("telegram", "1")[0]
        assert invite.max_uses == 5
        assert invite.note == "launch week"
        assert invite.expires_at > time.time() + 29 * 86400

    @pytest.mark.asyncio
    async def test_invited_user_cannot_mint_invites_by_default(self):
        tb.ALLOWED_USER_IDS = {1}
        invites.get_store().grant_direct("telegram", "2")
        update = _make_update(uid=2, text="/invite")
        await tb.cmd_invite(update, _make_ctx())
        assert "INVITE_ALLOW_CHAINING" in _last_reply(update)
        assert invites.get_store().list_created_by("telegram", "2") == []

    @pytest.mark.asyncio
    async def test_chaining_lets_an_invited_user_invite(self, monkeypatch):
        tb.ALLOWED_USER_IDS = {1}
        monkeypatch.setenv("INVITE_ALLOW_CHAINING", "true")
        invites.get_store().grant_direct("telegram", "2")
        update = _make_update(uid=2, text="/invite")
        await tb.cmd_invite(update, _make_ctx())
        assert len(invites.get_store().list_created_by("telegram", "2")) == 1

    @pytest.mark.asyncio
    async def test_stranger_gets_nothing(self):
        tb.ALLOWED_USER_IDS = {1}
        update = _make_update(uid=9, text="/invite")
        await tb.cmd_invite(update, _make_ctx())
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_invites_listing_when_empty(self):
        update = _make_update(uid=1, text="/invites")
        await tb.cmd_invites(update, _make_ctx())
        assert "haven't invited anyone" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_invites_listing_shows_codes_and_joiners(self):
        store = invites.get_store()
        invite = store.create("telegram", "1", max_uses=3)
        store.redeem(invite.code, "telegram", "2", display_name="Bob")

        update = _make_update(uid=1, text="/invites")
        await tb.cmd_invites(update, _make_ctx())
        reply = _last_reply(update)
        assert invite.code in reply
        assert "Bob" in reply

    @pytest.mark.asyncio
    async def test_revoke_kills_a_link(self):
        invite = invites.get_store().create("telegram", "1")
        update = _make_update(uid=1, text=f"/revoke {invite.code}")
        await tb.cmd_revoke(update, _make_ctx())
        assert "Revoked" in _last_reply(update)
        assert invites.get_store().get(invite.code).revoked

    @pytest.mark.asyncio
    async def test_revoke_accepts_a_lowercase_code(self):
        invite = invites.get_store().create("telegram", "1")
        update = _make_update(uid=1, text=f"/revoke {invite.code.lower()}")
        await tb.cmd_revoke(update, _make_ctx())
        assert invites.get_store().get(invite.code).revoked

    @pytest.mark.asyncio
    async def test_revoke_without_an_argument_explains_itself(self):
        update = _make_update(uid=1, text="/revoke")
        await tb.cmd_revoke(update, _make_ctx())
        assert "Usage" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_a_guest_cannot_revoke_someone_elses_invite(self, monkeypatch):
        tb.ALLOWED_USER_IDS = {1}
        monkeypatch.setenv("INVITE_ALLOW_CHAINING", "true")
        store = invites.get_store()
        store.grant_direct("telegram", "2")
        invite = store.create("telegram", "1")

        update = _make_update(uid=2, text=f"/revoke {invite.code}")
        await tb.cmd_revoke(update, _make_ctx())
        assert "No such invite" in _last_reply(update)
        assert not store.get(invite.code).revoked

    @pytest.mark.asyncio
    async def test_kick_removes_an_invited_user(self):
        tb.ALLOWED_USER_IDS = {1}
        invites.get_store().grant_direct("telegram", "2")
        update = _make_update(uid=1, text="/kick 2")
        await tb.cmd_kick(update, _make_ctx())
        assert not tb._allowed(2)

    @pytest.mark.asyncio
    async def test_kick_is_operator_only(self):
        tb.ALLOWED_USER_IDS = {1}
        store = invites.get_store()
        store.grant_direct("telegram", "2")
        store.grant_direct("telegram", "3")
        update = _make_update(uid=2, text="/kick 3")
        await tb.cmd_kick(update, _make_ctx())
        assert "Only whoever runs this bot" in _last_reply(update)
        assert tb._allowed(3)

    @pytest.mark.asyncio
    async def test_kick_needs_a_numeric_id(self):
        update = _make_update(uid=1, text="/kick nobody")
        await tb.cmd_kick(update, _make_ctx())
        assert "Usage" in _last_reply(update)

    @pytest.mark.asyncio
    async def test_kick_an_allowlisted_user_points_at_the_env(self):
        tb.ALLOWED_USER_IDS = {1, 5}
        update = _make_update(uid=1, text="/kick 5")
        await tb.cmd_kick(update, _make_ctx())
        assert "TELEGRAM_ALLOWED_USER_IDS" in _last_reply(update)
