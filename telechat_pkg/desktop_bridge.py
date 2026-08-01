"""Claude Desktop ↔ Telegram bridge — integrated into telechat.

The bridge does three things:

1. **Hook entrypoints** (called as short-lived subprocesses by ~/.claude/settings.json):
     telechat bridge notify Stop|Notification|SubagentStop   ← post a card to Telegram
     telechat bridge approve                                  ← block waiting for Approve/Deny

2. **Telegram command + callback handlers** (registered into telechat's running poller):
     /desktop, /desktop_use, /desktop_which, /desktop_clear, /desktop_all,
     /desktop_approve_on, /desktop_approve_off
     reply-to-card routing, use:<sid> + appr:<id> callbacks

3. **Install / uninstall / migrate** — manages ~/.claude/settings.json hooks and the
   standalone ~/.claude-bridge/ retirement.

All persistent state lives in telechat's bot.db via three new tables created by
`init_bridge_schema(conn)` (called from store.init_db).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HOME = Path.home()
TELECHAT_HOME = Path(os.environ.get("TELECHAT_HOME") or HOME / ".telechat")
STANDALONE_BRIDGE_DIR = HOME / ".claude-bridge"
STANDALONE_LAUNCHD_PLIST = HOME / "Library/LaunchAgents/com.user.claude-bridge.plist"
CLAUDE_SETTINGS = HOME / ".claude/settings.json"

# Persistent-service (macOS launchd) constants.
SERVICE_LABEL = "com.telechat.bot"
SERVICE_PLIST = HOME / "Library/LaunchAgents" / f"{SERVICE_LABEL}.plist"

# ───────────────────────── approval policy ─────────────────────────
# How long the PreToolUse hook waits for a phone tap, and what happens when
# nobody taps. Fail-open ("fallthrough": Claude Code applies its normal
# permission flow, which usually means prompting at the desktop) is the
# historical behaviour and stays the default — it is the right one for a
# personal tool where the operator is typically sitting at the machine.
#
# But it should be a choice, not an accident: a user who turns on approval
# precisely because they are *away* from the machine wants "deny". That is what
# BRIDGE_APPROVAL_TIMEOUT_ACTION is for.

_APPROVAL_TIMEOUT_ACTIONS = ("fallthrough", "deny", "allow")


def _approval_timeout() -> float:
    """Seconds to wait for an approval decision. Non-numeric input → 300."""
    raw = os.environ.get("BRIDGE_APPROVAL_TIMEOUT", "300")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 300.0
    # A zero or negative timeout would make every request resolve instantly by
    # policy, silently. If that is what someone wants, the action setting says
    # so explicitly; a bad number should not.
    return value if value > 0 else 300.0


def _approval_timeout_action() -> str:
    """What an un-answered approval request resolves to.

    ``fallthrough`` (default) hands the decision back to Claude Code, ``deny``
    refuses the tool call, ``allow`` permits it. Anything unrecognised is
    treated as ``fallthrough`` — the safe reading of a typo is "behave the way
    you always did", not "invent a policy".
    """
    action = os.environ.get("BRIDGE_APPROVAL_TIMEOUT_ACTION", "fallthrough").strip().lower()
    return action if action in _APPROVAL_TIMEOUT_ACTIONS else "fallthrough"

# ───────────────────────── schema ─────────────────────────

def init_bridge_schema(conn: sqlite3.Connection) -> None:
    """Called from store.init_db to add bridge-specific tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bridge_session_messages (
            message_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            cwd        TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bridge_sm_sid
            ON bridge_session_messages(session_id);

        CREATE TABLE IF NOT EXISTS bridge_approvals (
            request_id TEXT PRIMARY KEY,
            session_id TEXT,
            cwd        TEXT,
            tool       TEXT,
            decision   TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bridge_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS bridge_approve_mode (
            cwd TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL
        );

        -- Standing "don't ask me again" decisions, scoped to one project.
        -- prefix is the Bash command prefix a rule covers ('git push'); empty
        -- means the rule covers the whole tool.
        CREATE TABLE IF NOT EXISTS bridge_approval_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            cwd        TEXT NOT NULL,
            tool       TEXT NOT NULL,
            prefix     TEXT NOT NULL DEFAULT '',
            decision   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(cwd, tool, prefix)
        );

        CREATE TABLE IF NOT EXISTS bridge_full_outputs (
            token      TEXT PRIMARY KEY,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bridge_follows (
            sid        TEXT PRIMARY KEY,
            cwd        TEXT,
            last_pos   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    # bridge_approvals predates standing rules, and every CREATE above is IF NOT
    # EXISTS — so an existing install would keep its old table and the "always
    # allow" button would have no prefix to write a rule from.
    _add_column(conn, "bridge_approvals", "rule_prefix", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column if the table doesn't have it yet."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ───────────────────────── env + binary lookup ─────────────────────────

def _load_env_file() -> dict:
    env_path = TELECHAT_HOME / ".env"
    if not env_path.exists():
        return {}
    out = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _find_claude_bin() -> str:
    for p in (
        shutil.which("claude"),
        str(HOME / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if p and Path(p).exists():
            return p
    return "claude"


def _claude_env() -> dict:
    """Build env for spawning `claude`. Loads token from telechat .env if present."""
    env = os.environ.copy()
    env.setdefault("HOME", str(HOME))
    cfg = _load_env_file()
    token = cfg.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env.pop("ANTHROPIC_API_KEY", None)
    return env


# ───────────────────────── DB helpers ─────────────────────────

_SCHEMA_READY = False


def _db() -> sqlite3.Connection:
    """Return a connection to telechat's bot.db, ensuring bridge tables exist.
    Late-imports store to avoid pulling in telegram_bot side-effects when used
    from a short-lived hook subprocess."""
    global _SCHEMA_READY
    from . import store  # late import
    conn = store._get_conn()
    if not _SCHEMA_READY:
        try:
            init_bridge_schema(conn)
            conn.commit()
        except Exception:
            # Every bridge query after this fails with "no such table"; say why
            # once, here, rather than N times without a cause.
            log.warning("bridge schema init failed", exc_info=True)
        _SCHEMA_READY = True
    return conn


def _state_get(key: str, default: Optional[str] = None) -> Optional[str]:
    c = _db()
    row = c.execute("SELECT value FROM bridge_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _state_set(key: str, value: Optional[str]) -> None:
    c = _db()
    if value is None:
        c.execute("DELETE FROM bridge_state WHERE key=?", (key,))
    else:
        c.execute("INSERT OR REPLACE INTO bridge_state(key,value) VALUES(?,?)", (key, str(value)))
    c.commit()


def get_current_session() -> tuple[Optional[str], Optional[str]]:
    return _state_get("current_session_id"), _state_get("current_session_cwd")


def set_current_session(sid: Optional[str], cwd: Optional[str]) -> None:
    if sid and cwd:
        _state_set("current_session_id", sid)
        _state_set("current_session_cwd", cwd)
    else:
        _state_set("current_session_id", None)
        _state_set("current_session_cwd", None)


def resolve_short_session(short: str) -> Optional[tuple[str, str]]:
    short = (short or "").strip().lower()
    if not short:
        return None
    c = _db()
    row = c.execute(
        "SELECT session_id, cwd FROM bridge_session_messages WHERE session_id LIKE ? "
        "ORDER BY created_at DESC LIMIT 1",
        (short + "%",),
    ).fetchone()
    if row:
        return tuple(row)
    # Fallback: not in our DB (e.g. an older/closed session) — find its transcript on disk
    # so the user can still resume any existing session, not just ones we've notified about.
    matches = list((HOME / ".claude" / "projects").glob(f"*/{short}*.jsonl"))
    if matches:
        sid = matches[0].stem
        cwd = _resolve_session_cwd(sid)
        if cwd:
            return (sid, cwd)
    return None


def approve_mode_on(cwd: str) -> bool:
    # Global toggle takes precedence — every project's permissions route to Telegram.
    if _state_get("approve_all") == "1":
        return True
    c = _db()
    row = c.execute("SELECT enabled FROM bridge_approve_mode WHERE cwd=?", (cwd,)).fetchone()
    return bool(row and row[0])


def lifecycle_on() -> bool:
    # Default ON — pings when Desktop sessions start/exit.
    return _state_get("lifecycle", "1") == "1"


# ── follow-mode state ──

def follow_add(sid: str, cwd: str, start_pos: int) -> None:
    c = _db()
    c.execute(
        "INSERT OR REPLACE INTO bridge_follows(sid,cwd,last_pos,created_at) VALUES(?,?,?,?)",
        (sid, cwd, start_pos, datetime.now().isoformat()),
    )
    c.commit()


def follow_remove(sid: str) -> int:
    c = _db()
    cur = c.execute("DELETE FROM bridge_follows WHERE sid LIKE ?", (sid + "%",))
    c.commit()
    return cur.rowcount


def list_follows() -> list[tuple]:
    c = _db()
    return c.execute("SELECT sid, cwd, last_pos FROM bridge_follows").fetchall()


def _follow_set_pos(sid: str, pos: int) -> None:
    c = _db()
    c.execute("UPDATE bridge_follows SET last_pos=? WHERE sid=?", (pos, sid))
    c.commit()


def set_approve_mode(cwd: str, enabled: bool) -> None:
    c = _db()
    c.execute(
        "INSERT OR REPLACE INTO bridge_approve_mode(cwd,enabled) VALUES(?,?)",
        (cwd, 1 if enabled else 0),
    )
    c.commit()


# ───────────────────────── standing approval rules ─────────────────────────
# Approval mode asked again for every single call — `git status`, `git status`,
# `git status` — which is why people armed it once and turned it off. A rule is
# the "and stop asking" half of a decision: one extra tap on the card, and
# matching calls in that project resolve silently from then on.

#: Anything that could turn one command into two. A prefix rule is a promise
#: about what will run, and `git push; rm -rf /` derives the prefix `git push`
#: while running something else entirely — so a command carrying shell
#: metacharacters is never ruleable and never matches an existing rule. It is
#: always still approvable by hand; only the standing permission is withheld.
_SHELL_META = re.compile(r"[;&|`$(){}<>\n\\]")

#: A word that looks like a subcommand (`push`, `test`, `build`) rather than a
#: flag, a path, or a filename.
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9_-]*$")


def _bash_prefix(command: str) -> Optional[str]:
    """The rule prefix a Bash command belongs to, or None if it cannot have one.

    ``git push origin main`` and ``git push --force`` both derive ``git push``,
    so one rule covers both; ``git status`` derives its own. Matching is by
    equality of derived prefixes rather than string containment, which is what
    keeps ``git push`` from also matching a command that merely starts with
    those characters.
    """
    cmd = (command or "").strip()
    if not cmd or _SHELL_META.search(cmd):
        return None
    tokens = cmd.split()
    if not tokens:
        return None
    prefix = tokens[0]
    if len(tokens) > 1 and _SUBCOMMAND.match(tokens[1]):
        prefix += " " + tokens[1]
    return prefix


def rule_key(tool: str, tool_input: dict) -> Optional[tuple[str, str]]:
    """``(tool, prefix)`` identifying the rule a call would fall under, or None.

    Non-Bash tools rule at tool granularity: "stop asking about edits in this
    project" is a coherent thing to want, and there is no equivalent of a
    command prefix to narrow it with.
    """
    if not tool:
        return None
    if tool == "Bash":
        prefix = _bash_prefix(str((tool_input or {}).get("command") or ""))
        return (tool, prefix) if prefix else None
    return (tool, "")


def find_approval_rule(cwd: str, tool: str, tool_input: dict) -> Optional[str]:
    """The standing decision for this call — 'y', 'n', or None if it must ask."""
    key = rule_key(tool, tool_input)
    if not key or not cwd:
        return None
    row = _db().execute(
        "SELECT decision FROM bridge_approval_rules WHERE cwd=? AND tool=? AND prefix=?",
        (cwd, key[0], key[1]),
    ).fetchone()
    return row[0] if row else None


def add_approval_rule(cwd: str, tool: str, prefix: str, decision: str) -> None:
    c = _db()
    c.execute(
        "INSERT OR REPLACE INTO bridge_approval_rules(cwd,tool,prefix,decision,created_at)"
        " VALUES(?,?,?,?,?)",
        (cwd, tool, prefix, decision, datetime.now().isoformat()),
    )
    c.commit()


def list_approval_rules(cwd: Optional[str] = None) -> list[tuple]:
    """(id, cwd, tool, prefix, decision), newest first."""
    c = _db()
    sql = "SELECT id, cwd, tool, prefix, decision FROM bridge_approval_rules"
    if cwd:
        return c.execute(sql + " WHERE cwd=? ORDER BY id DESC", (cwd,)).fetchall()
    return c.execute(sql + " ORDER BY id DESC").fetchall()


def remove_approval_rule(rule_id: int) -> int:
    c = _db()
    cur = c.execute("DELETE FROM bridge_approval_rules WHERE id=?", (rule_id,))
    c.commit()
    return cur.rowcount


def clear_approval_rules(cwd: Optional[str] = None) -> int:
    c = _db()
    if cwd:
        cur = c.execute("DELETE FROM bridge_approval_rules WHERE cwd=?", (cwd,))
    else:
        cur = c.execute("DELETE FROM bridge_approval_rules")
    c.commit()
    return cur.rowcount


def _rule_label(tool: str, prefix: str) -> str:
    return f"{tool} {prefix}".strip() if prefix else tool


# ───────────────────────── Telegram API (raw, no python-telegram-bot) ─────────────────────────

def _tg_call(method: str, **params) -> Optional[dict]:
    env = _load_env_file()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v)
    body = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return None


def _tg_edit(message_id: int, text: str, chat_id: Optional[str] = None) -> Optional[dict]:
    """Edit a message in place. Same Markdown-then-plain retry as _tg_send.

    Returns None on failure, including Telegram's "message is not modified" —
    callers treat an edit as best-effort, since losing one frame of a live
    progress card is not worth failing the turn over.
    """
    env = _load_env_file()
    cid = chat_id or env.get("TELEGRAM_CHAT_ID") or env.get(
        "TELEGRAM_ALLOWED_USER_IDS", "").split(",")[0].strip()
    if not cid or not message_id:
        return None
    params = {"chat_id": cid, "message_id": message_id, "text": text,
              "parse_mode": "Markdown"}
    r = _tg_call("editMessageText", **params)
    if not r or not r.get("ok"):
        params.pop("parse_mode", None)
        r = _tg_call("editMessageText", **params)
    return r


def _tg_send(text: str, reply_markup=None, reply_to=None, chat_id: Optional[str] = None) -> Optional[dict]:
    env = _load_env_file()
    cid = chat_id or env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")[0].strip()
    if not cid:
        return None
    params = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        params["reply_markup"] = reply_markup
    if reply_to:
        params["reply_to_message_id"] = reply_to
    r = _tg_call("sendMessage", **params)
    if not r or not r.get("ok"):
        params.pop("parse_mode", None)
        r = _tg_call("sendMessage", **params)
    return r


# Telegram limits: 4096 chars/message, 10MB/document. Chunk safely below.
_TG_CHUNK = 3800
_FILE_THRESHOLD = 30_000  # if reply > 30KB, attach as .txt instead of spamming chunks


def _tg_send_long(prefix: str, body: str, reply_markup_on_last=None) -> None:
    """Post a long body across multiple messages with [n/N] footers.
    For huge bodies (>_FILE_THRESHOLD chars), post a short head + attach full as .txt."""
    if not body:
        body = "(no output)"

    if len(body) > _FILE_THRESHOLD:
        head = body[:1500].rstrip() + "\n…"
        _tg_send(f"{prefix}\n\n{_md(head)}\n\n_(full output attached — {len(body):,} chars)_",
                 reply_markup=reply_markup_on_last)
        _tg_send_document(body, filename=f"{prefix.split('`')[1][:8] if '`' in prefix else 'reply'}.txt",
                          caption=prefix)
        return

    # Smart chunking via telechat's existing helper.
    try:
        from .text_chunking import chunk_text
        chunks = chunk_text(body, limit=_TG_CHUNK, mode="smart")
        parts = [c.text for c in chunks]
    except Exception:
        parts = [body[i:i+_TG_CHUNK] for i in range(0, len(body), _TG_CHUNK)]

    n = len(parts)
    for i, part in enumerate(parts):
        footer = f"\n\n_[{i+1}/{n}]_" if n > 1 else ""
        if i == 0:
            text = f"{prefix}\n\n{_md(part)}{footer}"
        else:
            text = f"{_md(part)}{footer}"
        rm = reply_markup_on_last if i == n - 1 else None
        _tg_send(text, reply_markup=rm)


def _tg_send_document(content: str, filename: str, caption: str = "") -> Optional[dict]:
    """Send raw text as a file attachment via sendDocument (multipart/form-data)."""
    env = _load_env_file()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    cid = env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")[0].strip()
    if not token or not cid:
        return None
    boundary = "----telechatBoundary" + uuid.uuid4().hex
    payload = bytearray()

    def add_field(name: str, value: str):
        payload.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    add_field("chat_id", cid)
    if caption:
        add_field("caption", caption[:1024])
        add_field("parse_mode", "Markdown")
    payload.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
    )
    payload.extend(content.encode("utf-8", errors="replace"))
    payload.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(payload),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return None


def _md(s: str) -> str:
    return (s or "").replace("`", "'").replace("*", "·").replace("_", " ").replace("[", "(").replace("]", ")")


# ───────────────────────── transcript helpers ─────────────────────────

def _find_transcript(payload: dict) -> Optional[Path]:
    p = payload.get("transcript_path")
    if p and Path(p).exists():
        return Path(p)
    sid, cwd = payload.get("session_id"), payload.get("cwd")
    if sid and cwd:
        slug = cwd.replace("/", "-")
        cand = HOME / ".claude" / "projects" / slug / f"{sid}.jsonl"
        if cand.exists():
            return cand
    return _find_transcript_by_sid(sid) if sid else None


def _find_transcript_by_sid(sid: str) -> Optional[Path]:
    """Locate a session's transcript anywhere under ~/.claude/projects/."""
    if not sid:
        return None
    matches = list((HOME / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    return matches[0] if matches else None


def _resolve_session_cwd(sid: str, fallback: Optional[str] = None) -> Optional[str]:
    """Return the TRUE working directory a session was created in, read from its
    transcript's `cwd` field. `claude --resume` is scoped by cwd, so resuming from a
    stale/wrong directory fails with 'No conversation found' — always resolve here."""
    tpath = _find_transcript_by_sid(sid)
    if not tpath:
        return fallback
    try:
        for raw in tpath.read_text().splitlines():
            try:
                cwd = json.loads(raw).get("cwd")
            except Exception:
                continue
            if cwd:
                return cwd
    except OSError:
        # Transcript vanished or is unreadable — the caller falls back to the
        # stored cwd, which is the whole point of the fallback argument.
        log.debug("could not read transcript for cwd", exc_info=True)
    return fallback


def _last_assistant_text(tpath: Optional[Path], max_chars: Optional[int] = None) -> Optional[str]:
    """Return the last assistant message text from a Claude Code transcript.
    Pass max_chars to cap; None = full text."""
    if not tpath:
        return None
    try:
        lines = tpath.read_text().splitlines()
    except Exception:
        return None
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content", [])
        texts, tools = [], []
        for c in content:
            t = c.get("type")
            if t == "text":
                texts.append(c.get("text", ""))
            elif t == "tool_use":
                tools.append(c.get("name", "?"))
        text = "\n".join(t for t in texts if t).strip()
        if text:
            if max_chars and len(text) > max_chars:
                return text[:max_chars].rstrip() + "…"
            return text
        if tools:
            return f"(ran tools: {', '.join(tools[:5])})"
    return None


# Heuristic window: a transcript written within this many seconds is "actively working".
_BUSY_WINDOW_SECS = 8


def _session_status(sid: str) -> dict:
    """Cheap activity probe for a session: {busy: bool, last: str|None}.
    busy = transcript modified very recently (mid-turn streaming).
    last = short snippet of the last assistant message (tail-read, so fast on big files)."""
    tpath = _find_transcript_by_sid(sid)
    if not tpath:
        return {"busy": False, "last": None}
    busy = False
    try:
        import time as _t
        busy = (_t.time() - tpath.stat().st_mtime) < _BUSY_WINDOW_SECS
    except OSError:
        log.debug("could not stat transcript %s", tpath, exc_info=True)
    return {"busy": busy, "last": _tail_last_assistant(tpath, max_chars=70)}


def _tail_last_assistant(tpath: Path, max_chars: int = 70) -> Optional[str]:
    """Like _last_assistant_text but reads only the file's tail — fast on large transcripts."""
    try:
        size = tpath.stat().st_size
        with tpath.open("rb") as f:
            if size > 65536:
                f.seek(-65536, 2)
            chunk = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for raw in reversed(chunk.splitlines()):
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        tools = [c.get("name", "?") for c in content if c.get("type") == "tool_use"]
        text = " ".join(t for t in texts if t).strip().replace("\n", " ")
        if text:
            return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")
        if tools:
            return f"(running {', '.join(tools[:3])})"
    return None


# ───────────────────────── running-sessions helpers ─────────────────────────

def _proc_cwd(pid: str) -> Optional[str]:
    """Working directory of a running process (macOS: lsof -d cwd)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        # lsof missing (common in containers) or the process is gone.
        log.debug("could not resolve cwd of pid via lsof", exc_info=True)
    return None


def _latest_session_in_cwd(cwd: str) -> Optional[str]:
    """Most-recently-modified session id under a cwd's project dir (the active session
    for a Desktop process started without --resume)."""
    if not cwd:
        return None
    slug = cwd.replace("/", "-")
    proj = HOME / ".claude" / "projects" / slug
    if not proj.is_dir():
        return None
    jsonls = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonls[0].stem if jsonls else None


def _rel_time(epoch: float) -> str:
    """Compact 'time ago' string."""
    import time as _t
    secs = max(0, int(_t.time() - epoch))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def list_recent_sessions(limit: int = 8) -> list[dict]:
    """Recent sessions across ALL projects (running or not), newest first.
    This is the universal browser — lets you resume any existing session, not just live ones."""
    proj_root = HOME / ".claude" / "projects"
    if not proj_root.is_dir():
        return []
    files = []
    for p in proj_root.glob("*/*.jsonl"):
        try:
            files.append((p.stat().st_mtime, p))
        except Exception:
            continue
    files.sort(key=lambda t: t[0], reverse=True)
    running = {s["sid"] for s in list_running_sessions() if s["sid"]}
    out = []
    seen = set()
    for mtime, p in files:
        sid = p.stem
        if sid in seen:
            continue
        seen.add(sid)
        cwd = _resolve_session_cwd(sid) or ""
        out.append({
            "sid": sid,
            "cwd": cwd,
            "mtime": mtime,
            "ago": _rel_time(mtime),
            "running": sid in running,
            "last": _tail_last_assistant(p, max_chars=70),
        })
        if len(out) >= limit:
            break
    return out


def list_running_sessions() -> list[dict]:
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,etime,command"], text=True)
    except Exception:
        return []
    sessions = []
    for line in out.splitlines():
        if "claude-code/" not in line or "claude.app/Contents/MacOS/claude " not in line:
            continue
        if "disclaimer" in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, cmd = parts
        sid = ""
        model = ""
        toks = cmd.split()
        for i, t in enumerate(toks):
            if t == "--resume" and i + 1 < len(toks):
                sid = toks[i + 1]
            elif t == "--model" and i + 1 < len(toks):
                model = toks[i + 1]
        cwd = ""
        if sid:
            c = _db()
            row = c.execute(
                "SELECT cwd FROM bridge_session_messages WHERE session_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if row:
                cwd = row[0]
            if not cwd:
                cwd = _resolve_session_cwd(sid) or ""
        else:
            # Fresh Desktop session (no --resume): recover its id from its working
            # directory's most recent transcript so it's still actionable.
            pcwd = _proc_cwd(pid)
            if pcwd:
                recovered = _latest_session_in_cwd(pcwd)
                if recovered:
                    sid, cwd = recovered, pcwd
        sessions.append({"sid": sid, "cwd": cwd, "model": model, "etime": etime, "pid": pid})
    return sessions


# ───────────────────────── background watcher: follow-mode + lifecycle ─────────────────────────

_WATCHER_STARTED = False
_WATCH_POLL_SECS = 4
_prev_running_procs = None  # pid -> session info; baseline for lifecycle diffing


def _session_label(s: dict) -> str:
    """Human label for a lifecycle ping: project name + short session id (best-effort)."""
    proj = Path(s["cwd"]).name if s.get("cwd") else "(unknown)"
    sid = s.get("sid") or ""
    return f"`{proj}`" + (f"  `[{sid[:8]}]`" if sid else "")


def _watch_lifecycle() -> None:
    """Ping Telegram when Desktop sessions start or exit.

    Diff on process id (pid), not on the recovered session id. Sessions started
    without ``--resume`` have no sid on their command line, so we recover one from
    the most-recently-modified transcript in their cwd — but that recovered id flips
    between concurrent sessions sharing a project dir (mtime ordering swaps as each
    writes), which produced a flood of phantom start/end pings. The pid is stable for
    the life of the process, so it is the correct identity for lifecycle events.
    """
    global _prev_running_procs
    cur = {s["pid"]: s for s in list_running_sessions() if s.get("pid")}
    if not lifecycle_on():
        # Keep the baseline fresh so toggling on later doesn't dump a backlog.
        _prev_running_procs = cur
        return
    if _prev_running_procs is not None:
        for pid in cur.keys() - _prev_running_procs.keys():
            _tg_send(f"🟢 *Session started* — {_session_label(cur[pid])}")
        for pid in _prev_running_procs.keys() - cur.keys():
            _tg_send(f"⚪ *Session ended* — {_session_label(_prev_running_procs[pid])}")
    _prev_running_procs = cur


def _watch_follows() -> None:
    """Stream new assistant turns / tool actions of followed sessions to Telegram."""
    for sid, _cwd, last_pos in list_follows():
        tpath = _find_transcript_by_sid(sid)
        if not tpath:
            continue
        try:
            size = tpath.stat().st_size
        except Exception:
            continue
        if size <= last_pos:
            if size < last_pos:        # file rotated/truncated — resync
                _follow_set_pos(sid, size)
            continue
        try:
            with tpath.open("rb") as f:
                f.seek(last_pos)
                data = f.read()
        except Exception:
            continue
        nl = data.rfind(b"\n")
        if nl == -1:
            continue  # no complete line yet
        complete = data[:nl + 1]
        new_pos = last_pos + len(complete)
        for raw in complete.decode("utf-8", "replace").splitlines():
            msg = _follow_format_entry(raw, sid)
            if msg:
                _tg_send(msg)
        _follow_set_pos(sid, new_pos)


def _follow_format_entry(raw: str, sid: str) -> Optional[str]:
    """Render one transcript line as a follow-mode update, or None to skip."""
    try:
        e = json.loads(raw)
    except Exception:
        return None
    if e.get("type") != "assistant":
        return None
    content = e.get("message", {}).get("content", [])
    parts = []
    for c in content:
        t = c.get("type")
        if t == "text" and c.get("text", "").strip():
            txt = c["text"].strip()
            parts.append(txt[:600] + ("…" if len(txt) > 600 else ""))
        elif t == "tool_use":
            name = c.get("name", "?")
            inp = c.get("input", {}) or {}
            detail = inp.get("command") or inp.get("file_path") or inp.get("path") or ""
            parts.append(f"🔧 {name}" + (f" · `{_md(str(detail)[:80])}`" if detail else ""))
    if not parts:
        return None
    return f"👁 `[{sid[:8]}]`\n" + "\n".join(_md(p) if not p.startswith("🔧") else p for p in parts)


#: Consecutive watcher passes in which something failed. Drives the escalation
#: in :func:`_watch_once` — see the comment there.
_watch_failures = 0


def _watch_once() -> bool:
    """Run one watcher pass. Returns True if every watch succeeded.

    Never raises: this runs on a daemon thread, and a transient read error must
    not stop the watcher for the rest of the process's life. It used to swallow
    those errors *silently*, though, which made a permanently broken watcher
    indistinguishable from a quiet one — session pings and `/follow` mirrors
    simply never arrived and nothing said why.
    """
    global _watch_failures
    failed = False
    for name, watch in (("lifecycle", _watch_lifecycle), ("follows", _watch_follows)):
        try:
            watch()
        except Exception:
            failed = True
            # First failure gets a warning; the rest are debug. At a 4-second
            # poll, warning every pass is ~900 lines an hour for one broken
            # transcript.
            if _watch_failures == 0:
                log.warning("bridge %s watch failed", name, exc_info=True)
            else:
                log.debug("bridge %s watch failed", name, exc_info=True)
    if failed:
        _watch_failures += 1
        if _watch_failures in (10, 100, 1000):
            log.error(
                "bridge watcher has failed %d consecutive passes — "
                "session pings and /follow mirroring are not working",
                _watch_failures,
            )
    else:
        _watch_failures = 0
    return not failed


def _watcher_loop() -> None:
    import time as _t
    while True:
        _watch_once()
        _t.sleep(_WATCH_POLL_SECS)


def start_watcher() -> None:
    """Start the background watcher once (called from register())."""
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    _WATCHER_STARTED = True
    threading.Thread(target=_watcher_loop, daemon=True).start()


# ───────────────────────── AI digest (triage summary) ─────────────────────────

_DIGEST_MODEL = "haiku"
_DIGEST_MIN_CHARS = 200      # below this, just show the raw text — not worth summarizing
_STATUS_ICONS = {
    "DONE": "✅", "NEEDS DECISION": "⚠️", "BLOCKED": "❌", "UPDATE": "ℹ️",
}


def _summarize(raw: str) -> Optional[str]:
    """Produce a compact triage digest of `raw` using a fast model.
    Returns the raw model text (status line + sentences + optional DECISION line), or None."""
    if not raw or len(raw.strip()) < _DIGEST_MIN_CHARS:
        return None
    # Cap input for speed: head + tail captures intro and conclusion.
    snippet = raw if len(raw) <= 8000 else (raw[:6000] + "\n…\n" + raw[-2000:])
    # Robust prompt: the text is opaque content delimited by tags and is ALWAYS present.
    # Without this framing, small models sometimes "converse back" (ask for input, or
    # echo the format spec) instead of summarizing short/conversational text.
    prompt = (
        "You are a notification summarizer. The text inside <CONTENT> tags below is the "
        "complete output of an AI assistant. Summarize THAT TEXT into a phone notification.\n\n"
        "Hard rules:\n"
        "- The content is ALWAYS present below. NEVER ask for input. NEVER say you have nothing to summarize.\n"
        "- NEVER describe or restate this format. Only produce the filled-in result.\n"
        "- Do NOT use tools. Reply with plain text only.\n\n"
        "Produce exactly:\n"
        "Line 1: one of these words — DONE | NEEDS DECISION | BLOCKED | UPDATE\n"
        "Then: 1-2 sentences (max ~40 words) describing what the assistant said or did.\n"
        "If the assistant asks the user a question or presents a choice, add a final line:\n"
        "DECISION: <the exact question or choice, one sentence>\n\n"
        f"<CONTENT>\n{snippet}\n</CONTENT>"
    )
    env = _claude_env()
    env["TELECHAT_BRIDGE_INTERNAL"] = "1"  # guard: stops the digest's own hooks from notifying
    try:
        r = subprocess.run(
            [_find_claude_bin(), "-p", prompt, "--model", _DIGEST_MODEL, "--output-format", "text"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        out = (r.stdout or "").strip()
        if not out or _looks_like_meta_response(out):
            return None  # model conversed instead of summarizing → fall back to raw output
        return out
    except Exception:
        return None


# Phrases that indicate the model talked back instead of summarizing the content.
_META_MARKERS = (
    "i don't have", "i do not have", "please share", "please provide", "please paste",
    "what would you like", "what should i", "no work has been", "i'm ready to",
    "i am ready to", "once you give me", "i need you to provide", "provide the",
    "no active", "nothing to summarize", "i understand the format",
)


def _looks_like_meta_response(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _META_MARKERS)


def _format_digest(digest: str) -> tuple[str, bool]:
    """Turn the model's raw digest into a styled card body.
    Returns (markdown_text, has_decision)."""
    lines = [l for l in digest.splitlines() if l.strip()]
    if not lines:
        return _md(digest), False
    first = lines[0].strip().upper().lstrip("•-* ").strip()
    status = None
    for key in _STATUS_ICONS:
        if first.startswith(key) or key in first:
            status = key
            break
    icon = _STATUS_ICONS.get(status, "ℹ️")
    rest = lines[1:] if status else lines
    decision = ""
    body_lines = []
    for l in rest:
        if l.strip().upper().startswith("DECISION:"):
            decision = l.split(":", 1)[1].strip()
        else:
            body_lines.append(l.strip())
    body = " ".join(body_lines).strip()
    out = f"{icon} *{status or 'UPDATE'}*"
    if body:
        out += f"\n{_md(body)}"
    if decision:
        out += f"\n\n⚠️ *NEEDS YOU:* {_md(decision)}"
    return out, bool(decision or status == "NEEDS DECISION")


def _evidence_block(tpath: Optional[Path], cwd: Optional[str] = None) -> str:
    """Rendered files/tests/errors for the turn a transcript just finished.

    Wrapped rather than called directly so that a malformed transcript costs a
    card its evidence block and nothing more — this runs inside a Stop hook,
    where an exception means the notification never arrives at all.
    """
    if not tpath:
        return ""
    try:
        from .bridge_evidence import collect_evidence
        return collect_evidence(tpath, cwd).render()
    except Exception:
        log.debug("evidence block failed", exc_info=True)
        return ""


def _store_full_output(content: str) -> str:
    """Stash full output, return a short token for the 'Full output' button."""
    token = uuid.uuid4().hex[:10]
    c = _db()
    c.execute(
        "INSERT INTO bridge_full_outputs(token,content,created_at) VALUES(?,?,?)",
        (token, content, datetime.now().isoformat()),
    )
    # Keep table bounded.
    c.execute(
        "DELETE FROM bridge_full_outputs WHERE token NOT IN "
        "(SELECT token FROM bridge_full_outputs ORDER BY created_at DESC LIMIT 200)"
    )
    c.commit()
    return token


def _get_full_output(token: str) -> Optional[str]:
    c = _db()
    row = c.execute("SELECT content FROM bridge_full_outputs WHERE token=?", (token,)).fetchone()
    return row[0] if row else None


def _digest_card(header: str, raw_body: str, session_short: Optional[str] = None,
                 evidence: str = "") -> Optional[dict]:
    """Send a triage card: AI digest + evidence block + quick-action buttons.
      Row 1 (when session known): [✅ Proceed] (decisions only) · [📊 Status]
      Row 2: [💬 Use session] · [📄 Full output]
    Falls back to chunked full text if summarization is unavailable.
    Returns the Telegram response of the button-bearing message (for DB row capture).

    ``evidence`` is the rendered files/tests/errors block from bridge_evidence.
    It is deliberately *not* fed to the summarizer: those facts are already
    exact, and paraphrasing them through a model can only make them wrong."""
    digest = _summarize(raw_body)
    if not digest:
        # Even with no digest the evidence is worth having — arguably more so,
        # since the fallback dumps raw text the reader has to skim. It goes in
        # its own message rather than appended to the body: _tg_send_long runs
        # everything through _md(), which strips exactly the backticks and
        # asterisks the evidence block is made of.
        _tg_send_long(header, raw_body)
        if evidence:
            _tg_send(evidence)
        return None

    body_text, has_decision = _format_digest(digest)
    if evidence:
        body_text += f"\n\n{evidence}"
    token = _store_full_output(raw_body)

    rows = []
    if session_short:
        action_row = []
        if has_decision:
            action_row.append({"text": "✅ Proceed",
                               "callback_data": f"bridge:act:{session_short}:proceed"})
        action_row.append({"text": "📊 Status",
                           "callback_data": f"bridge:act:{session_short}:status"})
        rows.append(action_row)
        rows.append([
            {"text": "💬 Use session", "callback_data": f"bridge:use:{session_short}"},
            {"text": "📄 Full output", "callback_data": f"bridge:full:{token}"},
        ])
    else:
        rows.append([{"text": "📄 Full output", "callback_data": f"bridge:full:{token}"}])

    markup = {"inline_keyboard": rows}
    text = f"{header}\n\n{body_text}"
    return _tg_send(text, reply_markup=markup)


# ───────────────────────── claude --resume runner ─────────────────────────

# ───────────────────────── live turn streaming ─────────────────────────

#: Trace lines kept on the live card. Telegram caps a message at 4096 chars and
#: a long turn emits hundreds of tool calls; the recent ones are the useful ones.
_STREAM_MAX_TRACE = 12
#: How much of the in-flight assistant text to show. The full text arrives in
#: the digest card at the end — this is a progress indicator, not the output.
_STREAM_TEXT_TAIL = 700


def _stream_enabled() -> bool:
    return os.environ.get("BRIDGE_STREAM", "1").strip().lower() not in ("0", "false", "no", "off")


def _stream_edit_secs() -> float:
    """Seconds between live-card edits. Telegram rate-limits edits per chat, and
    a turn that emits a tool call every 200ms would otherwise earn a 429."""
    try:
        value = float(os.environ.get("BRIDGE_STREAM_EDIT_SECS", "3"))
    except (TypeError, ValueError):
        return 3.0
    return max(1.0, value)


class _StreamTrace:
    """Accumulates `--output-format stream-json` events into a progress card.

    Kept separate from the subprocess plumbing so the rendering is testable
    against fixture events without spawning anything.
    """

    def __init__(self, sid: str):
        self.sid = sid
        self.lines: list[str] = []      # rendered trace, newest last
        self.steps = 0                  # tool calls seen, including trimmed ones
        self.text = ""                  # latest assistant prose
        self.result: Optional[str] = None
        self.is_error = False

    def feed(self, event: dict) -> bool:
        """Absorb one event. Returns True if the rendered card would change."""
        etype = event.get("type")
        if etype == "result":
            # `result` is the final text; subtype/is_error say whether it worked.
            res = event.get("result")
            if isinstance(res, str):
                self.result = res
            self.is_error = bool(event.get("is_error")) or event.get("subtype") not in (
                None, "success")
            return True
        if etype != "assistant":
            return False

        changed = False
        for c in event.get("message", {}).get("content", []) or []:
            ctype = c.get("type")
            if ctype == "text" and (c.get("text") or "").strip():
                self.text = c["text"].strip()
                changed = True
            elif ctype == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                detail = (inp.get("command") or inp.get("file_path")
                          or inp.get("path") or inp.get("pattern") or "")
                line = f"🔧 {name}"
                if detail:
                    line += f" · `{_backtick_safe(str(detail)[:80])}`"
                self.lines.append(line)
                self.steps += 1
                changed = True
        # Trim after appending so `steps` still counts everything that happened.
        if len(self.lines) > _STREAM_MAX_TRACE:
            self.lines = self.lines[-_STREAM_MAX_TRACE:]
        return changed

    def render(self, header: str, state: str) -> str:
        parts = [f"{header}\n{state}"]
        hidden = self.steps - len(self.lines)
        if hidden > 0:
            parts.append(f"_…{hidden} earlier step(s)_")
        if self.lines:
            parts.append("\n".join(self.lines))
        if self.text:
            tail = self.text[-_STREAM_TEXT_TAIL:]
            prefix = "…" if len(self.text) > _STREAM_TEXT_TAIL else ""
            parts.append(f"_{_md(prefix + tail)}_")
        return "\n\n".join(parts)


def _stream_partial(trace: "_StreamTrace", note: str) -> str:
    """Best available text for a turn that ended without a `result` event.

    Whatever prose the model had produced beats the bare note, and the note
    beats returning nothing — the digest card that follows should say the turn
    happened even when it cannot say what the turn concluded.
    """
    text = (trace.result or trace.text or "").strip()
    return f"{text}\n\n{note}".strip() if text else note


def _stream_resume(sid: str, real_cwd: str, message: str, env: dict) -> Optional[str]:
    """Run one `claude --resume` turn, editing a live Telegram card as it goes.

    Returns the turn's output text, or None if streaming could not be used at
    all — an old CLI without `stream-json`, a stream we could not parse, a
    Telegram send that failed. None means "fall back to the blocking path",
    never "the turn failed": a progress card is a nicety and must not be able
    to cost someone their reply.

    The converse matters just as much: None is only safe *before* the turn has
    visibly done anything. Once events have arrived, the model has already run
    tools — edited files, pushed commits — and re-running it through the
    blocking path would do all of it a second time. So every failure after the
    first parsed event returns text instead, however unsatisfying.
    """
    header = f"💬 *Working…* `[{sid[:8]}]`"
    sent = _tg_send(f"{header}\n⏳ starting")
    message_id = ((sent or {}).get("result") or {}).get("message_id")
    if not message_id:
        return None

    trace = _StreamTrace(sid)
    proc = subprocess.Popen(
        [_find_claude_bin(), "--resume", sid, "-p", message,
         "--output-format", "stream-json", "--verbose"],
        cwd=real_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    deadline = time.time() + 900
    last_edit = 0.0
    parsed = 0
    try:
        for raw in proc.stdout or []:
            if time.time() > deadline:
                proc.kill()
                _tg_edit(message_id, trace.render(header, "⏱ timed out (15 min)"))
                # Terminal, not a fallback: the turn ran for fifteen minutes and
                # whatever it did to the working tree is already done.
                return _stream_partial(trace, f"(timed out after 15 min, {trace.steps} step(s))")
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            parsed += 1
            if trace.feed(event) and time.time() - last_edit >= _stream_edit_secs():
                _tg_edit(message_id, trace.render(header, f"⏳ {trace.steps} step(s)"))
                last_edit = time.time()
        proc.wait(timeout=60)
    except Exception:
        log.debug("stream-json resume failed for %s", sid[:8], exc_info=True)
        try:
            proc.kill()
        except Exception:
            pass
        if parsed:
            # Telegram is a plausible cause of landing here, so this last
            # courtesy edit gets its own guard — it must not replace the
            # exception we are already handling.
            try:
                _tg_edit(message_id, trace.render(header, "❌ lost the stream"))
            except Exception:
                log.debug("could not mark the stream card as failed", exc_info=True)
            return _stream_partial(trace, "(the turn ran, but its output was lost mid-stream)")
        return None
    finally:
        if proc.stdout:
            proc.stdout.close()

    if not parsed:
        # Almost certainly a CLI that doesn't know `stream-json`. Take the
        # progress card away rather than leaving "⏳ starting" as a headstone.
        _tg_call("deleteMessage", chat_id=_stream_chat_id(), message_id=message_id)
        return None

    state = "❌ finished with an error" if trace.is_error else f"✅ done · {trace.steps} step(s)"
    _tg_edit(message_id, trace.render(header, state))

    out = (trace.result or trace.text or "").strip()
    if not out:
        out = (proc.stderr.read() if proc.stderr else "").strip()
    return out or "(no output)"


def _stream_chat_id() -> str:
    env = _load_env_file()
    return (env.get("TELEGRAM_CHAT_ID")
            or env.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")[0].strip())


def _run_resume_background(sid: str, cwd: str, message: str) -> None:
    """Spawn `claude --resume` in a background thread, post a triage digest (+ full-output
    button) to Telegram on completion."""
    def task():
        try:
            env = _claude_env()
            # Mark this as bridge-spawned so the resumed session's own Stop hook is
            # swallowed — the user already gets the "Reply from" card below; a second
            # "Stop" card would be redundant.
            env["TELECHAT_BRIDGE_INTERNAL"] = "1"
            # `claude --resume` is scoped by cwd. The stored cwd can be stale (a hook
            # fired from a subdir), so resolve the session's true cwd from its transcript.
            real_cwd = _resolve_session_cwd(sid, fallback=cwd) or cwd

            out = _stream_resume(sid, real_cwd, message, env) if _stream_enabled() else None
            if out is None:
                r = subprocess.run(
                    [_find_claude_bin(), "--resume", sid, "-p", message,
                     "--output-format", "text"],
                    cwd=real_cwd, capture_output=True, text=True, timeout=900,
                    env=env,
                )
                out = (r.stdout or "").strip() or (r.stderr or "").strip() or "(no output)"
            # The resume just wrote its turn to the transcript, so the same
            # extraction that enriches a Stop card works here — a reply sent
            # from your phone should show what it changed.
            evidence = _evidence_block(_find_transcript_by_sid(sid), real_cwd)
            _digest_card(f"💬 *Reply from* `[{sid[:8]}]`", out,
                         session_short=sid[:8], evidence=evidence)
        except subprocess.TimeoutExpired:
            _tg_send(f"⏱ Session `[{sid[:8]}]` timed out (15 min)")
        except Exception as e:
            _tg_send(f"❌ Failed to send to `[{sid[:8]}]`: {_md(str(e))}")
    threading.Thread(target=task, daemon=True).start()


# ───────────────────────── hook entry: notify ─────────────────────────

def hook_notify(event: str, payload: dict) -> None:
    sid = payload.get("session_id", "")
    # Prefer the session's true cwd (from its transcript) over the payload cwd, which
    # can be a stale subdirectory and would break later --resume calls.
    cwd = _resolve_session_cwd(sid, fallback=payload.get("cwd", "")) or payload.get("cwd", "")
    project = Path(cwd).name if cwd else "(unknown)"
    short = sid[:8] if sid else "?"
    icon = {"Stop": "✅", "Notification": "🔔", "SubagentStop": "🤖"}.get(event, "ℹ️")
    notif_msg = payload.get("message", "")

    header = f"{icon} *{event}* — `{project}`  `[{short}]`"
    body_parts = []
    if notif_msg:
        body_parts.append(notif_msg)
    transcript = _find_transcript(payload)
    summary = _last_assistant_text(transcript) if event in ("Stop", "SubagentStop") else None
    if summary:
        body_parts.append(summary)
    body = "\n\n".join(body_parts)

    # What the turn actually did — files, tests, errors — read straight out of
    # the transcript. A Notification is a permission prompt, not finished work,
    # so it has no turn to describe.
    evidence = (_evidence_block(transcript, cwd)
                if event in ("Stop", "SubagentStop") else "")

    footer_note = "_Reply to interact, or tap a button._"
    session_short = short if (sid and cwd) else None

    # Substantial body → AI triage digest + [📄 Full output] (+ [💬 Use session]) buttons.
    # Short / empty body → simple card with the Use-session button.
    if body and len(body) >= _DIGEST_MIN_CHARS:
        r = _digest_card(header, body, session_short=session_short, evidence=evidence)
        if r is None:
            # digest unavailable → fallback already chunked the body; add a button-bearing tail.
            markup = {"inline_keyboard": [[
                {"text": "💬 Use this session", "callback_data": f"bridge:use:{short}"},
            ]]} if session_short else None
            r = _tg_send(f"↑ {project} `[{short}]` — {footer_note}", reply_markup=markup)
    else:
        markup = {"inline_keyboard": [[
            {"text": "💬 Use this session", "callback_data": f"bridge:use:{short}"},
        ]]} if session_short else None
        # A short body is where evidence earns the most: "Done." plus three
        # files and a green suite is a card you can act on; "Done." alone is not.
        text = header + ("\n\n" + _md(body) if body else "")
        if evidence:
            text += "\n\n" + evidence
        text += "\n\n" + footer_note
        r = _tg_send(text, reply_markup=markup)

    if r and r.get("ok") and sid and cwd:
        c = _db()
        c.execute(
            "INSERT OR REPLACE INTO bridge_session_messages(message_id,session_id,cwd,created_at)"
            " VALUES(?,?,?,?)",
            (r["result"]["message_id"], sid, cwd, datetime.now().isoformat()),
        )
        c.commit()


# ───────────────────────── what you are approving ─────────────────────────
# The card used to show a tool name and a file path. Approving a Write on that
# basis is approving a change you cannot see, which is not a decision — it is a
# guess with a button under it. These render the pending call itself.

_PREVIEW_MAX_LINES = 14
_PREVIEW_MAX_LINE = 110


def _fence(body: str) -> str:
    """A Telegram code block that cannot be escaped by its own contents."""
    return "```\n" + body.replace("```", "'''") + "\n```"


def _clip_lines(text: str, limit: int = _PREVIEW_MAX_LINES) -> str:
    lines = (text or "").splitlines() or [""]
    kept = [
        (l if len(l) <= _PREVIEW_MAX_LINE else l[: _PREVIEW_MAX_LINE - 1].rstrip() + "…")
        for l in lines[:limit]
    ]
    if len(lines) > limit:
        kept.append(f"… {len(lines) - limit} more line(s)")
    return "\n".join(kept)


def _diff_preview(old: str, new: str) -> str:
    """A minus/plus block for an edit, budgeted evenly between the two sides."""
    half = max(2, _PREVIEW_MAX_LINES // 2)
    out = []
    for sign, text in (("-", old), ("+", new)):
        lines = (text or "").splitlines()
        for line in lines[:half]:
            trimmed = line if len(line) <= _PREVIEW_MAX_LINE else line[: _PREVIEW_MAX_LINE - 1] + "…"
            out.append(f"{sign} {trimmed}")
        if len(lines) > half:
            out.append(f"{sign} … {len(lines) - half} more line(s)")
    return "\n".join(out)


def describe_tool_call(tool: str, tool_input: dict, cwd: str = "") -> str:
    """Markdown showing what a pending tool call would actually do."""
    ti = tool_input if isinstance(tool_input, dict) else {}

    def short(path) -> str:
        path = str(path or "")
        if cwd and path.startswith(cwd + "/"):
            return path[len(cwd) + 1:]
        return path

    if tool == "Bash":
        command = str(ti.get("command") or "")
        head = "*Bash*"
        desc = str(ti.get("description") or "").strip()
        if desc:
            head += f" — _{_md(desc)}_"
        return f"{head}\n{_fence(_clip_lines(command))}"

    if tool in ("Edit", "NotebookEdit"):
        old = str(ti.get("old_string") or ti.get("old_source") or "")
        new = str(ti.get("new_string") or ti.get("new_source") or "")
        path = short(ti.get("file_path") or ti.get("notebook_path"))
        body = _diff_preview(old, new)
        return f"*{tool}* `{_backtick_safe(path)}`" + (f"\n{_fence(body)}" if body else "")

    if tool == "MultiEdit":
        raw_edits = ti.get("edits")
        edits = raw_edits if isinstance(raw_edits, list) else []
        path = short(ti.get("file_path"))
        head = f"*MultiEdit* `{_backtick_safe(path)}` — {len(edits)} edit(s)"
        first = next((e for e in edits if isinstance(e, dict)), None)
        if not first:
            return head
        body = _diff_preview(str(first.get("old_string") or ""),
                             str(first.get("new_string") or ""))
        more = f"\n_…and {len(edits) - 1} more edit(s)_" if len(edits) > 1 else ""
        return f"{head}\n{_fence(body)}{more}"

    if tool == "Write":
        content = str(ti.get("content") or "")
        path = short(ti.get("file_path"))
        size = f"{len(content.splitlines())} lines · {len(content):,} chars"
        return (f"*Write* `{_backtick_safe(path)}` — {size}\n"
                f"{_fence(_clip_lines(content, 10))}")

    # Anything else: its inputs, which beats the tool name on its own.
    try:
        body = json.dumps(ti, indent=2, default=str)
    except Exception:
        body = str(ti)
    return f"*{_md(tool)}*\n{_fence(_clip_lines(body))}"


def _backtick_safe(text: str) -> str:
    return (text or "").replace("`", "'")


def _record_approval(req_id: str, sid: str, cwd: str, tool: str,
                     prefix: Optional[str] = None,
                     decision: Optional[str] = None) -> None:
    """Log an approval request. ``prefix`` is what a rule tap would cover.

    Rules are written from the *card*, which by then knows only a request id —
    so the prefix has to be persisted with the request rather than recomputed
    from a tool input that is long gone by the time anyone taps.
    """
    c = _db()
    c.execute(
        "INSERT INTO bridge_approvals"
        "(request_id,session_id,cwd,tool,rule_prefix,decision,created_at,decided_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (req_id, sid, cwd, tool, prefix, decision, datetime.now().isoformat(),
         datetime.now().isoformat() if decision else None),
    )
    c.commit()


# ───────────────────────── hook entry: approve ─────────────────────────

def hook_approve(payload: dict) -> Optional[dict]:
    """PreToolUse hook. Returns the decision dict (or None for pass-through)."""
    cwd = payload.get("cwd", "")
    sid = payload.get("session_id", "")
    if not cwd or not approve_mode_on(cwd):
        return None  # pass-through

    tool_name = payload.get("tool_name", "?")
    ti = payload.get("tool_input", {}) or {}

    key = rule_key(tool_name, ti)
    prefix = key[1] if key else None

    # A standing rule is a decision the user already made. Resolve it silently:
    # being asked again is exactly what the rule was tapped to prevent.
    standing = find_approval_rule(cwd, tool_name, ti)
    if standing in ("y", "n"):
        _record_approval(uuid.uuid4().hex[:8], sid, cwd, tool_name, prefix,
                         decision=standing)
        label = _rule_label(*(key or (tool_name, "")))
        if standing == "y":
            return _approval_allow()
        return _approval_deny(f"Denied by a standing rule for {label} (see /approvals)")

    req_id = uuid.uuid4().hex[:8]
    _record_approval(req_id, sid, cwd, tool_name, prefix)

    timeout = _approval_timeout()
    on_timeout = _approval_timeout_action()
    minutes = timeout / 60
    expiry_note = {
        "deny": f"_Auto-denies in {minutes:.0f} min._",
        "allow": f"_Auto-approves in {minutes:.0f} min._",
        "fallthrough": f"_Falls back to the desktop prompt in {minutes:.0f} min._",
    }[on_timeout]
    text = (
        f"⚠️ *Approval needed* — `{Path(cwd).name}`  `[{sid[:8]}]`\n\n"
        f"{describe_tool_call(tool_name, ti, cwd)}\n\n{expiry_note}"
    )
    rows = [[
        {"text": "✅ Approve", "callback_data": f"bridge:appr:{req_id}:y"},
        {"text": "❌ Deny",    "callback_data": f"bridge:appr:{req_id}:n"},
    ]]
    # "…and stop asking" — offered only when the call can be described by a rule
    # that will still mean the same thing next time.
    if key:
        rows.append([
            {"text": f"👍 Always allow {_rule_label(*key)}",
             "callback_data": f"bridge:rule:{req_id}:y"},
        ])
    _tg_send(text, reply_markup={"inline_keyboard": rows})

    deadline = time.time() + timeout
    decision = None
    while time.time() < deadline:
        c = _db()
        row = c.execute(
            "SELECT decision FROM bridge_approvals WHERE request_id=?", (req_id,)
        ).fetchone()
        if row and row[0]:
            decision = row[0]
            break
        time.sleep(0.5)

    if decision == "y":
        return _approval_allow()
    if decision == "n":
        return _approval_deny("Denied via Telegram")

    # Nobody answered. What that means is policy, not an accident.
    if on_timeout == "deny":
        return _approval_deny(
            f"No response on Telegram within {minutes:.0f} min "
            "(BRIDGE_APPROVAL_TIMEOUT_ACTION=deny)"
        )
    if on_timeout == "allow":
        return _approval_allow()
    return None  # fallthrough → Claude Code's normal permission flow


def _approval_allow() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "allow"}}


def _approval_deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ───────────────────────── telechat-runtime: command handlers ─────────────────────────
# These are invoked from telegram_bot.py with the python-telegram-bot Update/Context types.

HELP_TEXT = (
    "*Claude Desktop Bridge*\n\n"
    "💬 *Interact with your Claude Desktop sessions:*\n"
    "• `/desktop` — live panel: running sessions' status (⏳ working / 💤 idle) + activity\n"
    "• `/recent` — browse & resume any recent session (running or finished)\n"
    "• Tap *✅ Proceed* on a decision card to reply \"yes, go ahead\" in one tap\n"
    "• Tap *📊 Status* to ask any session for a quick status update\n"
    "• `/desktop_use <id>` — switch to a session (8-char short id)\n"
    "• `/desktop_which` — show current session\n"
    "• `/desktop_clear` — clear current session\n"
    "• Reply to any session card → goes to that session\n"
    "• Otherwise typed messages go to the current session, if one is set\n"
    "• `/desktop_all <msg>` — broadcast to every running session\n\n"
    "👁 *Live sync:*\n"
    "• `/follow <id>` — stream a session's messages & tool actions here live\n"
    "• `/unfollow [id|all]` · `/following` — manage live mirrors\n"
    "• Session start/exit pings are on by default (`/lifecycle on|off`)\n\n"
    "🔐 *Permission control:*\n"
    "• Approval cards show the *actual* call — the command, or a diff of the edit\n"
    "• Tap *✅ Approve* / *❌ Deny*, or *👍 Always allow …* to decide it and stop being asked\n"
    "• `/approvals` — list standing rules and revoke any of them (`/approvals clear` for all)\n"
    "• `/approve_all_on` — route *every* session's permissions to Telegram (no per-project arming)\n"
    "• `/desktop_approve_on` (reply to a card) — arm approval for that one project\n"
    "• `/desktop_approve_off` / `/approve_all_off` — disable"
)


def _build_sessions_panel() -> tuple[str, object]:
    """Build the /desktop panel: (markdown_text, InlineKeyboardMarkup | None)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    sessions = list_running_sessions()
    cur_sid, _ = get_current_session()
    if not sessions:
        return "_No Claude Desktop sessions running._", None
    lines = ["🖥 *Running sessions*\n"]
    keyboard = []
    for s in sessions:
        sid = s["sid"]
        proj = Path(s["cwd"]).name if s["cwd"] else "(new)"
        if not sid:
            lines.append(f"• _new session, no id yet_  {s['etime']}  pid={s['pid']}")
            continue
        short = sid[:8]
        selected = "🟢" if cur_sid == sid else "▫️"
        approve = " 🔐" if approve_mode_on(s["cwd"]) else ""
        st = _session_status(sid)
        state = "⏳ working" if st["busy"] else "💤 idle"
        cur_tag = "  ← current" if cur_sid == sid else ""
        lines.append(f"{selected} *{proj}* `[{short}]`{approve}{cur_tag}")
        lines.append(f"    {state} · {s['model']} · up {s['etime']}")
        if st["last"]:
            lines.append(f"    _{_md(st['last'])}_")
        lines.append("")
        label = ("✅ " if cur_sid == sid else "💬 ") + f"Use {proj}"
        keyboard.append([{"text": label, "callback_data": f"bridge:use:{short}"}])
    if cur_sid:
        keyboard.append([{"text": "🚫 Clear current", "callback_data": "bridge:use:clear"}])
    keyboard.append([{"text": "🔄 Refresh", "callback_data": "bridge:refresh"}])
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(**b) for b in row] for row in keyboard])
    return "\n".join(lines).rstrip(), markup


async def cmd_desktop(update, ctx):
    text, markup = _build_sessions_panel()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


def _build_recent_panel(limit: int = 8) -> tuple[str, object]:
    """Panel of recent sessions across all projects (running or not)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    recent = list_recent_sessions(limit=limit)
    cur_sid, _ = get_current_session()
    if not recent:
        return "_No Claude sessions found._", None
    lines = ["🕘 *Recent sessions* — resume any of them:\n"]
    keyboard = []
    for s in recent:
        sid, short = s["sid"], s["sid"][:8]
        proj = Path(s["cwd"]).name if s["cwd"] else "(unknown)"
        dot = "🟢" if cur_sid == sid else ("⏳" if s["running"] else "💤")
        run_tag = " · running" if s["running"] else ""
        lines.append(f"{dot} *{proj}* `[{short}]` · {s['ago']}{run_tag}")
        if s["last"]:
            lines.append(f"    _{_md(s['last'])}_")
        lines.append("")
        keyboard.append([{"text": f"💬 Resume {proj} · {s['ago']}",
                          "callback_data": f"bridge:use:{short}"}])
    keyboard.append([{"text": "🔄 Refresh", "callback_data": "bridge:refresh_recent"}])
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(**b) for b in row] for row in keyboard])
    return "\n".join(lines).rstrip(), markup


async def cmd_desktop_recent(update, ctx):
    text, markup = _build_recent_panel()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def cmd_desktop_use(update, ctx):
    arg = " ".join(ctx.args).strip() if ctx.args else ""
    if not arg:
        await update.message.reply_text("Usage: `/desktop_use <short-id>`",
                                        parse_mode="Markdown")
        return
    res = resolve_short_session(arg.split()[0])
    if not res:
        await update.message.reply_text(f"No session matches `{_md(arg)}`. Try /desktop.",
                                        parse_mode="Markdown")
        return
    sid, cwd = res
    set_current_session(sid, cwd)
    await update.message.reply_text(
        f"🟢 Current session: *{Path(cwd).name}*  `[{sid[:8]}]`\nType freely.",
        parse_mode="Markdown",
    )


async def cmd_desktop_which(update, ctx):
    sid, cwd = get_current_session()
    if sid:
        await update.message.reply_text(
            f"Current: *{Path(cwd).name}*  `[{sid[:8]}]`", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("No current session. /desktop to pick one.")


async def cmd_desktop_clear(update, ctx):
    set_current_session(None, None)
    await update.message.reply_text("Cleared current session.")


async def cmd_desktop_all(update, ctx):
    msg = " ".join(ctx.args).strip() if ctx.args else ""
    if not msg:
        await update.message.reply_text("Usage: `/desktop_all <message>`", parse_mode="Markdown")
        return
    targets = [s for s in list_running_sessions() if s["sid"] and s["cwd"]]
    if not targets:
        await update.message.reply_text("_No interactable sessions running._",
                                        parse_mode="Markdown")
        return
    names = ", ".join(Path(s["cwd"]).name for s in targets)
    await update.message.reply_text(
        f"📡 Broadcasting to *{len(targets)}* session(s): {_md(names)}",
        parse_mode="Markdown",
    )
    for s in targets:
        _run_resume_background(s["sid"], s["cwd"], msg)


async def cmd_desktop_approve_on(update, ctx):
    await _toggle_approve(update, True)


async def cmd_desktop_approve_off(update, ctx):
    await _toggle_approve(update, False)


async def _toggle_approve(update, enable: bool) -> None:
    reply_to = update.message.reply_to_message
    if not reply_to:
        await update.message.reply_text(
            "Reply to a session card first, then use the command."
        )
        return
    c = _db()
    row = c.execute(
        "SELECT cwd FROM bridge_session_messages WHERE message_id=?", (reply_to.message_id,)
    ).fetchone()
    if not row:
        await update.message.reply_text("Couldn't find that session in my records.")
        return
    cwd = row[0]
    set_approve_mode(cwd, enable)
    state = "ON" if enable else "OFF"
    await update.message.reply_text(
        f"🔐 Approval mode *{state}* for `{Path(cwd).name}`", parse_mode="Markdown"
    )


# ── global approve toggle ──

async def cmd_approve_all_on(update, ctx):
    _state_set("approve_all", "1")
    await update.message.reply_text(
        "🔐 *Global approval ON* — every session's Bash/Write/Edit now asks you here "
        "before running. Use /approve_all_off to disable.", parse_mode="Markdown")


async def cmd_approve_all_off(update, ctx):
    _state_set("approve_all", "0")
    await update.message.reply_text(
        "🔓 *Global approval OFF* — sessions use their normal permission flow "
        "(per-project /desktop_approve_on still applies).", parse_mode="Markdown")


# ── standing approval rules ──

def _build_rules_panel() -> tuple[str, object]:
    """The /approvals panel: every standing rule, each with a revoke button.

    A permission you granted from a phone weeks ago and cannot see is a trap, so
    listing and revoking are part of the feature rather than an extra."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rules = list_approval_rules()
    if not rules:
        return (
            "_No standing approval rules._\n\n"
            "Tap *👍 Always allow …* on an approval card to add one.",
            None,
        )
    lines = ["🔐 *Standing approval rules*\n"]
    keyboard = []
    for rule_id, cwd, tool, prefix, decision in rules:
        icon = "✅" if decision == "y" else "❌"
        label = _rule_label(tool, prefix)
        project = Path(cwd).name if cwd else "(unknown)"
        lines.append(f"{icon} `{_backtick_safe(label)}` — in *{_md(project)}*")
        keyboard.append([{"text": f"🗑 Revoke {label} · {project}",
                          "callback_data": f"bridge:rulerm:{rule_id}"}])
    lines.append("\n_Rules are per project. `/approvals clear` removes them all._")
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(**b) for b in row] for row in keyboard]
    )
    return "\n".join(lines), markup


