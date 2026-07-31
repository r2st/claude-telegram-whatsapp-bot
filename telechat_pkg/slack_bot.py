"""
Slack adapter — uses Socket Mode (no webhook, no public URL).

Each developer creates their own Slack app and runs this locally.
Works on corporate networks — pure outbound WebSocket.

Setup (one-time, ~5 min):
  1. Go to https://api.slack.com/apps → Create New App → From scratch
  2. Settings → Socket Mode → Enable → Create App-Level Token
     Scopes: connections:write  →  copy the xapp-... token
  3. OAuth & Permissions → Bot Token Scopes:
       chat:write, channels:history, groups:history,
       im:history, im:write, app_mentions:read, reactions:write
  4. Event Subscriptions → Enable → Subscribe to bot events:
       message.im, message.channels, message.groups, app_mention
  5. Install to workspace → copy the xoxb-... Bot Token
  6. Invite your bot to any channels you want it active in (/invite @yourbot)
"""
from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import claude_core as cc
from .memory import MemoryStore

log = logging.getLogger(__name__)

PLATFORM = "slack"

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]   # xoxb-...
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]    # xapp-...

ALLOWED_SLACK_USERS: list[str] = [
    u.strip()
    for u in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",")
    if u.strip()
]

# ─── Per-user settings (in-memory) ────────────────────────────────────────────

_user_model:  dict[str, str] = {}
_user_engine: dict[str, str] = {}

_DEFAULT_MODEL  = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_ENGINE = cc.CLAUDE_MODE

CLI_MODELS = {"haiku": "Haiku (fastest)", "sonnet": "Sonnet (balanced)", "opus": "Opus (most capable)"}
ENGINE_MODES = {"cli": "CLI (subprocess)", "api": "API (Anthropic Messages)"}

def _model(uid: str)  -> str: return _user_model.get(uid, _DEFAULT_MODEL)
def _engine(uid: str) -> str: return _user_engine.get(uid, _DEFAULT_ENGINE)

_memory = MemoryStore()


def _parse_remember_args(text: str) -> tuple[str, list[str], float]:
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


# ─── Task tracking ────────────────────────────────────────────────────────────

_task_counter = itertools.count(1)

_TOOL_LABELS = {
    "Bash":      ":computer: Running command",
    "Read":      ":book: Reading file",
    "Write":     ":memo: Writing file",
    "Edit":      ":pencil2: Editing file",
    "Grep":      ":mag: Searching code",
    "ListDir":   ":file_folder: Listing directory",
    "WebSearch": ":globe_with_meridians: Searching the web",
    "WebFetch":  ":globe_with_meridians: Fetching page",
    "Agent":     ":robot_face: Delegating to agent",
    "TodoWrite": ":clipboard: Planning",
}

def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, f":wrench: Using {name}")


