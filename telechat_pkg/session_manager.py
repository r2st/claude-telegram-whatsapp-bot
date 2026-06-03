"""
Session Resume/Fork (Feature 4) — resume previous conversations or fork them.

Inspired by Claude Agent SDK's session persistence where sessions can be
resumed later or forked to explore different approaches.

Usage:
    from telechat_pkg.session_manager import SessionBrowser
    browser = SessionBrowser()
    sessions = browser.list_sessions("telegram", "123", limit=10)
    browser.resume_session("telegram", "123", session_name="coding-project")
    browser.fork_session("telegram", "123", "coding-project", "coding-project-alt")

Schema notes:
    Conversation rows live in the `conversations` table with columns
    (platform, user_id, role, content, ts). Named sessions are encoded by
    suffixing user_id as "<uid>:<session_name>". Session metadata
    (title, pinned, archived, counts) lives in `user_sessions`.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    name: str
    created_at: float
    last_active: float
    message_count: int
    total_cost: float
    claude_session_id: str | None
    is_active: bool
    preview: str = ""


@dataclass
class ForkResult:
    new_session_name: str
    messages_copied: int
    success: bool
    error: str = ""


def _effective_uid(user_id: str, session_name: str) -> str:
    """Match store.py's convention for encoding session_name into user_id."""
    return f"{user_id}:{session_name}" if session_name else user_id