async def cmd_approvals(update, ctx):
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg == "clear":
        removed = clear_approval_rules()
        await update.message.reply_text(
            f"🔓 Removed {removed} standing approval rule(s). "
            "Tool calls will ask again."
        )
        return
    text, markup = _build_rules_panel()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


# ── lifecycle toggle ──

async def cmd_lifecycle(update, ctx):
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("on", "off"):
        _state_set("lifecycle", "1" if arg == "on" else "0")
    state = "ON" if lifecycle_on() else "OFF"
    await update.message.reply_text(
        f"🔔 Session start/exit pings: *{state}*  (use `/lifecycle on|off`)",
        parse_mode="Markdown")


# ── follow mode (live mirror) ──

async def cmd_follow(update, ctx):
    arg = " ".join(ctx.args).strip() if ctx.args else ""
    sid_cwd = None
    if arg:
        sid_cwd = resolve_short_session(arg.split()[0])
    else:
        cur_sid, cur_cwd = get_current_session()
        if cur_sid:
            sid_cwd = (cur_sid, cur_cwd)
    if not sid_cwd:
        await update.message.reply_text(
            "Usage: `/follow <short-id>` (or set a current session first). See /desktop or /recent.",
            parse_mode="Markdown")
        return
    sid, cwd = sid_cwd
    cwd = _resolve_session_cwd(sid, fallback=cwd) or cwd
    # Start streaming from the current end of the transcript (only NEW activity).
    tpath = _find_transcript_by_sid(sid)
    start_pos = tpath.stat().st_size if tpath else 0
    follow_add(sid, cwd, start_pos)
    await update.message.reply_text(
        f"👁 *Following* `[{sid[:8]}]` — *{Path(cwd).name}*\n"
        f"New messages & tool actions will stream here live. Stop with `/unfollow {sid[:8]}`.",
        parse_mode="Markdown")