class SlackTask:
    """Tracks a running Claude request with live progress updates."""

    def __init__(self, client, channel: str, thread_ts: str, user_id: str, prompt: str):
        self.task_id = next(_task_counter)
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.user_id = user_id
        self.prompt_preview = prompt[:40]
        self.start_time = time.time()
        self.tools: list[str] = []
        self.tool_count = 0
        self._cancelled = False
        self._status_ts: str | None = None
        self._last_update = 0.0
        self._last_status = ""
        self._phase = "thinking"

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
        secs = int(time.time() - self.start_time)
        if self._phase == "streaming":
            fill = min(9, 7 + (secs % 3))
        elif self.tool_count > 0:
            fill = min(7, 2 + self.tool_count)
        else:
            fill = min(3, 1 + secs // 10)
        return "`[" + "=" * fill + " " * (10 - fill) + "]`"

    def _build_status(self) -> str:
        elapsed = self._elapsed()
        if self._phase == "thinking":
            phase = ":brain: *Thinking…*"
        elif self._phase == "working":
            phase = ":gear: *Working…*"
        else:
            phase = ":writing_hand: *Writing…*"

        lines = [f"{phase}  `{elapsed}`", self._progress_bar()]

        if self.tools:
            last = self.tools[-1]
            lines.append(f"\n{_tool_label(last)}…")

        if self.tool_count > 1:
            lines.append(f"_({self.tool_count} steps so far)_")

        return "\n".join(lines)

    def post_status(self):
        status = self._build_status()
        if status == self._last_status:
            return
        now = time.time()
        min_interval = 2.0 if (now - self.start_time) < 15 else 4.0
        if self._status_ts and (now - self._last_update < min_interval):
            return
        self._last_update = now
        self._last_status = status

        cancel_block = {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": ":stop_button: Cancel"},
                "action_id": f"cancel_task_{self.task_id}",
                "style": "danger",
            }]
        }
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": status}},
            cancel_block,
        ]

        try:
            if self._status_ts:
                self.client.chat_update(
                    channel=self.channel, ts=self._status_ts,
                    text=status, blocks=blocks,
                )
            else:
                resp = self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts,
                    text=status, blocks=blocks,
                )
                self._status_ts = resp["ts"]
        except Exception:
            # The status card is a nicety; losing it must not fail the turn.
            # Logged so "why did the progress card stop updating?" is answerable.
            log.debug("Slack status post failed", exc_info=True)

    def finish_status(self, summary: str):
        if not self._status_ts:
            return
        try:
            self.client.chat_update(
                channel=self.channel, ts=self._status_ts,
                text=summary,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}],
            )
        except Exception:
            log.debug("Slack status update failed", exc_info=True)

    def delete_status(self):
        if not self._status_ts:
            return
        try:
            self.client.chat_delete(channel=self.channel, ts=self._status_ts)
        except Exception:
            log.debug("Slack status delete failed", exc_info=True)

    def on_tool(self, tool_name: str, detail: str = ""):
        self._phase = "working"
        self.tools.append(tool_name)
        self.tool_count += 1
        self.post_status()

    def on_text(self, text: str):
        self._phase = "streaming"
        self.post_status()


class TaskRegistry:
    def __init__(self):
        self._tasks: dict[int, SlackTask] = {}
        self._lock = threading.Lock()

    def register(self, task: SlackTask):
        with self._lock:
            self._tasks[task.task_id] = task

    def unregister(self, task_id: int):
        with self._lock:
            self._tasks.pop(task_id, None)

    def get(self, task_id: int) -> SlackTask | None:
        return self._tasks.get(task_id)

    def get_user_tasks(self, uid: str) -> list[SlackTask]:
        return [t for t in self._tasks.values() if t.user_id == uid]

    def cancel_all_user(self, uid: str) -> int:
        count = 0
        for t in self.get_user_tasks(uid):
            t.cancel()
            count += 1
        return count


_task_registry = TaskRegistry()

# ─── Slack app ─────────────────────────────────────────────────────────────────

app = App(token=SLACK_BOT_TOKEN)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def _allowed(user_id: str) -> bool:
    return not ALLOWED_SLACK_USERS or user_id in ALLOWED_SLACK_USERS


def _add_reaction(client, channel: str, ts: str, emoji: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        # Usually already_reacted or a missing reactions:write scope — both
        # cosmetic, both worth seeing when someone asks why the ⏳ never appears.
        log.debug("Slack reactions_add(%s) failed", emoji, exc_info=True)


def _remove_reaction(client, channel: str, ts: str, emoji: str) -> None:
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        log.debug("Slack reactions_remove(%s) failed", emoji, exc_info=True)


def _post_reply(client, channel: str, thread_ts: str, text: str, blocks=None) -> str | None:
    """Send reply in-thread. Returns the message ts."""
    chunk = 3000
    if len(text) <= chunk:
        resp = client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=text, blocks=blocks, mrkdwn=True,
        )
        return resp.get("ts")

    # Chunked send for long responses
    first_ts = None
    for i in range(0, len(text), chunk):
        resp = client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=text[i:i + chunk], mrkdwn=True,
        )
        if first_ts is None:
            first_ts = resp.get("ts")
    return first_ts


def _finish_summary(task: SlackTask) -> str:
    elapsed = task._elapsed()
    if task.tool_count > 0:
        return f":white_check_mark: _{task.tool_count} tools · {elapsed}_"
    return f":white_check_mark: _{elapsed}_"


# ─── Core message handler ────────────────────────────────────────────────────