class SessionBrowser:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # Use the same DB file as store.py so session browsing reflects
            # the real conversation history instead of an empty bot.db
            # inside the installed package directory.
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._ensure_schema(self._local.conn)
        return self._local.conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Make SessionBrowser robust against bare or legacy DBs.

        Production code (store.init_db) creates these tables, but SessionBrowser
        can be pointed at an arbitrary DB (tests, ad-hoc tools, legacy installs
        that only have the old flat ``history`` table). Ensuring the new schema
        exists here lets list/fork/search degrade gracefully to "no sessions"
        instead of raising sqlite3.OperationalError, and lets us auto-migrate
        legacy rows so old DBs surface their data through the new API.
        """
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform    TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    title       TEXT DEFAULT '',
                    pinned      INTEGER DEFAULT 0,
                    archived    INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL,
                    last_active REAL NOT NULL,
                    message_count INTEGER DEFAULT 0,
                    UNIQUE(platform, user_id, name)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    platform  TEXT NOT NULL,
                    user_id   TEXT NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    ts        REAL NOT NULL,
                    PRIMARY KEY (platform, user_id, ts)
                );
                CREATE TABLE IF NOT EXISTS active_sessions (
                    platform     TEXT NOT NULL,
                    user_id      TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );
            """)
            conn.commit()
        except sqlite3.OperationalError:
            log.exception("SessionBrowser._ensure_schema failed")
            return

        # Best-effort: migrate legacy `history` rows into the new shape.
        # Only fires when `history` exists AND `user_sessions` is empty for
        # any platform/user we find — so re-runs are idempotent and we never
        # clobber data that already lives in the new tables.
        try:
            has_history = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history'"
            ).fetchone()
            if not has_history:
                return
            agg = conn.execute("""
                SELECT platform, user_id, session_name,
                       MIN(timestamp) AS created_at,
                       MAX(timestamp) AS last_active,
                       COUNT(*)       AS msg_pairs
                FROM history
                WHERE session_name IS NOT NULL AND session_name <> ''
                GROUP BY platform, user_id, session_name
            """).fetchall()
            for r in agg:
                platform, user_id, sess = r["platform"], r["user_id"], r["session_name"]
                conn.execute(
                    """INSERT OR IGNORE INTO user_sessions
                       (platform, user_id, name, title, pinned, archived,
                        created_at, last_active, message_count)
                       VALUES (?, ?, ?, '', 0, 0, ?, ?, ?)""",
                    (platform, user_id, sess,
                     r["created_at"] or time.time(),
                     r["last_active"] or time.time(),
                     (r["msg_pairs"] or 0) * 2),
                )
                # Re-project each history row as a user/assistant pair under
                # the effective uid the new code expects.
                eff_uid = _effective_uid(user_id, sess)
                rows = conn.execute(
                    """SELECT user_text, bot_reply, timestamp FROM history
                       WHERE platform=? AND user_id=? AND session_name=?
                       ORDER BY timestamp ASC""",
                    (platform, user_id, sess),
                ).fetchall()
                for hr in rows:
                    ts = hr["timestamp"] or time.time()
                    if hr["user_text"]:
                        conn.execute(
                            """INSERT OR IGNORE INTO conversations
                               (platform, user_id, role, content, ts)
                               VALUES (?, ?, 'user', ?, ?)""",
                            (platform, eff_uid, hr["user_text"], ts),
                        )
                    if hr["bot_reply"]:
                        conn.execute(
                            """INSERT OR IGNORE INTO conversations
                               (platform, user_id, role, content, ts)
                               VALUES (?, ?, 'assistant', ?, ?)""",
                            (platform, eff_uid, hr["bot_reply"], ts + 0.0005),
                        )
            conn.commit()
        except sqlite3.OperationalError:
            log.exception("SessionBrowser legacy history migration failed")

    # ─── Listing ────────────────────────────────────────────────────────────

    def list_sessions(
        self,
        platform: str,
        user_id: str,
        *,
        limit: int = 10,
        include_preview: bool = True,
        include_archived: bool = False,
    ) -> list[SessionInfo]:
        """List all sessions for a user, ordered by last activity."""
        conn = self._conn()
        archived_clause = "" if include_archived else "AND archived = 0"
        rows = conn.execute(
            f"""SELECT name, created_at, last_active, message_count
                FROM user_sessions
                WHERE platform = ? AND user_id = ? {archived_clause}
                ORDER BY pinned DESC, last_active DESC
                LIMIT ?""",
            (platform, user_id, limit),
        ).fetchall()

        # Resolve active session name (best-effort; missing table is fine).
        active_name = ""
        try:
            row = conn.execute(
                "SELECT session_name FROM active_sessions WHERE platform=? AND user_id=?",
                (platform, user_id),
            ).fetchone()
            if row:
                active_name = row[0]
        except sqlite3.OperationalError:
            pass

        sessions: list[SessionInfo] = []
        for r in rows:
            session_name = r["name"]
            preview = ""
            total_cost = 0.0

            if include_preview:
                prev_row = conn.execute(
                    """SELECT content FROM conversations
                       WHERE platform = ? AND user_id = ? AND role = 'user'
                       ORDER BY ts DESC LIMIT 1""",
                    (platform, _effective_uid(user_id, session_name)),
                ).fetchone()
                if prev_row:
                    preview = (prev_row["content"] or "")[:100]

            # Best-effort cost lookup from sessions table.
            try:
                cost_row = conn.execute(
                    """SELECT COALESCE(SUM(total_cost_usd), 0) AS cost
                       FROM sessions WHERE platform = ? AND user_id = ?""",
                    (platform, _effective_uid(user_id, session_name)),
                ).fetchone()
                if cost_row:
                    total_cost = float(cost_row["cost"] or 0.0)
            except sqlite3.OperationalError:
                pass

            sessions.append(SessionInfo(
                name=session_name,
                created_at=r["created_at"],
                last_active=r["last_active"],
                message_count=r["message_count"] or 0,
                total_cost=total_cost,
                claude_session_id=None,
                is_active=(session_name == active_name),
                preview=preview,
            ))
        return sessions

    # ─── History ────────────────────────────────────────────────────────────

    def get_session_history(
        self,
        platform: str,
        user_id: str,
        session_name: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Get conversation history for a specific session, oldest first."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT role, content, ts FROM conversations
               WHERE platform = ? AND user_id = ?
               ORDER BY ts ASC
               LIMIT ?""",
            (platform, _effective_uid(user_id, session_name), limit),
        ).fetchall()
        return [
            {"role": r["role"], "content": r["content"], "ts": r["ts"]}
            for r in rows
        ]

    # ─── Forking ────────────────────────────────────────────────────────────

    def fork_session(
        self,
        platform: str,
        user_id: str,
        source_session: str,
        new_session_name: str | None = None,
        *,
        max_messages: int = 50,
    ) -> ForkResult:
        """Fork (copy) a session's history into a new session.

        Creates a new `user_sessions` row and copies the most recent
        ``max_messages`` `conversations` rows under the new effective uid.
        """
        if not new_session_name:
            new_session_name = f"{source_session}-fork-{int(time.time()) % 10000}"

        if new_session_name == source_session:
            return ForkResult(new_session_name, 0, False, "Fork target name must differ from source")

        conn = self._conn()
        src_uid = _effective_uid(user_id, source_session)
        dst_uid = _effective_uid(user_id, new_session_name)

        rows = conn.execute(
            """SELECT role, content, ts FROM conversations
               WHERE platform = ? AND user_id = ?
               ORDER BY ts ASC
               LIMIT ?""",
            (platform, src_uid, max_messages),
        ).fetchall()

        if not rows:
            return ForkResult(new_session_name, 0, False, f"Session '{source_session}' not found or empty")

        # Refuse to clobber an existing destination session.
        existing = conn.execute(
            "SELECT 1 FROM user_sessions WHERE platform=? AND user_id=? AND name=?",
            (platform, user_id, new_session_name),
        ).fetchone()
        if existing:
            return ForkResult(new_session_name, 0, False, f"Session '{new_session_name}' already exists")

        now = time.time()
        try:
            with conn:
                # Register the new session in user_sessions.
                conn.execute(
                    """INSERT INTO user_sessions
                       (platform, user_id, name, title, pinned, archived,
                        created_at, last_active, message_count)
                       VALUES (?, ?, ?, '', 0, 0, ?, ?, ?)""",
                    (platform, user_id, new_session_name, now, now, len(rows)),
                )
                # Copy the conversation rows under the new effective uid.
                # Preserve relative ordering by spacing timestamps off of `now`.
                copied = 0
                for r in rows:
                    conn.execute(
                        """INSERT OR IGNORE INTO conversations
                           (platform, user_id, role, content, ts)
                           VALUES (?, ?, ?, ?, ?)""",
                        (platform, dst_uid, r["role"], r["content"], now + copied * 0.001),
                    )
                    copied += 1
        except sqlite3.Error as e:
            log.exception("fork_session failed")
            return ForkResult(new_session_name, 0, False, f"DB error: {e}")

        return ForkResult(new_session_name, copied, True)

    # ─── Search ─────────────────────────────────────────────────────────────

    def search_sessions(
        self,
        platform: str,
        user_id: str,
        query: str,
        *,
        limit: int = 5,
    ) -> list[SessionInfo]:
        """Search across the user's sessions by conversation content.

        Returns SessionInfo for each session whose history contains ``query``
        in either a user or assistant message.
        """
        if not query:
            return []
        conn = self._conn()
        # Match either the bare user_id (default session) or any "<uid>:<name>" suffix.
        prefix = f"{user_id}:"
        rows = conn.execute(
            """SELECT DISTINCT user_id FROM conversations
               WHERE platform = ?
                 AND (user_id = ? OR user_id LIKE ? || '%')
                 AND content LIKE '%' || ? || '%'
               LIMIT ?""",
            (platform, user_id, prefix, query, limit * 4),
        ).fetchall()

        matching_names: set[str] = set()
        for r in rows:
            uid = r["user_id"]
            if uid == user_id:
                matching_names.add("default")
            elif uid.startswith(prefix):
                matching_names.add(uid[len(prefix):])

        if not matching_names:
            return []

        # Reuse list_sessions so callers get consistent SessionInfo shapes.
        all_sessions = self.list_sessions(
            platform, user_id, limit=max(limit * 4, 20), include_archived=True
        )
        filtered = [s for s in all_sessions if s.name in matching_names]
        return filtered[:limit]

    # ─── Resume (compat shim) ───────────────────────────────────────────────

    def resume_session(
        self,
        platform: str,
        user_id: str,
        *,
        session_name: str,
    ) -> bool:
        """Mark a session as active for the user.

        Returns True on success, False if the session doesn't exist.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM user_sessions WHERE platform=? AND user_id=? AND name=?",
            (platform, user_id, session_name),
        ).fetchone()
        if not row:
            return False
        try:
            conn.execute(
                """INSERT INTO active_sessions (platform, user_id, session_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(platform, user_id) DO UPDATE
                   SET session_name = excluded.session_name""",
                (platform, user_id, session_name),
            )
            conn.execute(
                """UPDATE user_sessions SET last_active = ?
                   WHERE platform=? AND user_id=? AND name=?""",
                (time.time(), platform, user_id, session_name),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError:
            log.exception("resume_session failed (active_sessions table missing?)")
            return False