async def cmd_unfollow(update, ctx):
    arg = " ".join(ctx.args).strip() if ctx.args else ""
    if arg.lower() in ("", "all"):
        follows = list_follows()
        for sid, _, _ in follows:
            follow_remove(sid)
        await update.message.reply_text(f"👁 Unfollowed {len(follows)} session(s).")
        return
    n = follow_remove(arg.split()[0])
    await update.message.reply_text(
        f"👁 Unfollowed `{_md(arg)}`." if n else f"Not following `{_md(arg)}`.",
        parse_mode="Markdown")


async def cmd_following(update, ctx):
    follows = list_follows()
    if not follows:
        await update.message.reply_text("Not following any sessions. `/follow <id>` to start.",
                                        parse_mode="Markdown")
        return
    lines = ["👁 *Following:*"]
    for sid, cwd, _ in follows:
        lines.append(f"• `[{sid[:8]}]` — {Path(cwd).name if cwd else '?'}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# Returns True if the bot consumed the message (so the default text handler should skip it).
async def try_handle_text_message(update, ctx) -> bool:
    """Called by telegram_bot.handle_message before its default routing.
    Routes replies to session cards and current-session typed messages to claude --resume.
    Returns True if handled, False to let normal flow continue."""
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    reply_to = update.message.reply_to_message
    sid_cwd = None
    if reply_to:
        c = _db()
        row = c.execute(
            "SELECT session_id, cwd FROM bridge_session_messages WHERE message_id=?",
            (reply_to.message_id,),
        ).fetchone()
        if row:
            sid_cwd = tuple(row)
    if sid_cwd is None:
        cur_sid, cur_cwd = get_current_session()
        if cur_sid and cur_cwd:
            sid_cwd = (cur_sid, cur_cwd)
        # No implicit fallback to a lone running session: hijacking every plain
        # message whenever one Claude session happens to be open makes the bot
        # unusable as a normal assistant. Route only on explicit targets —
        # a reply to a session card, or a session pinned via "Use session".
    if sid_cwd is None:
        return False
    sid, cwd = sid_cwd
    # Resolve the true cwd so the label matches where the resume actually runs.
    cwd = _resolve_session_cwd(sid, fallback=cwd) or cwd
    # Warn (but proceed) if the session is mid-turn — the resume will queue behind it.
    busy_note = ""
    if _session_status(sid)["busy"]:
        busy_note = "  ⏳ _(session is working — your message will queue until it's free)_"
    await update.message.reply_text(
        f"🔄 → *{Path(cwd).name}*  `[{sid[:8]}]`…{busy_note}", parse_mode="Markdown"
    )
    _run_resume_background(sid, cwd, text)
    return True


# Returns True if the bot consumed the callback.
async def try_handle_callback(update, ctx) -> bool:
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("bridge:"):
        return False
    body = data[len("bridge:"):]
    if body in ("refresh", "refresh_recent"):
        await q.answer("Refreshed")
        text, markup = (_build_recent_panel() if body == "refresh_recent"
                        else _build_sessions_panel())
        try:
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            # Telegram rejects edits with identical content, which is the
            # common case here: nothing changed since the last refresh.
            log.debug("panel refresh edit rejected", exc_info=True)
        return True
    if body.startswith("act:"):
        # Quick action: bridge:act:<short>:<proceed|status>
        try:
            short, action = body[len("act:"):].split(":", 1)
        except ValueError:
            await q.answer("Bad action")
            return True
        res = resolve_short_session(short)
        if not res:
            await q.answer("Session not found")
            return True
        sid, cwd = res
        cwd = _resolve_session_cwd(sid, fallback=cwd) or cwd
        prompts = {
            "proceed": "Yes, please proceed.",
            "status": "Give me a brief one or two sentence status update on what you're "
                      "currently doing — no need to take any action.",
        }
        msg = prompts.get(action)
        if not msg:
            await q.answer("Unknown action")
            return True
        labels = {"proceed": "✅ Proceeding", "status": "📊 Asking for status"}
        await q.answer(labels.get(action, "Sending…"))
        await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"{labels.get(action,'→')} → *{Path(cwd).name}*  `[{short}]`…",
            parse_mode="Markdown",
        )
        _run_resume_background(sid, cwd, msg)
        return True
    if body.startswith("use:"):
        short = body[4:]
        if short == "clear":
            set_current_session(None, None)
            await q.answer("Cleared")
            await ctx.bot.send_message(chat_id=q.message.chat_id, text="Cleared current session.")
            return True
        res = resolve_short_session(short)
        if not res:
            await q.answer("Session not found")
            return True
        sid, cwd = res
        set_current_session(sid, cwd)
        await q.answer(f"Now using {Path(cwd).name}")
        await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"🟢 Current session: *{Path(cwd).name}*  `[{sid[:8]}]`\nType freely.",
            parse_mode="Markdown",
        )
        return True
    if body.startswith("rulerm:"):
        try:
            rule_id = int(body[len("rulerm:"):])
        except ValueError:
            await q.answer("Bad rule")
            return True
        await q.answer("Removed" if remove_approval_rule(rule_id) else "Already gone")
        text, markup = _build_rules_panel()
        try:
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            log.debug("rules panel edit rejected", exc_info=True)
        return True
    if body.startswith("appr:") or body.startswith("rule:"):
        standing = body.startswith("rule:")
        try:
            _, req_id, decision = body.split(":", 2)
        except ValueError:
            return True
        rule_label = ""
        if standing:
            # Same tap decides this call *and* writes the rule, so approving
            # "and stop asking" costs one tap rather than two.
            row = _db().execute(
                "SELECT cwd, tool, rule_prefix FROM bridge_approvals WHERE request_id=?",
                (req_id,),
            ).fetchone()
            if row and row[0] and row[1]:
                add_approval_rule(row[0], row[1], row[2] or "", decision)
                rule_label = _rule_label(row[1], row[2] or "")
        c = _db()
        c.execute(
            "UPDATE bridge_approvals SET decision=?, decided_at=? WHERE request_id=?",
            (decision, datetime.now().isoformat(), req_id),
        )
        c.commit()
        label = "✅ Approved" if decision == "y" else "❌ Denied"
        if rule_label:
            label += f" · always {rule_label}"
        await q.answer(label)
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(text=label, callback_data="noop")]]
                )
            )
        except Exception:
            # Cosmetic button relabel; Telegram rejects a no-op edit.
            log.debug("could not relabel approval button", exc_info=True)
        return True
    if body.startswith("full:"):
        token = body[len("full:"):]
        content = _get_full_output(token)
        if content is None:
            await q.answer("Full output expired")
            return True
        await q.answer("Posting full output…")
        # Reuse the chunker; runs in the async loop's executor to avoid blocking.
        import asyncio as _aio
        await _aio.get_event_loop().run_in_executor(
            None, lambda: _tg_send_long("📄 *Full output*", content)
        )
        return True
    return False