def _handle(client, channel: str, user_id: str, thread_ts: str, text: str) -> None:
    """Runs in a background thread so the Slack ack isn't blocked."""

    # ── Slash-style commands (work in DMs and @mentions) ──
    lower = text.lower().strip()
    if lower in ("help", "/help"):
        _cmd_help(client, channel, thread_ts)
        return
    if lower in ("reset", "/reset"):
        _cmd_reset(client, channel, thread_ts, user_id)
        return
    if lower in ("model", "/model"):
        _cmd_model(client, channel, thread_ts, user_id)
        return
    if lower in ("engine", "/engine"):
        _cmd_engine(client, channel, thread_ts, user_id)
        return
    if lower in ("mode", "/mode", "status", "/status"):
        _cmd_mode(client, channel, thread_ts, user_id)
        return
    if lower in ("usage", "/usage", "stats", "/stats"):
        _cmd_usage(client, channel, thread_ts, user_id)
        return
    if lower in ("sessions", "/sessions"):
        _cmd_sessions(client, channel, thread_ts, user_id)
        return
    if lower.startswith("new ") or lower.startswith("/new "):
        name = re.sub(r"^/?new\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_new_session(client, channel, thread_ts, user_id, name)
        return
    if lower.startswith("switch ") or lower.startswith("/switch "):
        target = re.sub(r"^/?switch\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_switch(client, channel, thread_ts, user_id, target)
        return
    if lower in ("tasks", "/tasks"):
        _cmd_tasks(client, channel, thread_ts, user_id)
        return
    if lower in ("cancel", "/cancel", "cancel all", "/cancel all"):
        _cmd_cancel(client, channel, thread_ts, user_id)
        return
    if lower.startswith("remember ") or lower.startswith("/remember "):
        arg = re.sub(r"^/?remember\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_remember(client, channel, thread_ts, user_id, arg)
        return
    if lower.startswith("recall ") or lower.startswith("/recall "):
        arg = re.sub(r"^/?recall\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_recall(client, channel, thread_ts, user_id, arg)
        return
    if lower in ("memories", "/memories") or lower.startswith("memories ") or lower.startswith("/memories "):
        arg = re.sub(r"^/?memories\s*", "", text, flags=re.IGNORECASE).strip()
        _cmd_memories(client, channel, thread_ts, user_id, arg)
        return
    if lower.startswith("forget ") or lower.startswith("/forget "):
        arg = re.sub(r"^/?forget\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_forget(client, channel, thread_ts, user_id, arg)
        return
    if lower.startswith("rename ") or lower.startswith("/rename "):
        arg = re.sub(r"^/?rename\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_rename_session(client, channel, thread_ts, user_id, arg)
        return
    if lower.startswith("title ") or lower.startswith("/title "):
        arg = re.sub(r"^/?title\s+", "", text, flags=re.IGNORECASE).strip()
        _cmd_title_session(client, channel, thread_ts, user_id, arg)
        return
    if lower in ("pin", "/pin"):
        _cmd_pin_session(client, channel, thread_ts, user_id)
        return
    if lower in ("archive", "/archive") or lower.startswith("archive ") or lower.startswith("/archive "):
        arg = re.sub(r"^/?archive\s*", "", text, flags=re.IGNORECASE).strip()
        _cmd_archive_session(client, channel, thread_ts, user_id, arg)
        return

    # ── Rate limiting ──
    if not cc.check_rate_limit(f"slack:{user_id}"):
        _post_reply(
            client, channel, thread_ts,
            f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s.",
        )
        return

    # ── Create task with live progress ──
    task = SlackTask(client, channel, thread_ts, user_id, text)
    _task_registry.register(task)

    _add_reaction(client, channel, thread_ts, "hourglass_flowing_sand")
    task.post_status()

    # Heartbeat thread for elapsed-time updates
    stop_evt = threading.Event()

    def _heartbeat():
        while not stop_evt.wait(timeout=4):
            if task.cancelled:
                break
            task.post_status()

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    try:
        log.info("← Slack [%s/%s] %s", channel, user_id, text[:120])

        sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        history = cc.load_history(PLATFORM, user_id, session_name=sess.name)
        engine = _engine(user_id)

        if engine == "api":
            reply, stats = cc.ask_claude_api(text, history)
        else:
            reply, stats = cc.ask_claude_sync(
                text, history,
                model=_model(user_id),
            )

        if task.cancelled:
            task.finish_status(f":stop_button: _Cancelled after {task._elapsed()}._")
            return

        is_error = reply.startswith("[Error]") or reply.startswith("[Claude error]") or reply.startswith("[Timeout]")

        if not is_error:
            cc.save_turn(PLATFORM, user_id, text, reply, session_name=sess.name)
        cc.track_usage(PLATFORM, user_id, stats.get("input_tokens", 0), stats.get("output_tokens", 0))
        log.info("→ Slack [%s/%s] %s", channel, user_id, reply[:120])

        # Stop heartbeat before sending final response
        stop_evt.set()
        hb.join(timeout=2)

        # Build final response with summary header
        summary = _finish_summary(task)
        tools_used = stats.get("tools_used", [])
        if tools_used:
            tools_str = ", ".join(tools_used[:5])
            if len(tools_used) > 5:
                tools_str += f" +{len(tools_used) - 5} more"
            header = f"{summary}\n:wrench: _{tools_str}_\n\n"
        else:
            header = f"{summary}\n\n"

        # Delete progress message, post final reply
        task.delete_status()

        if is_error:
            # Show error with retry context
            _post_reply(client, channel, thread_ts, f":x: {reply}")
        else:
            _post_reply(client, channel, thread_ts, header + reply)

    except Exception as exc:
        log.exception("Slack handler error")
        stop_evt.set()
        task.finish_status(f":x: _Error after {task._elapsed()}_")
        _post_reply(client, channel, thread_ts, f":x: Error: {exc}")
    finally:
        stop_evt.set()
        _remove_reaction(client, channel, thread_ts, "hourglass_flowing_sand")
        _task_registry.unregister(task.task_id)


# ─── Commands ─────────────────────────────────────────────────────────────────

def _cmd_help(client, channel: str, thread_ts: str):
    text = (
        ":wave: *Claude Bot — Commands*\n\n"
        "Just send a message to chat with Claude.\n\n"
        "*Settings:*\n"
        "• `model` — switch Claude model (haiku/sonnet/opus)\n"
        "• `engine` — switch engine (cli/api)\n"
        "• `mode` — show current settings\n"
        "• `reset` — clear conversation history\n\n"
        "*Sessions:*\n"
        "• `sessions [all]` — list sessions (all = include archived)\n"
        "• `new <name>` — create a new session\n"
        "• `switch <name>` — switch to a session\n"
        "• `rename <name>` — rename current session\n"
        "• `title <text>` — set session description\n"
        "• `pin` — pin/unpin session\n"
        "• `archive [name]` — archive a session\n\n"
        "*Tasks:*\n"
        "• `tasks` — show running tasks\n"
        "• `cancel` — cancel all running tasks\n\n"
        "*Memory:*\n"
        "• `remember <text> [#tag] [!0.9]` — save a memory\n"
        "• `recall <query>` — search your memories\n"
        "• `memories [#tag]` — list recent memories\n"
        "• `forget <id>` — delete a memory\n\n"
        "*Info:*\n"
        "• `usage` — show token usage stats\n"
        "• `help` — show this message"
    )
    _post_reply(client, channel, thread_ts, text)


def _cmd_remember(client, channel: str, thread_ts: str, user_id: str, arg: str):
    if not arg:
        _post_reply(client, channel, thread_ts, "Usage: `remember <text> [#tag1 #tag2] [!0.9]`")
        return
    content, tags, importance = _parse_remember_args(arg)
    if not content:
        _post_reply(client, channel, thread_ts, "Memory content can't be empty.")
        return
    mem = _memory.remember(PLATFORM, user_id, content, tags=tags or None, importance=importance)
    tag_str = f"  Tags: {', '.join(mem.tags)}" if mem.tags else ""
    _post_reply(client, channel, thread_ts, f":white_check_mark: Remembered! _ID: `{mem.id[:8]}…`_{tag_str}")


def _cmd_recall(client, channel: str, thread_ts: str, user_id: str, query: str):
    if not query:
        _post_reply(client, channel, thread_ts, "Usage: `recall <search query>`")
        return
    results = _memory.recall(PLATFORM, user_id, query, limit=5)
    if not results:
        _post_reply(client, channel, thread_ts, ":mag: No memories found.")
        return
    lines = [f":mag: *Found {len(results)} memor{'y' if len(results) == 1 else 'ies'}:*\n"]
    for r in results:
        tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
        lines.append(f"• {r.content}{tag_str}\n  _ID: `{r.id[:8]}…`_")
    _post_reply(client, channel, thread_ts, "\n".join(lines))


def _cmd_memories(client, channel: str, thread_ts: str, user_id: str, arg: str):
    filter_tags = None
    if arg:
        filter_tags = [w.lstrip("#") for w in arg.split() if w.startswith("#")]
    mems = _memory.list_memories(PLATFORM, user_id, limit=10, tags=filter_tags or None)
    if not mems:
        _post_reply(client, channel, thread_ts, ":mailbox_with_no_mail: No memories yet. Use `remember <text>` to save one.")
        return
    stats = _memory.stats(PLATFORM, user_id)
    tag_label = f" (tag: {', '.join(filter_tags)})" if filter_tags else ""
    lines = [f":brain: *Your memories* ({stats['total']} total){tag_label}:\n"]
    for m in mems:
        tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(f"• {m.content}{tag_str}\n  _ID: `{m.id[:8]}…`_")
    _post_reply(client, channel, thread_ts, "\n".join(lines))


def _cmd_forget(client, channel: str, thread_ts: str, user_id: str, target_id: str):
    if not target_id:
        _post_reply(client, channel, thread_ts, "Usage: `forget <memory-id>` — use `memories` to see IDs")
        return
    target_id = target_id.strip().rstrip("…")
    mems = _memory.list_memories(PLATFORM, user_id, limit=100)
    match = next((m for m in mems if m.id.startswith(target_id)), None)
    if match and _memory.forget(PLATFORM, user_id, match.id):
        _post_reply(client, channel, thread_ts, f":wastebasket: Forgotten: _{match.content[:60]}_")
    else:
        _post_reply(client, channel, thread_ts, ":x: Memory not found. Use `memories` to see your memories.")


def _cmd_reset(client, channel: str, thread_ts: str, user_id: str):
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    cc.clear_history(PLATFORM, user_id, session_name=sess.name)
    cc.clear_session(PLATFORM, user_id)
    sess.claude_session_id = None
    sess.message_count = 0
    _post_reply(client, channel, thread_ts,
                f":wastebasket: History cleared for session `{sess.name}`.")


def _cmd_model(client, channel: str, thread_ts: str, user_id: str):
    cur = _model(user_id)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":robot_face: *Current model:* `{cur}`\n\nSelect a model:"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"{label}{' :white_check_mark:' if key == cur else ''}"},
                    "action_id": f"set_model_{key}",
                    "value": key,
                }
                for key, label in CLI_MODELS.items()
            ],
        },
    ]
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Select model", blocks=blocks)


def _cmd_engine(client, channel: str, thread_ts: str, user_id: str):
    cur = _engine(user_id)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":gear: *Current engine:* `{cur}`\n\nSelect an engine:"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"{label}{' :white_check_mark:' if key == cur else ''}"},
                    "action_id": f"set_engine_{key}",
                    "value": key,
                }
                for key, label in ENGINE_MODES.items()
            ],
        },
    ]
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Select engine", blocks=blocks)


