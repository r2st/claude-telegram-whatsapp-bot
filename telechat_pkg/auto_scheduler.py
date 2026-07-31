"""
Scheduled Autonomous Tasks (Feature 7) — agent-driven natural language scheduling.

Inspired by Claude Managed Agents which can run autonomously on schedules, and
the Dreaming feature where agents work between sessions.

Extends the existing scheduled_tasks.py with:
- Natural language schedule parsing ("remind me every morning at 9am")
- Agent-driven execution (Claude processes the scheduled task)
- Result delivery back to the user via their chat platform

Usage:
    from telechat_pkg.auto_scheduler import AutoScheduler
    sched = AutoScheduler()
    task = sched.parse_and_create("telegram", "123", "remind me to check deploys every 2 hours")
    sched.start()
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

AUTO_SCHEDULER_ENABLED = os.getenv("AUTO_SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")


@dataclass
class AutoTask:
    id: int
    platform: str
    user_id: str
    description: str
    prompt: str  # what to ask Claude when this fires
    interval_seconds: int
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0
    max_runs: int = 0  # 0 = unlimited
    created_at: float = 0.0

    @property
    def is_due(self) -> bool:
        return self.enabled and time.time() >= self.next_run

    @property
    def is_exhausted(self) -> bool:
        return self.max_runs > 0 and self.run_count >= self.max_runs


# ─── Natural language interval parser ─────────────────────────────────────────

_INTERVAL_PATTERNS = [
    (r"every\s+(\d+)\s*(?:sec(?:ond)?s?)", lambda m: int(m.group(1))),
    (r"every\s+(\d+)\s*(?:min(?:ute)?s?)", lambda m: int(m.group(1)) * 60),
    (r"every\s+(\d+)\s*(?:hour?s?|hr?s?)", lambda m: int(m.group(1)) * 3600),
    (r"every\s+(\d+)\s*(?:day?s?)", lambda m: int(m.group(1)) * 86400),
    (r"every\s+(?:half\s+)?hour", lambda m: 1800),
    (r"hourly", lambda m: 3600),
    (r"daily|every\s+day", lambda m: 86400),
    (r"weekly|every\s+week", lambda m: 604800),
    (r"every\s+morning", lambda m: 86400),
    (r"every\s+evening", lambda m: 86400),
    (r"twice\s+(?:a\s+)?day", lambda m: 43200),
    (r"(?:once|one\s+time|in)\s+(\d+)\s*(?:min(?:ute)?s?)", lambda m: int(m.group(1)) * 60),
    (r"(?:once|one\s+time|in)\s+(\d+)\s*(?:hour?s?|hr?s?)", lambda m: int(m.group(1)) * 3600),
]

_compiled_intervals = [(re.compile(p, re.IGNORECASE), fn) for p, fn in _INTERVAL_PATTERNS]


def parse_interval(text: str) -> int | None:
    """Extract interval in seconds from natural language. Returns None if not found."""
    for pattern, extractor in _compiled_intervals:
        match = pattern.search(text)
        if match:
            return extractor(match)
    return None


def parse_schedule_request(text: str) -> dict | None:
    """Parse a scheduling request from natural language.

    Returns {"description": ..., "prompt": ..., "interval": ..., "max_runs": ...} or None.
    """
    interval = parse_interval(text)
    if interval is None:
        return None

    # Clean the description — remove the scheduling parts
    desc = text
    for pattern, _ in _compiled_intervals:
        desc = re.sub(pattern.pattern, "", desc, flags=re.IGNORECASE)

    # Remove common scheduling prefixes
    desc = re.sub(r"^(?:remind\s+me\s+to|schedule|set\s+(?:a\s+)?(?:reminder|task)\s+(?:to|for))\s*",
                  "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"\s+", " ", desc).strip()

    if not desc:
        desc = "scheduled task"

    # Determine if one-shot or recurring
    one_shot = bool(re.search(r"\b(?:once|one\s+time|in\s+\d+)\b", text, re.IGNORECASE))

    return {
        "description": desc,
        "prompt": desc,  # what to ask Claude
        "interval": interval,
        "max_runs": 1 if one_shot else 0,
    }


class AutoScheduler:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # Share the canonical DB location with store.py instead of
            # writing scheduler state into the installed package dir.
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_fire: Optional[Callable] = None  # callback(AutoTask) -> str result
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self):
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS auto_scheduled_tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                platform        TEXT NOT NULL,
                user_id         TEXT NOT NULL,
                description     TEXT NOT NULL,
                prompt          TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                enabled         INTEGER DEFAULT 1,
                last_run        REAL DEFAULT 0,
                next_run        REAL DEFAULT 0,
                run_count       INTEGER DEFAULT 0,
                max_runs        INTEGER DEFAULT 0,
                created_at      REAL NOT NULL
            );
        """)
        self._conn().commit()

    def create_task(
        self,
        platform: str,
        user_id: str,
        description: str,
        prompt: str,
        interval_seconds: int,
        max_runs: int = 0,
    ) -> AutoTask:
        now = time.time()
        conn = self._conn()
        cursor = conn.execute(
            """INSERT INTO auto_scheduled_tasks
               (platform, user_id, description, prompt, interval_seconds, next_run, max_runs, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (platform, user_id, description, prompt, interval_seconds, now + interval_seconds, max_runs, now),
        )
        conn.commit()
        return AutoTask(
            id=cursor.lastrowid,
            platform=platform,
            user_id=user_id,
            description=description,
            prompt=prompt,
            interval_seconds=interval_seconds,
            next_run=now + interval_seconds,
            max_runs=max_runs,
            created_at=now,
        )

    def parse_and_create(self, platform: str, user_id: str, text: str) -> AutoTask | None:
        """Parse natural language and create a scheduled task."""
        parsed = parse_schedule_request(text)
        if not parsed:
            return None
        return self.create_task(
            platform, user_id,
            parsed["description"],
            parsed["prompt"],
            parsed["interval"],
            parsed["max_runs"],
        )

    def list_tasks(self, platform: str, user_id: str) -> list[AutoTask]:
        rows = self._conn().execute(
            """SELECT * FROM auto_scheduled_tasks
               WHERE platform = ? AND user_id = ? AND enabled = 1
               ORDER BY next_run ASC""",
            (platform, user_id),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def delete_task(self, task_id: int, platform: str, user_id: str) -> bool:
        result = self._conn().execute(
            "DELETE FROM auto_scheduled_tasks WHERE id = ? AND platform = ? AND user_id = ?",
            (task_id, platform, user_id),
        )
        self._conn().commit()
        return result.rowcount > 0

    def get_due_tasks(self) -> list[AutoTask]:
        rows = self._conn().execute(
            """SELECT * FROM auto_scheduled_tasks
               WHERE enabled = 1 AND next_run <= ?
               ORDER BY next_run ASC""",
            (time.time(),),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def mark_run(self, task_id: int):
        now = time.time()
        conn = self._conn()
        row = conn.execute("SELECT * FROM auto_scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return
        new_count = row["run_count"] + 1
        new_next = now + row["interval_seconds"]
        enabled = 1
        if row["max_runs"] > 0 and new_count >= row["max_runs"]:
            enabled = 0
        conn.execute(
            """UPDATE auto_scheduled_tasks
               SET last_run = ?, next_run = ?, run_count = ?, enabled = ?
               WHERE id = ?""",
            (now, new_next, new_count, enabled, task_id),
        )
        conn.commit()

    def set_callback(self, callback: Callable):
        """Set the callback that fires when a task is due: callback(task) -> result_str"""
        self._on_fire = callback

    async def start(self):
        if not AUTO_SCHEDULER_ENABLED:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        log.info("Auto scheduler started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _tick_loop(self):
        while self._running:
            try:
                due = self.get_due_tasks()
                for task in due:
                    if self._on_fire:
                        try:
                            await self._on_fire(task)
                        except Exception as e:
                            log.error("Scheduled task %d failed: %s", task.id, e)
                    self.mark_run(task.id)
                await asyncio.sleep(30)  # check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Scheduler tick error: %s", e)
                await asyncio.sleep(60)

    def _row_to_task(self, row) -> AutoTask:
        return AutoTask(
            id=row["id"],
            platform=row["platform"],
            user_id=row["user_id"],
            description=row["description"],
            prompt=row["prompt"],
            interval_seconds=row["interval_seconds"],
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
            next_run=row["next_run"],
            run_count=row["run_count"],
            max_runs=row["max_runs"],
            created_at=row["created_at"],
        )

    def format_task_list(self, tasks: list[AutoTask]) -> str:
        if not tasks:
            return "No scheduled tasks."
        lines = ["**Scheduled Tasks:**\n"]
        for t in tasks:
            interval_str = _format_interval(t.interval_seconds)
            status = "🟢" if t.enabled else "🔴"
            runs_str = f" ({t.run_count}/{t.max_runs})" if t.max_runs else f" ({t.run_count} runs)"
            lines.append(f"{status} `#{t.id}` {t.description} — every {interval_str}{runs_str}")
        return "\n".join(lines)


def _format_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