def register(app) -> None:
    """Register all bridge command + callback handlers into a python-telegram-bot Application.
    Called from telegram_bot.build_app."""
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("desktop",             cmd_desktop))
    app.add_handler(CommandHandler("recent",              cmd_desktop_recent))
    app.add_handler(CommandHandler("desktop_recent",      cmd_desktop_recent))
    app.add_handler(CommandHandler("desktop_use",         cmd_desktop_use))
    app.add_handler(CommandHandler("desktop_which",       cmd_desktop_which))
    app.add_handler(CommandHandler("desktop_clear",       cmd_desktop_clear))
    app.add_handler(CommandHandler("desktop_all",         cmd_desktop_all))
    app.add_handler(CommandHandler("desktop_approve_on",  cmd_desktop_approve_on))
    app.add_handler(CommandHandler("desktop_approve_off", cmd_desktop_approve_off))
    app.add_handler(CommandHandler("approve_all_on",      cmd_approve_all_on))
    app.add_handler(CommandHandler("approve_all_off",     cmd_approve_all_off))
    app.add_handler(CommandHandler("approvals",           cmd_approvals))
    app.add_handler(CommandHandler("lifecycle",           cmd_lifecycle))
    app.add_handler(CommandHandler("follow",              cmd_follow))
    app.add_handler(CommandHandler("unfollow",            cmd_unfollow))
    app.add_handler(CommandHandler("following",           cmd_following))
    # Background watcher: lifecycle pings + follow-mode live streaming.
    start_watcher()
    # NOTE: callback + text-reply routing is handled cooperatively by hooks in
    # telegram_bot.handle_callback / handle_message (they call try_handle_callback /
    # try_handle_text_message respectively), so we do not register catch-all handlers
    # here — those would conflict with telechat's existing chat flow.