def _cmd_mode(client, channel: str, thread_ts: str, user_id: str):
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    text = (
        f":gear: *Current Settings*\n\n"
        f"• *Session:* `{sess.name}` {sess.status_emoji()}\n"
        f"• *Engine:* `{_engine(user_id)}`\n"
        f"• *Model:* `{_model(user_id)}`\n"
        f"• *Timeout:* `{cc.CLAUDE_TIMEOUT}s`"
    )
    _post_reply(client, channel, thread_ts, text)


def _cmd_usage(client, channel: str, thread_ts: str, user_id: str):
    u = cc.get_usage(PLATFORM, user_id)
    text = (
        f":bar_chart: *Usage Stats*\n\n"
        f"• Messages: `{u['messages']}`\n"
        f"• Input tokens: `{u['input']:,}`\n"
        f"• Output tokens: `{u['output']:,}`"
    )
    _post_reply(client, channel, thread_ts, text)


def _cmd_sessions(client, channel: str, thread_ts: str, user_id: str):
    sessions = cc._session_mgr.get_all(PLATFORM, user_id)
    if not sessions:
        sessions = [cc._session_mgr.get_or_create_active(PLATFORM, user_id)]

    # Auto-archive idle sessions
    cc._session_mgr.auto_archive_idle(PLATFORM, user_id)

    active_idx = cc._session_mgr.get_active_index(PLATFORM, user_id)
    lines = [":card_index_dividers: *Your Sessions*\n"]
    for i, s in enumerate(sessions):
        marker = "  :point_left: _active_" if i == active_idx else ""
        lines.append(f"• {s.summary_line()}{marker}")

    elements = []
    for i, s in enumerate(sessions):
        if i != active_idx:
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": f"Switch to: {s.display_name[:30]}"},
                "action_id": f"switch_session_{i}",
                "value": str(i),
            })
    elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": ":heavy_plus_sign: New session"},
        "action_id": "new_session_auto",
        "style": "primary",
    })

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    if elements:
        blocks.append({"type": "actions", "elements": elements[:5]})

    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines), blocks=blocks)


