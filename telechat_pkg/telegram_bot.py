"""
Telegram adapter — wraps claude_core for the Telegram Bot API.
Run via main.py with BOT_MODE=telegram or BOT_MODE=both.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import claude_core as cc
from .memory import MemoryStore, extract_memories
from .link_understanding import understand_links, extract_links, ENABLED as LINK_ENABLED
from .polls import parse_poll_command
from .tts import synthesize as tts_synthesize, is_available as tts_available, VOICES as TTS_VOICES
from .image_gen import generate as image_generate, is_available as image_gen_available
from .web_search import search as web_search, format_results as format_search_results, is_available as search_available
from .voice_transcription import transcribe as voice_transcribe, is_available as transcription_available
from .music_gen import generate as music_generate, is_available as music_gen_available
from .video_gen import generate as video_generate, is_available as video_gen_available
from .web_fetch import fetch_readable
from . import coder

log = logging.getLogger(__name__)

PLATFORM = "telegram"

# ─── Telegram-specific config ───────────────────────────────────────────────────

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_USER_IDS: set[int] = set()
_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
if _raw:
    ALLOWED_USER_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

# Per-user overrides (in-memory)
_user_model:   dict[int, str] = {}
_user_perm:    dict[int, str] = {}
_user_verbose: dict[int, int] = {}

PERMISSION_MODES = {
    "default":           "Default (prompts for everything)",
    "acceptEdits":       "Accept Edits (auto-approve file r/w)",
    "auto":              "Auto (approve most actions)",
    "bypassPermissions": "Bypass All (no restrictions)",
}
CLI_MODELS = {
    "haiku":  "Haiku (fastest)",
    "sonnet": "Sonnet (balanced)",
    "opus":   "Opus (most capable)",
}
VERBOSE_LEVELS = {0: "Quiet", 1: "Normal", 2: "Detailed"}

_DEFAULT_MODEL = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_PERM  = os.getenv("CLAUDE_CLI_PERMISSION_MODE", cc.CLAUDE_PERM_MODE)

_memory = MemoryStore()

# ─── Feature flags ────────────────────────────────────────────────────────────────
AUTO_MEMORY_ENABLED = os.getenv("AUTO_MEMORY", "true").lower() in ("1", "true", "yes")
AUTO_MEMORY_MIN_LENGTH = int(os.getenv("AUTO_MEMORY_MIN_LENGTH", "100"))
COST_BUDGET_ENABLED = os.getenv("COST_BUDGET_ENABLED", "true").lower() in ("1", "true", "yes")
SMART_ROUTING_ENABLED = os.getenv("SMART_ROUTING_ENABLED", "true").lower() in ("1", "true", "yes")
# Prompt A/B optimization is opt-in: off by default so the static system prompt
# is used unless an operator explicitly turns on variant routing.
PROMPT_OPTIMIZER_ENABLED = os.getenv("PROMPT_OPTIMIZER_ENABLED", "false").lower() in ("1", "true", "yes")

# ─── Settings panel icons ────────────────────────────────────────────────────────

_MODEL_ICONS = {"haiku": "⚡", "sonnet": "⚖️", "opus": "🧠"}
_PERM_ICONS = {"default": "🔒", "acceptEdits": "📝", "auto": "🤖", "bypassPermissions": "⚠️"}
_VERBOSE_ICONS = {0: "🔇", 1: "🔈", 2: "🔊"}
_ENGINE_ICONS = {"cli": "🖥️", "sdk": "🔌", "api": "🌐"}


def _build_settings_text(uid: int) -> str:
    """Build the settings panel message text."""
    m = _model(uid)
    e = _engine(uid)
    p = _perm(uid) or "default"
    v = _verbose(uid)
    sess = _active_session(uid)
    return (
        f"⚙️ *Settings*\n\n"
        f"Session: `{sess.name}` {sess.status_emoji()}\n"
        f"Model: {_MODEL_ICONS.get(m, '')} `{m}`\n"
        f"Engine: {_ENGINE_ICONS.get(e, '')} `{e}`\n"
        f"Permissions: {_PERM_ICONS.get(p, '')} `{p}`\n"
        f"Verbosity: {_VERBOSE_ICONS.get(v, '')} `{v}`"
    )


def _build_settings_markup(uid: int) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the settings panel."""
    m = _model(uid)
    e = _engine(uid)
    p = _perm(uid) or "default"
    v = _verbose(uid)

    # Row 1: Model selection
    model_row = [
        InlineKeyboardButton(
            f"{_MODEL_ICONS.get(k, '')} {k.title()}{' ✓' if k == m else ''}",
            callback_data=f"tg:set:model:{k}",
        )
        for k in CLI_MODELS
    ]
    # Row 2: Engine selection
    engine_row = [
        InlineKeyboardButton(
            f"{_ENGINE_ICONS.get(k, '')} {k.upper()}{' ✓' if k == e else ''}",
            callback_data=f"tg:set:engine:{k}",
        )
        for k in ENGINE_MODES
    ]
    # Row 3: Permissions (compact)
    perm_row = [
        InlineKeyboardButton(
            f"{_PERM_ICONS.get(k, '')} {k[:8]}{' ✓' if k == p else ''}",
            callback_data=f"tg:set:perm:{k}",
        )
        for k in PERMISSION_MODES
    ]
    # Row 4: Verbosity
    verbose_row = [
        InlineKeyboardButton(
            f"{_VERBOSE_ICONS.get(lvl, '')} {lbl}{' ✓' if lvl == v else ''}",
            callback_data=f"tg:set:verbose:{lvl}",
        )
        for lvl, lbl in VERBOSE_LEVELS.items()
    ]

    return InlineKeyboardMarkup([model_row, engine_row, perm_row, verbose_row])


# ─── Per-user getters ───────────────────────────────────────────────────────────

def _model(uid: int)   -> str: return _user_model.get(uid, _DEFAULT_MODEL)
def _perm(uid: int)    -> str: return _user_perm.get(uid, _DEFAULT_PERM)
def _verbose(uid: int) -> int: return _user_verbose.get(uid, 1)


# ─── Auth / rate limit ──────────────────────────────────────────────────────────

def _is_operator(uid: int) -> bool:
    """Whether this identity is one the operator configured, not an invited guest.

    An open bot (empty allowlist) cannot tell the difference, so everyone
    counts — the same assumption every other privileged command already makes.
    """
    return not ALLOWED_USER_IDS or uid in ALLOWED_USER_IDS


def _allowed(uid: int) -> bool:
    """Access = the env allowlist, plus anyone who redeemed an invite code.

    Grants live in the database, so admitting someone needs no edit to
    ``.env`` and no restart.
    """
    if not ALLOWED_USER_IDS or uid in ALLOWED_USER_IDS:
        return True
    from . import invites
    return invites.is_granted(PLATFORM, str(uid))


# ─── Typing indicator ───────────────────────────────────────────────────────────

async def _typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            # Cosmetic. Never let the typing indicator take down a turn.
            log.debug("send_chat_action failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            continue


# ─── Message deduplication (Telegram can retry sending the same update) ────────

_processed_msgs: dict[int, float] = {}  # message_id → timestamp
_DEDUP_TTL = 30  # seconds
_dedup_last_cleanup = 0.0


def _is_duplicate(msg_id: int) -> bool:
    """Return True if this message was already processed recently."""
    global _dedup_last_cleanup
    now = time.time()
    if msg_id in _processed_msgs:
        return True
    # Periodic cleanup instead of every call (every 60s)
    if now - _dedup_last_cleanup > 60:
        _dedup_last_cleanup = now
        cutoff = now - _DEDUP_TTL
        stale = [k for k, v in _processed_msgs.items() if v < cutoff]
        for k in stale:
            del _processed_msgs[k]
    _processed_msgs[msg_id] = now
    return False


# ─── Task manager for concurrent sessions ─────────────────────────────────────

_task_counter = itertools.count(1)

# Max concurrent tasks per user
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))