# ───────────────────────── CLI: install / uninstall / migrate ─────────────────────────

_HOOK_MARKER = "telechat-bridge"  # used to identify our entries for clean uninstall


def _settings_load() -> dict:
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS.read_text())
    except Exception:
        return {}


def _settings_save(data: dict) -> None:
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2))


def _hook_command_path() -> str:
    """Path to the telechat binary the hooks will invoke."""
    return shutil.which("telechat") or "telechat"


def _is_bridge_entry(entry: dict) -> bool:
    """True if a settings.json hook entry belongs to the bridge — matched by command
    content (robust to Claude Code stripping our custom `_source` marker)."""
    s = json.dumps(entry)
    return ("bridge notify" in s or "bridge approve" in s
            or "/.claude-bridge/bridge.py" in s or _HOOK_MARKER in s)


def cli_install(approve_hook: bool = False, with_service: bool = True) -> int:
    """Full bridge install: preflight checks → hooks → migrate → persistent service.
    Idempotent."""
    print("Installing Claude Desktop bridge…\n")
    settings = _settings_load()
    hooks = settings.setdefault("hooks", {})
    tc = _hook_command_path()

    def _ensure(event_name: str, command: str, matcher: Optional[str] = None,
                timeout: int = 90):
        bucket = hooks.setdefault(event_name, [])
        # Identify our entries by COMMAND CONTENT, not a custom marker — Claude Code
        # normalizes settings.json and strips unknown keys like "_source", which used to
        # break dedup and stack duplicate hooks on every reinstall.
        bucket[:] = [b for b in bucket if not _is_bridge_entry(b)]
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout,
                             "_source": _HOOK_MARKER}]}
        if matcher:
            entry["matcher"] = matcher
        bucket.append(entry)

    # 90s timeout: notify hooks now run a synchronous AI digest (haiku, ~2-5s).
    _ensure("Stop",         f"{tc} bridge notify Stop")
    _ensure("Notification", f"{tc} bridge notify Notification")
    _ensure("SubagentStop", f"{tc} bridge notify SubagentStop")
    if approve_hook:
        _ensure("PreToolUse", f"{tc} bridge approve",
                matcher="Bash|Write|Edit|MultiEdit", timeout=310)

    _settings_save(settings)
    print(f"  ✓ Hooks installed in {CLAUDE_SETTINGS}")
    print(f"    Stop · Notification · SubagentStop → {tc} bridge notify <event>")
    if approve_hook:
        print("  ✓ Approval hook registered (Bash|Write|Edit|MultiEdit)")

    # Migrate from standalone bridge if present (also copies the OAuth token).
    _migrate_standalone()

    # Persistent service so the bridge is always listening.
    if with_service:
        print()
        service_install()

    # Preflight last, so the thing that still needs doing is the thing you read.
    warnings = _preflight()
    if warnings:
        print("\n⚠ Installed, but not working yet. Resolve:")
        for w in warnings:
            print(f"  • {w}")
        print("\nThen re-check with:  telechat bridge status")
    else:
        print("\n✓ All prerequisites satisfied — the bridge is ready.")
        print("\nNext:")
        print("  1. Run a Claude Code session and let it finish a turn.")
        print("  2. The triage card lands in Telegram — reply to it to resume that session.")
        print("  3. /desktop lists every running session; tap one to make it the current one.")
        if not approve_hook:
            print("\nTo approve Bash/Write/Edit calls from your phone, re-run with --approval.")
        print("\nVerify any time with:  telechat bridge status")
    return 0


