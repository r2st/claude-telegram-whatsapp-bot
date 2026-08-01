"""
Group-chat policy shared by every adapter.

A bot in a group is the cheapest distribution telechat has: one person adds
it, everyone in the room sees it work. That only holds if it behaves in a
group — and on Telegram it did not. The Telegram adapter answered every
message in every chat, so adding it to a group meant it replied to the
humans talking to each other. The realistic outcome is being removed within
a minute.

This module is the single place that decides whether a group message is for
the bot. It is platform-agnostic on purpose: Discord's mention rule, Slack's
``<@U…>`` form and Telegram's ``@botname`` all reduce to the same question,
and having three adapters answer it three ways is how they drift.

Modes:

- ``mention`` (default) — reply when addressed: an @mention, a reply to one
  of the bot's messages, or a command aimed at it (``/ask@thebot``).
- ``all`` — reply to everything, for a group that exists to talk to the bot.
- ``off`` — stay silent except for explicit commands.

Direct messages ignore the mode entirely; a DM is always addressed to you.

Usage:
    from telechat_pkg import group_policy as gp

    d = gp.decide(
        text="@mybot what's the weather",
        is_direct=False,
        mode=gp.get_settings().get_mode("telegram", "-100123"),
        bot_username="mybot",
    )
    if d.respond:
        run(d.text)          # 'what's the weather' — mention stripped
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)

MODES = ("mention", "all", "off")

MODE_HELP = {
    "mention": "Only when you @mention me, reply to me, or use a command.",
    "all": "Every message in this chat.",
    "off": "Never — commands only.",
}

_FALLBACK_MODE = "mention"


def default_mode() -> str:
    """The mode a chat has before anyone sets one.

    ``GROUP_DEFAULT_MODE`` overrides it. Read per call so tests and a
    restart-free config change both take effect.
    """
    return normalize_mode(os.getenv("GROUP_DEFAULT_MODE", "")) or _FALLBACK_MODE


def normalize_mode(value: Optional[str]) -> Optional[str]:
    """Canonicalise a mode name, accepting the obvious synonyms. None if invalid."""
    if not value:
        return None
    v = str(value).strip().lower()
    aliases = {
        "mentions": "mention", "mentioned": "mention", "mention-only": "mention",
        "mentiononly": "mention", "@": "mention",
        "always": "all", "everything": "all", "on": "all", "every": "all",
        "none": "off", "silent": "off", "mute": "off", "quiet": "off",
    }
    v = aliases.get(v, v)
    return v if v in MODES else None


# ─── Addressing detection ─────────────────────────────────────────────────────

_mention_cache: dict[str, re.Pattern] = {}
_mention_cache_lock = threading.Lock()
_MENTION_CACHE_MAX = 64

# Slack (<@U123>, <@U123|name>) and Discord (<@123>, <@!123>) mention forms.
_PLATFORM_MENTION_RE = re.compile(r"<@!?([A-Za-z0-9_]+)(?:\|[^>]*)?>")


def mention_pattern(username: str) -> re.Pattern:
    """A cached, case-insensitive pattern matching ``@username`` as a whole word.

    ``@mybot`` matches; ``@mybot2`` and ``email@mybot.com`` do not — the
    former because of the trailing word boundary, the latter because the
    lookbehind rejects an ``@`` preceded by a word character.
    """
    key = username.lower()
    cached = _mention_cache.get(key)
    if cached is not None:
        return cached
    pat = re.compile(rf"(?<![\w@])@{re.escape(username)}\b", re.IGNORECASE)
    with _mention_cache_lock:
        if len(_mention_cache) >= _MENTION_CACHE_MAX:
            # Bounded: a bot has one username, but a shared process running
            # several must not grow this without limit.
            _mention_cache.clear()
        _mention_cache[key] = pat
    return pat


def _command_targets_bot(text: str, bot_username: Optional[str]) -> Optional[bool]:
    """For a leading ``/command`` return whether it targets this bot.

    ``/help@otherbot`` → False (explicitly someone else's). ``/help@mybot`` →
    True. A bare ``/help`` → True: in a group Telegram delivers it to every
    bot, and refusing to answer an unqualified command is more surprising
    than answering one. Returns None when the text is not a command.
    """
    m = re.match(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?", text.strip())
    if not m:
        return None
    target = m.group(2)
    if not target:
        return True
    if not bot_username:
        return True
    return target.lower() == bot_username.lower()


def is_addressed(
    text: str,
    *,
    bot_username: Optional[str] = None,
    bot_user_id: Optional[str] = None,
    is_reply_to_bot: bool = False,
    mention_ids: Sequence[str] = (),
) -> bool:
    """Whether this group message is aimed at the bot."""
    if is_reply_to_bot:
        return True

    if bot_user_id and any(str(m) == str(bot_user_id) for m in mention_ids):
        return True

    body = text or ""

    if bot_user_id:
        for found in _PLATFORM_MENTION_RE.findall(body):
            if found == str(bot_user_id):
                return True

    if bot_username and mention_pattern(bot_username).search(body):
        return True

    cmd = _command_targets_bot(body, bot_username)
    if cmd is not None:
        return cmd

    return False


def strip_addressing(
    text: str,
    *,
    bot_username: Optional[str] = None,
    bot_user_id: Optional[str] = None,
) -> str:
    """Remove the bot's own mentions so the prompt reads naturally.

    "@mybot summarise this" becomes "summarise this" — otherwise every group
    prompt starts by telling Claude its own handle.
    """
    out = text or ""
    if bot_username:
        out = mention_pattern(bot_username).sub(" ", out)
    if bot_user_id:
        out = _PLATFORM_MENTION_RE.sub(
            lambda m: " " if m.group(1) == str(bot_user_id) else m.group(0), out
        )
    # Also drop the "@bot" suffix Telegram appends to commands in groups.
    if bot_username:
        out = re.sub(rf"^(/[A-Za-z0-9_]+)@{re.escape(bot_username)}\b",
                     r"\1", out.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class Decision:
    respond: bool
    reason: str      # direct | addressed | mode_all | not_addressed | mode_off | empty
    text: str = ""   # the prompt with the bot's mentions removed
    addressed: bool = False


def decide(
    *,
    text: str,
    is_direct: bool,
    mode: Optional[str] = None,
    bot_username: Optional[str] = None,
    bot_user_id: Optional[str] = None,
    is_reply_to_bot: bool = False,
    mention_ids: Sequence[str] = (),
) -> Decision:
    """Decide whether to answer, and with what prompt text."""
    cleaned = strip_addressing(text, bot_username=bot_username, bot_user_id=bot_user_id)

    if is_direct:
        # A DM is addressed by construction; mode never applies.
        return Decision(True, "direct", cleaned, addressed=True)

    addressed = is_addressed(
        text,
        bot_username=bot_username,
        bot_user_id=bot_user_id,
        is_reply_to_bot=is_reply_to_bot,
        mention_ids=mention_ids,
    )

    resolved = normalize_mode(mode) or default_mode()

    if resolved == "off":
        return Decision(False, "mode_off", cleaned, addressed=addressed)
    if resolved == "all":
        if not cleaned:
            return Decision(False, "empty", "", addressed=addressed)
        return Decision(True, "mode_all", cleaned, addressed=addressed)

    # mention mode
    if not addressed:
        return Decision(False, "not_addressed", cleaned, addressed=False)
    if not cleaned:
        # Bare "@mybot" with nothing else — worth acknowledging, but the
        # caller decides how; it has no prompt to run.
        return Decision(False, "empty", "", addressed=True)
    return Decision(True, "addressed", cleaned, addressed=True)


# ─── Per-chat settings ────────────────────────────────────────────────────────


@dataclass
class ChatSetting:
    platform: str
    chat_id: str
    mode: str
    updated_at: float
    updated_by: str = ""
    title: str = ""


class GroupSettings:
    """Per-chat group mode, persisted so a restart does not reset every room."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        self._cache: dict[tuple[str, str], str] = {}
        self._cache_lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS group_settings (
                platform   TEXT NOT NULL,
                chat_id    TEXT NOT NULL,
                mode       TEXT NOT NULL,
                updated_at REAL NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                title      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (platform, chat_id)
            );
        """)

    def get_mode(self, platform: str, chat_id: str) -> str:
        key = (platform, str(chat_id))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        row = self._conn().execute(
            "SELECT mode FROM group_settings WHERE platform = ? AND chat_id = ?", key
        ).fetchone()
        mode = normalize_mode(row["mode"]) if row else None
        mode = mode or default_mode()
        with self._cache_lock:
            self._cache[key] = mode
        return mode

    def set_mode(
        self, platform: str, chat_id: str, mode: str, *, updated_by: str = "", title: str = ""
    ) -> str:
        """Persist a chat's mode. Raises ValueError on an unknown mode name."""
        norm = normalize_mode(mode)
        if norm is None:
            raise ValueError(f"unknown group mode: {mode!r} (expected one of {', '.join(MODES)})")
        key = (platform, str(chat_id))
        self._conn().execute(
            "INSERT INTO group_settings (platform, chat_id, mode, updated_at, updated_by, title) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(platform, chat_id) DO UPDATE SET "
            "mode = excluded.mode, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by, "
            "title = CASE WHEN excluded.title != '' THEN excluded.title ELSE group_settings.title END",
            (*key, norm, time.time(), str(updated_by), title[:120]),
        )
        self._conn().commit()
        with self._cache_lock:
            self._cache[key] = norm
        return norm

    def clear(self, platform: str, chat_id: str) -> bool:
        key = (platform, str(chat_id))
        cur = self._conn().execute(
            "DELETE FROM group_settings WHERE platform = ? AND chat_id = ?", key
        )
        self._conn().commit()
        with self._cache_lock:
            self._cache.pop(key, None)
        return cur.rowcount > 0

    def all_for(self, platform: str) -> list[ChatSetting]:
        rows = self._conn().execute(
            "SELECT * FROM group_settings WHERE platform = ? ORDER BY updated_at DESC",
            (platform,),
        ).fetchall()
        return [
            ChatSetting(
                platform=r["platform"], chat_id=r["chat_id"], mode=r["mode"],
                updated_at=r["updated_at"], updated_by=r["updated_by"] or "",
                title=r["title"] or "",
            )
            for r in rows
        ]

    def invalidate(self) -> None:
        """Drop the in-memory cache — another process may have written."""
        with self._cache_lock:
            self._cache.clear()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


_settings: Optional[GroupSettings] = None
_settings_lock = threading.Lock()


def get_settings() -> GroupSettings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = GroupSettings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings store — for tests that repoint DB_PATH."""
    global _settings
    with _settings_lock:
        if _settings is not None:
            _settings.close()
        _settings = None


def mode_of(platform: str, chat_id: str) -> str:
    """Look up a chat's mode, falling back to the default if the DB is unhappy."""
    try:
        return get_settings().get_mode(platform, str(chat_id))
    except Exception:
        log.debug("group mode lookup failed", exc_info=True)
        return default_mode()


def describe(mode: str, *, chat_title: str = "") -> str:
    """Explain the current mode in a sentence, for a chat reply."""
    norm = normalize_mode(mode) or default_mode()
    where = f" in *{chat_title}*" if chat_title else " here"
    return f"I reply{where}: {MODE_HELP[norm]}"


def mode_options(current: str) -> list[tuple[str, str]]:
    """(mode, label) pairs for building a picker, marking the current one."""
    norm = normalize_mode(current) or default_mode()
    labels = {"mention": "🔔 When mentioned", "all": "💬 Every message", "off": "🔕 Off"}
    return [(m, labels[m] + (" ✓" if m == norm else "")) for m in MODES]


def summarize_settings(settings: Iterable[ChatSetting]) -> str:
    """Multi-line summary of every configured chat, for an operator view."""
    rows = list(settings)
    if not rows:
        return "No group chats configured — every group uses the default."
    return "\n".join(f"`{s.chat_id}` — {s.mode}" + (f" ({s.title})" if s.title else "")
                     for s in rows)