def _cmd_new_session(client, channel: str, thread_ts: str, user_id: str, name: str):
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:20] if name else f"session-{int(time.time()) % 10000}"
    sess = cc._session_mgr.create(PLATFORM, user_id, name)
    _post_reply(client, channel, thread_ts,
                f":white_check_mark: Created and switched to session `{sess.name}`")


def _cmd_switch(client, channel: str, thread_ts: str, user_id: str, target: str):
    # Try by name first, then by index
    s = cc._session_mgr.switch_to_name(PLATFORM, user_id, target)
    if not s:
        try:
            idx = int(target)
            s = cc._session_mgr.switch_to(PLATFORM, user_id, idx)
        except ValueError:
            pass
    if s:
        _post_reply(client, channel, thread_ts,
                    f":white_check_mark: Switched to `{s.display_name}`")
    else:
        _post_reply(client, channel, thread_ts, f"Session `{target}` not found.")


def _cmd_rename_session(client, channel: str, thread_ts: str, user_id: str, new_name: str):
    if not new_name:
        _post_reply(client, channel, thread_ts, "Usage: `rename <new-name>`")
        return
    new_name = re.sub(r"[^a-zA-Z0-9_-]", "-", new_name)[:20]
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    result = cc._session_mgr.rename(PLATFORM, user_id, sess.name, new_name)
    if result:
        _post_reply(client, channel, thread_ts, f":white_check_mark: Renamed to `{result.name}`")
    else:
        _post_reply(client, channel, thread_ts, ":x: Rename failed — name may already be taken.")