def cli_status() -> int:
    """Answer the only question worth asking: is the bridge actually wired up?

    Exits non-zero when something blocking is wrong, so it can be used as a
    check in a script rather than read by eye.
    """
    checks = bridge_checks()
    print("Claude Desktop bridge\n")
    for c in checks:
        icon = "✓" if c["ok"] else ("✗" if c["blocking"] else "•")
        print(f"  {icon} {c['name']}: {c['detail']}")
        if not c["ok"]:
            print(f"      Fix: {c['fix']}")

    broken = [c for c in checks if c["blocking"] and not c["ok"]]
    print()
    if broken:
        print(f"✗ Not ready — {len(broken)} thing(s) above must be fixed first.")
    else:
        print("✓ Wired up. Finish a Claude Code session and the card lands in Telegram.")

    sessions = list_running_sessions()
    sid, cwd = get_current_session()
    print(f"\nRunning sessions: {len(sessions)}")
    for s in sessions:
        proj = Path(s["cwd"]).name if s["cwd"] else ""
        print(f"  {s['sid'][:8] or '(new)':8}  {s['model']:24}  {s['etime']:>10}  {proj}")
    print(f"\nCurrent session: {sid[:8] if sid else '(none)'}  cwd={cwd or '-'}")
    return 1 if broken else 0


