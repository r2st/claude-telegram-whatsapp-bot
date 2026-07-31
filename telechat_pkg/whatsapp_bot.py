"""
WhatsApp adapter — polls Green API, hands messages to claude_core.
Run via main.py with BOT_MODE=whatsapp or BOT_MODE=both.

Each user runs their own instance against their own Green API credentials
(personal free-tier account, QR-scanned to their WhatsApp number).

Interactive commands (send any of these):
  !help       — Show available commands
  !reset      — Clear conversation history
  !mode       — Show current mode and model
  !model X    — Switch model (haiku/sonnet/opus)
  !sessions   — List sessions
  !new NAME   — Create new session
  !switch N   — Switch to session N
  !usage      — Show usage statistics
  !verbose    — Toggle verbose mode (show tool activity)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from . import claude_core as cc
from .memory import MemoryStore, extract_memories

load_dotenv()

log = logging.getLogger(__name__)

PLATFORM = "whatsapp"

# ─── Green API config ───────────────────────────────────────────────────────────

INSTANCE_ID   = os.environ["GREEN_API_INSTANCE_ID"]
API_TOKEN     = os.environ["GREEN_API_TOKEN"]
BASE_URL      = os.getenv("GREEN_API_BASE_URL", "https://api.green-api.com").rstrip("/") + f"/waInstance{INSTANCE_ID}"

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))

ALLOWED_NUMBERS: list[str] = [
    n.strip().lstrip("+")
    for n in os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",")
    if n.strip()
]

# ─── Per-user state ─────────────────────────────────────────────────────────────

_locks: dict[str, threading.Lock] = {}
_verbose: dict[str, bool] = {}  # user → verbose mode
_user_model: dict[str, str] = {}  # user → model override
_browse_cwd: dict[str, Path] = {}  # user → current browse directory
_browse_items: dict[str, list[Path]] = {}  # user → last listed items (for !cd N)

BROWSE_ROOT = Path(cc.CLAUDE_WORK_DIR).expanduser().resolve()
BROWSE_PAGE_SIZE = 15


def _within_root(p: Path) -> bool:
    """True iff ``p`` resolves to a path inside BROWSE_ROOT (no .. / symlink escape)."""
    try:
        resolved = p.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    # Path.is_relative_to was added in 3.9; we target 3.10+ per pyproject.
    try:
        return resolved == BROWSE_ROOT or resolved.is_relative_to(BROWSE_ROOT)
    except AttributeError:  # pragma: no cover — defensive for older runtimes
        try:
            resolved.relative_to(BROWSE_ROOT)
            return True
        except ValueError:
            return False


def _safe_join(base: Path, user_input: str) -> Path | None:
    """Resolve ``user_input`` against ``base`` and confirm the result stays under BROWSE_ROOT.

    Returns None if the resolved path escapes the sandbox.
    """
    candidate = Path(user_input).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate if _within_root(candidate) else None

_memory = MemoryStore()

TOOL_ICONS = {
    "Read": "📖", "Edit": "✏️", "Write": "📝", "Bash": "💻",
    "Grep": "🔍", "Glob": "📂", "WebSearch": "🌐", "WebFetch": "🌐",
    "TodoRead": "📋", "TodoWrite": "📋", "Agent": "🤖",
}


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


def _active_session(sender: str):
    return cc._session_mgr.get_or_create_active(PLATFORM, sender)


#: Guards creation of the per-chat locks below. `if chat_id not in _locks:
#: _locks[chat_id] = Lock()` is a check-then-act: the poll loop spawns a thread
#: per message, so two messages arriving together in the same chat could both
#: find the key missing and each get their *own* lock — losing exactly the
#: one-turn-at-a-time guarantee the lock exists to provide.
_locks_registry_lock = threading.Lock()


def _lock_for(chat_id: str) -> threading.Lock:
    """The per-chat lock, created once however many threads ask at once."""
    with _locks_registry_lock:
        return _locks.setdefault(chat_id, threading.Lock())


# ─── Green API helpers ──────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> Optional[dict]:
    url = f"{BASE_URL}/{path}/{API_TOKEN}"
    try:
        r = requests.request(method, url, timeout=30, **kwargs)
        r.raise_for_status()
        return r.json() if r.text else {}
    except requests.RequestException as exc:
        log.error("Green API %s %s → %s", method, path, exc)
        return None


def receive_notification() -> Optional[dict]:
    return _api("GET", "receiveNotification")


def delete_notification(receipt_id: int) -> None:
    url = f"{BASE_URL}/deleteNotification/{API_TOKEN}/{receipt_id}"
    try:
        r = requests.request("DELETE", url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.error("Green API DELETE deleteNotification → %s", exc)


def send_message(chat_id: str, text: str) -> None:
    _api("POST", "sendMessage", json={"chatId": chat_id, "message": text})


def send_typing(chat_id: str) -> None:
    _api("POST", "sendChatState", json={"chatId": chat_id, "chatState": "textMessage"})


# ─── Auth / rate limit ──────────────────────────────────────────────────────────

def _allowed(sender: str) -> bool:
    if not ALLOWED_NUMBERS:
        return True
    number = sender.split("@")[0]
    return number in ALLOWED_NUMBERS


# ─── Command handling ───────────────────────────────────────────────────────────

HELP_TEXT = """*Claude Bot Commands*