def _cmd_title_session(client, channel: str, thread_ts: str, user_id: str, title: str):
    if not title:
        _post_reply(client, channel, thread_ts, "Usage: `title <description>`")
        return
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    result = cc._session_mgr.set_title(PLATFORM, user_id, sess.name, title)
    if result:
        _post_reply(client, channel, thread_ts, f":white_check_mark: Title set: _{result.title}_")
    else:
        _post_reply(client, channel, thread_ts, ":x: Failed.")


def _cmd_pin_session(client, channel: str, thread_ts: str, user_id: str):
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    new_state = not sess.pinned
    result = cc._session_mgr.pin(PLATFORM, user_id, sess.name, new_state)
    if result:
        label = ":pushpin: Pinned" if result.pinned else "Unpinned"
        _post_reply(client, channel, thread_ts, f"{label} session `{result.name}`")
    else:
        _post_reply(client, channel, thread_ts, ":x: Failed.")


def _cmd_archive_session(client, channel: str, thread_ts: str, user_id: str, name: str):
    if not name:
        name = cc._session_mgr.get_or_create_active(PLATFORM, user_id).name
    result = cc._session_mgr.archive(PLATFORM, user_id, name)
    if result:
        _post_reply(client, channel, thread_ts,
                    f":package: Archived `{result.display_name}`. Use `sessions all` to see archived.")
    else:
        _post_reply(client, channel, thread_ts, ":x: Cannot archive.")


def _cmd_tasks(client, channel: str, thread_ts: str, user_id: str):
    tasks = _task_registry.get_user_tasks(user_id)
    if not tasks:
        _post_reply(client, channel, thread_ts, "No active tasks.")
        return
    lines = [f":zap: *Active Tasks ({len(tasks)}):*\n"]
    for t in tasks:
        lines.append(f"• `#{t.task_id}` — {t.prompt_preview}… ({t._elapsed()})")

    elements = [{
        "type": "button",
        "text": {"type": "plain_text", "text": ":stop_button: Cancel All"},
        "action_id": "cancel_all_tasks",
        "style": "danger",
    }]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "actions", "elements": elements},
    ]
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines), blocks=blocks)