def cli_uninstall() -> int:
    """Remove bridge hooks from ~/.claude/settings.json."""
    print("Uninstalling Claude Desktop bridge hooks…")
    settings = _settings_load()
    hooks = settings.get("hooks", {})
    removed = 0
    for event in list(hooks.keys()):
        before = len(hooks[event])
        hooks[event] = [b for b in hooks[event] if not _is_bridge_entry(b)]
        removed += before - len(hooks[event])
        if not hooks[event]:
            del hooks[event]
    _settings_save(settings)
    print(f"  ✓ Removed {removed} hook entries from {CLAUDE_SETTINGS}")
    print("  ℹ The telechat service is left running (it also powers chat).")
    print("    To stop it too:  telechat bridge service uninstall")
    return 0


def _migrate_standalone() -> None:
    """If ~/.claude-bridge/ exists, unload its launchd plist and copy state into telechat DB."""
    if not STANDALONE_BRIDGE_DIR.exists():
        return
    print(f"  • Standalone bridge detected at {STANDALONE_BRIDGE_DIR} — migrating…")
    # Unload launchd agent
    if STANDALONE_LAUNCHD_PLIST.exists():
        try:
            subprocess.run(["launchctl", "unload", str(STANDALONE_LAUNCHD_PLIST)],
                           check=False, capture_output=True)
            print(f"    ✓ Unloaded {STANDALONE_LAUNCHD_PLIST.name}")
        except Exception as e:
            print(f"    ! launchctl unload failed: {e}")
    # Copy state.db rows into telechat DB
    src_db = STANDALONE_BRIDGE_DIR / "state.db"
    if src_db.exists():
        try:
            src = sqlite3.connect(src_db)
            dst = _db()
            src_rows = src.execute(
                "SELECT message_id, session_id, cwd, created_at FROM session_messages"
            ).fetchall()
            for row in src_rows:
                dst.execute(
                    "INSERT OR IGNORE INTO bridge_session_messages"
                    "(message_id,session_id,cwd,created_at) VALUES(?,?,?,?)", row
                )
            am_rows = src.execute("SELECT cwd, enabled FROM approve_mode").fetchall()
            for row in am_rows:
                dst.execute(
                    "INSERT OR IGNORE INTO bridge_approve_mode(cwd,enabled) VALUES(?,?)", row
                )
            dst.commit()
            src.close()
            print(f"    ✓ Copied {len(src_rows)} session msgs + {len(am_rows)} approve modes")
        except Exception as e:
            print(f"    ! state copy failed: {e}")
    # Copy CLAUDE_CODE_OAUTH_TOKEN from standalone .env into telechat .env if missing.
    src_env = STANDALONE_BRIDGE_DIR / ".env"
    tc_env = TELECHAT_HOME / ".env"
    if src_env.exists() and tc_env.exists():
        try:
            src_token = ""
            for line in src_env.read_text().splitlines():
                if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    src_token = line.split("=", 1)[1].strip()
                    break
            cur_env = tc_env.read_text()
            if src_token and "CLAUDE_CODE_OAUTH_TOKEN=" not in cur_env:
                if not cur_env.endswith("\n"):
                    cur_env += "\n"
                cur_env += f"CLAUDE_CODE_OAUTH_TOKEN={src_token}\n"
                tc_env.write_text(cur_env)
                print(f"    ✓ Copied CLAUDE_CODE_OAUTH_TOKEN into {tc_env}")
        except Exception as e:
            print(f"    ! token copy failed: {e}")
    # Archive the dir (don't delete)
    archive = STANDALONE_BRIDGE_DIR.with_suffix(".retired")
    try:
        if not archive.exists():
            STANDALONE_BRIDGE_DIR.rename(archive)
            print(f"    ✓ Archived to {archive}")
    except Exception as e:
        print(f"    ! archive failed: {e}")


