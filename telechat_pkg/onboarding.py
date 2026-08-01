"""
First-run onboarding.

``/start`` used to answer a brand-new user with forty lines listing every
command the bot has. That is a reference manual, not a welcome: it is the
one moment where someone decides whether this thing is worth keeping, and
the response was a wall of syntax for features they have no reason to want
yet.

This module replaces it with a short welcome and a five-step tour the user
steps through at their own pace — each step one capability, one example
they can copy, nothing else. First-run state is recorded per platform and
user so a returning user gets a one-liner instead, and ``/tour`` replays it
for anyone who wants it back.

Usage:
    from telechat_pkg import onboarding as ob

    first = ob.get_store().start("telegram", "12345")
    text = ob.welcome_text("Ada", first_time=first, invited_by="Grace")
    step = ob.tour_step(0)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TourStep:
    key: str
    title: str
    body: str
    example: str = ""       # something the user can copy and send verbatim

    def render(self, index: int, total: int) -> str:
        head = f"*{self.title}*  ({index + 1}/{total})"
        parts = [head, "", self.body]
        if self.example:
            parts += ["", "Try it:", f"`{self.example}`"]
        return "\n".join(parts)


#: The tour. Ordered by what makes someone keep the bot, not by what was
#: built first: a real answer, then the two things that make it *theirs*
#: (memory and files), then sessions, then how to hand it to someone else.
TOUR: tuple[TourStep, ...] = (
    TourStep(
        key="ask",
        title="Just talk to it",
        body=(
            "Send anything — a question, a paste of code, a half-formed idea. "
            "It runs Claude and answers here. Several messages at once run as "
            "parallel tasks, so you never wait on the slow one."
        ),
        example="Explain what a WAL is in SQLite, briefly",
    ),
    TourStep(
        key="memory",
        title="It remembers",
        body=(
            "Tell it something once and it holds on to it across sessions and "
            "across platforms. `/remember` saves a fact, `/recall` searches "
            "them, `/memories` lists them."
        ),
        example="/remember I deploy with Docker Compose, not Kubernetes",
    ),
    TourStep(
        key="files",
        title="Send it things",
        body=(
            "Photos, PDFs, documents and voice notes all work. Send a voice "
            "message and it transcribes it first; send a screenshot of an "
            "error and ask what broke."
        ),
        example="",
    ),
    TourStep(
        key="sessions",
        title="Keep work separate",
        body=(
            "`/new work` starts a fresh thread with its own history. `/sessions` "
            "switches between them, `/reset` clears the current one. Your "
            "weekend project does not leak into your day job."
        ),
        example="/new sideproject",
    ),
    TourStep(
        key="share",
        title="Bring someone in",
        body=(
            "`/invite` mints a one-tap link that grants a friend access to this "
            "bot — no user IDs to copy, no restart, and you can revoke it "
            "later. `/invites` shows who came in through yours."
        ),
        example="/invite",
    ),
)

TOTAL_STEPS = len(TOUR)


def tour_step(index: int) -> Optional[TourStep]:
    """The step at ``index``, or None when the tour is over."""
    if index < 0 or index >= TOTAL_STEPS:
        return None
    return TOUR[index]


def step_text(index: int) -> str:
    """Rendered text for a step, or the closing line past the end."""
    step = tour_step(index)
    if step is None:
        return finish_text()
    return step.render(index, TOTAL_STEPS)


def progress_bar(index: int) -> str:
    """A ●●○○○ progress indicator for the tour."""
    done = max(0, min(index + 1, TOTAL_STEPS))
    return "●" * done + "○" * (TOTAL_STEPS - done)


def finish_text() -> str:
    return (
        "*That's the tour.*\n\n"
        "`/help` has the full command list when you want it. "
        "`/settings` changes the model, engine and verbosity.\n\n"
        "Ask it something real — that's the fastest way to find out if it's useful."
    )


#: Characters that open a legacy-Markdown entity. A name is decoration, so a
#: stray one is dropped rather than escaped: Telegram's legacy parser does not
#: reliably honour backslash escapes, and an unbalanced ``_`` — ordinary in a
#: handle like ``max_power`` — makes the API reject the whole message. Losing a
#: character from a name beats a new user's first ``/start`` returning nothing.
_MD_META = str.maketrans({c: None for c in "_*`["})


def plain_name(name: Optional[str]) -> str:
    """A display name safe to interpolate into a legacy-Markdown message."""
    return str(name or "").translate(_MD_META).strip()


def welcome_text(
    name: str = "",
    *,
    first_time: bool = True,
    invited_by: str = "",
) -> str:
    """The greeting shown for ``/start``.

    First-timers get an orientation and an offer of the tour; returning users
    get a line, because they came back for the bot and not for the greeting.

    Both names are user-controlled and land in a message the caller sends with
    Markdown enabled, so they are stripped of markup characters first.
    """
    name = plain_name(name)
    invited_by = plain_name(invited_by)
    who = f" {name}" if name else ""
    if not first_time:
        return (
            f"Welcome back{who}. Send me anything — or `/help` for commands, "
            "`/tour` to see the walkthrough again."
        )

    lines = []
    if invited_by:
        lines.append(f"👋 Hi{who} — *{invited_by}* invited you in.")
    else:
        lines.append(f"👋 Hi{who}.")
    lines += [
        "",
        "I'm Claude, running in your chat. Send a message and I'll work on it: "
        "questions, code, documents, photos, voice notes. I remember what you "
        "tell me between sessions.",
        "",
        "Want the 60-second tour?",
    ]
    return "\n".join(lines)


def invite_pitch(link: str, *, uses_left: Optional[int] = None, expires_hours: Optional[int] = None) -> str:
    """The message an inviter forwards. Written to be forwarded as-is.

    Deliberately plain text with no markup, and it must be *sent* that way —
    see the caller in ``telegram_bot.cmd_invite``. A Telegram bot username
    nearly always contains underscores (they must end in ``bot``, so
    ``..._bot`` is the norm), and the code is preceded by ``inv_``. Run that
    URL through Telegram's legacy Markdown parser and the underscores pair up
    into italics — mangling the link — or, with an odd number of them, fail
    the parse outright and the operator gets no message at all. Telegram
    auto-links a bare URL in a plain-text message anyway, so the markup buys
    nothing and costs the one message the whole invite flow depends on.
    """
    lines = [
        "Here's your invite link — send it to whoever should have access:",
        "",
        link,
        "",
    ]
    detail = []
    if uses_left is None:
        detail.append("unlimited uses")
    else:
        detail.append(f"{uses_left} use" + ("" if uses_left == 1 else "s"))
    if expires_hours is not None:
        detail.append(
            f"expires in {expires_hours}h" if expires_hours < 48
            else f"expires in {expires_hours // 24}d"
        )
    lines.append("One tap grants access · " + " · ".join(detail) + ".")
    lines.append("/invites lists yours · /revoke <code> kills one.")
    return "\n".join(lines)


# ─── First-run state ──────────────────────────────────────────────────────────


@dataclass
class OnboardingState:
    platform: str
    user_id: str
    first_seen: float
    step: int = 0
    completed_at: Optional[float] = None
    source: str = ""       # e.g. 'invite', 'direct'

    @property
    def completed(self) -> bool:
        return self.completed_at is not None


class OnboardingStore:
    """Remembers who has already been welcomed, and how far they got."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.RLock()
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
            CREATE TABLE IF NOT EXISTS onboarding (
                platform     TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                first_seen   REAL NOT NULL,
                step         INTEGER NOT NULL DEFAULT 0,
                completed_at REAL,
                source       TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (platform, user_id)
            );
        """)

    def get(self, platform: str, user_id: str) -> Optional[OnboardingState]:
        row = self._conn().execute(
            "SELECT * FROM onboarding WHERE platform = ? AND user_id = ?",
            (platform, str(user_id)),
        ).fetchone()
        if row is None:
            return None
        return OnboardingState(
            platform=row["platform"], user_id=row["user_id"], first_seen=row["first_seen"],
            step=row["step"], completed_at=row["completed_at"], source=row["source"] or "",
        )

    def is_new(self, platform: str, user_id: str) -> bool:
        return self.get(platform, user_id) is None

    def start(self, platform: str, user_id: str, *, source: str = "") -> bool:
        """Record a first sighting. Returns True only the first time.

        The INSERT is conditional inside a lock, so a user who double-taps
        ``/start`` is welcomed once rather than twice.
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO onboarding (platform, user_id, first_seen, step, source) "
                "VALUES (?,?,?,0,?)",
                (platform, str(user_id), time.time(), source[:40]),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_step(self, platform: str, user_id: str, step: int) -> int:
        """Move the user to ``step``, clamped to the tour, marking completion at the end."""
        step = max(0, min(int(step), TOTAL_STEPS))
        with self._lock:
            conn = self._conn()
            self.start(platform, user_id)
            completed = time.time() if step >= TOTAL_STEPS else None
            conn.execute(
                "UPDATE onboarding SET step = ?, completed_at = COALESCE(?, completed_at) "
                "WHERE platform = ? AND user_id = ?",
                (step, completed, platform, str(user_id)),
            )
            conn.commit()
        return step

    def advance(self, platform: str, user_id: str) -> Optional[TourStep]:
        """Move one step forward and return the step now shown, or None at the end."""
        state = self.get(platform, user_id)
        nxt = (state.step if state else 0) + 1
        self.set_step(platform, user_id, nxt)
        return tour_step(nxt)

    def complete(self, platform: str, user_id: str) -> None:
        self.set_step(platform, user_id, TOTAL_STEPS)

    def reset(self, platform: str, user_id: str) -> None:
        """Forget this user entirely — the next ``/start`` welcomes them again."""
        with self._lock:
            conn = self._conn()
            conn.execute(
                "DELETE FROM onboarding WHERE platform = ? AND user_id = ?",
                (platform, str(user_id)),
            )
            conn.commit()

    def counts(self, platform: str) -> dict:
        """Activation funnel for the operator: started, mid-tour, completed."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS started, "
            "SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed "
            "FROM onboarding WHERE platform = ?",
            (platform,),
        ).fetchone()
        started = int(row["started"] or 0)
        completed = int(row["completed"] or 0)
        return {"started": started, "completed": completed, "in_progress": started - completed}

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


_store: Optional[OnboardingStore] = None
_store_lock = threading.Lock()


def get_store() -> OnboardingStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = OnboardingStore()
    return _store


def reset_store() -> None:
    """Drop the cached store — for tests that repoint DB_PATH."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


def is_first_run(platform: str, user_id: str) -> bool:
    """Never raises: a database problem must not stop ``/start`` from replying."""
    try:
        return get_store().is_new(platform, str(user_id))
    except Exception:
        log.debug("onboarding lookup failed", exc_info=True)
        return False