def _cmd_cancel(client, channel: str, thread_ts: str, user_id: str):
    count = _task_registry.cancel_all_user(user_id)
    if count:
        _post_reply(client, channel, thread_ts, f":stop_button: Cancelling {count} task(s)…")
    else:
        _post_reply(client, channel, thread_ts, "No active tasks to cancel.")


# ─── Button action handlers ──────────────────────────────────────────────────

@app.action(re.compile(r"^set_model_"))
def handle_set_model(ack, body, client):
    ack()
    action = body["actions"][0]
    model_key = action["action_id"].replace("set_model_", "")
    user_id = body["user"]["id"]
    _user_model[user_id] = model_key
    label = CLI_MODELS.get(model_key, model_key)
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
        text=f":white_check_mark: Model set to *{label}*",
        mrkdwn=True,
    )


@app.action(re.compile(r"^set_engine_"))
def handle_set_engine(ack, body, client):
    ack()
    action = body["actions"][0]
    engine_key = action["action_id"].replace("set_engine_", "")
    user_id = body["user"]["id"]
    _user_engine[user_id] = engine_key
    label = ENGINE_MODES.get(engine_key, engine_key)
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
        text=f":white_check_mark: Engine set to *{label}*",
        mrkdwn=True,
    )


@app.action(re.compile(r"^cancel_task_"))
def handle_cancel_task(ack, body, client):
    ack()
    action = body["actions"][0]
    task_id = int(action["action_id"].replace("cancel_task_", ""))
    user_id = body["user"]["id"]
    task = _task_registry.get(task_id)
    if task and task.user_id == user_id:
        task.cancel()


@app.action("cancel_all_tasks")
def handle_cancel_all(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    count = _task_registry.cancel_all_user(user_id)
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
        text=f":stop_button: Cancelling {count} task(s)…",
    )


@app.action(re.compile(r"^switch_session_"))
def handle_switch_session(ack, body, client):
    ack()
    action = body["actions"][0]
    idx = int(action["value"])
    user_id = body["user"]["id"]
    s = cc._session_mgr.switch_to(PLATFORM, user_id, idx)
    msg = f":white_check_mark: Switched to `{s.name}`" if s else "Session not found."
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
        text=msg, mrkdwn=True,
    )


@app.action("new_session_auto")
def handle_new_session(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    name = f"session-{int(time.time()) % 10000}"
    sess = cc._session_mgr.create(PLATFORM, user_id, name)
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
        text=f":white_check_mark: Created and switched to session `{sess.name}`",
        mrkdwn=True,
    )


# ─── Event handlers ────────────────────────────────────────────────────────────

def _dispatch(client, event: dict) -> None:
    """Common dispatch for all message/mention events."""
    user_id  = event.get("user", "")
    channel  = event.get("channel", "")
    text     = (event.get("text") or "").strip()
    ts       = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts

    if event.get("bot_id") or not user_id or not text:
        return

    if not _allowed(user_id):
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Sorry, you're not on the allowed list.",
        )
        return

    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not text:
        return

    threading.Thread(
        target=_handle,
        args=(client, channel, user_id, thread_ts, text),
        daemon=True,
    ).start()


@app.event("app_mention")
def handle_mention(client, event, say):
    """Respond when @mentioned in any channel."""
    _dispatch(client, event)


@app.event("message")
def handle_dm(client, event, say):
    """Respond to DMs only. Channel messages require an @mention (handled by app_mention)."""
    if event.get("subtype"):
        return
    channel_type = event.get("channel_type", "")
    if channel_type != "im":
        channel = event.get("channel", "")
        if not channel.startswith("D"):
            return
    _dispatch(client, event)


# ─── Entry point ─────────────────────────────────────────────────────────────────

def run_slack() -> None:
    cc.init_db()
    log.info("Slack bot starting (Socket Mode)…")
    log.info("Model: %s | Claude mode: %s", cc.CLAUDE_MODEL, cc.CLAUDE_MODE)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()   # blocking