# ───────────────────────── CLI: notify / approve subprocess entry ─────────────────────────

def cli_notify(event: str) -> int:
    # Recursion guard: the AI digest itself runs `claude`, whose Stop hook calls us again.
    # The digest subprocess carries TELECHAT_BRIDGE_INTERNAL=1 → swallow that notification.
    if os.environ.get("TELECHAT_BRIDGE_INTERNAL"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    hook_notify(event, payload)
    return 0


def cli_approve() -> int:
    if os.environ.get("TELECHAT_BRIDGE_INTERNAL"):
        return 0  # digest's own tool calls must never prompt for approval
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    decision = hook_approve(payload)
    if decision:
        print(json.dumps(decision))
    return 0


# ───────────────────────── preflight + persistent service ─────────────────────────

NOTIFY_EVENTS = ("Stop", "Notification", "SubagentStop")


def _hooked_events() -> list[str]:
    """Which notify events currently have a bridge hook registered."""
    hooks = _settings_load().get("hooks", {})
    found = []
    for event in NOTIFY_EVENTS:
        entries = hooks.get(event) or []
        if any(_is_bridge_entry(e) and "bridge notify" in json.dumps(e) for e in entries):
            found.append(event)
    return found


def _approval_hook_registered() -> bool:
    entries = _settings_load().get("hooks", {}).get("PreToolUse") or []
    return any("bridge approve" in json.dumps(e) for e in entries)


def _service_loaded() -> Optional[bool]:
    """True/False if launchd knows about the service; None where launchd doesn't apply."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        log.debug("launchctl list failed", exc_info=True)
        return False
    return any(SERVICE_LABEL in line for line in r.stdout.splitlines())


def bridge_checks() -> list[dict]:
    """Everything that has to be true for the bridge to work end to end.

    Each entry is ``{name, ok, detail, fix, blocking}``. ``blocking`` marks the
    ones that stop cards or replies from working at all; the rest are reported
    so the state is visible but do not count as failures.

    This exists because "did the install work?" and "why is nothing arriving?"
    are the same question, and neither the installer's one-shot warnings nor a
    session list could answer it. Both `telechat bridge install` and
    `telechat bridge status` render this list, so they can never disagree.
    """
    env = _load_env_file()
    checks: list[dict] = []

    claude_bin = shutil.which("claude") or (
        str(HOME / ".local/bin/claude") if (HOME / ".local/bin/claude").exists() else ""
    )
    checks.append({
        "name": "Claude Code CLI",
        "ok": bool(claude_bin),
        "detail": claude_bin or "not found on PATH",
        "fix": "npm install -g @anthropic-ai/claude-code && claude auth login",
        "blocking": True,
    })

    events = _hooked_events()
    checks.append({
        "name": "Hooks registered",
        "ok": len(events) == len(NOTIFY_EVENTS),
        "detail": (", ".join(events) if events else "none")
                  + f"  ({CLAUDE_SETTINGS})",
        "fix": "telechat bridge install",
        "blocking": True,
    })

    checks.append({
        "name": "Telegram bot token",
        "ok": bool(env.get("TELEGRAM_BOT_TOKEN")),
        "detail": "set" if env.get("TELEGRAM_BOT_TOKEN") else f"missing from {TELECHAT_HOME}/.env",
        "fix": "telechat init",
        "blocking": True,
    })

    recipient = env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_ALLOWED_USER_IDS")
    checks.append({
        "name": "Telegram recipient",
        "ok": bool(recipient),
        "detail": "set" if recipient else "no TELEGRAM_CHAT_ID / TELEGRAM_ALLOWED_USER_IDS",
        "fix": "telechat init",
        "blocking": True,
    })

    # Headless `claude --resume` (replies and digests) authenticates with a
    # long-lived token, not the interactive login — without it every reply 401s.
    checks.append({
        "name": "Long-lived OAuth token",
        "ok": bool(env.get("CLAUDE_CODE_OAUTH_TOKEN")),
        "detail": "set" if env.get("CLAUDE_CODE_OAUTH_TOKEN")
                  else "missing — replies and digests will fail with 401",
        "fix": f"claude setup-token, then add CLAUDE_CODE_OAUTH_TOKEN=… to {TELECHAT_HOME}/.env",
        "blocking": True,
    })

    loaded = _service_loaded()
    checks.append({
        "name": "Background service",
        "ok": True if loaded is None else loaded,
        "detail": ("not applicable on this platform — run telechat under systemd --user"
                   if loaded is None
                   else (f"{SERVICE_LABEL} loaded" if loaded else f"{SERVICE_LABEL} not loaded")),
        "fix": "telechat bridge service install",
        # Cards are posted by the hook subprocess itself, so they still arrive
        # without the service. Only your *replies* need the running poller.
        "blocking": False,
    })

    armed = _approval_hook_registered()
    checks.append({
        "name": "Tool approval hook",
        "ok": True,
        "detail": "registered — arm per project with /desktop_approve_on" if armed
                  else "not registered (optional)",
        "fix": "telechat bridge install --approval",
        "blocking": False,
    })

    return checks


def _preflight() -> list[str]:
    """Blocking problems, as human-readable strings. Built from bridge_checks()."""
    return [
        f"{c['name']}: {c['detail']}\n      Fix: {c['fix']}"
        for c in bridge_checks()
        if c["blocking"] and not c["ok"]
    ]


def service_install() -> int:
    """Install telechat as a persistent background service (macOS launchd).
    Idempotent: regenerates the plist and reloads."""
    if sys.platform != "darwin":
        print("  ⚠ Persistent service auto-setup supports macOS (launchd) only.")
        print("    On Linux, run telechat under systemd --user or a process manager.")
        return 1
    py = sys.executable or shutil.which("python3") or "python3"
    claude_dir = str((HOME / ".local/bin"))
    path_env = f"{claude_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>-m</string>
        <string>telechat_pkg.main</string>
        <string>start</string>
    </array>
    <key>WorkingDirectory</key><string>{TELECHAT_HOME}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TELECHAT_HOME</key><string>{TELECHAT_HOME}</string>
        <key>HOME</key><string>{HOME}</string>
        <key>PATH</key><string>{path_env}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>{TELECHAT_HOME}/service.out</string>
    <key>StandardErrorPath</key><string>{TELECHAT_HOME}/service.err</string>
</dict>
</plist>
"""
    SERVICE_PLIST.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_PLIST.write_text(plist)
    # Reload (unload first in case it already exists), and stop any nohup/foreground instance.
    subprocess.run(["launchctl", "unload", str(SERVICE_PLIST)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", str(SERVICE_PLIST)], capture_output=True, text=True)
    if r.returncode != 0 and r.stderr.strip():
        print(f"  ⚠ launchctl load: {r.stderr.strip()}")
    print(f"  ✓ Persistent service installed: {SERVICE_PLIST.name}")
    print(f"    Runs at login, restarts on crash. Logs: {TELECHAT_HOME}/service.out")
    return 0


def service_uninstall() -> int:
    if sys.platform != "darwin":
        print("  ⚠ launchd is macOS-only.")
        return 1
    if SERVICE_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(SERVICE_PLIST)], capture_output=True)
        SERVICE_PLIST.unlink()
        print(f"  ✓ Removed service {SERVICE_PLIST.name}")
    else:
        print("  (no service installed)")
    return 0


def service_status() -> int:
    if sys.platform != "darwin":
        print("launchd is macOS-only.")
        return 1
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if SERVICE_LABEL in l), None)
    if line:
        parts = line.split()
        print(f"Service {SERVICE_LABEL}: running (pid={parts[0]}, last exit={parts[1]})")
    else:
        print(f"Service {SERVICE_LABEL}: not loaded")
    print(f"Plist: {SERVICE_PLIST} ({'present' if SERVICE_PLIST.exists() else 'missing'})")
    return 0


def cli_dispatch(argv: list[str]) -> int:
    """Entry point for `telechat bridge ...`. Called from main.cli_entry."""
    if not argv:
        print("Usage: telechat bridge install [--approval] [--no-service]\n"
              "       telechat bridge uninstall | status | service <install|uninstall|status>\n"
              "       telechat bridge notify <event> | approve   (hook entrypoints)")
        return 1
    sub = argv[0]
    rest = argv[1:]
    if sub == "install":
        return cli_install(
            approve_hook=("--approval" in rest or "--approve" in rest),
            with_service=("--no-service" not in rest),
        )
    if sub == "service":
        action = rest[0] if rest else "status"
        return {"install": service_install, "uninstall": service_uninstall,
                "status": service_status}.get(action, service_status)()
    if sub == "uninstall":
        return cli_uninstall()
    if sub == "notify":
        if not rest:
            print("Usage: telechat bridge notify <event>")
            return 1
        return cli_notify(rest[0])
    if sub == "approve":
        return cli_approve()
    if sub == "status":
        return cli_status()
    print(f"Unknown subcommand: {sub}")
    return 1