*Chat:*
!reset — Clear conversation history
!model <name> — Switch model (haiku/sonnet/opus)
!mode — Show current mode/model
!verbose — Toggle tool activity messages

*Browse files:*
!browse — Browse project folders
!cd <n> — Enter folder #n
!up — Go to parent folder
!view <n> — View file #n
!ask — Ask Claude about current folder

*Sessions:*
!sessions [all] — List sessions (all = include archived)
!new <name> — Create new session
!switch <name|n> — Switch to session
!rename <name> — Rename current session
!title <text> — Set session description
!pin — Pin/unpin session
!archive [name] — Archive a session
!searchsess <q> — Search sessions

*Memory:*
!remember <text> [#tag] [!0.9] — Save a memory
!recall <query> — Search your memories
!memories [#tag] — List recent memories
!forget <id> — Delete a memory
!editmem <id> <new text> — Update a memory
!exportmem — Export memories as JSON
!extractmem — Extract memories from chat

*Stats:*
!usage — Usage statistics
!id — Show your WhatsApp number

_Just type normally to chat with Claude._"""


def _format_browse(sender: str, directory: Path, page: int = 0) -> str:
    """Build a text listing of a directory for WhatsApp."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return "⛔ Permission denied."

    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    all_items = dirs + files

    total_pages = max(1, (len(all_items) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    page = min(page, total_pages - 1)
    start = page * BROWSE_PAGE_SIZE
    page_items = all_items[start:start + BROWSE_PAGE_SIZE]

    # Store items for !cd N / !view N
    _browse_items[sender] = page_items
    _browse_cwd[sender] = directory

    try:
        rel = directory.relative_to(BROWSE_ROOT)
    except ValueError:
        rel = directory
    header = f"📂 *{rel or '.'}/*\n_{len(dirs)} folders, {len(files)} files_"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"

    lines = [header, ""]
    for i, item in enumerate(page_items, start + 1):
        if item.is_dir():
            lines.append(f"  {i}. 📁 {item.name}/")
        else:
            size = item.stat().st_size
            if size < 1024:
                sz = f"{size}B"
            elif size < 1024 * 1024:
                sz = f"{size // 1024}KB"
            else:
                sz = f"{size // (1024*1024)}MB"
            lines.append(f"  {i}. 📄 {item.name} ({sz})")

    lines.append("")
    nav = []
    if directory != BROWSE_ROOT:
        nav.append("!up — parent")
    if page > 0:
        nav.append(f"!page {page} — prev")
    if page < total_pages - 1:
        nav.append(f"!page {page + 2} — next")
    nav.append("!cd <n> — enter folder")
    nav.append("!view <n> — view file")
    nav.append("!ask — ask Claude")
    lines.append("_" + " | ".join(nav[:3]) + "_")
    if len(nav) > 3:
        lines.append("_" + " | ".join(nav[3:]) + "_")

    return "\n".join(lines)


def _handle_command(chat_id: str, sender: str, text: str) -> bool:
    """Handle ! commands. Returns True if it was a command."""
    lower = text.lower().strip()
    if not lower.startswith("!"):
        return False

    parts = lower.split(None, 1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "!help":
        send_message(chat_id, HELP_TEXT)

    elif cmd == "!reset":
        # Clear history for the currently active session so !reset actually
        # affects what normal chat reads/writes.
        sess = _active_session(sender)
        cc.clear_history(PLATFORM, sender, session_name=sess.name)
        cc.clear_session(PLATFORM, sender)
        send_message(chat_id, "🔄 Conversation reset. Starting fresh!")

    elif cmd == "!mode":
        model = _user_model.get(sender, cc.CLAUDE_MODEL)
        verbose = "on" if _verbose.get(sender) else "off"
        sess = cc._session_mgr.get_or_create_active(PLATFORM, sender)
        msg = (
            f"*Mode:* {cc.CLAUDE_MODE}\n"
            f"*Model:* {model}\n"
            f"*Session:* {sess.name}\n"
            f"*Verbose:* {verbose}\n"
            f"*Timeout:* {cc.CLAUDE_TIMEOUT}s"
        )
        send_message(chat_id, msg)

    elif cmd == "!model":
        valid = ("haiku", "sonnet", "opus")
        if arg not in valid:
            send_message(chat_id, f"Usage: !model <{'|'.join(valid)}>")
        else:
            _user_model[sender] = arg
            send_message(chat_id, f"✅ Model switched to *{arg}*")

    elif cmd == "!sessions":
        show_archived = arg == "all"
        sessions = cc._session_mgr.get_all(PLATFORM, sender, include_archived=show_archived)
        if not sessions:
            sessions = [cc._session_mgr.get_or_create_active(PLATFORM, sender)]
        # Auto-archive idle
        cc._session_mgr.auto_archive_idle(PLATFORM, sender)
        active_idx = cc._session_mgr.get_active_index(PLATFORM, sender)
        lines = ["*Your sessions:*\n"]
        for i, s in enumerate(sessions):
            marker = " 👈" if i == active_idx else ""
            lines.append(f"{i+1}. {s.summary_line()}{marker}")
        lines.append("\n_!switch <n>, !new <name>, !rename <name>, !title <text>, !pin, !archive_")
        send_message(chat_id, "\n".join(lines))

    elif cmd == "!new":
        name = arg or f"session-{int(time.time()) % 10000}"
        name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:20]
        sess = cc._session_mgr.create(PLATFORM, sender, name)
        send_message(chat_id, f"✅ Created session *{sess.name}*")

    elif cmd == "!switch":
        # Try by name first, then by 1-based index
        s = cc._session_mgr.switch_to_name(PLATFORM, sender, arg)
        if not s:
            try:
                idx = int(arg) - 1
                s = cc._session_mgr.switch_to(PLATFORM, sender, idx)
            except ValueError:
                pass
        if s:
            send_message(chat_id, f"✅ Switched to *{s.display_name}*")
        else:
            send_message(chat_id, "❌ Session not found. Use !sessions to see the list.")

    elif cmd == "!rename":
        if not arg:
            send_message(chat_id, "Usage: !rename <new-name>")
        else:
            new_name = re.sub(r"[^a-zA-Z0-9_-]", "-", arg)[:20]
            sess = _active_session(sender)
            result = cc._session_mgr.rename(PLATFORM, sender, sess.name, new_name)
            if result:
                send_message(chat_id, f"✅ Renamed to *{result.name}*")
            else:
                send_message(chat_id, "❌ Rename failed — name may already be taken.")

    elif cmd == "!title":
        if not arg:
            send_message(chat_id, "Usage: !title <description>")
        else:
            sess = _active_session(sender)
            raw_title = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else arg
            result = cc._session_mgr.set_title(PLATFORM, sender, sess.name, raw_title)
            if result:
                send_message(chat_id, f"✅ Title: _{result.title}_")
            else:
                send_message(chat_id, "❌ Failed.")

    elif cmd == "!pin":
        sess = _active_session(sender)
        new_state = not sess.pinned
        result = cc._session_mgr.pin(PLATFORM, sender, sess.name, new_state)
        if result:
            send_message(chat_id, f"{'📌 Pinned' if result.pinned else 'Unpinned'} session *{result.name}*")
        else:
            send_message(chat_id, "❌ Failed.")

    elif cmd == "!archive":
        name = arg or _active_session(sender).name
        result = cc._session_mgr.archive(PLATFORM, sender, name)
        if result:
            send_message(chat_id, f"📦 Archived *{result.name}*. Use !sessions all to see.")
        else:
            send_message(chat_id, "❌ Cannot archive.")

    elif cmd == "!searchsess":
        if not arg:
            send_message(chat_id, "Usage: !searchsess <query>")
        else:
            raw_query = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else arg
            results = cc._session_mgr.search(PLATFORM, sender, raw_query)
            if not results:
                send_message(chat_id, "🔍 No sessions found.")
            else:
                lines = [f"🔍 *Found {len(results)} session(s):*\n"]
                for s in results:
                    lines.append(s.summary_line())
                send_message(chat_id, "\n".join(lines))

    elif cmd == "!usage":
        usage = cc.get_usage(PLATFORM, sender)
        msg = (
            f"*Usage Stats*\n\n"
            f"Messages: {usage['messages']}\n"
            f"Input tokens: {usage['input']:,}\n"
            f"Output tokens: {usage['output']:,}\n"
            f"Total tokens: {usage['input'] + usage['output']:,}"
        )
        send_message(chat_id, msg)

    elif cmd == "!id":
        number = sender.split("@")[0]
        send_message(chat_id, f"📱 Your WhatsApp number: *{number}*\n\nAdd it to WHATSAPP_ALLOWED_NUMBERS in .env to whitelist yourself.")

    elif cmd == "!verbose":
        current = _verbose.get(sender, False)
        _verbose[sender] = not current
        state = "on" if not current else "off"
        send_message(chat_id, f"🔧 Verbose mode: *{state}*\n{'Tool activity will be shown.' if not current else 'Quiet mode.'}")

    elif cmd == "!browse":
        if arg:
            target = _safe_join(BROWSE_ROOT, arg)
            if target is None:
                send_message(chat_id, "❌ Path is outside the allowed browse root.")
                return True
        else:
            target = _browse_cwd.get(sender, BROWSE_ROOT)
        if target.is_dir():
            send_message(chat_id, _format_browse(sender, target))
        else:
            send_message(chat_id, f"❌ Not a directory: {target}")

    elif cmd == "!cd":
        if not arg:
            send_message(chat_id, "Usage: !cd <number>")
        else:
            try:
                idx = int(arg) - 1
                items = _browse_items.get(sender, [])
                if 0 <= idx < len(items) and items[idx].is_dir():
                    # Items came from _format_browse which only lists paths inside BROWSE_ROOT,
                    # but re-check defensively in case the listing pre-dated a config change.
                    if not _within_root(items[idx]):
                        send_message(chat_id, "❌ That entry is outside the allowed browse root.")
                    else:
                        send_message(chat_id, _format_browse(sender, items[idx]))
                elif 0 <= idx < len(items):
                    send_message(chat_id, f"❌ #{arg} is a file, not a folder. Use !view {arg}")
                else:
                    send_message(chat_id, "❌ Invalid number. Use !browse to see the list.")
            except ValueError:
                # Treat as path
                cwd = _browse_cwd.get(sender, BROWSE_ROOT)
                target = _safe_join(cwd, arg)
                if target is None:
                    send_message(chat_id, "❌ Path is outside the allowed browse root.")
                elif target.is_dir():
                    send_message(chat_id, _format_browse(sender, target))
                else:
                    send_message(chat_id, f"❌ Not a directory: {target}")

    elif cmd == "!up":
        cwd = _browse_cwd.get(sender, BROWSE_ROOT)
        parent = cwd.parent
        if parent.is_dir() and _within_root(parent):
            send_message(chat_id, _format_browse(sender, parent))
        else:
            send_message(chat_id, "📂 Already at the top level.")

    elif cmd == "!page":
        try:
            page = int(arg) - 1
            cwd = _browse_cwd.get(sender, BROWSE_ROOT)
            send_message(chat_id, _format_browse(sender, cwd, page))
        except ValueError:
            send_message(chat_id, "Usage: !page <number>")

    elif cmd == "!view":
        try:
            idx = int(arg) - 1
            items = _browse_items.get(sender, [])
            if 0 <= idx < len(items) and items[idx].is_file():
                fpath = items[idx]
                if not _within_root(fpath):
                    send_message(chat_id, "❌ That file is outside the allowed browse root.")
                    return True
                try:
                    content = fpath.read_text(errors="replace")[:3000]
                    try:
                        rel = fpath.resolve().relative_to(BROWSE_ROOT)
                    except ValueError:
                        rel = fpath
                    msg = f"📄 *{rel}*\n```\n{content}\n```"
                    send_message(chat_id, msg)
                except Exception as e:
                    send_message(chat_id, f"❌ Cannot read file: {e}")
            elif 0 <= idx < len(items):
                send_message(chat_id, f"❌ #{arg} is a folder. Use !cd {arg}")
            else:
                send_message(chat_id, "❌ Invalid number. Use !browse to see the list.")
        except ValueError:
            send_message(chat_id, "Usage: !view <number>")

    elif cmd == "!ask":
        cwd = _browse_cwd.get(sender, BROWSE_ROOT)
        try:
            rel = cwd.relative_to(BROWSE_ROOT)
        except ValueError:
            rel = cwd
        prompt = f"Describe what's in the folder {cwd} — what is this project/directory about? List the key files and their purposes."
        # This goes through Claude, so return False to let _handle process it
        # But we need to inject the prompt. Use a thread directly.
        threading.Thread(target=_handle, args=(chat_id, sender, prompt), daemon=True).start()

    elif cmd == "!remember":
        if not arg:
            send_message(chat_id, "Usage: !remember <text> [#tag1 #tag2] [!0.9]")
        else:
            raw_arg = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            content, tags, importance = _parse_remember_args(raw_arg)
            if not content:
                send_message(chat_id, "Memory content can't be empty.")
            else:
                mem = _memory.remember(PLATFORM, sender, content, tags=tags or None, importance=importance)
                tag_str = f"  Tags: {', '.join(mem.tags)}" if mem.tags else ""
                send_message(chat_id, f"✅ Remembered!\n_ID: {mem.id[:8]}…_{tag_str}")

    elif cmd == "!recall":
        if not arg:
            send_message(chat_id, "Usage: !recall <search query>")
        else:
            raw_arg = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            results = _memory.recall(PLATFORM, sender, raw_arg, limit=5)
            if not results:
                send_message(chat_id, "🔍 No memories found.")
            else:
                lines = [f"🔍 *Found {len(results)} memor{'y' if len(results) == 1 else 'ies'}:*\n"]
                for r in results:
                    tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
                    lines.append(f"• {r.content}{tag_str}\n  _ID: {r.id[:8]}…_")
                send_message(chat_id, "\n".join(lines))

    elif cmd == "!memories":
        filter_tags = None
        if arg:
            filter_tags = [w.lstrip("#") for w in arg.split() if w.startswith("#")]
        mems = _memory.list_memories(PLATFORM, sender, limit=10, tags=filter_tags or None)
        if not mems:
            send_message(chat_id, "📭 No memories yet. Use !remember <text> to save one.")
        else:
            stats = _memory.stats(PLATFORM, sender)
            tag_label = f" (tag: {', '.join(filter_tags)})" if filter_tags else ""
            lines = [f"🧠 *Your memories* ({stats['total']} total){tag_label}:\n"]
            for m in mems:
                tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
                lines.append(f"• {m.content}{tag_str}\n  _ID: {m.id[:8]}…_")
            send_message(chat_id, "\n".join(lines))

    elif cmd == "!forget":
        if not arg:
            send_message(chat_id, "Usage: !forget <memory-id>\n_Use !memories to see IDs_")
        else:
            target_id = arg.strip().rstrip("…")
            mems = _memory.list_memories(PLATFORM, sender, limit=100)
            match = next((m for m in mems if m.id.startswith(target_id)), None)
            if match and _memory.forget(PLATFORM, sender, match.id):
                send_message(chat_id, f"🗑️ Forgotten: _{match.content[:60]}_")
            else:
                send_message(chat_id, "❌ Memory not found. Use !memories to see your memories.")

    elif cmd == "!editmem":
        parts = (arg or "").split(None, 1)
        if len(parts) < 2:
            send_message(chat_id, "Usage: !editmem <id> <new text> [#tag] [!0.9]")
        else:
            target_id = parts[0].rstrip("…")
            content, tags, importance = _parse_remember_args(parts[1])
            mems = _memory.list_memories(PLATFORM, sender, limit=100)
            match = next((m for m in mems if m.id.startswith(target_id)), None)
            if not match:
                send_message(chat_id, "❌ Memory not found. Use !memories to see IDs.")
            else:
                updated = _memory.update(
                    PLATFORM, sender, match.id,
                    content=content or None,
                    tags=tags or None,
                    importance=importance if importance != 0.5 else None,
                )
                if updated:
                    send_message(chat_id, f"✏️ Updated: _{updated.content[:80]}_")
                else:
                    send_message(chat_id, "❌ Update failed.")

    elif cmd == "!exportmem":
        data = _memory.export_all(PLATFORM, sender)
        if not data:
            send_message(chat_id, "📭 No memories to export.")
        else:
            payload = json.dumps({"memories": data, "platform": PLATFORM, "user_id": sender}, indent=2)
            send_message(chat_id, f"📦 Exported {len(data)} memories:\n```\n{payload[:3000]}\n```")

    elif cmd == "!extractmem":
        sess = _active_session(sender)
        history = cc.get_history(PLATFORM, sender, session_name=sess.name)
        if not history:
            send_message(chat_id, "No conversation history to extract from.")
        else:
            text_parts = []
            for msg in history[-20:]:
                role = msg.get("role", "")
                content_val = msg.get("content", "")
                if isinstance(content_val, str) and content_val.strip():
                    text_parts.append(f"{role}: {content_val}")
            if not text_parts:
                send_message(chat_id, "No text messages found in recent history.")
            else:
                send_message(chat_id, "🔍 Extracting memories from recent conversation...")
                import asyncio
                extracted = asyncio.get_event_loop().run_until_complete(
                    extract_memories("\n".join(text_parts))
                )
                if not extracted:
                    send_message(chat_id, "No memorable facts found.")
                else:
                    saved = []
                    for entry in extracted:
                        mem = _memory.remember(
                            PLATFORM, sender, entry["content"],
                            tags=entry.get("tags"),
                            importance=entry.get("importance", 0.5),
                            metadata={"source": "auto-extract"},
                        )
                        saved.append(mem)
                    lines = [f"🧠 *Extracted {len(saved)} memories:*\n"]
                    for m in saved:
                        tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
                        lines.append(f"• {m.content}{tag_str}")
                    send_message(chat_id, "\n".join(lines))

    else:
        send_message(chat_id, f"Unknown command: {cmd}\nType !help for available commands.")

    return True


# ─── Message handling with progress ────────────────────────────────────────────

def _handle(chat_id: str, sender: str, text: str) -> None:
    """Runs in a background thread per chat."""
    lock = _lock_for(chat_id)
    if not lock.acquire(blocking=False):
        send_message(chat_id, "⏳ Still working on your previous message…")
        return

    try:
        if not cc.check_rate_limit(f"wa:{sender}"):
            send_message(
                chat_id,
                f"⚠️ Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s.",
            )
            return

        send_typing(chat_id)
        log.info("← WA [%s] %s", chat_id, text[:120])

        model = _user_model.get(sender, cc.CLAUDE_MODEL)
        verbose = _verbose.get(sender, False)
        # Use the user's active session so !new / !switch / !rename actually
        # scope the conversation history. Without this, normal chat keeps
        # writing to the unsuffixed global thread regardless of session.
        active_sess = _active_session(sender)
        history = cc.load_history(PLATFORM, sender, session_name=active_sess.name)

        # Use async path for streaming progress
        loop = asyncio.new_event_loop()
        try:
            reply, stats = loop.run_until_complete(
                _ask_with_progress(chat_id, sender, text, history, model, verbose)
            )
        finally:
            loop.close()

        cc.save_turn(PLATFORM, sender, text, reply, session_name=active_sess.name)
        cc.track_usage(PLATFORM, sender, stats.get("input_tokens", 0), stats.get("output_tokens", 0))

        # Track tools used
        tools_used = stats.get("tools_used", [])
        if tools_used:
            cc.track_tool_usage(PLATFORM, sender, tools_used)

        log.info("→ WA [%s] %s", chat_id, reply[:120])

        # Chunk long replies (WhatsApp limit ≈ 65 536 chars; use 4 000 to be safe)
        chunk = 4000
        for i in range(0, len(reply), chunk):
            send_message(chat_id, reply[i : i + chunk])
            if i + chunk < len(reply):
                time.sleep(0.5)

        # Show stats footer for verbose users
        if verbose and stats:
            in_tok = stats.get("input_tokens", 0)
            out_tok = stats.get("output_tokens", 0)
            duration = stats.get("duration", 0)
            footer_parts = []
            if in_tok or out_tok:
                footer_parts.append(f"tokens: {in_tok}→{out_tok}")
            if duration:
                footer_parts.append(f"time: {duration:.1f}s")
            if tools_used:
                footer_parts.append(f"tools: {len(tools_used)}")
            if footer_parts:
                send_message(chat_id, f"_📊 {' | '.join(footer_parts)}_")

    except Exception as exc:
        log.exception("Error in _handle")
        send_message(chat_id, f"❌ Error: {exc}")
    finally:
        lock.release()


async def _ask_with_progress(
    chat_id: str, sender: str, text: str,
    history: list[dict], model: str, verbose: bool
) -> tuple[str, dict]:
    """Call Claude async with progress updates sent to WhatsApp."""
    tools_used: list[str] = []
    last_progress_time = [0.0]
    start_time = time.time()

    async def on_progress(tool_name: str, detail: str = ""):
        tools_used.append(tool_name)
        now = time.time()
        # Rate-limit progress messages (max 1 every 3 seconds)
        if verbose and now - last_progress_time[0] >= 3.0:
            last_progress_time[0] = now
            icon = TOOL_ICONS.get(tool_name, "⚙️")
            msg = f"{icon} _{tool_name}_"
            if detail:
                msg += f": {detail[:60]}"
            send_message(chat_id, msg)

    # Send initial thinking indicator
    elapsed_before_reply = [False]

    async def _send_thinking_after_delay():
        await asyncio.sleep(5)
        if not elapsed_before_reply[0]:
            send_message(chat_id, "🧠 _Thinking…_")

    thinking_task = asyncio.create_task(_send_thinking_after_delay())

    try:
        reply, stats = await cc.ask_claude_async(
            text, history,
            model=model,
            on_progress=on_progress,
            platform=PLATFORM,
            user_id=sender,
        )
    finally:
        elapsed_before_reply[0] = True
        thinking_task.cancel()

    stats["tools_used"] = tools_used
    stats["duration"] = time.time() - start_time
    return reply, stats


# ─── Notification processing ───────────────────────────────────────────────────

def _process(notification: dict) -> None:
    receipt_id = notification.get("receiptId")
    body = notification.get("body", {})

    if body.get("typeWebhook") != "incomingMessageReceived":
        delete_notification(receipt_id)
        return

    msg_data = body.get("messageData", {})
    msg_type = msg_data.get("typeMessage", "")
    if msg_type not in ("textMessage", "extendedTextMessage", "quotedMessage"):
        delete_notification(receipt_id)
        return

    sender_data = body.get("senderData", {})
    chat_id = sender_data.get("chatId", "")
    sender  = sender_data.get("sender", chat_id)

    text = ""
    if msg_type == "textMessage":
        text = msg_data.get("textMessageData", {}).get("textMessage", "")
    elif msg_type == "extendedTextMessage":
        text = msg_data.get("extendedTextMessageData", {}).get("text", "")
    elif msg_type == "quotedMessage":
        text = msg_data.get("extendedTextMessageData", {}).get("text", "")
    text = text.strip()

    delete_notification(receipt_id)

    if not text:
        return

    if not _allowed(sender):
        number = sender.split("@")[0]
        if text.strip().lower() == "!id":
            send_message(chat_id, f"📱 Your WhatsApp number: *{number}*\n\nAdd it to WHATSAPP_ALLOWED_NUMBERS in .env to whitelist yourself.")
            return
        log.warning("Rejected message from %s", sender)
        send_message(chat_id, f"⛔ Not on the allowed list.\nYour number: *{number}*\n\nAdd it to WHATSAPP_ALLOWED_NUMBERS in .env")
        return

    # Handle commands synchronously (fast, no Claude call)
    if _handle_command(chat_id, sender, text):
        return

    threading.Thread(target=_handle, args=(chat_id, sender, text), daemon=True).start()


# ─── Entry point ─────────────────────────────────────────────────────────────────

def run_whatsapp() -> None:
    cc.init_db()
    log.info("WhatsApp bot started (Green API instance %s). Polling every %.1fs…", INSTANCE_ID, POLL_INTERVAL)
    log.info("Model: %s | Mode: %s", cc.CLAUDE_MODEL, cc.CLAUDE_MODE)

    while True:
        try:
            notification = receive_notification()
            if notification:
                _process(notification)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("WhatsApp bot shutting down.")
            break
        except Exception as exc:
            log.exception("Poll loop error: %s", exc)
            time.sleep(5)
