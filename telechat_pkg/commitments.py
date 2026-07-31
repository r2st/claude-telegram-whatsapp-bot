"""
Commitments / reminders — detect follow-up promises and schedule proactive reminders.

When Claude's response or user's message contains a promise or follow-up commitment
(e.g. "remind me tomorrow", "I'll check on that"), the system extracts it and
schedules a proactive reminder.

"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import store as _store

log = logging.getLogger(__name__)


@dataclass
class CommitmentRecord:
    id: str
    platform: str
    user_id: str
    kind: str  # reminder, follow_up, deadline
    status: str  # pending, sent, dismissed, snoozed
    reason: str
    due_at: float  # Unix timestamp
    created_at: float
    source_text: str = ""
    snoozed_until: float = 0.0


# ─── Database ───────────────────────────────────────────────────────────────

def init_db():
    """Create the commitments table if it doesn't exist."""
    import sqlite3
    conn = _store._get_conn()
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commitments (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'reminder',
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT NOT NULL,
            due_at REAL NOT NULL,
            created_at REAL NOT NULL,
            source_text TEXT DEFAULT '',
            snoozed_until REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_commitments_user
        ON commitments (platform, user_id, status)
    """)
    conn.commit()


def _parse_row(row) -> CommitmentRecord:
    keys = row.keys() if hasattr(row, "keys") else []
    return CommitmentRecord(
        id=row["id"],
        platform=row["platform"],
        user_id=row["user_id"],
        kind=row["kind"],
        status=row["status"],
        reason=row["reason"],
        due_at=row["due_at"],
        created_at=row["created_at"],
        source_text=row["source_text"] if "source_text" in keys else "",
        snoozed_until=row["snoozed_until"] if "snoozed_until" in keys else 0,
    )


# ─── Time parsing ──────────────────────────────────────────────────────────

_RELATIVE_TIME_PATTERNS = [
    (r"\b(?:in\s+)?(\d+)\s*min(?:ute)?s?\b", lambda m: timedelta(minutes=int(m.group(1)))),
    (r"\b(?:in\s+)?(\d+)\s*hours?\b", lambda m: timedelta(hours=int(m.group(1)))),
    (r"\b(?:in\s+)?(\d+)\s*days?\b", lambda m: timedelta(days=int(m.group(1)))),
    (r"\b(?:in\s+)?(\d+)\s*weeks?\b", lambda m: timedelta(weeks=int(m.group(1)))),
    (r"\btomorrow\b", lambda m: timedelta(days=1)),
    (r"\bnext\s+week\b", lambda m: timedelta(weeks=1)),
    (r"\btonight\b", lambda m: timedelta(hours=max(0, 20 - datetime.now().hour))),
    (r"\bthis\s+evening\b", lambda m: timedelta(hours=max(0, 18 - datetime.now().hour))),
    (r"\bthis\s+afternoon\b", lambda m: timedelta(hours=max(0, 14 - datetime.now().hour))),
    (r"\bend\s+of\s+(?:the\s+)?day\b", lambda m: timedelta(hours=max(0, 17 - datetime.now().hour))),
    (r"\bnext\s+month\b", lambda m: timedelta(days=30)),
    (r"\bmonday\b", lambda m: _days_until_weekday(0)),
    (r"\btuesday\b", lambda m: _days_until_weekday(1)),
    (r"\bwednesday\b", lambda m: _days_until_weekday(2)),
    (r"\bthursday\b", lambda m: _days_until_weekday(3)),
    (r"\bfriday\b", lambda m: _days_until_weekday(4)),
    (r"\bsaturday\b", lambda m: _days_until_weekday(5)),
    (r"\bsunday\b", lambda m: _days_until_weekday(6)),
]


def _days_until_weekday(target: int) -> timedelta:
    today = datetime.now().weekday()
    days_ahead = target - today
    if days_ahead <= 0:
        days_ahead += 7
    return timedelta(days=days_ahead)


def parse_due_time(text: str) -> Optional[float]:
    """Parse a relative time expression and return a Unix timestamp."""
    lower = text.lower()
    for pattern, delta_fn in _RELATIVE_TIME_PATTERNS:
        match = re.search(pattern, lower, re.IGNORECASE)
        if match:
            delta = delta_fn(match)
            due = datetime.now() + delta
            return due.timestamp()
    return None


# ─── Extraction ─────────────────────────────────────────────────────────────

_COMMITMENT_PATTERNS = [
    # User requests
    (r"remind\s+me\s+(?:to\s+)?(.+?)(?:\.|$)", "reminder"),
    (r"don'?t\s+(?:let\s+me\s+)?forget\s+(?:to\s+)?(.+?)(?:\.|$)", "reminder"),
    (r"(?:I\s+)?need\s+to\s+remember\s+(?:to\s+)?(.+?)(?:\.|$)", "reminder"),
    # Follow-ups from assistant
    (r"I'?ll\s+(?:check|follow|look)\s+(?:on|up|into)\s+(.+?)(?:\.|$)", "follow_up"),
    (r"(?:let\s+me|I\s+will|I'?ll)\s+get\s+back\s+to\s+you\s+(?:on|about)\s+(.+?)(?:\.|$)", "follow_up"),
    # Deadlines
    (r"deadline\s+(?:is\s+)?(?:on\s+)?(.+?)(?:\.|$)", "deadline"),
    (r"due\s+(?:by|on|at)\s+(.+?)(?:\.|$)", "deadline"),
]


def extract_commitments(user_text: str, assistant_text: str = "") -> list[dict]:
    """Extract commitment candidates from conversation text."""
    results = []
    combined = f"{user_text}\n{assistant_text}"

    for pattern, kind in _COMMITMENT_PATTERNS:
        for match in re.finditer(pattern, combined, re.IGNORECASE):
            reason = match.group(1).strip()
            if len(reason) < 3 or len(reason) > 500:
                continue

            # Try to find a time reference in the surrounding text
            context = combined[max(0, match.start() - 100):match.end() + 100]
            due_at = parse_due_time(context)

            if not due_at:
                # Default: remind in 24 hours if no time specified
                due_at = (datetime.now() + timedelta(hours=24)).timestamp()

            results.append({
                "kind": kind,
                "reason": reason,
                "due_at": due_at,
                "source_text": match.group(0).strip(),
            })

    # Deduplicate by reason similarity
    seen_reasons: set[str] = set()
    unique = []
    for r in results:
        key = r["reason"].lower()[:50]
        if key not in seen_reasons:
            seen_reasons.add(key)
            unique.append(r)

    return unique


# ─── CRUD ───────────────────────────────────────────────────────────────────

def add_commitment(
    platform: str,
    user_id: str,
    kind: str,
    reason: str,
    due_at: float,
    source_text: str = "",
) -> CommitmentRecord:
    """Store a new commitment."""
    record = CommitmentRecord(
        id=str(uuid.uuid4())[:8],
        platform=platform,
        user_id=user_id,
        kind=kind,
        status="pending",
        reason=reason,
        due_at=due_at,
        created_at=time.time(),
        source_text=source_text,
    )
    _store._enqueue_write(
        """INSERT INTO commitments (id, platform, user_id, kind, status, reason, due_at, created_at, source_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (record.id, platform, user_id, kind, "pending", reason, due_at, record.created_at, source_text),
    )
    log.info("Added commitment %s for %s:%s — due at %s", record.id, platform, user_id,
             datetime.fromtimestamp(due_at).isoformat())
    return record


def get_pending(platform: str, user_id: str) -> list[CommitmentRecord]:
    """Get all pending commitments for a user."""
    conn = _store._get_conn()
    rows = conn.execute(
        """SELECT * FROM commitments
           WHERE platform = ? AND user_id = ? AND status = 'pending'
           ORDER BY due_at ASC""",
        (platform, user_id),
    ).fetchall()
    return [_parse_row(r) for r in rows]


def get_due(platform: str, user_id: str) -> list[CommitmentRecord]:
    """Get commitments that are past due."""
    now = time.time()
    conn = _store._get_conn()
    rows = conn.execute(
        """SELECT * FROM commitments
           WHERE platform = ? AND user_id = ? AND status = 'pending'
           AND due_at <= ? AND (snoozed_until = 0 OR snoozed_until <= ?)
           ORDER BY due_at ASC""",
        (platform, user_id, now, now),
    ).fetchall()
    return [_parse_row(r) for r in rows]


def dismiss(commitment_id: str):
    """Mark a commitment as dismissed."""
    _store._enqueue_write(
        "UPDATE commitments SET status = 'dismissed' WHERE id = ?",
        (commitment_id,),
    )


def snooze(commitment_id: str, until: float):
    """Snooze a commitment until a new time."""
    _store._enqueue_write(
        "UPDATE commitments SET snoozed_until = ? WHERE id = ?",
        (until, commitment_id),
    )


def mark_sent(commitment_id: str):
    """Mark a commitment as sent/delivered."""
    _store._enqueue_write(
        "UPDATE commitments SET status = 'sent' WHERE id = ?",
        (commitment_id,),
    )


def auto_extract_and_store(
    platform: str,
    user_id: str,
    user_text: str,
    assistant_text: str = "",
) -> list[CommitmentRecord]:
    """Convenience: extract commitments from conversation and store them."""
    candidates = extract_commitments(user_text, assistant_text)
    records = []
    for c in candidates:
        record = add_commitment(
            platform=platform,
            user_id=user_id,
            kind=c["kind"],
            reason=c["reason"],
            due_at=c["due_at"],
            source_text=c.get("source_text", ""),
        )
        records.append(record)
    return records


def format_pending(commitments: list[CommitmentRecord]) -> str:
    """Format pending commitments for display."""
    if not commitments:
        return "📋 No pending reminders."
    lines = ["📋 **Pending reminders:**\n"]
    for c in commitments:
        due = datetime.fromtimestamp(c.due_at)
        now = datetime.now()
        if due < now:
            time_str = "⏰ **overdue**"
        else:
            delta = due - now
            if delta.days > 0:
                time_str = f"in {delta.days}d"
            elif delta.seconds > 3600:
                time_str = f"in {delta.seconds // 3600}h"
            else:
                time_str = f"in {delta.seconds // 60}m"

        kind_icon = {"reminder": "🔔", "follow_up": "🔄", "deadline": "⏳"}.get(c.kind, "📌")
        lines.append(f"{kind_icon} `{c.id}` {c.reason} — {time_str}")
    return "\n".join(lines)