class TaskSession:
    """Manages a single running Claude task with progress, cancel, and identity."""

    def __init__(self, placeholder, uid: int, prompt_preview: str):
        self.task_id = next(_task_counter)
        self.placeholder = placeholder
        self.chat_id = None  # set externally for intermediate messages
        self.bot = None       # set externally
        self.uid = uid
        self.prompt_preview = prompt_preview[:40]
        self.start_time = time.time()
        self.tools: list[str] = []
        self._tool_counts: dict[str, int] = {}
        self.tool_count = 0
        self._last_update = 0.0
        self._last_status = ""
        self._cancelled = False
        self._heartbeat_task: asyncio.Task | None = None
        self._partial_text = ""
        self._phase = "thinking"  # thinking → working → streaming
        self._current_activity = ""  # detailed activity description
        self._sent_intermediate = False  # only send one intermediate update

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    def _elapsed(self) -> str:
        secs = int(time.time() - self.start_time)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"

    def _progress_bar(self) -> str:
        """Visual progress bar based on activity."""
        secs = int(time.time() - self.start_time)
        # Animated fill that moves based on time + activity
        if self._phase == "streaming":
            fill = min(9, 7 + (secs % 3))
        elif self.tool_count > 0:
            fill = min(7, 2 + self.tool_count)
        else:
            fill = min(3, 1 + secs // 10)
        bar = "▓" * fill + "░" * (10 - fill)
        return f"[{bar}]"

    def _build_status(self) -> str:
        elapsed = self._elapsed()

        # Show task ID if user has multiple active tasks
        user_tasks = _task_registry.get_user_tasks(self.uid)
        task_label = f"  `#{self.task_id}`" if len(user_tasks) > 1 else ""

        # Phase with emoji and animated spinner
        secs = int(time.time() - self.start_time)
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][secs % 10]
        if self._phase == "thinking":
            phase = f"{spinner} 🧠 *Thinking…*"
        elif self._phase == "working":
            phase = f"{spinner} ⚙️ *Working…*"
        else:
            phase = f"{spinner} ✍️ *Writing…*"

        # Model badge
        model_name = _model(self.uid)
        model_badge = f"  `{model_name}`" if model_name else ""

        progress = self._progress_bar()
        lines = [f"{phase} `{elapsed}`{model_badge}{task_label}", progress]

        # Current activity (detailed)
        if self._current_activity:
            lines.append(f"\n{self._current_activity}")
        elif self.tools:
            last_tool = self.tools[-1]
            lines.append(f"\n{_tool_label(last_tool)}")

        # Tool history — compact icon timeline
        if self.tool_count > 1:
            unique = list(dict.fromkeys(self.tools))
            summary_parts = []
            for t in unique[-5:]:
                icon = _TOOL_ICONS.get(t, "🔧")
                summary_parts.append(f"{icon}×{self._tool_counts[t]}" if self._tool_counts[t] > 1 else icon)
            lines.append(f"\n{'  '.join(summary_parts)}  _({self.tool_count} steps)_")

        # Show partial streaming text preview
        if self._partial_text:
            preview = self._partial_text[-400:]
            if len(self._partial_text) > 400:
                preview = "…" + preview
            lines.append(f"\n{'─' * 20}\n{preview}")

        return "\n".join(lines)

    async def on_tool(self, tool_name: str, detail: str = ""):
        """Called when Claude starts using a tool."""
        self._phase = "working"
        self.tools.append(tool_name)
        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1
        self.tool_count += 1
        if detail:
            self._current_activity = f"{_tool_label(tool_name).rstrip('…')} `{detail}`…"
        else:
            self._current_activity = _tool_label(tool_name)
        await self._update()

    async def on_text(self, text: str):
        """Called when Claude streams text content."""
        self._phase = "streaming"
        self._current_activity = ""
        # Handle both full-text updates and delta appends
        if len(text) > len(self._partial_text):
            self._partial_text = text
        else:
            self._partial_text += text
        await self._update()

    async def _update(self, force: bool = False):
        """Update the placeholder message (rate-limited to avoid Telegram 429s)."""
        now = time.time()
        # Adaptive rate: faster for first 15s (2s), slower after (4s)
        min_interval = 2.0 if (now - self.start_time) < 15 else 4.0
        if not force and (now - self._last_update < min_interval):
            return
        self._last_update = now

        status = self._build_status()
        # Don't send identical updates (Telegram would error "message not modified")
        if status == self._last_status:
            return
        self._last_status = status

        # Truncate to stay within Telegram's 4096 char limit
        if len(status) > 4000:
            status = status[:4000] + "\n…"
        cancel_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Cancel", callback_data=f"tg:cancel:{self.task_id}")]
        ])
        try:
            await self.placeholder.edit_text(
                status,
                reply_markup=cancel_btn,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback without markdown if it fails
            try:
                await self.placeholder.edit_text(
                    status,
                    reply_markup=cancel_btn,
                )
            except Exception:
                # Both the markdown edit and its plain-text fallback failed —
                # usually a deleted message or an unchanged body.
                log.debug("progress edit failed (both attempts)", exc_info=True)

    async def _heartbeat(self):
        """Periodic updates to keep elapsed time fresh."""
        await asyncio.sleep(4)
        while not self._cancelled:
            await self._update(force=True)
            elapsed = time.time() - self.start_time

            # For very long tasks (>60s), send intermediate result as new message
            if (elapsed > 60 and not self._sent_intermediate
                    and self._partial_text and len(self._partial_text) > 100
                    and self.bot and self.chat_id):
                self._sent_intermediate = True
                preview = self._partial_text[:2000]
                if len(self._partial_text) > 2000:
                    preview += "\n\n⏳ _Still working… full response coming soon._"
                try:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=f"📝 *Interim update* ({self._elapsed()}):\n\n{preview}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    try:
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"📝 Interim update ({self._elapsed()}):\n\n{preview}",
                        )
                    except Exception:
                        log.debug("interim update failed (both attempts)", exc_info=True)

            if elapsed < 30:
                interval = 4
            elif elapsed < 120:
                interval = 8
            else:
                interval = 12
            await asyncio.sleep(interval)

    def start_heartbeat(self):
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def stop(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    def finish_summary(self) -> str:
        elapsed = self._elapsed()
        if self.tool_count > 0:
            return f"✅ _{self.tool_count} tools · {elapsed}_"
        return f"✅ _{elapsed}_"


class TaskRegistry:
    """Thread-safe registry of active tasks across all users."""

    def __init__(self):
        self._tasks: dict[int, TaskSession] = {}  # task_id → TaskSession

    def register(self, task: TaskSession) -> None:
        self._tasks[task.task_id] = task

    def unregister(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)

    def get(self, task_id: int) -> TaskSession | None:
        return self._tasks.get(task_id)

    def get_user_tasks(self, uid: int) -> list[TaskSession]:
        return [t for t in self._tasks.values() if t.uid == uid]

    def user_task_count(self, uid: int) -> int:
        return sum(1 for t in self._tasks.values() if t.uid == uid)

    def cancel_all_user(self, uid: int) -> int:
        """Cancel all tasks for a user. Returns count cancelled."""
        count = 0
        for t in self.get_user_tasks(uid):
            t.cancel()
            count += 1
        return count


_task_registry = TaskRegistry()

# ─── Response store (for pagination and retry) ─────────────────────────────────

_response_store: dict[str, dict] = {}  # response_id → {text, uid, prompt, page}
_response_counter = itertools.count(1)

RESPONSE_PAGE_SIZE = 3000  # chars per page
_RESPONSE_STORE_MAX = 50   # cap on paginated responses
_RETRY_STORE_MAX = 200     # cap on transient retry_* entries


def _store_response(uid: int, prompt: str, text: str) -> str:
    """Store a long response and return its ID for pagination.

    Two independent caps, because the entries are different things: `r*` keys
    hold paginated response text, `retry_*` keys hold a prompt stashed against
    a Retry button the user has not pressed yet.

    They have to be counted separately as well as evicted separately. The
    threshold used to be `len(_response_store) > 50` over *both* kinds, so
    accumulated retry stashes pushed the total past 50 on their own and every
    new response then evicted an older one — pagination expiring after a
    handful of responses instead of fifty.
    """
    rid = f"r{next(_response_counter)}"
    _response_store[rid] = {"text": text, "uid": uid, "prompt": prompt}

    # Oldest-first: rids are monotonic and dicts keep insertion order.
    paginated = [k for k in _response_store if not k.startswith("retry_")]
    for k in paginated[: max(0, len(paginated) - _RESPONSE_STORE_MAX)]:
        _response_store.pop(k, None)

    retry_keys = [k for k in _response_store if k.startswith("retry_")]
    for k in retry_keys[: max(0, len(retry_keys) - _RETRY_STORE_MAX)]:
        _response_store.pop(k, None)
    return rid


def _action_buttons(rid: str, has_pages: bool = False, page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Build quick-action buttons for a response."""
    rows = []
    # Pagination row
    if has_pages:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"tg:pg:{rid}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="tg:noop:_"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"tg:pg:{rid}:{page + 1}"))
        rows.append(nav)
    # Action row
    actions = [
        InlineKeyboardButton("🔄 Retry", callback_data=f"tg:act:{rid}:retry"),
        InlineKeyboardButton("➡️ Continue", callback_data=f"tg:act:{rid}:continue"),
    ]
    if tts_available():
        actions.append(InlineKeyboardButton("🔊 TTS", callback_data=f"tg:act:{rid}:tts"))
    rows.append(actions)
    return InlineKeyboardMarkup(rows)


# ─── Reply helper ───────────────────────────────────────────────────────────────

# Regex to find bare URLs not already inside markdown link syntax
_URL_RE = re.compile(r'(?<!\()(https?://[^\s\)\]>]+)')
# Regex to detect existing markdown links [text](url)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


def _protect_urls_for_markdown(text: str) -> str:
    """Wrap bare URLs in markdown link format so Telegram renders them clickable.

    Bare URLs containing underscores/special chars break Telegram's legacy Markdown
    parser. Wrapping them as [url](url) ensures they display as clickable links.
    """
    # First, collect positions of existing markdown links to avoid double-wrapping
    existing_link_spans = set()
    for m in _MD_LINK_RE.finditer(text):
        existing_link_spans.add((m.start(), m.end()))

    def _replace_bare_url(match):
        url = match.group(1)
        start = match.start(1)
        # Check if this URL is already inside a markdown link
        for ls, le in existing_link_spans:
            if ls <= start < le:
                return match.group(0)
        # Strip trailing markdown punctuation that got captured with the URL
        trailing = ""
        while url and url[-1] in ("*", "_", "`", "~"):
            trailing = url[-1] + trailing
            url = url[:-1]
        # Wrap bare URL as clickable markdown link
        return f"[{url}]({url}){trailing}"

    return _URL_RE.sub(_replace_bare_url, text)


async def _send(placeholder, update: Update, text: str):
    chunk = 4096
    if not text or not text.strip():
        await placeholder.edit_text("_(empty response)_", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        return
    # Protect URLs so they remain clickable in Markdown mode
    md_text = _protect_urls_for_markdown(text)
    try:
        if len(md_text) <= chunk:
            await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        else:
            await placeholder.edit_text(md_text[:chunk], parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            for i in range(chunk, len(md_text), chunk):
                await update.effective_message.reply_text(md_text[i:i+chunk], parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fallback 1: plain text edit
        try:
            if len(text) <= chunk:
                await placeholder.edit_text(text, reply_markup=None)
            else:
                await placeholder.edit_text(text[:chunk], reply_markup=None)
                for i in range(chunk, len(text), chunk):
                    await update.effective_message.reply_text(text[i:i+chunk])
        except Exception:
            # Fallback 2: send as new message if editing fails entirely
            try:
                await placeholder.edit_text("✅ Done — see reply below.", reply_markup=None)
            except Exception:
                log.debug("placeholder cleanup edit failed", exc_info=True)
            try:
                if len(text) <= chunk:
                    await update.effective_message.reply_text(text)
                else:
                    for i in range(0, len(text), chunk):
                        await update.effective_message.reply_text(text[i:i+chunk])
            except Exception as exc:
                log.error("_send all fallbacks failed: %s", exc)


# ─── Tool name → friendly emoji/label ──────────────────────────────────────────

_TOOL_LABELS = {
    "Bash":       "🖥️ Running command…",
    "Read":       "📖 Reading file…",
    "Write":      "📝 Writing file…",
    "Edit":       "✏️ Editing file…",
    "Grep":       "🔍 Searching code…",
    "ListDir":    "📂 Listing directory…",
    "WebSearch":  "🌐 Searching the web…",
    "WebFetch":   "🌐 Fetching page…",
    "Agent":      "🤖 Delegating to agent…",
    "TodoWrite":  "📋 Planning…",
    "TodoRead":   "📋 Checking plan…",
}

_TOOL_ICONS = {
    "Bash": "🖥️", "Read": "📖", "Write": "📝", "Edit": "✏️",
    "Grep": "🔍", "ListDir": "📂", "WebSearch": "🌐", "WebFetch": "🌐",
    "Agent": "🤖", "TodoWrite": "📋", "TodoRead": "📋",
}


def _tool_label(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, f"🔧 Using {tool_name}…")


# ─── Core ask ───────────────────────────────────────────────────────────────────

# Per-user engine override (cli, sdk, api)
_user_engine: dict[int, str] = {}

ENGINE_MODES = {
    "cli": "CLI (default — claude subprocess)",
    "sdk": "SDK (claude-code-sdk, streaming)",
    "api": "API (Anthropic Messages API)",
}


def _engine(uid: int) -> str:
    return _user_engine.get(uid, cc.CLAUDE_MODE)


def _active_session(uid: int) -> "cc.UserSession":
    return cc._session_mgr.get_or_create_active(PLATFORM, str(uid))


def _select_system_prompt(uid: str, conv_key: str) -> str:
    """Assemble the system prompt: A/B variant base (if the optimizer is on) plus
    the user's learned style hint. Falls back to the static system prompt."""
    base = cc.CLAUDE_SYSTEM
    if PROMPT_OPTIMIZER_ENABLED:
        try:
            from . import prompt_optimizer as po
            base = po.assign_variant(conv_key, baseline_text=cc.CLAUDE_SYSTEM).text
        except Exception:
            log.debug("prompt variant selection failed", exc_info=True)
    try:
        from . import preferences
        hint = preferences.prompt_hint(uid)
    except Exception:
        hint = ""
    return f"{base}\n\n{hint}" if hint else base


async def _ask(uid: int, text: str, tracker: TaskSession | None = None, session: "cc.UserSession | None" = None) -> tuple[str, dict]:
    sess = session or cc._session_mgr.get_or_create_active(PLATFORM, str(uid))
    history = cc.load_history(PLATFORM, str(uid), session_name=sess.name)
    engine = _engine(uid)

    # ── Feature 2: Budget check before calling Claude ──
    budget_msg = await _check_budget(uid)
    if budget_msg and budget_msg.startswith(("Daily budget exceeded", "Monthly budget exceeded")):
        return budget_msg, {}

    # ── Feature 3: Smart model selection ──
    selected_model = _smart_model(uid, text)

    # ── Feature 9: RAG context injection ──
    kb_context = _kb.build_context(PLATFORM, str(uid), text)
    if kb_context:
        text = text + kb_context

    # ── Feature 6: Publish chat event ──
    from .event_bus import get_event_bus, Event, EventTypes
    await get_event_bus().publish_async(Event(
        type=EventTypes.MESSAGE_RECEIVED,
        data={"uid": uid, "text": text[:200], "model": selected_model},
    ))

    # Progress callback via tracker
    async def _on_progress(tool_name: str, detail: str = ""):
        if tracker:
            await tracker.on_tool(tool_name, detail)

    # Text streaming callback
    async def _on_text(text_chunk: str):
        if tracker:
            await tracker.on_text(text_chunk)

    # Cancellation check
    def _is_cancelled() -> bool:
        return tracker.cancelled if tracker else False

    # ── System prompt: A/B variant (0003) + style preferences (0002) ──
    conv_key = f"{uid}:{sess.name}"
    system_prompt = _select_system_prompt(str(uid), conv_key)

    if engine == "api":
        return await cc.ask_claude_api_async(
            text, history, system=system_prompt,
            model=route_model_api(text) if SMART_ROUTING_ENABLED else cc.CLAUDE_API_MODEL,
            on_text=_on_text, is_cancelled=_is_cancelled,
        )

    if engine == "sdk":
        result = await cc.ask_claude_sdk(
            text, history,
            model=selected_model,
            system=system_prompt,
            add_dirs=cc.CLAUDE_ADD_DIRS,
            timeout=cc.CLAUDE_TIMEOUT,
            on_progress=_on_progress,
            on_text=_on_text,
            is_cancelled=_is_cancelled,
        )
        if result[1].get("session_id"):
            sess.claude_session_id = result[1]["session_id"]
            sess.touch()
        return result

    # Default: CLI mode
    cli_sid = sess.claude_session_id if sess.cli_session_valid else ""
    result = await cc.ask_claude_async(
        text, history,
        model=selected_model,
        system=system_prompt,
        add_dirs=cc.CLAUDE_ADD_DIRS,
        perm_mode=_perm(uid),
        timeout=cc.CLAUDE_TIMEOUT,
        on_progress=_on_progress,
        on_text=_on_text,
        is_cancelled=_is_cancelled,
        platform=PLATFORM,
        user_id=str(uid),
        resume_session_id=cli_sid or "",
    )
    if result[1].get("session_id"):
        sess.claude_session_id = result[1]["session_id"]
        sess.touch()
    return result


# ─── Commands ───────────────────────────────────────────────────────────────────

def _bot_username(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """The bot's @handle, or '' when it is not known yet.

    python-telegram-bot fills ``bot.username`` during initialisation, so this
    is populated in a running bot; anything else (a test double, a bot polled
    before initialize) yields '' and the callers degrade gracefully.
    """
    u = getattr(getattr(ctx, "bot", None), "username", None)
    return u.lstrip("@") if isinstance(u, str) and u else ""


def _display_name(update: Update) -> str:
    user = getattr(update, "effective_user", None)
    name = getattr(user, "first_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    handle = getattr(user, "username", None)
    return handle.strip() if isinstance(handle, str) else ""


def _tour_keyboard(index: int) -> InlineKeyboardMarkup:
    from . import onboarding as ob
    if index + 1 >= ob.TOTAL_STEPS:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✓ Done", callback_data="tg:tour:done")],
        ])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Next →", callback_data=f"tg:tour:{index + 1}"),
        InlineKeyboardButton("Skip", callback_data="tg:tour:done"),
    ]])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Welcome, redeem a deep-linked invite, and offer the tour.

    A ``https://t.me/<bot>?start=inv_CODE`` link arrives here with the code in
    ``ctx.args``, so an invited user's very first action grants them access.
    """
    from . import invites, onboarding as ob

    uid = update.effective_user.id
    payload = ctx.args[0] if getattr(ctx, "args", None) else ""
    inviter_name = ""
    source = "direct"

    code = invites.normalize_code(payload) if isinstance(payload, str) else ""
    if code:
        result = invites.get_store().redeem(
            code, PLATFORM, str(uid), display_name=_display_name(update)
        )
        if result.ok:
            source = "invite"
            inviter_name = _known_name(result.invite.created_by) if result.invite else ""
        elif result.reason not in ("already", "self"):
            await update.message.reply_text(result.message)
            return
    elif payload and not _allowed(uid):
        # A start payload that is not a code, from someone with no access.
        await update.message.reply_text("That invite link isn't valid.")
        return

    if not _allowed(uid):
        await update.message.reply_text(
            "This bot is private. Ask whoever runs it for an invite link — "
            f"they'll need your ID: `{uid}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        first_time = ob.get_store().start(PLATFORM, str(uid), source=source)
    except Exception:
        log.debug("onboarding start failed", exc_info=True)
        first_time = False

    text = ob.welcome_text(_display_name(update), first_time=first_time, invited_by=inviter_name)
    if not first_time:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Show me →", callback_data="tg:tour:0"),
            InlineKeyboardButton("No thanks", callback_data="tg:tour:done"),
        ]]),
    )


def _known_name(user_id: str) -> str:
    """Best-effort display name for a Telegram id we have seen before."""
    try:
        from . import invites
        grant = invites.get_store().get_grant(PLATFORM, str(user_id))
        if grant and grant.display_name:
            return grant.display_name
    except Exception:
        log.debug("name lookup failed", exc_info=True)
    return ""


async def cmd_tour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Replay the walkthrough from the beginning."""
    from . import onboarding as ob
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    try:
        ob.get_store().set_step(PLATFORM, str(uid), 0)
    except Exception:
        log.debug("tour reset failed", exc_info=True)
    await update.message.reply_text(
        ob.step_text(0) + f"\n\n{ob.progress_bar(0)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_tour_keyboard(0),
    )


# ─── Invites ──────────────────────────────────────────────────────────────────


def _parse_invite_args(raw: str) -> tuple[int, "float | None", str]:
    """Parse ``/invite [uses] [days] [note]`` into (max_uses, ttl_days, note).

    ``/invite`` alone is one use for seven days. ``unlimited`` and ``never``
    spell out the open-ended forms so nobody has to remember that 0 means
    unlimited.
    """
    from . import invites

    max_uses = invites.DEFAULT_MAX_USES
    ttl_days: float | None = invites.DEFAULT_TTL_DAYS
    tokens = (raw or "").split()
    idx = 0

    if idx < len(tokens):
        tok = tokens[idx].lower()
        if tok in ("unlimited", "many", "open"):
            max_uses, idx = 0, idx + 1
        elif tok.isdigit():
            max_uses, idx = int(tok), idx + 1

    if idx < len(tokens):
        tok = tokens[idx].lower().rstrip("d")
        if tokens[idx].lower() in ("never", "forever"):
            ttl_days, idx = None, idx + 1
        elif tok.isdigit():
            ttl_days, idx = float(tok), idx + 1

    return max_uses, ttl_days, " ".join(tokens[idx:]).strip()


async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mint a one-tap invite link."""
    from . import invites, onboarding as ob

    uid = update.effective_user.id
    if not _allowed(uid):
        return

    store = invites.get_store()
    if not store.can_invite(PLATFORM, str(uid), is_operator=_is_operator(uid)):
        await update.message.reply_text(
            "Only whoever runs this bot can create invites.\n\n"
            "They can set `INVITE_ALLOW_CHAINING=true` to let invited users "
            "invite others too.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    raw = (update.message.text or "").split(None, 1)
    max_uses, ttl_days, note = _parse_invite_args(raw[1] if len(raw) > 1 else "")

    try:
        invite = store.create(
            PLATFORM, str(uid), max_uses=max_uses, ttl_days=ttl_days, note=note
        )
    except Exception as e:
        log.warning("invite creation failed: %s", e)
        await update.message.reply_text("Couldn't create an invite just now.")
        return

    username = _bot_username(ctx)
    link = invites.deep_link(username, invite.code) if username else ""
    hours = None
    if invite.expires_at:
        hours = max(0, int((invite.expires_at - time.time()) // 3600))

    if link:
        # Sent without a parse mode on purpose: the link carries underscores
        # (``..._bot`` and the ``inv_`` prefix) that legacy Markdown would
        # italicise or choke on. Telegram auto-links a bare URL regardless.
        body = ob.invite_pitch(link, uses_left=invite.remaining(), expires_hours=hours)
        await update.message.reply_text(body, disable_web_page_preview=True)
        return

    # No username yet — the code still works via `/start <code>`.
    body = (
        f"Invite code: `{invite.code}`\n\n"
        "Whoever you send it to redeems it with `/start " + invite.code + "`."
    )
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def cmd_invites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List your invites and who came in through them."""
    from . import invites

    uid = update.effective_user.id
    if not _allowed(uid):
        return

    store = invites.get_store()
    mine = store.list_created_by(PLATFORM, str(uid))
    stats = store.stats(PLATFORM, str(uid))

    if not mine and not stats.direct:
        await update.message.reply_text(
            "You haven't invited anyone yet. `/invite` makes a link.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    now = time.time()
    lines = [invites.summarize(stats), ""]
    if mine:
        lines.append("*Your invites*")
        lines += [invites.format_invite_line(i, now=now) for i in mine[:15]]
        if len(mine) > 15:
            lines.append(f"…and {len(mine) - 15} more")
        lines.append("")
    lines.append("`/revoke <code>` kills a link. Already-admitted users keep access.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Revoke an invite code you created."""
    from . import invites

    uid = update.effective_user.id
    if not _allowed(uid):
        return

    parts = (update.message.text or "").split(None, 1)
    code = invites.normalize_code(parts[1]) if len(parts) > 1 else ""
    if not code:
        await update.message.reply_text(
            "Usage: `/revoke <code>` — `/invites` lists yours.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    store = invites.get_store()
    # Operators can revoke any code; everyone else only their own.
    requester = None if _is_operator(uid) else str(uid)
    if store.revoke(code, requester=requester):
        await update.message.reply_text(
            f"Revoked `{code}`. Anyone already admitted keeps their access — "
            "use `/kick <id>` to remove someone.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("No such invite of yours.")


async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove an invited user's access. Operators only."""
    from . import invites

    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not _is_operator(uid):
        await update.message.reply_text("Only whoever runs this bot can do that.")
        return

    parts = (update.message.text or "").split(None, 1)
    target = parts[1].strip() if len(parts) > 1 else ""
    if not target.isdigit():
        await update.message.reply_text(
            "Usage: `/kick <telegram user id>`", parse_mode=ParseMode.MARKDOWN
        )
        return

    if invites.get_store().revoke_access(PLATFORM, target):
        await update.message.reply_text(f"Removed access for `{target}`.",
                                        parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "That user wasn't invited — if they're in "
            "`TELEGRAM_ALLOWED_USER_IDS`, remove them there.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Group behaviour ──────────────────────────────────────────────────────────


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    sess = _active_session(int(uid))
    cc.clear_history(PLATFORM, uid, session_name=sess.name)
    cc.clear_session(PLATFORM, uid)
    sess.claude_session_id = None
    sess.message_count = 0
    await update.message.reply_text(f"History cleared for session `{sess.name}`.", parse_mode="Markdown")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = _active_session(uid)
    info = (
        f"Platform: `telegram`\n"
        f"Session: `{sess.name}` {sess.status_emoji()}\n"
        f"Engine: `{_engine(uid)}`\n"
        f"Model: `{_model(uid)}`\n"
        f"Permission mode: `{_perm(uid) or 'default'}`\n"
        f"Verbose: `{_verbose(uid)}`\n"
        f"Timeout: `{cc.CLAUDE_TIMEOUT}s`"
    )
    await update.message.reply_text(info, parse_mode="Markdown")


async def cmd_engine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Switch between CLI, SDK, and API execution engines."""
    uid = update.effective_user.id
    cur = _engine(uid)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:engine:{k}")]
        for k, lbl in ENGINE_MODES.items()
    ]
    await update.message.reply_text(
        f"Current engine: *{cur}*\n\nSelect execution engine:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show active tasks for this user."""
    uid = update.effective_user.id
    tasks = _task_registry.get_user_tasks(uid)

    if not tasks:
        await update.message.reply_text("No active tasks.")
        return

    lines = [f"*Active tasks ({len(tasks)}):*\n"]
    for t in tasks:
        lines.append(f"  `#{t.task_id}` — {t.prompt_preview}… ({t._elapsed()})")

    btns = []
    for t in tasks:
        btns.append([InlineKeyboardButton(
            f"⏹ Cancel #{t.task_id}", callback_data=f"tg:cancel:{t.task_id}"
        )])
    btns.append([InlineKeyboardButton("⏹ Cancel All", callback_data=f"tg:cancelall:{uid}")])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel a specific task or all tasks. Usage: /cancel [task_id|all]"""
    uid = update.effective_user.id
    args = ctx.args

    if args and args[0].lower() == "all":
        count = _task_registry.cancel_all_user(uid)
        await update.message.reply_text(f"⏹ Cancelled {count} task(s).")
        return

    if args:
        try:
            task_id = int(args[0].lstrip("#"))
            task = _task_registry.get(task_id)
            if task and task.uid == uid:
                task.cancel()
                await update.message.reply_text(f"⏹ Cancelling task `#{task_id}`.", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"Task `#{task_id}` not found.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Usage: `/cancel <task_id>` or `/cancel all`", parse_mode="Markdown")
        return

    # No args — show active tasks with cancel buttons
    tasks = _task_registry.get_user_tasks(uid)
    if not tasks:
        await update.message.reply_text("No active tasks to cancel.")
        return

    btns = []
    for t in tasks:
        btns.append([InlineKeyboardButton(
            f"⏹ #{t.task_id} — {t.prompt_preview[:20]}…", callback_data=f"tg:cancel:{t.task_id}"
        )])
    btns.append([InlineKeyboardButton("⏹ Cancel All", callback_data=f"tg:cancelall:{uid}")])
    await update.message.reply_text(
        f"*{len(tasks)} active task(s).* Which to cancel?",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Session management ────────────────────────────────────────────────────────

async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all sessions for this user with status indicators."""
    uid = update.effective_user.id
    show_archived = ctx.args and ctx.args[0] == "all"
    sessions = cc._session_mgr.get_all(PLATFORM, str(uid), include_archived=show_archived)
    if not sessions:
        sessions = [cc._session_mgr.get_or_create_active(PLATFORM, str(uid))]

    # Auto-archive idle sessions
    archived = cc._session_mgr.auto_archive_idle(PLATFORM, str(uid))

    active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))

    lines = ["*Your sessions:*\n"]
    btns = []
    for i, s in enumerate(sessions):
        marker = " ← active" if i == active_idx else ""
        lines.append(f"{s.summary_line()}{marker}")
        if i != active_idx and not s.archived:
            btns.append([InlineKeyboardButton(
                f"Switch to: {s.display_name}", callback_data=f"tg:sess:sw:{i}"
            )])

    if archived:
        lines.append(f"\n_Auto-archived {len(archived)} idle session(s)._")

    btns.append([InlineKeyboardButton("➕ New session", callback_data="tg:sess:new:_")])
    row2 = []
    if len(sessions) > 1:
        row2.append(InlineKeyboardButton("🗑 Delete…", callback_data="tg:sess:delmenu:_"))
    if any(s.archived for s in cc._session_mgr.get_all(PLATFORM, str(uid), include_archived=True)):
        row2.append(InlineKeyboardButton("📦 Archived…", callback_data="tg:sess:arcmenu:_"))
    if row2:
        btns.append(row2)

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create a new named session. Usage: /new <name>"""
    uid = update.effective_user.id
    name = " ".join(ctx.args).strip() if ctx.args else f"session-{int(time.time()) % 10000}"
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:20].strip("-")
    if not name:
        name = f"session-{int(time.time()) % 10000}"

    sess = cc._session_mgr.create(PLATFORM, str(uid), name)
    await update.message.reply_text(
        f"✅ Created and switched to session `{sess.name}`",
        parse_mode="Markdown",
    )


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Switch to another session by name or index."""
    uid = update.effective_user.id
    sessions = cc._session_mgr.get_all(PLATFORM, str(uid))

    if not sessions or len(sessions) <= 1:
        await update.message.reply_text("Only one session exists. Use /new to create more.")
        return

    if ctx.args:
        target = ctx.args[0].strip()
        # Try by name first, then by index
        s = cc._session_mgr.switch_to_name(PLATFORM, str(uid), target)
        if not s:
            try:
                idx = int(target)
                s = cc._session_mgr.switch_to(PLATFORM, str(uid), idx)
            except ValueError:
                pass
        if s:
            await update.message.reply_text(
                f"✅ Switched to `{s.display_name}`", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"Session `{target}` not found.", parse_mode="Markdown")
        return

    active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))
    btns = []
    for i, s in enumerate(sessions):
        if i == active_idx:
            continue
        btns.append([InlineKeyboardButton(
            f"{s.status_emoji()} {s.display_name} ({s.message_count} msgs)",
            callback_data=f"tg:sess:sw:{i}",
        )])

    await update.message.reply_text(
        "Switch to which session?",
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def cmd_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rename the current session. Usage: /rename <new-name>"""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /rename <new-name>")
        return
    new_name = re.sub(r"[^a-zA-Z0-9_-]", "-", " ".join(ctx.args))[:20].strip("-")
    if not new_name:
        await update.message.reply_text("❌ Name must contain at least one letter, digit, `_`, or `-`.", parse_mode="Markdown")
        return
    sess = _active_session(uid)
    result = cc._session_mgr.rename(PLATFORM, str(uid), sess.name, new_name)
    if result:
        await update.message.reply_text(f"✅ Session renamed to `{result.name}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Rename failed — name may already be taken.")


async def cmd_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set a title/description for the current session. Usage: /title <text>"""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /title <description>")
        return
    title = " ".join(ctx.args)
    sess = _active_session(uid)
    result = cc._session_mgr.set_title(PLATFORM, str(uid), sess.name, title)
    if result:
        await update.message.reply_text(f"✅ Title set: _{result.title}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Failed to set title.")


async def cmd_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pin/unpin the current session. Pinned sessions won't be auto-archived."""
    uid = update.effective_user.id
    sess = _active_session(uid)
    new_state = not sess.pinned
    result = cc._session_mgr.pin(PLATFORM, str(uid), sess.name, new_state)
    if result:
        emoji = "📌" if result.pinned else "📌❌"
        await update.message.reply_text(f"{emoji} Session `{result.name}` {'pinned' if result.pinned else 'unpinned'}.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Failed.")


async def cmd_archive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Archive the current or named session."""
    uid = update.effective_user.id
    if ctx.args:
        name = ctx.args[0]
    else:
        name = _active_session(uid).name
    result = cc._session_mgr.archive(PLATFORM, str(uid), name)
    if result:
        await update.message.reply_text(f"📦 Archived session `{result.name}`. Use /sessions all to see archived.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Cannot archive (busy or not found).")


async def cmd_search_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Search sessions by name, title, or content."""
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /searchsess <query>")
        return
    query = " ".join(ctx.args)
    results = cc._session_mgr.search(PLATFORM, str(uid), query)
    if not results:
        await update.message.reply_text("🔍 No sessions found.")
        return
    lines = [f"🔍 *Found {len(results)} session(s):*\n"]
    for s in results:
        lines.append(s.summary_line())
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Folder browser ─────────────────────────────────────────────────────────────

BROWSE_ROOT = Path(cc.CLAUDE_WORK_DIR)
BROWSE_PAGE_SIZE = 8

# Path registry: maps short IDs to absolute paths (per-session, in-memory)
_path_registry: dict[str, Path] = {}
_path_reverse: dict[Path, str] = {}
_path_counter = itertools.count(1)
_PATH_REGISTRY_MAX = 5000


def _pid(path: Path) -> str:
    """Register a path and return a short ID for use in callback_data."""
    existing = _path_reverse.get(path)
    if existing is not None:
        return existing
    if len(_path_registry) >= _PATH_REGISTRY_MAX:
        oldest_key = next(iter(_path_registry))
        oldest_path = _path_registry.pop(oldest_key)
        _path_reverse.pop(oldest_path, None)
    pid = f"p{next(_path_counter)}"
    _path_registry[pid] = path
    _path_reverse[path] = pid
    return pid


def _resolve_pid(pid: str) -> Path | None:
    """Look up a path by its short ID."""
    return _path_registry.get(pid)


def _browse_buttons(directory: Path, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Build message text and inline buttons for a directory listing."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        parent_id = _pid(directory.parent)
        return "⛔ Permission denied.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ ..", callback_data=f"tg:br:{parent_id}:0")]
        ])

    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    all_items = dirs + files

    total_pages = max(1, (len(all_items) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    page = min(page, total_pages - 1)
    start = page * BROWSE_PAGE_SIZE
    page_items = all_items[start:start + BROWSE_PAGE_SIZE]

    rel = directory.relative_to(BROWSE_ROOT) if directory != BROWSE_ROOT else Path(".")
    header = f"📂 `{rel}/`\n_{len(dirs)} folders, {len(files)} files_"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"

    dir_id = _pid(directory)
    btns = []
    for item in page_items:
        item_id = _pid(item)
        if item.is_dir():
            label = f"📁 {item.name}/"
            cb = f"tg:br:{item_id}:0"
        else:
            size = item.stat().st_size
            if size < 1024:
                sz = f"{size}B"
            elif size < 1024 * 1024:
                sz = f"{size // 1024}KB"
            else:
                sz = f"{size // (1024*1024)}MB"
            label = f"📄 {item.name} ({sz})"
            cb = f"tg:bf:{item_id}"
        btns.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if directory != BROWSE_ROOT:
        parent_id = _pid(directory.parent)
        nav.append(InlineKeyboardButton("⬆️ ..", callback_data=f"tg:br:{parent_id}:0"))
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tg:br:{dir_id}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tg:br:{dir_id}:{page+1}"))
    if nav:
        btns.append(nav)

    btns.append([InlineKeyboardButton(
        "🤖 Ask Claude about this folder",
        callback_data=f"tg:ba:{dir_id}"
    )])

    return header, InlineKeyboardMarkup(btns)


async def cmd_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Browse project folders interactively."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return

    args_text = " ".join(ctx.args) if ctx.args else ""
    if args_text:
        target = (BROWSE_ROOT / args_text).resolve()
        # Reject path traversal — target must stay inside BROWSE_ROOT
        try:
            target.relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await update.message.reply_text("⛔ Access denied.")
            return
        if not target.exists() or not target.is_dir():
            await update.message.reply_text(f"Directory not found: `{args_text}`", parse_mode="Markdown")
            return
    else:
        target = BROWSE_ROOT

    header, markup = _browse_buttons(target)
    await update.message.reply_text(header, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def _handle_browse_callback(q, uid: int):
    """Handle br/bf/bv/ba callbacks for folder browsing."""
    data = q.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    kind = parts[1]

    if kind == "br":
        # tg:br:<path_id>:<page>
        if len(parts) < 4:
            return
        pid, page = parts[2], int(parts[3])
        dir_path = _resolve_pid(pid)
        if not dir_path or not dir_path.is_dir():
            await q.edit_message_text("Directory no longer exists.")
            return
        try:
            dir_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        header, markup = _browse_buttons(dir_path, page)
        await q.edit_message_text(header, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    elif kind == "bf":
        # tg:bf:<path_id>
        pid = parts[2]
        file_path = _resolve_pid(pid)
        if not file_path or not file_path.is_file():
            await q.edit_message_text("File no longer exists.")
            return
        try:
            file_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        stat = file_path.stat()
        rel = file_path.relative_to(BROWSE_ROOT)
        info = f"📄 `{rel}`\nSize: {stat.st_size:,} bytes"

        parent_id = _pid(file_path.parent)
        btns = [
            [InlineKeyboardButton("📖 View (first 50 lines)", callback_data=f"tg:bv:{pid}")],
            [InlineKeyboardButton("🤖 Ask Claude about file", callback_data=f"tg:ba:{pid}")],
            [InlineKeyboardButton("⬆️ Back", callback_data=f"tg:br:{parent_id}:0")],
        ]
        await q.edit_message_text(info, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)

    elif kind == "bv":
        # tg:bv:<path_id>
        pid = parts[2]
        file_path = _resolve_pid(pid)
        if not file_path or not file_path.is_file():
            await q.edit_message_text("File no longer exists.")
            return
        try:
            file_path.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        try:
            stat = file_path.stat()
            if stat.st_size > 1_000_000:
                text = f"📄 `{file_path.relative_to(BROWSE_ROOT)}` is too large ({stat.st_size:,} bytes) to preview."
            else:
                lines = file_path.read_text(errors="replace").splitlines()[:50]
                content = "\n".join(lines)
                if len(content) > 3500:
                    content = content[:3500] + "\n…(truncated)"
                rel = file_path.relative_to(BROWSE_ROOT)
                text = f"📄 `{rel}` (first 50 lines):\n```\n{content}\n```"
        except Exception as e:
            text = f"Error reading file: {e}"

        parent_id = _pid(file_path.parent)
        btns = [
            [InlineKeyboardButton("🤖 Ask Claude about file", callback_data=f"tg:ba:{pid}")],
            [InlineKeyboardButton("⬆️ Back", callback_data=f"tg:br:{parent_id}:0")],
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)

    elif kind == "ba":
        # tg:ba:<path_id>
        pid = parts[2]
        target = _resolve_pid(pid)
        if not target or not target.exists():
            await q.edit_message_text("Path no longer exists.")
            return
        try:
            target.resolve().relative_to(BROWSE_ROOT.resolve())
        except ValueError:
            await q.edit_message_text("⛔ Access denied.")
            return

        rel = target.relative_to(BROWSE_ROOT)
        if target.is_dir():
            prompt = f"List and briefly describe the contents of the directory: {rel}/"
        else:
            prompt = f"Read and summarize this file: {rel}"

        await q.edit_message_text(f"🤖 Asking Claude about `{rel}`…", reply_markup=None, parse_mode=ParseMode.MARKDOWN)

        task = TaskSession(q.message, uid, f"[Browse] {rel}")
        task.start_heartbeat()
        _task_registry.register(task)
        try:
            reply, stats = await _ask(uid, prompt, tracker=task)
            browse_sess = _active_session(uid)
            cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=browse_sess.name)
            cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))

            back_dir = target if target.is_dir() else target.parent
            back_id = _pid(back_dir)
            btns = [[InlineKeyboardButton("⬆️ Back to browser", callback_data=f"tg:br:{back_id}:0")]]
            md_text = _protect_urls_for_markdown(reply)
            if len(md_text) > 4000:
                md_text = md_text[:4000] + "\n…(truncated)"
            try:
                await q.message.edit_text(md_text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await q.message.edit_text(reply[:4000], reply_markup=InlineKeyboardMarkup(btns))
        except Exception as exc:
            await q.message.edit_text(f"Error: {exc}", reply_markup=None)
        finally:
            await task.stop()
            _task_registry.unregister(task.task_id)


def _parse_remember_args(text: str) -> tuple[str, list[str], float]:
    """Parse tags (#tag) and importance (!0.9) from remember text."""
    tags = []
    importance = 0.5
    words = []
    for word in text.split():
        if word.startswith("#") and len(word) > 1:
            tags.append(word[1:].lower())
        elif word.startswith("!") and len(word) > 1:
            try:
                importance = float(word[1:])
            except ValueError:
                words.append(word)
        else:
            words.append(word)
    return " ".join(words), tags, importance


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /remember <text> [#tag1 #tag2] [!0.9]\n"
            "Example: /remember I prefer dark mode #preference !0.8"
        )
        return
    content, tags, importance = _parse_remember_args(text)
    if not content:
        await update.message.reply_text("Memory content can't be empty.")
        return
    mem = _memory.remember(PLATFORM, uid, content, tags=tags or None, importance=importance)
    tag_str = f"  Tags: {', '.join(mem.tags)}" if mem.tags else ""
    imp_str = f"  Importance: {mem.importance}" if mem.importance != 0.5 else ""
    await update.message.reply_text(
        f"✅ Remembered!\n_ID: `{mem.id[:8]}…`_{tag_str}{imp_str}",
        parse_mode="Markdown",
    )


async def cmd_recall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Usage: /recall <search query>")
        return
    results = _memory.recall(PLATFORM, uid, query, limit=5)
    if not results:
        await update.message.reply_text("🔍 No memories found.")
        return
    lines = [f"🔍 *Found {len(results)} memor{'y' if len(results) == 1 else 'ies'}:*\n"]
    for r in results:
        lines.append(f"• {r.content}\n  _ID: `{r.id[:8]}…`_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    filter_tags = None
    if ctx.args:
        filter_tags = [a.lstrip("#") for a in ctx.args if a.startswith("#")]
    mems = _memory.list_memories(PLATFORM, uid, limit=10, tags=filter_tags or None)
    if not mems:
        await update.message.reply_text("📭 No memories yet. Use /remember <text> to save one.")
        return
    stats = _memory.stats(PLATFORM, uid)
    tag_label = f" (tag: {', '.join(filter_tags)})" if filter_tags else ""
    lines = [f"🧠 *Your memories* ({stats['total']} total){tag_label}:\n"]
    for m in mems:
        tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(f"• {m.content}{tag_str}\n  _ID: `{m.id[:8]}…`_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    target_id = ctx.args[0].rstrip("…") if ctx.args else ""
    if not target_id:
        await update.message.reply_text("Usage: /forget <memory-id>\n_Use /memories to see IDs_")
        return
    mems = _memory.list_memories(PLATFORM, uid, limit=100)
    match = next((m for m in mems if m.id.startswith(target_id)), None)
    if match and _memory.forget(PLATFORM, uid, match.id):
        await update.message.reply_text(f"🗑️ Forgotten: _{match.content[:60]}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Memory not found. Use /memories to see your memories.")


async def cmd_editmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Usage: /editmem <id> <new text> [#tag1] [!0.9]")
        return
    target_id = ctx.args[0].rstrip("…")
    rest = " ".join(ctx.args[1:])
    content, tags, importance = _parse_remember_args(rest)

    mems = _memory.list_memories(PLATFORM, uid, limit=100)
    match = next((m for m in mems if m.id.startswith(target_id)), None)
    if not match:
        await update.message.reply_text("❌ Memory not found. Use /memories to see IDs.")
        return

    updated = _memory.update(
        PLATFORM, uid, match.id,
        content=content or None,
        tags=tags or None,
        importance=importance if importance != 0.5 else None,
    )
    if updated:
        await update.message.reply_text(f"✏️ Updated: _{updated.content[:80]}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Update failed.")


async def cmd_exportmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = _memory.export_all(PLATFORM, uid)
    if not data:
        await update.message.reply_text("📭 No memories to export.")
        return
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, prefix="memories_") as f:
        json.dump({"memories": data, "platform": PLATFORM, "user_id": uid}, f, indent=2)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                filename=f"memories_{uid}.json",
                caption=f"📦 Exported {len(data)} memories.",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def cmd_importmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "Usage: Reply to a JSON file with /importmem\n"
            "The file should have a `memories` array (from /exportmem)."
        )
        return
    try:
        file = await ctx.bot.get_file(reply.document.file_id)
        raw = await file.download_as_bytearray()
        payload = json.loads(raw.decode())
        entries = payload.get("memories", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            await update.message.reply_text("❌ Invalid format — expected a JSON array or {memories: [...]}.")
            return
        result = _memory.import_all(PLATFORM, uid, entries)
        await update.message.reply_text(
            f"📥 Imported {result['imported']} memories"
            + (f" ({result['skipped']} skipped)" if result["skipped"] else "")
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {e}")


async def cmd_extractmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    sess = _active_session(int(uid))
    history = cc.get_history(PLATFORM, uid, session_name=sess.name)
    if not history:
        await update.message.reply_text("No conversation history to extract from.")
        return
    text_parts = []
    for msg in history[-20:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            text_parts.append(f"{role}: {content}")
    if not text_parts:
        await update.message.reply_text("No text messages found in recent history.")
        return
    await update.message.reply_text("🔍 Extracting memories from recent conversation...")
    extracted = await extract_memories("\n".join(text_parts))
    if not extracted:
        await update.message.reply_text("No memorable facts found.")
        return
    saved = []
    for entry in extracted:
        mem = _memory.remember(
            PLATFORM, uid, entry["content"],
            tags=entry.get("tags"),
            importance=entry.get("importance", 0.5),
            metadata={"source": "auto-extract"},
        )
        saved.append(mem)
    lines = [f"🧠 *Extracted {len(saved)} memories:*\n"]
    for m in saved:
        tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(f"• {m.content}{tag_str}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = cc.get_usage(PLATFORM, str(uid))
    await update.message.reply_text(
        f"*Usage stats (Telegram)*\n\n"
        f"Messages: `{u['messages']}`\n"
        f"Input tokens: `{u['input']:,}`\n"
        f"Output tokens: `{u['output']:,}`",
        parse_mode="Markdown",
    )


def _evaluate_quality(uid: str, user_text: str, reply: str, stats: dict, conv_key: str = "") -> None:
    """Score a completed response: binary checks inline, sampled LLM judge
    off-thread, and (when the optimizer is on) attribute the score to the active
    prompt variant. Never raises into the chat path."""
    composite = None
    try:
        from . import feedback
        scores = feedback.evaluate_response(user_text, reply, stats)
        composite = scores["composite"]
        feedback.save_quality_score(PLATFORM, uid, "composite", composite, reply[:200])
    except Exception:
        log.debug("binary quality eval failed", exc_info=True)
    # Attribute the binary composite to the conversation's prompt variant.
    if PROMPT_OPTIMIZER_ENABLED and conv_key and composite is not None:
        try:
            from . import prompt_optimizer as po
            variant = po.assign_variant(conv_key, baseline_text=cc.CLAUDE_SYSTEM)
            po.record_score(variant.id, composite)
            po.maybe_promote()
        except Exception:
            log.debug("variant scoring failed", exc_info=True)
    try:
        from . import evaluator
        if evaluator.JUDGE_SAMPLE_RATE > 0:
            # The judge may call the network — keep it off the event loop.
            threading.Thread(
                target=evaluator.maybe_judge,
                args=(PLATFORM, uid, user_text, reply),
                daemon=True,
            ).start()
    except Exception:
        log.debug("judge dispatch failed", exc_info=True)


async def cmd_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    from . import feedback, evaluator
    binary = feedback.get_quality_trend(PLATFORM, uid, "composite", 50)
    lines = ["*Response quality (Telegram)*", ""]
    if binary:
        lines.append(f"Binary composite (last {len(binary)}): `{round(sum(binary) / len(binary), 2)}`")
    else:
        lines.append("Binary composite: _no samples yet_")
    judged = evaluator.get_judge_averages(PLATFORM, uid)
    lines.append("")
    if judged:
        lines.append("*LLM-as-judge averages (1.0 = best):*")
        lines.append(f"Helpfulness: `{judged['judge_helpfulness']}`")
        lines.append(f"Accuracy: `{judged['judge_accuracy']}`")
        lines.append(f"Tone: `{judged['judge_tone']}`")
        lines.append(f"Composite: `{judged['judge_composite']}`")
    else:
        lines.append(f"LLM-as-judge: _no judged samples yet_ (sampling {evaluator.JUDGE_SAMPLE_RATE:.0%})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_prefer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/prefer <dimension> <value>` — set an explicit style preference."""
    from . import preferences
    uid = str(update.effective_user.id)
    arg = " ".join(ctx.args) if ctx.args else ""
    parsed = preferences.parse_prefer_command(arg)
    if not parsed:
        dims = "\n".join(f"  `{d}`: {', '.join(vs)}" for d, vs in preferences.DIMENSIONS.items())
        await update.message.reply_text(
            "Usage: `/prefer <dimension> <value>`\n\n*Dimensions:*\n" + dims +
            "\n\nExample: `/prefer length short`",
            parse_mode="Markdown",
        )
        return
    dimension, value = parsed
    # Explicit command is a strong signal — weight it so one /prefer takes effect.
    preferences.record_signal(uid, dimension, value, weight=3.0)
    await update.message.reply_text(
        f"Got it — I'll prefer *{value}* for *{dimension}*.", parse_mode="Markdown"
    )


async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/feedback <text>` — store feedback and mine it for style preferences."""
    from . import feedback, preferences
    uid = str(update.effective_user.id)
    text = " ".join(ctx.args) if ctx.args else ""
    if not text.strip():
        await update.message.reply_text(
            "Usage: `/feedback <your feedback>`\n"
            "e.g. `/feedback please be shorter and use code blocks`",
            parse_mode="Markdown",
        )
        return
    feedback.save_feedback(PLATFORM, uid, text_feedback=text)
    recorded = preferences.record_text_feedback(uid, text)
    if recorded:
        noted = ", ".join(f"{d}→{v}" for d, v in recorded)
        await update.message.reply_text(f"Thanks — noted your preferences ({noted}).")
    else:
        await update.message.reply_text("Thanks for the feedback — logged it.")


async def cmd_prompts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/prompts` — list A/B prompt variants and their scores.

    Subcommands:
      `/prompts add <text>`       — add a candidate variant
      `/prompts promote <id>`     — force a variant to baseline
    """
    from . import prompt_optimizer as po
    args = ctx.args or []
    if args and args[0] == "add" and len(args) > 1:
        vid = po.add_variant(" ".join(args[1:]))
        await update.message.reply_text(f"Added variant `{vid}`.", parse_mode="Markdown")
        return
    if args and args[0] == "promote" and len(args) > 1:
        ok = po.force_promote(args[1])
        await update.message.reply_text(
            f"Promoted `{args[1]}` to baseline." if ok else f"Unknown variant `{args[1]}`.",
            parse_mode="Markdown",
        )
        return
    variants = po.list_variants()
    if not variants:
        await update.message.reply_text(
            "No prompt variants yet. "
            + ("Optimizer is ON." if PROMPT_OPTIMIZER_ENABLED else "Optimizer is OFF (set `PROMPT_OPTIMIZER_ENABLED=1`).")
        )
        return
    lines = [f"*Prompt variants* (optimizer {'ON' if PROMPT_OPTIMIZER_ENABLED else 'OFF'})", ""]
    for v in variants:
        s = po.variant_stats(v.id)
        lines.append(
            f"`{v.id}` [{v.status}] w={v.traffic_weight:g} "
            f"avg={s['avg']} n={s['count']}\n  _{v.text[:60]}_"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_watchdog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show watchdog status and recent auto-fixes."""
    state_file = Path(cc.CLAUDE_WORK_DIR) / "projects/telechat/.watchdog_state.json"
    if not state_file.exists():
        state_file = Path(__file__).parent / ".watchdog_state.json"
    if not state_file.exists():
        await update.message.reply_text("Watchdog state not found. Is the watchdog service running?")
        return

    try:
        data = json.loads(state_file.read_text())
    except Exception:
        await update.message.reply_text("Could not read watchdog state.")
        return

    fixes = data.get("fix_attempts", [])
    cooldowns = data.get("cooldowns", {})
    fixes_hour = data.get("fixes_this_hour", [])

    # Recent fixes (last 5)
    recent = fixes[-5:] if fixes else []
    lines = ["*🔧 Watchdog Status*\n"]

    now = time.time()
    active_cooldowns = sum(1 for t in cooldowns.values() if now - t < 1800)
    hour_count = sum(1 for t in fixes_hour if now - t < 3600)
    lines.append(f"Fixes this hour: `{hour_count}/3`")
    lines.append(f"Active cooldowns: `{active_cooldowns}`")
    lines.append(f"Total fixes: `{len(fixes)}`\n")

    if recent:
        lines.append("*Recent fixes:*")
        for f in reversed(recent):
            age = int(now - f["timestamp"])
            if age < 60:
                ago = f"{age}s ago"
            elif age < 3600:
                ago = f"{age // 60}m ago"
            else:
                ago = f"{age // 3600}h ago"

            status = "✅" if f.get("success") else "↩️" if f.get("reverted") else "❌"
            desc = f.get("description", "")[:60]
            lines.append(f"  {status} `{f['fingerprint'][:8]}` — {ago}")
            if desc:
                lines.append(f"     _{desc}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if cc.CLAUDE_MODE == "api":
        await update.message.reply_text(f"API mode uses model `{cc.CLAUDE_API_MODEL}`.", parse_mode="Markdown")
        return
    cur = _model(update.effective_user.id)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:model:{k}")]
        for k, lbl in CLI_MODELS.items()
    ]
    await update.message.reply_text(
        f"Current model: *{cur}*\n\nSelect a model:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_permissions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if cc.CLAUDE_MODE == "api":
        await update.message.reply_text("Permissions only apply in CLI mode.")
        return
    cur = _perm(update.effective_user.id) or "default"
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:perm:{k}")]
        for k, lbl in PERMISSION_MODES.items()
    ]
    await update.message.reply_text(
        f"Current permission mode: *{cur}*",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = ctx.args
    if args:
        if args[0] in ("0", "1", "2"):
            _user_verbose[uid] = int(args[0])
            await update.message.reply_text(f"Verbosity set to {args[0]}.")
            return
        await update.message.reply_text("Invalid level. Use 0, 1, or 2.")
        return
    cur = _verbose(uid)
    btns = [
        [InlineKeyboardButton(f"{lbl}{' ✓' if lvl==cur else ''}", callback_data=f"tg:verbose:{lvl}")]
        for lvl, lbl in VERBOSE_LEVELS.items()
    ]
    await update.message.reply_text(
        f"Current verbosity: *{cur}*",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown",
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    await update.message.reply_text(
        _build_settings_text(uid),
        reply_markup=_build_settings_markup(uid),
        parse_mode="Markdown",
    )


# ─── Callback buttons ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not _allowed(uid):
        await q.answer()
        return

    # Claude Desktop bridge gets first refusal — its callback_data uses "bridge:" prefix.
    try:
        from . import desktop_bridge
        if await desktop_bridge.try_handle_callback(update, ctx):
            return
    except Exception as _bridge_exc:
        log.warning("desktop_bridge callback failed: %s", _bridge_exc)

    await q.answer()
    data = q.data  # e.g. "tg:model:sonnet"
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    _, kind, value = parts

    # Handle cancel button (value is task_id)
    if kind == "cancel":
        try:
            task_id = int(value)
        except ValueError:
            return
        task = _task_registry.get(task_id)
        if task and task.uid == uid:
            task.cancel()
            await q.edit_message_text("⏹ Cancelling…", reply_markup=None)
        return

    # Onboarding tour: value is the step index, or "done"
    if kind == "tour":
        from . import onboarding as ob
        if value == "done":
            try:
                ob.get_store().complete(PLATFORM, str(uid))
            except Exception:
                log.debug("tour completion failed", exc_info=True)
            await q.edit_message_text(ob.finish_text(), parse_mode=ParseMode.MARKDOWN)
            return
        try:
            idx = int(value)
        except ValueError:
            return
        step = ob.tour_step(idx)
        if step is None:
            await q.edit_message_text(ob.finish_text(), parse_mode=ParseMode.MARKDOWN)
            return
        try:
            ob.get_store().set_step(PLATFORM, str(uid), idx)
        except Exception:
            log.debug("tour step save failed", exc_info=True)
        await q.edit_message_text(
            ob.step_text(idx) + f"\n\n{ob.progress_bar(idx)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_tour_keyboard(idx),
        )
        return

    # Handle cancel all
    if kind == "cancelall":
        count = _task_registry.cancel_all_user(uid)
        await q.edit_message_text(f"⏹ Cancelling {count} task(s)…", reply_markup=None)
        return

    # Handle session management callbacks
    if kind == "sess":
        # value format: "action:param" e.g. "sw:2", "del:1", "new:_", "delmenu:_"
        sess_parts = value.split(":", 1)
        action = sess_parts[0]
        param = sess_parts[1] if len(sess_parts) > 1 else ""

        if action == "sw":
            idx = int(param)
            s = cc._session_mgr.switch_to(PLATFORM, str(uid), idx)
            if s:
                await q.edit_message_text(f"✅ Switched to `{s.display_name}`", parse_mode="Markdown")
            else:
                await q.edit_message_text("Session not found.")
        elif action == "new":
            name = f"session-{int(time.time()) % 10000}"
            s = cc._session_mgr.create(PLATFORM, str(uid), name)
            await q.edit_message_text(
                f"✅ Created and switched to `{s.name}`\n\nTip: use `/new <name>` to pick a name, "
                f"`/title <text>` to describe it.",
                parse_mode="Markdown",
            )
        elif action == "delmenu":
            sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
            active_idx = cc._session_mgr.get_active_index(PLATFORM, str(uid))
            btns = []
            for i, s in enumerate(sessions):
                if s.is_busy:
                    continue
                label = f"🗑 {s.display_name}" + (" (active)" if i == active_idx else "")
                btns.append([InlineKeyboardButton(label, callback_data=f"tg:sess:del:{i}")])
            btns.append([InlineKeyboardButton("📦 Archive instead…", callback_data="tg:sess:arcmenu:_")])
            btns.append([InlineKeyboardButton("↩️ Cancel", callback_data="tg:sess:back:_")])
            await q.edit_message_text(
                "Which session to delete?\n_Tip: /archive preserves history._",
                reply_markup=InlineKeyboardMarkup(btns),
                parse_mode="Markdown",
            )
        elif action == "del":
            idx = int(param)
            sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
            name = sessions[idx].name if idx < len(sessions) else "?"
            if cc._session_mgr.delete(PLATFORM, str(uid), idx):
                await q.edit_message_text(f"🗑 Deleted session `{name}` and its history.", parse_mode="Markdown")
            else:
                await q.edit_message_text("Cannot delete (busy or not found).")
        elif action == "arcmenu":
            sessions = cc._session_mgr.get_all(PLATFORM, str(uid))
            btns = []
            for s in sessions:
                if s.is_busy or s.archived:
                    continue
                btns.append([InlineKeyboardButton(
                    f"📦 {s.display_name}", callback_data=f"tg:sess:arc:{s.name}"
                )])
            btns.append([InlineKeyboardButton("↩️ Cancel", callback_data="tg:sess:back:_")])
            await q.edit_message_text(
                "Which session to archive?\n_Archived sessions are hidden but keep their history._",
                reply_markup=InlineKeyboardMarkup(btns),
                parse_mode="Markdown",
            )
        elif action == "arc":
            s = cc._session_mgr.archive(PLATFORM, str(uid), param)
            if s:
                await q.edit_message_text(f"📦 Archived `{s.display_name}`. Use /sessions all to see.", parse_mode="Markdown")
            else:
                await q.edit_message_text("Cannot archive.")
        elif action == "unarc":
            s = cc._session_mgr.unarchive(PLATFORM, str(uid), param)
            if s:
                await q.edit_message_text(f"✅ Restored and switched to `{s.display_name}`", parse_mode="Markdown")
            else:
                await q.edit_message_text("Session not found.")
        elif action == "back":
            await q.edit_message_text("Cancelled.")
        return

    # Handle folder browser callbacks
    if kind in ("br", "bf", "bv", "ba"):
        await _handle_browse_callback(q, uid)
        return

    # Handle no-op (used for page counter display)
    if kind == "noop":
        return

    # Handle pagination
    if kind == "pg":
        # value = "rid:page"
        pg_parts = value.split(":")
        if len(pg_parts) < 2:
            return
        try:
            rid, page_num = pg_parts[0], int(pg_parts[1])
        except ValueError:
            return
        resp = _response_store.get(rid)
        if not resp or resp["uid"] != uid:
            await q.edit_message_text("Response expired.", reply_markup=None)
            return

        text = resp["text"]
        total_pages = max(1, (len(text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE)
        page_num = max(0, min(page_num, total_pages - 1))
        start = page_num * RESPONSE_PAGE_SIZE
        page_text = text[start:start + RESPONSE_PAGE_SIZE]

        footer = f"\n\n📄 _Page {page_num + 1}/{total_pages}_"
        markup = _action_buttons(rid, has_pages=True, page=page_num, total_pages=total_pages)

        try:
            await q.edit_message_text(page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception:
            await q.edit_message_text(page_text + footer, reply_markup=markup)
        return

    # Handle retry
    if kind == "retry":
        retry_key = f"retry_{value}"
        resp = _response_store.get(retry_key)
        if not resp or resp["uid"] != uid:
            await q.edit_message_text("Retry expired.", reply_markup=None)
            return

        prompt = resp["prompt"]
        del _response_store[retry_key]
        await q.edit_message_text("🔄 Retrying…", reply_markup=None)

        # Re-run as a task
        placeholder = q.message
        task = TaskSession(placeholder, uid, prompt)
        task.start_heartbeat()
        _task_registry.register(task)
        try:
            reply, stats = await _ask(uid, prompt, tracker=task)
            if task.cancelled:
                await placeholder.edit_text("⏹ Retry cancelled.", reply_markup=None)
                return
            retry_sess = _active_session(uid)
            cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=retry_sess.name)
            cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
            summary = task.finish_summary()
            reply = f"{summary}\n\n{reply}"
            md_text = _protect_urls_for_markdown(reply)
            r_rid = _store_response(uid, prompt, md_text)
            if len(md_text) <= 4096:
                r_markup = _action_buttons(r_rid)
                try:
                    await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=r_markup)
                except Exception:
                    await placeholder.edit_text(reply[:4096], reply_markup=r_markup)
            else:
                total_pages = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
                page_text = md_text[:RESPONSE_PAGE_SIZE]
                footer = f"\n\n📄 _Page 1/{total_pages}_"
                r_markup = _action_buttons(r_rid, has_pages=True, page=0, total_pages=total_pages)
                try:
                    await placeholder.edit_text(page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=r_markup)
                except Exception:
                    await placeholder.edit_text(reply[:RESPONSE_PAGE_SIZE] + footer, reply_markup=r_markup)
        except Exception as exc:
            retry_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")]])
            _response_store[f"retry_{task.task_id}"] = {"prompt": prompt, "uid": uid}
            await placeholder.edit_text(f"❌ Retry failed: {str(exc)[:150]}", reply_markup=retry_btn)
        finally:
            await task.stop()
            _task_registry.unregister(task.task_id)
        return

    # Handle response action buttons (retry/continue/tts from action row)
    if kind == "act":
        # value = "rid:action"
        act_parts = value.split(":", 1)
        if len(act_parts) < 2:
            return
        rid, action = act_parts
        resp = _response_store.get(rid)
        if not resp or resp["uid"] != uid:
            await q.edit_message_text("Response expired.", reply_markup=None)
            return

        if action == "retry":
            prompt = resp["prompt"]
            await q.edit_message_text("🔄 Retrying…", reply_markup=None)
            placeholder = q.message
            task = TaskSession(placeholder, uid, prompt)
            task.start_heartbeat()
            _task_registry.register(task)
            try:
                reply, stats = await _ask(uid, prompt, tracker=task)
                if task.cancelled:
                    await placeholder.edit_text("⏹ Retry cancelled.", reply_markup=None)
                    return
                act_sess = _active_session(uid)
                cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=act_sess.name)
                cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
                summary = task.finish_summary()
                reply = f"{summary}\n\n{reply}"
                md_text = _protect_urls_for_markdown(reply)
                new_rid = _store_response(uid, prompt, md_text)
                if len(md_text) <= 4096:
                    act_markup = _action_buttons(new_rid)
                    try:
                        await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=act_markup)
                    except Exception:
                        await placeholder.edit_text(reply[:4096], reply_markup=act_markup)
                else:
                    tp = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
                    act_markup = _action_buttons(new_rid, has_pages=True, page=0, total_pages=tp)
                    footer = f"\n\n📄 _Page 1/{tp}_"
                    try:
                        await placeholder.edit_text(md_text[:RESPONSE_PAGE_SIZE] + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=act_markup)
                    except Exception:
                        await placeholder.edit_text(reply[:RESPONSE_PAGE_SIZE] + footer, reply_markup=act_markup)
            except Exception as exc:
                await placeholder.edit_text(f"❌ Retry failed: {str(exc)[:150]}", reply_markup=None)
            finally:
                await task.stop()
                _task_registry.unregister(task.task_id)
            return

        elif action == "continue":
            prompt = "Continue from where you left off."
            await q.edit_message_text("➡️ Continuing…", reply_markup=None)
            placeholder = q.message
            task = TaskSession(placeholder, uid, prompt)
            task.start_heartbeat()
            _task_registry.register(task)
            try:
                reply, stats = await _ask(uid, prompt, tracker=task)
                if task.cancelled:
                    await placeholder.edit_text("⏹ Cancelled.", reply_markup=None)
                    return
                cont_sess = _active_session(uid)
                cc.save_turn(PLATFORM, str(uid), prompt, reply, session_name=cont_sess.name)
                cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
                summary = task.finish_summary()
                reply = f"{summary}\n\n{reply}"
                md_text = _protect_urls_for_markdown(reply)
                new_rid = _store_response(uid, prompt, md_text)
                if len(md_text) <= 4096:
                    act_markup = _action_buttons(new_rid)
                    try:
                        await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=act_markup)
                    except Exception:
                        await placeholder.edit_text(reply[:4096], reply_markup=act_markup)
                else:
                    tp = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
                    act_markup = _action_buttons(new_rid, has_pages=True, page=0, total_pages=tp)
                    footer = f"\n\n📄 _Page 1/{tp}_"
                    try:
                        await placeholder.edit_text(md_text[:RESPONSE_PAGE_SIZE] + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=act_markup)
                    except Exception:
                        await placeholder.edit_text(reply[:RESPONSE_PAGE_SIZE] + footer, reply_markup=act_markup)
            except Exception as exc:
                await placeholder.edit_text(f"❌ Continue failed: {str(exc)[:150]}", reply_markup=None)
            finally:
                await task.stop()
                _task_registry.unregister(task.task_id)
            return

        elif action == "tts":
            resp_text = resp["text"]
            clean = re.sub(r'[*_`\[\]()]', '', resp_text)[:4000]
            await q.edit_message_reply_markup(reply_markup=None)
            try:
                result = await tts_synthesize(clean, voice="alloy")
                if result.error:
                    await q.message.reply_text(f"TTS error: {result.error}")
                    return
                with open(result.audio_path, "rb") as f:
                    await q.message.reply_voice(
                        voice=f,
                        caption=f"🔊 _{result.voice}_ · {result.text_length} chars",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                os.unlink(result.audio_path)
            except Exception as exc:
                await q.message.reply_text(f"TTS failed: {str(exc)[:100]}")
            return
        return

    # Handle settings panel callbacks
    if kind == "set":
        # value = "category:choice" e.g. "model:sonnet", "perm:auto"
        set_parts = value.split(":", 1)
        if len(set_parts) < 2:
            return
        cat, choice = set_parts

        if cat == "model":
            _user_model[uid] = choice
        elif cat == "perm":
            _user_perm[uid] = "" if choice == "default" else choice
        elif cat == "verbose":
            _user_verbose[uid] = int(choice)
        elif cat == "engine":
            _user_engine[uid] = choice

        # Re-render the settings panel
        await q.edit_message_text(
            _build_settings_text(uid),
            reply_markup=_build_settings_markup(uid),
            parse_mode="Markdown",
        )
        return

    if kind == "model":
        _user_model[uid] = value
        cur = _model(uid)
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:model:{k}")]
            for k, lbl in CLI_MODELS.items()
        ]
        await q.edit_message_text(
            f"Model set to: *{CLI_MODELS.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "perm":
        _user_perm[uid] = "" if value == "default" else value
        cur = _perm(uid) or "default"
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:perm:{k}")]
            for k, lbl in PERMISSION_MODES.items()
        ]
        await q.edit_message_text(
            f"Permission mode set to: *{PERMISSION_MODES.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "verbose":
        level = int(value)
        _user_verbose[uid] = level
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if lvl==level else ''}", callback_data=f"tg:verbose:{lvl}")]
            for lvl, lbl in VERBOSE_LEVELS.items()
        ]
        await q.edit_message_text(
            f"Verbosity set to: *{level}* ({VERBOSE_LEVELS[level]})",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )

    elif kind == "engine":
        _user_engine[uid] = value
        cur = _engine(uid)
        btns = [
            [InlineKeyboardButton(f"{lbl}{' ✓' if k==cur else ''}", callback_data=f"tg:engine:{k}")]
            for k, lbl in ENGINE_MODES.items()
        ]
        await q.edit_message_text(
            f"Engine set to: *{ENGINE_MODES.get(value, value)}*",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown",
        )


# ─── New features: /help, /poll, /tts, /imagine, /search ─────────────────────

HELP_TEXT = """*Available commands:*

*Chat & Sessions*
/start — Welcome message
/reset — Clear conversation history
/sessions — List sessions
/new `name` — Create new session
/switch `N` — Switch to session N
/tasks — List running tasks
/cancel — Cancel a task
/id — Show your user ID
/tour — Replay the walkthrough

*Sharing*
/invite `[uses] [days] [note]` — Make an invite link
/invites — Your invites and who joined
/revoke `code` — Kill an invite link
/kick `id` — Remove an invited user

*Settings*
/settings — All settings in one panel
/model — Change Claude model
/engine — Switch CLI/API/SDK
/mode — Show current settings
/permissions — Set CLI permissions
/verbose — Set detail level

*Memory*
/remember `text` — Save a memory
/recall `query` — Search memories
/memories — List all memories
/forget `id` — Delete a memory
/editmem — Edit a memory
/exportmem — Export all memories
/importmem — Import memories
/extractmem — Auto-extract from chat

*Tools*
/poll `Q | A | B | C` — Create a poll
/tts `text` — Text to speech
/imagine `prompt` — Generate an image
/search `query` — Web search
/fetch `url` — Extract page content
/music `prompt` — Generate music
/video `prompt` — Generate video
/browse — Browse files
/usage — Token usage stats
/watchdog — Watchdog status

*Productivity*
/remind `text` — Set a reminder
/commitments — View pending reminders
/doctor — Run diagnostic checks
/export `format` — Export chat (text/md/html/json)
/compact — Compact conversation history

*Tips:*
- Send photos/documents for analysis
- Send voice messages for transcription + Claude
- URLs in messages are auto-fetched for context
- Long responses are paginated with nav buttons
- Every response has 🔄 Retry, ➡️ Continue, and 🔊 TTS buttons
"""


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_poll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    text = (update.message.text or "").split(None, 1)
    args = text[1] if len(text) > 1 else ""
    result = parse_poll_command(args)
    if isinstance(result, str):
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)
        return
    try:
        await ctx.bot.send_poll(
            chat_id=update.effective_chat.id,
            question=result.question,
            options=result.options,
            is_anonymous=result.is_anonymous,
            allows_multiple_answers=result.allows_multiple_answers,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to create poll: {e}")


async def cmd_tts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not tts_available():
        await update.message.reply_text(
            "TTS not configured. Set `OPENAI_API_KEY` and `TTS_ENABLED=true` in .env."
        )
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        voices = ", ".join(f"`{v}`" for v in TTS_VOICES)
        await update.message.reply_text(
            f"Usage: `/tts text to speak`\n\nVoice: `/tts --voice nova Hello!`\n\nVoices: {voices}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    args = text[1].strip()
    voice = None
    if args.startswith("--voice "):
        parts = args.split(None, 2)
        if len(parts) >= 3:
            voice = parts[1]
            args = parts[2]

    placeholder = await update.message.reply_text("🔊 Generating speech…")
    result = await tts_synthesize(args, voice=voice or "alloy")
    if result.error:
        await placeholder.edit_text(f"TTS error: {result.error}")
        return
    try:
        with open(result.audio_path, "rb") as f:
            await ctx.bot.send_voice(
                chat_id=update.effective_chat.id,
                voice=f,
                caption=f"🔊 _{result.voice}_ · {result.text_length} chars",
                parse_mode=ParseMode.MARKDOWN,
            )
        await placeholder.delete()
    except Exception as e:
        await placeholder.edit_text(f"Failed to send audio: {e}")
    finally:
        try:
            os.unlink(result.audio_path)
        except OSError:
            pass


async def cmd_imagine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not image_gen_available():
        await update.message.reply_text(
            "Image generation not configured. Set `OPENAI_API_KEY` and `IMAGE_GEN_ENABLED=true` in .env."
        )
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/imagine a sunset over mountains`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    prompt = text[1].strip()
    placeholder = await update.message.reply_text("🎨 Generating image…")
    result = await image_generate(prompt)
    if result.error:
        await placeholder.edit_text(f"Image generation error: {result.error}")
        return
    try:
        with open(result.image_path, "rb") as f:
            caption = f"🎨 _{result.revised_prompt[:200]}_" if result.revised_prompt != prompt else f"🎨 _{prompt[:200]}_"
            await ctx.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=f,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        await placeholder.delete()
    except Exception as e:
        await placeholder.edit_text(f"Failed to send image: {e}")
    finally:
        try:
            os.unlink(result.image_path)
        except OSError:
            pass


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not search_available():
        await update.message.reply_text(
            "Web search not configured. Set `BRAVE_SEARCH_API_KEY` or `TAVILY_API_KEY` and `WEB_SEARCH_ENABLED=true` in .env."
        )
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/search your query here`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query = text[1].strip()
    placeholder = await update.message.reply_text("🔍 Searching…")
    resp = await web_search(query)
    formatted = format_search_results(resp)
    try:
        await placeholder.edit_text(
            formatted, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        )
    except Exception:
        await placeholder.edit_text(formatted, disable_web_page_preview=True)


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Get or set the project directory for /code tasks."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return

    if not ctx.args:
        proj = coder.get_project(PLATFORM, str(uid))
        if proj:
            await update.message.reply_text(f"Current project directory: `{proj}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("No project directory set. Usage: `/project /path/to/dir`", parse_mode="Markdown")
        return

    path = " ".join(ctx.args).strip()
    ok, result = coder.set_project(PLATFORM, str(uid), path)
    if ok:
        await update.message.reply_text(f"✓ Project set to `{result}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"✗ {result}")


async def cmd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Run a coding task in the project directory."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return

    if not ctx.args:
        await update.message.reply_text("Usage: `/code <task description>`", parse_mode="Markdown")
        return

    proj = coder.get_project(PLATFORM, str(uid))
    if not proj:
        await update.message.reply_text("No project directory set. Use `/project /path/to/dir` first.", parse_mode="Markdown")
        return

    if not os.path.isdir(proj):
        await update.message.reply_text(f"Project directory no longer exists: `{proj}`", parse_mode="Markdown")
        return

    task = " ".join(ctx.args).strip()
    prompt = coder.build_task_prompt(task, proj)

    placeholder = await update.message.reply_text("⚙️ Working on your code task…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))

    ts = TaskSession(placeholder, uid, task[:40])
    _task_registry.register(ts)

    try:
        reply, stats = await cc.ask_claude_async(
            prompt, [],
            system=coder.CODER_SYSTEM,
            timeout=cc.CLAUDE_TIMEOUT,
            is_cancelled=lambda: ts.cancelled,
            platform=PLATFORM,
            user_id=str(uid),
        )
        if ts.cancelled:
            await placeholder.edit_text("🚫 Cancelled.")
            return

        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        cc.track_tool_usage(PLATFORM, str(uid), "code")

        await placeholder.edit_text(reply[:4096])
    except Exception as e:
        # log.error() hid the traceback; the operator of a self-hosted bot is
        # the person debugging it, so they get both the message and the trace.
        log.exception("cmd_code failed")
        await placeholder.edit_text(f"❌ Error: {e}")
    finally:
        stop.set()
        typing_task.cancel()
        _task_registry.unregister(ts.task_id)


async def cmd_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not music_gen_available():
        await update.message.reply_text(
            "Music generation not configured. Set `REPLICATE_API_TOKEN` and `MUSIC_GEN_ENABLED=true` in .env."
        )
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/music upbeat jazz piano solo`\n\nDuration: `/music --dur 15 chill lofi beats`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    args = text[1].strip()
    duration = None
    if args.startswith("--dur "):
        parts = args.split(None, 2)
        if len(parts) >= 3:
            try:
                duration = int(parts[1])
            except ValueError:
                pass
            args = parts[2]

    placeholder = await update.message.reply_text("🎵 Generating music… (this may take 1-2 min)")
    result = await music_generate(args, duration=duration or 10)
    if result.error:
        await placeholder.edit_text(f"Music generation error: {result.error}")
        return
    try:
        with open(result.audio_path, "rb") as f:
            await ctx.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=f,
                title=f"🎵 {args[:60]}",
                caption=f"🎵 _{args[:200]}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        await placeholder.delete()
    except Exception as e:
        await placeholder.edit_text(f"Failed to send audio: {e}")
    finally:
        try:
            os.unlink(result.audio_path)
        except OSError:
            pass


async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not video_gen_available():
        await update.message.reply_text(
            "Video generation not configured. Set `REPLICATE_API_TOKEN` and `VIDEO_GEN_ENABLED=true` in .env."
        )
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/video a cat playing piano`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    prompt = text[1].strip()
    placeholder = await update.message.reply_text("🎬 Generating video… (this may take 2-5 min)")
    result = await video_generate(prompt)
    if result.error:
        await placeholder.edit_text(f"Video generation error: {result.error}")
        return
    try:
        with open(result.video_path, "rb") as f:
            await ctx.bot.send_video(
                chat_id=update.effective_chat.id,
                video=f,
                caption=f"🎬 _{prompt[:200]}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        await placeholder.delete()
    except Exception as e:
        await placeholder.edit_text(f"Failed to send video: {e}")
    finally:
        try:
            os.unlink(result.video_path)
        except OSError:
            pass


async def cmd_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return

    text = (update.message.text or "").split(None, 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/fetch https://example.com`\nExtracts readable content from a URL.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    url = text[1].strip()
    placeholder = await update.message.reply_text("📖 Fetching content…")
    result = await fetch_readable(url)
    if result.error:
        await placeholder.edit_text(f"Fetch error: {result.error}")
        return

    response = f"📖 *{result.title}*\n_{result.word_count} words_\n\n{result.content}"
    if len(response) > 4000:
        response = response[:4000] + "\n…_[truncated]_"
    try:
        await placeholder.edit_text(response, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)
    except Exception:
        await placeholder.edit_text(response[:4000], disable_web_page_preview=True)


# ─── Voice message handler ──────────────────────────────────────────────────────

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages: transcribe and optionally send to Claude."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text("Rate limit exceeded.")
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    # Reject excessively large audio uploads (25 MB matches OpenAI Whisper limit)
    fsize = getattr(voice, "file_size", None)
    if isinstance(fsize, int) and fsize > 25 * 1024 * 1024:
        await update.message.reply_text("Audio file too large (max 25 MB).")
        return

    # Download voice file
    file = await ctx.bot.get_file(voice.file_id)
    raw = bytes(await file.download_as_bytearray())
    save_path = _upload_path(uid, ".ogg", "voice_")
    save_path.write_bytes(raw)

    if transcription_available():
        placeholder = await update.message.reply_text("🎤 Transcribing…")
        result = await voice_transcribe(str(save_path))
        if result.error:
            await placeholder.edit_text(f"Transcription error: {result.error}")
            return

        transcript = result.text.strip()
        if not transcript:
            await placeholder.edit_text("Could not transcribe audio (no speech detected).")
            return

        lang_note = f" [{result.language}]" if result.language else ""
        dur_note = f" {result.duration_seconds:.0f}s" if result.duration_seconds else ""
        header = f"🎤 _Transcribed{lang_note}{dur_note}:_\n\n"

        # Send transcription and then process as a regular message
        await placeholder.edit_text(f"{header}{transcript}", parse_mode=ParseMode.MARKDOWN)

        # Now send the transcribed text to Claude
        if _task_registry.user_task_count(uid) < MAX_CONCURRENT_TASKS:
            asyncio.create_task(_run_task(update, ctx, uid, transcript))
    else:
        # No transcription available — send audio path to Claude
        caption = update.message.caption or "Analyze this voice message."
        prompt = f"{caption}\n\n[Voice message saved at: {save_path}]"
        asyncio.create_task(_run_task(update, ctx, uid, prompt))


# ─── Message handler ─────────────────────────────────────────────────────────────

async def _send_paginated(update: Update, uid: int, prompt: str, text: str, placeholder=None):
    """Send a response with pagination buttons if it's long."""
    chunk = 4096
    md_text = _protect_urls_for_markdown(text)

    # Store every response for action buttons (retry/continue/tts)
    rid = _store_response(uid, prompt, md_text)

    # Short responses: send with action buttons
    if len(md_text) <= chunk:
        markup = _action_buttons(rid)
        if placeholder:
            try:
                await placeholder.edit_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                return
            except Exception:
                try:
                    await placeholder.edit_text(text[:chunk], reply_markup=markup)
                    return
                except Exception:
                    # Markdown and plain both refused; the caller falls through
                    # to sending a fresh message.
                    log.debug("reply edit failed (both attempts)", exc_info=True)

        # Fallback: always try a new message
        try:
            await update.effective_message.reply_text(md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception:
            await update.effective_message.reply_text(text[:chunk], reply_markup=markup)
        return

    # Long responses: paginate with action buttons
    total_pages = (len(md_text) + RESPONSE_PAGE_SIZE - 1) // RESPONSE_PAGE_SIZE
    page_text = md_text[:RESPONSE_PAGE_SIZE]
    footer = f"\n\n📄 _Page 1/{total_pages}_"
    markup = _action_buttons(rid, has_pages=True, page=0, total_pages=total_pages)

    if placeholder:
        try:
            await placeholder.edit_text(
                page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
            return
        except Exception:
            try:
                await placeholder.edit_text(
                    text[:RESPONSE_PAGE_SIZE] + footer, reply_markup=markup
                )
                return
            except Exception:
                log.debug("paged reply edit failed (both attempts)", exc_info=True)

    # Fallback: always send as new message (never silently lose a response)
    try:
        await update.effective_message.reply_text(
            page_text + footer, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
        )
    except Exception:
        try:
            await update.effective_message.reply_text(text[:RESPONSE_PAGE_SIZE], reply_markup=markup)
        except Exception:
            log.error("Failed to send response for uid=%s, text len=%d", uid, len(text))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1: Auto Memory Extraction
# ═══════════════════════════════════════════════════════════════════════════════

async def _auto_extract_memories(uid: int, user_text: str, reply: str):
    """Background task: extract key facts from conversation and store as memories."""
    try:
        combined = f"User: {user_text}\nAssistant: {reply}"
        if len(combined) < AUTO_MEMORY_MIN_LENGTH:
            return
        extracted = await extract_memories(combined)
        stored = 0
        for mem_data in extracted:
            content = mem_data.get("content", "").strip()
            if not content:
                continue
            # Deduplicate: skip if very similar memory already exists
            existing = _memory.recall(PLATFORM, str(uid), content, limit=1)
            if existing and existing[0].score and abs(existing[0].score) < 0.5:
                continue
            _memory.remember(
                PLATFORM, str(uid), content,
                tags=mem_data.get("tags", ["auto"]),
                importance=mem_data.get("importance", 0.6),
                metadata={"source": "auto_extract"},
            )
            stored += 1
        if stored:
            log.info("Auto-extracted %d memories for uid=%s", stored, uid)
    except Exception:
        log.debug("Auto memory extraction failed for uid=%s", uid, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2: Cost Budget System
# ═══════════════════════════════════════════════════════════════════════════════

from .cost_budget import BudgetManager

_budget_mgr = BudgetManager()


async def _check_budget(uid: int) -> str | None:
    """Check if user is within budget. Returns warning/error message or None."""
    if not COST_BUDGET_ENABLED:
        return None
    return _budget_mgr.check(PLATFORM, str(uid))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3: Smart Model Routing
# ═══════════════════════════════════════════════════════════════════════════════

from .smart_router import route_model, route_model_api


def _smart_model(uid: int, text: str) -> str:
    """Pick model based on query complexity, or user override."""
    override = _user_model.get(uid)
    if override or not SMART_ROUTING_ENABLED:
        return override or _DEFAULT_MODEL
    return route_model(text)


async def _run_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, user_text: str):
    """Execute a single Claude task. Can run concurrently with other tasks."""
    placeholder = await update.message.reply_text("🧠 Thinking…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))

    # Link understanding: fetch URL content after placeholder is visible
    if LINK_ENABLED and extract_links(user_text):
        try:
            link_context = await understand_links(user_text)
            if link_context:
                user_text = f"{user_text}\n\n---\n[Auto-fetched link content below]\n{link_context}"
        except Exception:
            log.debug("Link understanding failed", exc_info=True)

    # Create task session with progress tracking
    task = TaskSession(placeholder, uid, user_text)
    task.chat_id = update.effective_chat.id
    task.bot = ctx.bot
    task.start_heartbeat()
    _task_registry.register(task)

    sess = _active_session(uid)
    sess.is_busy = True
    try:
        reply, stats = await _ask(uid, user_text, tracker=task, session=sess)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled after {task._elapsed()}.", reply_markup=None,
            )
            return

        # Stop heartbeat before sending final response to avoid edit race
        await task.stop()

        is_timeout = reply.startswith("[Timeout]")
        is_error = reply.startswith("[Claude error]")

        if not reply or not reply.strip():
            reply = "(No response from Claude — the task may have completed without output.)"
            log.warning("Empty reply for uid=%s, task #%d", uid, task.task_id)

        # Don't save timeouts/errors to conversation history — they pollute context
        if not is_timeout and not is_error:
            cc.save_turn(PLATFORM, str(uid), user_text, reply, session_name=sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        cc.track_tool_usage(PLATFORM, str(uid), stats.get("tools_used", []))
        cc.track_cost(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0), stats.get("cost_usd", 0))

        # ── Quality evaluation: binary checks + sampled LLM-as-judge ─────
        if not is_timeout and not is_error:
            _evaluate_quality(str(uid), user_text, reply, stats, conv_key=f"{uid}:{sess.name}")

        # ── Auto Memory Extraction (Feature 1) ──────────────────────────
        if not is_timeout and not is_error and AUTO_MEMORY_ENABLED:
            asyncio.create_task(_auto_extract_memories(uid, user_text, reply))

        # Build header with completion stats
        v = _verbose(uid)
        if is_timeout:
            # Show timeout with retry button instead of success summary
            retry_btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")],
            ])
            _response_store[f"retry_{task.task_id}"] = {"prompt": user_text, "uid": uid}
            # Include any partial results from streaming
            partial = task._partial_text.strip() if task._partial_text else ""
            timeout_msg = f"⏱ *Timed out* after {task._elapsed()}"
            if task.tool_count:
                timeout_msg += f" ({task.tool_count} tools used)"
            if partial:
                preview = partial[:1500]
                if len(partial) > 1500:
                    preview += "\n\n⏳ _(partial result — task timed out before finishing)_"
                timeout_msg += f"\n\n{preview}"
            try:
                await placeholder.edit_text(
                    timeout_msg, reply_markup=retry_btn, parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
                    await placeholder.edit_text(
                        f"⏱ Timed out after {task._elapsed()}\n\n{partial[:1500] if partial else reply}",
                        reply_markup=retry_btn,
                    )
                except Exception:
                    await update.effective_message.reply_text(
                        f"⏱ Timed out after {task._elapsed()}", reply_markup=retry_btn,
                    )
            return

        summary = task.finish_summary()
        if v >= 1 and stats.get("tools_used"):
            tools_str = ', '.join(stats['tools_used'][:5])
            if len(stats['tools_used']) > 5:
                tools_str += f" +{len(stats['tools_used']) - 5} more"
            reply = f"{summary}\n🔧 _{tools_str}_\n\n{reply}"
        elif v >= 1:
            reply = f"{summary}\n\n{reply}"
        if v >= 2 and stats.get("input_tokens"):
            cost = stats.get("cost_usd", 0)
            # "~" marks a figure we computed from tokens rather than one the
            # backend reported — API mode has no cost in its response.
            approx = "~" if stats.get("cost_estimated") else ""
            cost_str = f" · {approx}${cost:.4f}" if cost else ""
            reply += f"\n\n_({stats['input_tokens']}→{stats['output_tokens']} tokens{cost_str})_"

        await _send_paginated(update, uid, user_text, reply, placeholder=placeholder)

        # Notification ping for long tasks (>30s) — vibrates the phone
        elapsed_secs = time.time() - task.start_time
        if elapsed_secs > 30:
            try:
                # Include a short preview so the user sees what happened
                raw_reply = reply
                # Strip the prepended summary/tools header to get actual content
                if "\n\n" in raw_reply:
                    raw_reply = raw_reply.split("\n\n", 1)[1]
                preview = raw_reply[:200].strip()
                if len(raw_reply) > 200:
                    preview += "…"
                await update.effective_message.reply_text(
                    f"🔔 Done ({task._elapsed()})\n\n{preview}",
                    disable_notification=False,
                )
            except Exception:
                # The completion ping is a courtesy; the answer itself already
                # went out above.
                log.debug("completion notification failed", exc_info=True)

    except asyncio.CancelledError:
        await placeholder.edit_text(
            f"⏹ Task cancelled after {task._elapsed()}.", reply_markup=None,
        )
    except Exception as exc:
        log.exception("handle_message error (task #%d)", task.task_id)
        error_msg = str(exc)[:200]
        # Show error with retry button
        retry_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry", callback_data=f"tg:retry:{task.task_id}")],
        ])
        # Store the prompt for retry
        _response_store[f"retry_{task.task_id}"] = {"prompt": user_text, "uid": uid}
        try:
            await placeholder.edit_text(
                f"❌ Error after {task._elapsed()}: {error_msg}",
                reply_markup=retry_btn,
            )
        except Exception:
            await update.message.reply_text(f"❌ Error: {error_msg}", reply_markup=retry_btn)
    finally:
        sess.is_busy = False
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Deduplicate: Telegram can send the same message multiple times on retries
    if _is_duplicate(update.message.message_id):
        return

    user_text = update.message.text or ""

    if not _allowed(uid):
        await update.message.reply_text("You're not authorized.")
        return

    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text(
            f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s."
        )
        return

    if not user_text.strip():
        return

    conv_uid = uid

    # Claude Desktop bridge: route replies to session cards / current-session messages
    # to a live `claude --resume` and post the result back. If handled, skip the
    # normal chat flow.
    try:
        from . import desktop_bridge
        if await desktop_bridge.try_handle_text_message(update, ctx):
            return
    except Exception as _bridge_exc:
        log.warning("desktop_bridge text routing failed: %s", _bridge_exc)

    # Link understanding moved to _run_task (after placeholder is shown)

    # Check concurrent task limit — per conversation, so one busy group does
    # not consume a member's personal allowance and vice versa.
    if _task_registry.user_task_count(conv_uid) >= MAX_CONCURRENT_TASKS:
        active = _task_registry.get_user_tasks(conv_uid)
        task_list = "\n".join(
            f"  `#{t.task_id}` — {t.prompt_preview}… ({t._elapsed()})"
            for t in active
        )
        await update.message.reply_text(
            f"⚠️ You have {len(active)} tasks running (max {MAX_CONCURRENT_TASKS}).\n\n"
            f"{task_list}\n\n"
            f"Use /tasks to manage or /cancel to stop one.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Fire and forget — task runs concurrently
    asyncio.create_task(_run_task(update, ctx, conv_uid, user_text))


# ─── Upload directory for persistent file storage ─────────────────────────────

UPLOAD_DIR = Path(cc.CLAUDE_WORK_DIR) / "telegram_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def _upload_path(uid: int, suffix: str, prefix: str = "") -> Path:
    """Return a persistent path for an uploaded file."""
    ts = int(time.time() * 1000)
    name = f"{prefix}{uid}_{ts}{suffix}"
    return UPLOAD_DIR / name


# ─── Photo handler ───────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text("Rate limit exceeded.")
        return

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    raw = bytes(await file.download_as_bytearray())
    caption = update.message.caption or "Analyze this image."

    save_path = _upload_path(uid, ".jpg", "img_")
    save_path.write_bytes(raw)
    prompt = f"{caption}\n\n[Image saved at: {save_path}]"

    placeholder = await update.message.reply_text("🔍 Analyzing image…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    task = TaskSession(placeholder, uid, f"[Image] {caption}")
    task.start_heartbeat()
    _task_registry.register(task)

    try:
        reply, stats = await _ask(uid, prompt, tracker=task)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled. `#{task.task_id}`", reply_markup=None, parse_mode=ParseMode.MARKDOWN
            )
            return

        img_sess = _active_session(uid)
        cc.save_turn(PLATFORM, str(uid), f"[Image at {save_path}] {caption}", reply, session_name=img_sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_photo error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


# ─── Document handler ────────────────────────────────────────────────────────────

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not cc.check_rate_limit(f"tg:{uid}"):
        await update.message.reply_text("Rate limit exceeded.")
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large (max 10 MB).")
        return

    file = await ctx.bot.get_file(doc.file_id)
    raw = bytes(await file.download_as_bytearray())
    filename = doc.file_name or "file"
    caption = update.message.caption or f"Analyze this file: {filename}"

    suffix = Path(filename).suffix or ".txt"
    save_path = _upload_path(uid, suffix, "doc_")
    save_path.write_bytes(raw)
    prompt = f"{caption}\n\n[File '{filename}' saved at: {save_path}]"

    placeholder = await update.message.reply_text(f"📄 Processing {filename}…")
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(update.effective_chat.id, ctx, stop))
    task = TaskSession(placeholder, uid, f"[File] {filename}")
    task.start_heartbeat()
    _task_registry.register(task)

    try:
        reply, stats = await _ask(uid, prompt, tracker=task)

        if task.cancelled:
            await placeholder.edit_text(
                f"⏹ Task cancelled. `#{task.task_id}`", reply_markup=None, parse_mode=ParseMode.MARKDOWN
            )
            return

        doc_sess = _active_session(uid)
        cc.save_turn(PLATFORM, str(uid), f"[File '{filename}' at {save_path}] {caption}", reply, session_name=doc_sess.name)
        cc.track_usage(PLATFORM, str(uid), stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        await _send(placeholder, update, reply)
    except Exception as exc:
        log.exception("handle_document error")
        await placeholder.edit_text(f"Error: {exc}", reply_markup=None)
    finally:
        await task.stop()
        _task_registry.unregister(task.task_id)
        stop.set()
        await typing_task


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 4: Session Resume/Fork commands
# ═══════════════════════════════════════════════════════════════════════════════

from .session_manager import SessionBrowser
_session_browser = SessionBrowser()


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resume a previous conversation session."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        # Show list of sessions to choose from
        sessions = _session_browser.list_sessions(PLATFORM, str(uid), limit=5)
        if not sessions:
            await update.message.reply_text("No previous sessions found.")
            return
        lines = ["**Recent sessions** (use `/resume <name>`):\n"]
        for s in sessions:
            age = _format_age(s.last_active)
            lines.append(f"  `{s.name}` — {s.message_count} msgs, {age} ago")
            if s.preview:
                lines.append(f"    _{s.preview[:60]}…_")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    session_name = args[1].strip()
    sess = cc._session_mgr.get_or_create_active(PLATFORM, str(uid))
    sess.name = session_name
    await update.message.reply_text(f"Resumed session `{session_name}`.", parse_mode=ParseMode.MARKDOWN)


async def cmd_fork(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fork a session into a new branch."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    args = (update.message.text or "").split(maxsplit=2)
    source = args[1].strip() if len(args) > 1 else None
    new_name = args[2].strip() if len(args) > 2 else None

    if not source:
        sess = cc._session_mgr.get_or_create_active(PLATFORM, str(uid))
        source = sess.name

    result = _session_browser.fork_session(PLATFORM, str(uid), source, new_name)
    if result.success:
        # Switch to the forked session
        new_sess = cc._session_mgr.get_or_create_active(PLATFORM, str(uid))
        new_sess.name = result.new_session_name
        await update.message.reply_text(
            f"Forked `{source}` → `{result.new_session_name}` ({result.messages_copied} messages copied).",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"Fork failed: {result.error}")


def _format_age(ts: float) -> str:
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s"
    if diff < 3600:
        return f"{int(diff / 60)}m"
    if diff < 86400:
        return f"{int(diff / 3600)}h"
    return f"{int(diff / 86400)}d"


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2: Budget command
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show or set cost budget."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    args = (update.message.text or "").split()

    if len(args) >= 3:
        # /budget daily 2.0  or  /budget monthly 30.0
        period = args[1].lower()
        try:
            amount = float(args[2])
        except ValueError:
            await update.message.reply_text("Usage: /budget daily 5.0 or /budget monthly 50.0")
            return
        if period == "daily":
            _budget_mgr.set_budget(PLATFORM, str(uid), daily=amount)
        elif period == "monthly":
            _budget_mgr.set_budget(PLATFORM, str(uid), monthly=amount)
        else:
            await update.message.reply_text("Usage: /budget daily 5.0 or /budget monthly 50.0")
            return
        await update.message.reply_text(f"Budget updated: {period} = ${amount:.2f}")
        return

    report = _budget_mgr.usage_report(PLATFORM, str(uid))
    daily_bar = _progress_bar(report.daily_pct)
    monthly_bar = _progress_bar(report.monthly_pct)
    await update.message.reply_text(
        f"**Cost Budget**\n\n"
        f"Today: ${report.daily_cost:.3f} / ${report.daily_limit:.2f} ({report.daily_requests} reqs)\n"
        f"{daily_bar}\n\n"
        f"Month: ${report.monthly_cost:.3f} / ${report.monthly_limit:.2f} ({report.monthly_requests} reqs)\n"
        f"{monthly_bar}\n\n"
        f"{_cost_basis_note()}"
        f"Use `/budget daily 5.0` or `/budget monthly 50.0` to adjust.",
        parse_mode=ParseMode.MARKDOWN,
    )


def _cost_basis_note() -> str:
    """One line saying where the budget's cost figures came from.

    In API mode the backend reports tokens but no cost, so the figures are
    computed from a local pricing table (telechat_pkg/models.py) and can drift
    from the invoice — tier discounts and partner-platform rates aren't modelled.
    Saying so matters for a spend guard: a user who sets a cap should know
    whether it is enforcing against measured or estimated spend.
    """
    if cc.CLAUDE_MODE == "api":
        return "_Costs are estimated from token counts at list prices._\n\n"
    return ""


def _progress_bar(pct: float, width: int = 20) -> str:
    filled = int(pct * width)
    filled = min(filled, width)
    bar = "█" * filled + "░" * (width - filled)
    icon = "🟢" if pct < 0.8 else "🟡" if pct < 1.0 else "🔴"
    return f"{icon} [{bar}] {pct:.0%}"


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 5: Two-Agent command (/plan)
# ═══════════════════════════════════════════════════════════════════════════════

from .two_agent import TwoAgentExecutor
_two_agent = TwoAgentExecutor()


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Decompose a complex task into sub-steps and execute them."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    task_text = (update.message.text or "").split(maxsplit=1)
    if len(task_text) < 2:
        await update.message.reply_text("Usage: /plan <complex task description>")
        return

    task = task_text[1].strip()
    placeholder = await update.message.reply_text("🧩 Planning task…")

    try:
        plan = await _two_agent.plan(task)
        await placeholder.edit_text(_two_agent.format_plan(plan), parse_mode=ParseMode.MARKDOWN)

        # Execute steps with progress updates
        async def on_step_start(step):
            try:
                text = _two_agent.format_plan(plan)
                await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                # Plan progress is a live-updating card; a rejected edit (most
                # often "message is not modified") must not abort the plan.
                log.debug("plan progress edit failed", exc_info=True)

        async def on_step_done(step):
            try:
                text = _two_agent.format_plan(plan)
                await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                log.debug("plan progress edit failed", exc_info=True)

        plan = await _two_agent.execute(plan, on_step_start=on_step_start, on_step_done=on_step_done)

        # Send final results
        result_text = _two_agent.format_result(plan)
        await _send_paginated(update, uid, task, result_text, placeholder=placeholder)

    except Exception as e:
        await placeholder.edit_text(f"❌ Planning failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 7: Schedule command
# ═══════════════════════════════════════════════════════════════════════════════

from .auto_scheduler import AutoScheduler
_auto_sched = AutoScheduler()


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create, list, or delete scheduled tasks."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    args = (update.message.text or "").split(maxsplit=1)

    if len(args) < 2 or args[1].strip().lower() == "list":
        tasks = _auto_sched.list_tasks(PLATFORM, str(uid))
        await update.message.reply_text(
            _auto_sched.format_task_list(tasks), parse_mode=ParseMode.MARKDOWN,
        )
        return

    sub = args[1].strip()

    # /schedule delete 5
    if sub.lower().startswith("delete "):
        try:
            task_id = int(sub.split()[1])
            if _auto_sched.delete_task(task_id, PLATFORM, str(uid)):
                await update.message.reply_text(f"Deleted task #{task_id}.")
            else:
                await update.message.reply_text(f"Task #{task_id} not found.")
        except (ValueError, IndexError):
            await update.message.reply_text("Usage: /schedule delete <id>")
        return

    # Natural language schedule
    task = _auto_sched.parse_and_create(PLATFORM, str(uid), sub)
    if task:
        from .auto_scheduler import _format_interval
        await update.message.reply_text(
            f"Scheduled: **{task.description}**\n"
            f"Every {_format_interval(task.interval_seconds)}"
            f"{' (one-time)' if task.max_runs == 1 else ''}\n"
            f"ID: `#{task.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "Couldn't parse schedule. Try:\n"
            "  `/schedule check deploys every 2 hours`\n"
            "  `/schedule remind me to stretch every 30 minutes`\n"
            "  `/schedule list`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 9: Knowledge Base commands
# ═══════════════════════════════════════════════════════════════════════════════

from .knowledge_base import KnowledgeBase
_kb = KnowledgeBase()


async def cmd_kb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Knowledge base management."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    args = (update.message.text or "").split(maxsplit=2)
    sub = args[1].strip().lower() if len(args) > 1 else "stats"

    if sub == "stats":
        s = _kb.stats(PLATFORM, str(uid))
        await update.message.reply_text(
            f"**Knowledge Base**\nDocuments: {s['documents']}\nChunks: {s['chunks']}",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif sub == "list":
        docs = _kb.list_documents(PLATFORM, str(uid))
        if not docs:
            await update.message.reply_text("No documents in knowledge base.")
            return
        lines = ["**Knowledge Base Documents:**\n"]
        for d in docs:
            lines.append(f"  `{d.id[:8]}` {d.title} ({d.chunk_count} chunks)")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    elif sub == "add" and len(args) > 2:
        # /kb add <text to add as knowledge>
        text = args[2].strip()
        doc = _kb.ingest_text(PLATFORM, str(uid), f"Note {int(time.time())}", text, tags=["manual"])
        await update.message.reply_text(
            f"Added to KB: {doc.chunk_count} chunks from '{doc.title}'.",
        )
    elif sub == "search" and len(args) > 2:
        query = args[2].strip()
        results = _kb.search(PLATFORM, str(uid), query, limit=3)
        if not results:
            await update.message.reply_text("No results found.")
            return
        lines = [f"**KB Search:** '{query}'\n"]
        for r in results:
            lines.append(f"📄 {r.document.title} (chunk {r.chunk.chunk_index})")
            lines.append(f"  _{r.chunk.content[:150]}…_\n")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    elif sub == "delete" and len(args) > 2:
        doc_id_prefix = args[2].strip()
        docs = _kb.list_documents(PLATFORM, str(uid))
        match = next((d for d in docs if d.id.startswith(doc_id_prefix)), None)
        if match and _kb.delete_document(PLATFORM, str(uid), match.id):
            await update.message.reply_text(f"Deleted: {match.title}")
        else:
            await update.message.reply_text("Document not found.")
    else:
        await update.message.reply_text(
            "Usage:\n"
            "  `/kb stats` — show KB stats\n"
            "  `/kb list` — list documents\n"
            "  `/kb add <text>` — add knowledge\n"
            "  `/kb search <query>` — search KB\n"
            "  `/kb delete <id>` — remove document",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 10: Browser command
# ═══════════════════════════════════════════════════════════════════════════════

from .browser_automation import get_browser_agent, BROWSER_ENABLED


async def cmd_browse_web(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Browser automation: screenshot, extract, or interact with web pages."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    if not BROWSER_ENABLED:
        await update.message.reply_text(
            "Browser automation is disabled. Set `BROWSER_ENABLED=true` in .env\n"
            "and install: `pip install playwright && playwright install chromium`"
        )
        return

    args = (update.message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await update.message.reply_text(
            "Usage:\n"
            "  `/web screenshot <url>`\n"
            "  `/web extract <url>`\n"
            "  `/web info <url>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    action = args[1].strip().lower()
    url = args[2].strip()
    agent = get_browser_agent()

    placeholder = await update.message.reply_text(f"🌐 {action}ing {url}…")

    try:
        if action == "screenshot":
            result = await agent.screenshot(url)
            if result.success and result.screenshot_path:
                with open(result.screenshot_path, "rb") as fh:
                    await update.message.reply_photo(
                        fh,
                        caption=f"{result.title}\n{result.url}\n({result.duration:.1f}s)",
                    )
                await placeholder.delete()
            else:
                await placeholder.edit_text(f"❌ {result.error}")

        elif action == "extract":
            result = await agent.extract_text(url)
            if result.success and result.data:
                page = result.data
                text = f"**{page.title}**\n\n{page.text_content[:3000]}"
                await _send_paginated(update, uid, url, text, placeholder=placeholder)
            else:
                await placeholder.edit_text(f"❌ {result.error}")

        elif action == "info":
            result = await agent.get_page_info(url)
            if result.success and result.data:
                data = result.data
                text = (
                    f"**{data['title']}**\n"
                    f"URL: {data['url']}\n"
                    f"Preview: {data['text_preview'][:500]}…"
                )
                await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            else:
                await placeholder.edit_text(f"❌ {result.error}")
        else:
            await placeholder.edit_text("Unknown action. Use: screenshot, extract, info")

    except Exception as e:
        await placeholder.edit_text(f"❌ Browser error: {e}")


# ─── Productivity commands: remind, commitments, doctor, export, compact ──────

async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set a reminder: /remind <text with time expression>."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    from . import commitments
    commitments.init_db()
    text = (update.message.text or "").split(None, 1)
    if len(text) < 2:
        await update.message.reply_text(
            "Usage: `/remind buy groceries tomorrow`\n"
            "Time expressions: in 30min, in 2 hours, tomorrow, next week, monday, etc.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    arg = text[1]
    records = commitments.auto_extract_and_store(
        platform=PLATFORM, user_id=str(uid),
        user_text=arg,
    )
    if records:
        lines = [f"Set {len(records)} reminder(s):"]
        for r in records:
            from datetime import datetime as _dt
            due = _dt.fromtimestamp(r.due_at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {r.reason} — due {due}")
        await update.message.reply_text("\n".join(lines))
    else:
        # If no pattern matched, store as a simple 24h reminder
        from datetime import datetime as _dt, timedelta as _td
        due = (_dt.now() + _td(hours=24)).timestamp()
        r = commitments.add_commitment(
            platform=PLATFORM, user_id=str(uid),
            kind="reminder", reason=arg, due_at=due,
        )
        due_str = _dt.fromtimestamp(r.due_at).strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text(f"Reminder set: {arg}\nDue: {due_str}")


async def cmd_commitments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View pending reminders and commitments."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    from . import commitments
    commitments.init_db()
    pending = commitments.get_pending(PLATFORM, str(uid))
    text = commitments.format_pending(pending)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_doctor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Run diagnostic checks."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    from . import doctor
    placeholder = await update.message.reply_text("🩺 Running diagnostics...")
    try:
        report = await doctor.run_doctor()
        await placeholder.edit_text(report.format())
    except Exception as e:
        await placeholder.edit_text(f"❌ Doctor error: {e}")


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Export conversation: /export [text|md|html|json]."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    from . import conversation_export
    from . import store as _st
    text = (update.message.text or "").split(None, 1)
    fmt = text[1].strip().lower() if len(text) > 1 else "text"

    sess = _active_session(uid)
    history = _st.load_history(PLATFORM, str(uid), session_name=sess.name)
    if not history:
        await update.message.reply_text("No conversation to export.")
        return

    try:
        result = conversation_export.export_conversation(history, fmt, title=f"Session: {sess}")
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f".{result.filename.rsplit('.', 1)[-1]}",
        delete=False, prefix="telechat_export_",
    ) as f:
        f.write(result.content)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as f:
            await update.message.reply_document(
                f, filename=result.filename,
                caption=f"Exported {result.message_count} messages as {result.format}",
            )
    finally:
        import os as _os
        _os.unlink(tmp_path)


async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Compact conversation history to save tokens."""
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    from . import context_compaction
    from . import store as _st
    sess = _active_session(uid)
    history = _st.load_history(PLATFORM, str(uid), session_name=sess.name)
    if not history:
        await update.message.reply_text("No conversation to compact.")
        return

    result = context_compaction.compact_history_sync(history)
    if result.messages_compacted == 0:
        await update.message.reply_text(
            f"No compaction needed ({result.messages_before} messages, "
            f"~{result.tokens_before:,} tokens)."
        )
        return

    # Save compacted history
    _st.replace_history(PLATFORM, str(uid), result.history, session_name=sess.name)
    await update.message.reply_text(
        f"Compacted conversation:\n"
        f"  Messages: {result.messages_before} → {result.messages_after}\n"
        f"  Tokens: ~{result.tokens_before:,} → ~{result.tokens_after:,}\n"
        f"  Summarized {result.messages_compacted} older messages."
    )


# ─── Entry point ─────────────────────────────────────────────────────────────────

def build_app() -> Application:
    cc.init_db()

    from telegram.request import HTTPXRequest
    req = HTTPXRequest(
        http_version="1.1",
        connection_pool_size=8,
        httpx_kwargs={"verify": False},   # needed on some corporate networks
    )
    app = Application.builder().token(TOKEN).request(req).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("tasks",       cmd_tasks))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("sessions",    cmd_sessions))
    app.add_handler(CommandHandler("new",         cmd_new))
    app.add_handler(CommandHandler("switch",      cmd_switch))
    app.add_handler(CommandHandler("rename",      cmd_rename))
    app.add_handler(CommandHandler("title",       cmd_title))
    app.add_handler(CommandHandler("pin",         cmd_pin))
    app.add_handler(CommandHandler("archive",     cmd_archive))
    app.add_handler(CommandHandler("searchsess",  cmd_search_sessions))
    app.add_handler(CommandHandler("browse",      cmd_browse))
    app.add_handler(CommandHandler("reset",       cmd_reset))
    app.add_handler(CommandHandler("mode",        cmd_mode))
    app.add_handler(CommandHandler("model",       cmd_model))
    app.add_handler(CommandHandler("engine",      cmd_engine))
    app.add_handler(CommandHandler("verbose",     cmd_verbose))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("usage",       cmd_usage))
    app.add_handler(CommandHandler("quality",     cmd_quality))
    app.add_handler(CommandHandler("prefer",      cmd_prefer))
    app.add_handler(CommandHandler("feedback",    cmd_feedback))
    app.add_handler(CommandHandler("prompts",     cmd_prompts))
    app.add_handler(CommandHandler("watchdog",    cmd_watchdog))
    app.add_handler(CommandHandler("id",          cmd_id))
    app.add_handler(CommandHandler("tour",        cmd_tour))
    app.add_handler(CommandHandler("invite",      cmd_invite))
    app.add_handler(CommandHandler("invites",     cmd_invites))
    app.add_handler(CommandHandler("revoke",      cmd_revoke))
    app.add_handler(CommandHandler("kick",        cmd_kick))
    app.add_handler(CommandHandler("remember",    cmd_remember))
    app.add_handler(CommandHandler("recall",      cmd_recall))
    app.add_handler(CommandHandler("memories",    cmd_memories))
    app.add_handler(CommandHandler("forget",      cmd_forget))
    app.add_handler(CommandHandler("editmem",     cmd_editmem))
    app.add_handler(CommandHandler("exportmem",   cmd_exportmem))
    app.add_handler(CommandHandler("importmem",   cmd_importmem))
    app.add_handler(CommandHandler("extractmem",  cmd_extractmem))
    app.add_handler(CommandHandler("poll",        cmd_poll))
    app.add_handler(CommandHandler("tts",         cmd_tts))
    app.add_handler(CommandHandler("imagine",     cmd_imagine))
    app.add_handler(CommandHandler("search",      cmd_search))
    app.add_handler(CommandHandler("project",     cmd_project))
    app.add_handler(CommandHandler("code",        cmd_code))
    app.add_handler(CommandHandler("music",       cmd_music))
    app.add_handler(CommandHandler("video",       cmd_video))
    app.add_handler(CommandHandler("fetch",       cmd_fetch))
    # ── New feature commands ──
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("fork",        cmd_fork))
    app.add_handler(CommandHandler("budget",      cmd_budget))
    app.add_handler(CommandHandler("plan",        cmd_plan))
    app.add_handler(CommandHandler("schedule",    cmd_schedule))
    app.add_handler(CommandHandler("kb",          cmd_kb))
    app.add_handler(CommandHandler("web",         cmd_browse_web))
    # ── Productivity commands ──
    app.add_handler(CommandHandler("remind",      cmd_remind))
    app.add_handler(CommandHandler("commitments", cmd_commitments))
    app.add_handler(CommandHandler("doctor",      cmd_doctor))
    app.add_handler(CommandHandler("export",      cmd_export))
    app.add_handler(CommandHandler("compact",     cmd_compact))

    # ── Claude Desktop bridge ──
    try:
        from . import desktop_bridge as _bridge
        _bridge.register(app)
        async def _bridge_cb_wrap(update, ctx):
            if not _allowed(update.callback_query.from_user.id):
                await update.callback_query.answer()
                return
            await _bridge.try_handle_callback(update, ctx)
        app.add_handler(CallbackQueryHandler(_bridge_cb_wrap, pattern=r"^bridge:"))
    except Exception as _bridge_exc:
        log.warning("desktop_bridge registration failed: %s", _bridge_exc)

    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^tg:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,              handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,       handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


BOT_COMMANDS = [
    BotCommand("desktop", "Running Claude Desktop sessions"),
    BotCommand("recent", "Resume a recent Claude session"),
    BotCommand("follow", "Live-mirror a session to Telegram"),
    BotCommand("settings", "Model, engine, permissions, verbosity"),
    BotCommand("sessions", "View and switch sessions"),
    BotCommand("new", "Create a new session"),
    BotCommand("tasks", "Show running tasks"),
    BotCommand("cancel", "Cancel a task"),
    BotCommand("reset", "Clear conversation history"),
    BotCommand("browse", "Browse project files"),
    BotCommand("search", "Web search"),
    BotCommand("imagine", "Generate an image"),
    BotCommand("tts", "Text to speech"),
    BotCommand("poll", "Create a poll"),
    BotCommand("remember", "Save a memory"),
    BotCommand("recall", "Search memories"),
    BotCommand("usage", "Token usage stats"),
    BotCommand("budget", "Cost budget and usage"),
    BotCommand("plan", "Decompose complex tasks (two-agent)"),
    BotCommand("resume", "Resume a previous session"),
    BotCommand("fork", "Fork a session into a new branch"),
    BotCommand("schedule", "Schedule recurring tasks"),
    BotCommand("kb", "Knowledge base management"),
    BotCommand("web", "Browser automation"),
    BotCommand("remind", "Set a reminder"),
    BotCommand("commitments", "View pending reminders"),
    BotCommand("invite", "Create an invite link"),
    BotCommand("invites", "Your invites and who joined"),
    BotCommand("tour", "Replay the walkthrough"),
    BotCommand("doctor", "Run diagnostic checks"),
    BotCommand("export", "Export conversation"),
    BotCommand("compact", "Compact conversation history"),
    BotCommand("help", "Show all commands"),
]


async def run_telegram():
    log.info("Telegram bot starting…")
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("Bot menu commands registered (%d commands).", len(BOT_COMMANDS))
    except Exception as exc:
        log.warning("Failed to set bot commands: %s", exc)
    log.info("Telegram bot is running.")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
