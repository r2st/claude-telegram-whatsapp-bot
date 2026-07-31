"""
Database layer + session management for telechat.

Thread-safe SQLite with WAL mode, async write queue, history caching,
rate limiting, conversation storage, usage/cost tracking, and multi-session
management (UserSession / SessionManager).
"""
from __future__ import annotations

import logging
import queue as _queue_mod
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
import os

log = logging.getLogger(__name__)

def _default_db_path() -> str:
    """Resolve the canonical default DB location.

    Priority: $DB_PATH → $TELECHAT_HOME/bot.db → ~/.telechat/bot.db.

    Never the installed package directory — that would write user data into
    site-packages on pip installs (often unwritable, lost on upgrade).
    """
    explicit = os.getenv("DB_PATH")
    if explicit:
        return explicit
    home = os.getenv("TELECHAT_HOME") or str(Path.home() / ".telechat")
    try:
        Path(home).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(Path(home) / "bot.db")


DB_PATH = _default_db_path()

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# ─── Connection pool (thread-local SQLite) ──────────────────────────────────────

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection (reused across calls)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        # Wait for the lock instead of erroring when several threads open a
        # brand-new database at once: switching journal_mode to WAL needs a
        # brief exclusive lock, and without a busy timeout the losers raise
        # "database is locked". Set this *before* the journal_mode pragma so it
        # covers the switch itself.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        # Publish only once fully initialized, so a mid-setup failure doesn't
        # leave a half-configured connection cached on this thread.
        _local.conn = conn
    return _local.conn


def _reset_conn_state() -> None:
    """Drop cached thread-local connection state (test-isolation helper).

    The thread-local connection caches the ``DB_PATH`` in effect when it was
    first opened. Tests that point ``DB_PATH`` at a temp database must clear the
    cache, or ``_get_conn()`` (and therefore ``init_db()``) keeps using a stale
    connection to a previous database. Replacing the ``threading.local`` object
    drops every thread's cached handle so the next ``_get_conn()`` reconnects
    against the current ``DB_PATH``.
    """
    global _local
    try:
        if getattr(_local, "conn", None) is not None:
            _local.conn.close()
    except Exception:
        pass
    _local = threading.local()


# ─── Thread-safe write queue (non-blocking DB writes) ─────────────────────────

_write_queue: _queue_mod.Queue | None = None
_writer_thread: threading.Thread | None = None

#: Enqueued by :func:`shutdown_writer` to stop the writer thread cleanly.
_WRITER_SHUTDOWN = object()

#: Upper bound on how many ops go into a single transaction, so one busy burst
#: can't build an arbitrarily large batch that fails (and retries) as a unit.
_WRITE_BATCH_MAX = 200

#: How long a caller waits for room in a full queue before giving up and writing
#: synchronously. Blocking here is deliberate back-pressure: it keeps writes in
#: order, which the old jump-the-queue fallback did not (see :func:`_enqueue_op`).
_WRITE_QUEUE_TIMEOUT = float(os.getenv("DB_WRITE_QUEUE_TIMEOUT", "5.0"))

#: Per-op retry budget for *transient* SQLite failures (lock/busy contention).
_WRITE_MAX_ATTEMPTS = int(os.getenv("DB_WRITE_MAX_ATTEMPTS", "3"))
_WRITE_RETRY_DELAY = 0.05

#: Observability counters — writes retried, and writes finally given up on.
#: The first useful numbers for a future /metrics surface.
write_retries = 0
write_failures = 0


class _WriteOp:
    """One unit of work for the writer thread.

    ``statements`` commit together as a single transaction, so a multi-statement
    logical write can never be left half-applied.

    ``cache_keys`` names the history-cache entries this op invalidates. The
    writer commits on its *own* connection, so nothing on a reader's connection
    would otherwise learn that the rows changed; it therefore drops these keys
    once the op settles. Enqueueing an op with cache keys also registers it as
    pending, so :func:`load_history` can wait for it rather than querying a
    table the write has not reached yet.
    """

    __slots__ = ("statements", "attempts", "cache_keys")

    def __init__(self, statements: list[tuple[str, tuple]], cache_keys: tuple[str, ...] = ()):
        self.statements = statements
        self.attempts = 0
        self.cache_keys = cache_keys


def _as_op(item) -> _WriteOp | None:
    """Normalize a queue item to a ``_WriteOp``.

    Plain ``(sql, params)`` tuples remain supported — that is the historical
    queue format, and callers (and tests) still enqueue them directly.
    """
    if isinstance(item, _WriteOp):
        return item
    try:
        sql, params = item
    except (TypeError, ValueError):
        log.error("db_writer discarding malformed queue item: %r", item)
        return None
    return _WriteOp([(sql, params)])


def _is_transient(exc: BaseException) -> bool:
    """True for SQLite errors worth retrying — lock/busy contention only.

    Bad SQL and constraint violations are permanent: retrying them just wedges
    the queue behind an op that can never succeed.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _apply_op(conn: sqlite3.Connection, op: _WriteOp) -> bool:
    """Execute one op in its own transaction, retrying transient failures.

    Returns True if it committed. A permanent failure is logged *with the
    offending SQL* and dropped — silently losing it is what the old
    swallow-the-whole-batch handler did.
    """
    global write_retries, write_failures
    for attempt in range(1, _WRITE_MAX_ATTEMPTS + 1):
        try:
            for sql, params in op.statements:
                conn.execute(sql, params)
            conn.commit()
            return True
        except Exception as e:  # noqa: BLE001 — the writer thread must not die
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            op.attempts = attempt
            if not _is_transient(e) or attempt >= _WRITE_MAX_ATTEMPTS:
                write_failures += 1
                log.error(
                    "db_writer dropping write after %d attempt(s): %s | sql=%s",
                    attempt, e, op.statements[0][0],
                )
                return False
            write_retries += 1
            time.sleep(_WRITE_RETRY_DELAY * attempt)
    return False


def _write_batch(conn: sqlite3.Connection, ops: list[_WriteOp]) -> None:
    """Commit a batch, replaying op-by-op if the batch as a whole fails.

    Batching is the fast path. Previously any failure discarded the *entire*
    batch, so one bad op silently took its neighbours — and the conversation
    turns they carried — with it.
    """
    try:
        for op in ops:
            for sql, params in op.statements:
                conn.execute(sql, params)
        conn.commit()
        return
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "db_writer batch failed (%s); replaying %d op(s) individually", e, len(ops)
        )
    for op in ops:
        _apply_op(conn, op)


def _close_writer_conn(conn: sqlite3.Connection) -> None:
    """Close the writer's connection and drop it from the thread-local cache."""
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    # The thread-local outlives the thread object in CPython's pool; drop the
    # closed handle so nothing reuses it.
    if getattr(_local, "conn", None) is conn:
        _local.conn = None


def _db_writer():
    """Background thread that drains the write queue and batches DB writes.

    Owns its thread-local SQLite connection for the life of the thread and
    closes it on the way out, so a restarted writer doesn't leak the handle.
    The connection is opened lazily on the first op: opening it eagerly races
    ``init_db`` for the exclusive lock the ``journal_mode=WAL`` switch needs.
    Stops when it dequeues ``_WRITER_SHUTDOWN`` (see :func:`shutdown_writer`).

    The connection is also reopened if ``DB_PATH`` moves after it was opened.
    A cached handle points at the database that was configured when the first
    op arrived, so a later repoint would have this thread committing to the
    *old* file while every reader had moved on — silent, total write loss for
    everything that went through the queue.
    """
    conn = None
    conn_path = None
    try:
        while True:
            q = _write_queue
            if q is None:
                time.sleep(0.1)
                continue
            try:
                item = q.get(timeout=1.0)
            except _queue_mod.Empty:
                continue
            except Exception as e:  # noqa: BLE001
                # The module-level queue was swapped for something we can't read
                # (tests do this). Back off rather than dying.
                log.debug("db_writer could not read the write queue: %s", e)
                time.sleep(0.1)
                continue

            stopping = item is _WRITER_SHUTDOWN
            ops: list[_WriteOp] = []
            if not stopping:
                op = _as_op(item)
                if op is not None:
                    ops.append(op)
            while not stopping and len(ops) < _WRITE_BATCH_MAX:
                try:
                    item = q.get_nowait()
                except Exception:  # noqa: BLE001 — Empty, or an unreadable queue
                    break
                if item is _WRITER_SHUTDOWN:
                    stopping = True
                    break
                op = _as_op(item)
                if op is not None:
                    ops.append(op)

            if ops:
                if conn is not None and conn_path != DB_PATH:
                    _close_writer_conn(conn)
                    conn = None
                if conn is None:
                    conn = _get_conn()
                    conn_path = DB_PATH
                try:
                    _write_batch(conn, ops)
                finally:
                    # Settle every op whether it committed or was dropped: a
                    # reader waiting on a write that will never arrive must not
                    # wait out the whole timeout. This is also where readers on
                    # other connections learn the rows changed.
                    for op in ops:
                        _settle_pending(op.cache_keys)
            if stopping:
                return
    finally:
        if conn is not None:
            _close_writer_conn(conn)


def _ensure_writer():
    """Start the DB writer thread if not already running."""
    global _write_queue, _writer_thread
    if _write_queue is None:
        _write_queue = _queue_mod.Queue(maxsize=1000)
    if _writer_thread is None or not _writer_thread.is_alive():
        _writer_thread = threading.Thread(
            target=_db_writer, name="telechat-db-writer", daemon=True
        )
        _writer_thread.start()


def shutdown_writer(timeout: float = 5.0) -> bool:
    """Drain the queue, stop the writer thread, and close its DB connection.

    :func:`flush_writes` only waits for the queue to empty — the thread itself
    ran forever and was killed abruptly at interpreter exit, leaking its SQLite
    connection. Shutdown paths should call this instead.

    Returns True if the writer stopped (or was never running).
    """
    global _writer_thread
    thread = _writer_thread
    if thread is None or not thread.is_alive():
        _writer_thread = None
        return True
    flush_writes(timeout=timeout)
    q = _write_queue
    if q is not None:
        try:
            q.put(_WRITER_SHUTDOWN, timeout=max(0.1, timeout))
        except Exception as e:  # noqa: BLE001
            log.warning("could not signal db_writer shutdown: %s", e)
            return False
    thread.join(timeout=timeout)
    stopped = not thread.is_alive()
    if stopped:
        _writer_thread = None
    else:
        log.warning("db_writer did not stop within %.1fs", timeout)
    return stopped


def _enqueue_write(sql: str, params: tuple):
    """Enqueue a single-statement DB write."""
    _enqueue_op(_WriteOp([(sql, params)]))


def _enqueue_op(op: _WriteOp) -> None:
    """Enqueue a write, applying back-pressure rather than jumping the queue.

    The old fallback wrote *synchronously* on the caller's own connection the
    moment the queue was full. That write committed immediately while earlier
    queued writes were still pending, so ordering inverted — a sync DELETE could
    land before the queued INSERT it was meant to follow, leaving the database
    in a state no sequential execution could produce. And it triggered under
    back-pressure, exactly when ordering matters most.

    So we block for up to ``_WRITE_QUEUE_TIMEOUT`` instead, which preserves
    order. The synchronous path survives only for the two cases where queueing
    cannot work at all: no queue configured, and a writer thread that is gone or
    wedged (blocking there would hang the caller forever).
    """
    q = _write_queue
    if q is not None:
        thread = _writer_thread
        draining = thread is not None and thread.is_alive()
        # Register before the put: once the op is on the queue the writer may
        # settle it at any moment, and settling a write that was never
        # registered would underflow the pending count.
        _register_pending(op.cache_keys)
        try:
            if draining:
                q.put(op, timeout=_WRITE_QUEUE_TIMEOUT)
            else:
                q.put_nowait(op)
            return
        except _queue_mod.Full:
            _settle_pending(op.cache_keys)
            log.error(
                "db write queue still full after %.1fs; writing synchronously "
                "(write ordering is no longer guaranteed)",
                _WRITE_QUEUE_TIMEOUT if draining else 0.0,
            )
        except Exception as e:  # noqa: BLE001 — a swapped-out queue must not lose the write
            _settle_pending(op.cache_keys)
            log.error("could not enqueue db write (%s); writing synchronously", e)

    conn = _get_conn()
    try:
        for stmt, stmt_params in op.statements:
            conn.execute(stmt, stmt_params)
        conn.commit()
    finally:
        # A synchronous write lands on this thread's connection, so readers on
        # other connections still need their cached history dropped.
        _invalidate_keys(op.cache_keys)


def flush_writes(timeout: float = 2.0) -> bool:
    """Block until the background write queue is drained.

    Used by the shutdown path so Ctrl-C doesn't lose queued conversation turns.
    Returns True if the queue drained within ``timeout``, False otherwise.
    """
    if _write_queue is None:
        return True
    import time as _time
    deadline = _time.monotonic() + max(0.0, timeout)
    while _time.monotonic() < deadline:
        if _write_queue.empty():
            # Give the writer thread one extra tick to commit the current batch.
            _time.sleep(0.05)
            if _write_queue.empty():
                return True
        _time.sleep(0.05)
    return _write_queue.empty()


# ─── History cache (avoid repeated DB reads for same user) ──────────────────────

#: Insertion-ordered so eviction can drop the *oldest* entry. The old code fell
#: back to `clear()` once the cache was full, which under load threw away every
#: conversation's history repeatedly and sent every reader back to the DB.
_history_cache: "OrderedDict[str, tuple[float, list[dict]]]" = OrderedDict()
_HISTORY_TTL = 30.0
_HISTORY_CACHE_MAX = 200

#: Guards ``_history_cache`` and ``_pending_writes``. Both are read and mutated
#: from every adapter thread and from the writer thread — the same exposure
#: ``_rate_state`` had. The condition lets :func:`load_history` block for a
#: pending write instead of polling for it.
_history_cond = threading.Condition(threading.Lock())

#: cache key -> number of enqueued-but-not-yet-committed writes touching it.
#:
#: Without this a reader could query the `conversations` table in the window
#: between `save_turn` enqueueing a turn and the writer committing it, get a
#: conversation missing its newest turns, and then *cache that answer for the
#: full TTL* — which is what "the bot forgot what I just said" looked like.
_pending_writes: dict[str, int] = {}

#: Upper bound on how long a reader waits for pending writes to settle. A read
#: that waits forever would deadlock behind a wedged writer; serving slightly
#: stale history is the better failure.
_HISTORY_WAIT_TIMEOUT = float(os.getenv("DB_HISTORY_WAIT_TIMEOUT", "2.0"))


def _cache_key(platform: str, user_id: str) -> str:
    return f"{platform}:{user_id}"


def _register_pending(keys: tuple[str, ...]) -> None:
    """Record that writes touching ``keys`` are in flight."""
    if not keys:
        return
    with _history_cond:
        for k in keys:
            _pending_writes[k] = _pending_writes.get(k, 0) + 1


def _settle_pending(keys: tuple[str, ...]) -> None:
    """Release in-flight writes for ``keys`` and drop their cached history.

    Called once an op has settled — committed *or* finally dropped. Dropping it
    must still release, or a reader would wait out the whole timeout for a write
    that is never coming.
    """
    if not keys:
        return
    with _history_cond:
        for k in keys:
            left = _pending_writes.get(k, 0) - 1
            if left > 0:
                _pending_writes[k] = left
            else:
                _pending_writes.pop(k, None)
            _history_cache.pop(k, None)
        _history_cond.notify_all()


def _invalidate_keys(keys: tuple[str, ...]) -> None:
    """Drop cached history for ``keys`` without touching the pending counts."""
    if not keys:
        return
    with _history_cond:
        for k in keys:
            _history_cache.pop(k, None)


def _await_pending(key: str) -> None:
    """Block briefly while writes touching ``key`` are still in flight.

    Caller must hold ``_history_cond``.
    """
    if not _pending_writes.get(key):
        return
    deadline = time.monotonic() + _HISTORY_WAIT_TIMEOUT
    while _pending_writes.get(key):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "history read for %s proceeded with %d write(s) still pending",
                key, _pending_writes.get(key, 0),
            )
            return
        _history_cond.wait(remaining)


# ─── Row timestamps ─────────────────────────────────────────────────────────────

#: `conversations` is keyed on (platform, user_id, ts), so `ts` doubles as the
#: row identity. Wall-clock time is not safe for that: two turns inside the same
#: millisecond produce the same key and one is lost. `_next_ts` hands out
#: strictly increasing values instead, so a conflict can't happen in-process.
_TS_STEP = 0.001
_ts_lock = threading.Lock()
_last_ts = 0.0


def _next_ts(count: int = 1) -> list[float]:
    """Allocate ``count`` strictly increasing row timestamps.

    Tracks wall-clock time when it can (so ordering stays meaningful across
    restarts) and steps forward by ``_TS_STEP`` when calls arrive faster than
    the clock advances.
    """
    global _last_ts
    with _ts_lock:
        base = max(time.time(), _last_ts + _TS_STEP)
        stamps = [base + i * _TS_STEP for i in range(max(1, count))]
        _last_ts = stamps[-1]
        return stamps


def _invalidate_history(platform: str, user_id: str):
    _invalidate_keys((_cache_key(platform, user_id),))


# ─── Rate limiting ──────────────────────────────────────────────────────────────

_rate_state: dict[str, list[float]] = {}
_rate_last_cleanup = 0.0

#: ``_rate_state`` is mutated from the Telegram asyncio loop, the WhatsApp
#: worker thread, the Slack thread, and web-chat tasks concurrently. The
#: read-filter-append sequence below is not atomic, so without this lock two
#: threads could each read a bucket at the limit and both append — letting a
#: user exceed their limit — and the periodic sweep could ``del`` a key another
#: thread was mid-append on. ``SessionManager`` got the same treatment; this
#: was the one place it was missed.
_rate_lock = threading.Lock()


def check_rate_limit(key: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    global _rate_last_cleanup
    now = time.time()
    with _rate_lock:
        bucket = [t for t in _rate_state.get(key, ()) if now - t < RATE_LIMIT_WINDOW]
        if len(bucket) >= RATE_LIMIT_REQUESTS:
            _rate_state[key] = bucket
            return False
        bucket.append(now)
        _rate_state[key] = bucket
        # Periodic cleanup of stale keys (every 5 minutes)
        if now - _rate_last_cleanup > 300:
            _rate_last_cleanup = now
            stale = [
                k for k, v in _rate_state.items()
                if k != key and (not v or now - v[-1] > RATE_LIMIT_WINDOW)
            ]
            for k in stale:
                _rate_state.pop(k, None)
        return True


# ─── SQLite conversation store ──────────────────────────────────────────────────

def init_db() -> None:
    _ensure_writer()
    conn = _get_conn()

    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if cols and "platform" not in cols:
        log.info("Migrating database to multi-platform schema…")
        conn.execute("ALTER TABLE conversations RENAME TO _conv_old")
        conn.execute("""
            CREATE TABLE conversations (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn.execute("""
            INSERT INTO conversations (platform, user_id, role, content, ts)
            SELECT 'telegram', CAST(user_id AS TEXT), role, content, timestamp FROM _conv_old
        """)
        conn.execute("DROP TABLE _conv_old")

        conn.execute("ALTER TABLE usage RENAME TO _usage_old")
        conn.execute("""
            CREATE TABLE usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, PRIMARY KEY (platform, user_id))
        """)
        conn.execute("""
            INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
            SELECT 'telegram', CAST(user_id AS TEXT), message_count, total_input_tokens, total_output_tokens FROM _usage_old
        """)
        conn.execute("DROP TABLE _usage_old")
        conn.commit()
        log.info("Database migration complete.")
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                platform  TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, PRIMARY KEY (platform, user_id))
        """)
        conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            success INTEGER DEFAULT 1,
            ts REAL NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_tracking (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            requests INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            PRIMARY KEY (platform, user_id, date))
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT,
            engine TEXT DEFAULT 'cli',
            model TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            total_cost_usd REAL DEFAULT 0,
            num_turns INTEGER DEFAULT 0)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            rating INTEGER,
            reaction TEXT,
            text_feedback TEXT,
            message_ts REAL,
            response_preview TEXT,
            ts REAL NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_scores (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            evaluator TEXT NOT NULL,
            score REAL NOT NULL,
            response_preview TEXT,
            metadata TEXT,
            ts REAL NOT NULL)
    """)
    conn.commit()

    SessionManager.init_schema(conn)

    # Desktop bridge tables (no-op if module not imported yet — late import below).
    try:
        from . import desktop_bridge
        desktop_bridge.init_bridge_schema(conn)
        conn.commit()
    except Exception:
        pass


def load_history(platform: str, user_id: str, limit: int = 20, session_name: str = "") -> list[dict]:
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id

    key = _cache_key(platform, effective_uid)
    with _history_cond:
        cached = _history_cache.get(key)
        if cached and (time.time() - cached[0]) < _HISTORY_TTL:
            _history_cache.move_to_end(key)
            return cached[1]
        # The writer commits on its own connection, so a turn that `save_turn`
        # has already accepted may not be visible to this one yet. Querying
        # anyway would return a conversation missing its newest turns *and*
        # cache that answer for the full TTL — the bot "forgetting" what was
        # just said. Wait for the write to settle first.
        _await_pending(key)

    conn = _get_conn()
    rows = conn.execute(
        """SELECT role, content FROM conversations
           WHERE platform=? AND user_id=?
           ORDER BY ts DESC LIMIT ?""",
        (platform, effective_uid, limit),
    ).fetchall()
    result = [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    with _history_cond:
        # Don't cache a read that raced a write enqueued while we were querying.
        if _pending_writes.get(key):
            return result
        # Drop expired entries first, then evict oldest-first while still over
        # the cap. The old code fell back to clearing the whole cache, which
        # under load simply cleared it again and again.
        if len(_history_cache) >= _HISTORY_CACHE_MAX:
            now = time.time()
            for k in [k for k, v in _history_cache.items() if now - v[0] > _HISTORY_TTL]:
                _history_cache.pop(k, None)
            while len(_history_cache) >= _HISTORY_CACHE_MAX:
                _history_cache.popitem(last=False)
        _history_cache[key] = (time.time(), result)
        _history_cache.move_to_end(key)
    return result


def save_turn(platform: str, user_id: str, user_text: str, reply: str, session_name: str = "") -> None:
    """Persist one conversation turn (user message + assistant reply).

    The three statements go out as a *single* queued op so they commit together.
    Previously they were three independent enqueues with no transaction, so a
    crash — or the queue filling — between them left a half-saved conversation,
    and the trim DELETE could run against rows its own INSERTs hadn't landed yet,
    drifting the retention window.

    Row timestamps come from :func:`_next_ts` rather than ``time.time()``. The
    primary key is ``(platform, user_id, ts)``, and the old code stamped the
    assistant row at ``now + 0.001``: two turns inside a millisecond collided,
    and ``INSERT OR IGNORE`` swallowed the second one. Monotonic timestamps make
    that collision impossible, so ``OR IGNORE`` is gone too — a genuine conflict
    now surfaces as a logged error instead of a silently missing turn.
    """
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id
    key = _cache_key(platform, effective_uid)
    user_ts, reply_ts = _next_ts(2)
    insert = (
        "INSERT INTO conversations (platform,user_id,role,content,ts) VALUES (?,?,?,?,?)"
    )
    # Extend the cached history *before* enqueueing, not after. The writer drops
    # this key the moment the op settles, and settling can happen as soon as the
    # op is on the queue — appending afterwards could therefore re-seed the
    # cache on top of an invalidation and leave the stale copy to be served for
    # the rest of the TTL.
    with _history_cond:
        cached = _history_cache.get(key)
        if cached:
            updated = cached[1] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ]
            _history_cache[key] = (time.time(), updated[-20:])
            _history_cache.move_to_end(key)

    _enqueue_op(_WriteOp([
        (insert, (platform, effective_uid, "user", user_text, user_ts)),
        (insert, (platform, effective_uid, "assistant", reply, reply_ts)),
        (
            """DELETE FROM conversations WHERE platform=? AND user_id=? AND ts < (
               SELECT ts FROM conversations WHERE platform=? AND user_id=?
               ORDER BY ts DESC LIMIT 1 OFFSET 20)""",
            (platform, effective_uid, platform, effective_uid),
        ),
    ], cache_keys=(key,)))

    _session_mgr.touch_active(platform, user_id)


def replace_history(platform: str, user_id: str, messages: list[dict], session_name: str = "") -> None:
    """Replace entire conversation history with compacted messages.

    Goes through the write queue rather than committing on the caller's own
    connection. Writing directly here jumped ahead of turns `save_turn` had
    already queued: the DELETE ran first, then the writer committed those
    pending turns *on top of* the compacted history, so a compaction that
    should have left three messages left seven. Same ordering hazard item 30
    fixed for the queue-full fallback.
    """
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id
    key = _cache_key(platform, effective_uid)
    statements: list[tuple[str, tuple]] = [(
        "DELETE FROM conversations WHERE platform=? AND user_id=?",
        (platform, effective_uid),
    )]
    # Same allocator as save_turn, so a compaction can't hand out a timestamp a
    # concurrent turn is about to reuse.
    stamps = _next_ts(len(messages)) if messages else []
    for m, ts in zip(messages, stamps, strict=True):
        statements.append((
            "INSERT INTO conversations (platform, user_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (platform, effective_uid, m.get("role", "user"), m.get("content", ""), ts),
        ))
    _enqueue_op(_WriteOp(statements, cache_keys=(key,)))
    _invalidate_history(platform, effective_uid)


def clear_history(platform: str, user_id: str, session_name: str = "") -> None:
    """Drop a conversation's history.

    Queued for the same reason as :func:`replace_history` — a direct commit
    would be overtaken by turns already waiting in the queue, so a cleared
    conversation could come back.
    """
    effective_uid = f"{user_id}:{session_name}" if session_name else user_id
    key = _cache_key(platform, effective_uid)
    _enqueue_op(_WriteOp([(
        "DELETE FROM conversations WHERE platform=? AND user_id=?",
        (platform, effective_uid),
    )], cache_keys=(key,)))
    _invalidate_history(platform, effective_uid)


def track_usage(platform: str, user_id: str, in_tok: int = 0, out_tok: int = 0) -> None:
    _enqueue_write(
        """INSERT INTO usage (platform, user_id, message_count, input_tokens, output_tokens)
           VALUES (?,?,1,?,?)
           ON CONFLICT(platform,user_id) DO UPDATE SET
               message_count = message_count + 1,
               input_tokens  = input_tokens  + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens""",
        (platform, user_id, in_tok, out_tok),
    )


def get_usage(platform: str, user_id: str) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT message_count, input_tokens, output_tokens FROM usage WHERE platform=? AND user_id=?",
        (platform, user_id),
    ).fetchone()
    return {"messages": row[0], "input": row[1], "output": row[2]} if row else {"messages": 0, "input": 0, "output": 0}


def track_tool_usage(platform: str, user_id: str, tools: list[str]) -> None:
    if not tools:
        return
    now = time.time()
    for tool in tools:
        _enqueue_write(
            "INSERT INTO tool_usage (platform, user_id, tool_name, ts) VALUES (?,?,?,?)",
            (platform, user_id, tool, now),
        )


def track_cost(platform: str, user_id: str, in_tok: int, out_tok: int, cost_usd: float) -> None:
    from datetime import date
    today = date.today().isoformat()
    _enqueue_write(
        """INSERT INTO cost_tracking (platform, user_id, date, requests, input_tokens, output_tokens, cost_usd)
           VALUES (?,?,?,1,?,?,?)
           ON CONFLICT(platform, user_id, date) DO UPDATE SET
               requests = requests + 1,
               input_tokens = input_tokens + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens,
               cost_usd = cost_usd + excluded.cost_usd""",
        (platform, user_id, today, in_tok, out_tok, cost_usd),
    )


# ─── Multi-session management ──────────────────────────────────────────────────

_SESSION_TTL = 3600
_SESSION_IDLE_DAYS = 30
_MAX_SESSIONS = 20


class UserSession:
    """A named conversation session with its own history and Claude session ID."""

    def __init__(
        self,
        name: str,
        platform: str,
        user_id: str,
        *,
        db_id: int | None = None,
        title: str = "",
        pinned: bool = False,
        archived: bool = False,
        created_at: float = 0.0,
        last_active: float = 0.0,
        message_count: int = 0,
    ):
        self.db_id: int | None = db_id
        self.name = name
        self.platform = platform
        self.user_id = user_id
        self.title = title
        self.pinned = pinned
        self.archived = archived
        self.claude_session_id: str | None = None
        self.last_active = last_active or time.time()
        self.created_at = created_at or time.time()
        self.message_count = message_count
        self.is_busy = False

    @property
    def cli_session_valid(self) -> bool:
        if self.claude_session_id is None:
            return False
        if self.is_busy:
            return True
        return (time.time() - self.last_active) < _SESSION_TTL

    def touch(self):
        self.last_active = time.time()
        self.message_count += 1

    @property
    def display_name(self) -> str:
        return self.title or self.name

    def age_str(self) -> str:
        secs = int(time.time() - self.last_active)
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    def status_emoji(self) -> str:
        if self.is_busy:
            return "⚙️"
        if self.archived:
            return "📦"
        if self.pinned:
            return "📌"
        if self.cli_session_valid:
            return "🟢"
        return "💤"

    def summary_line(self) -> str:
        title = f" — {self.title}" if self.title else ""
        pin = " 📌" if self.pinned else ""
        return f"{self.status_emoji()} `{self.name}`{title} ({self.message_count} msgs, {self.age_str()}){pin}"


class SessionManager:
    """Manages multiple named sessions per user, persisted to SQLite.

    Thread-safe: all mutations of in-memory state (``_cache``, ``_active``)
    happen under ``_lock`` so Telegram async handlers, WhatsApp/Slack worker
    threads, and the web chat task can all share a single manager instance
    without racing.
    """

    def __init__(self):
        self._cache: dict[str, list[UserSession]] = {}
        self._active: dict[str, str] = {}
        # RLock so a public method can call another public method (e.g.
        # archive() → switch_to_name()) without deadlocking.
        self._lock = threading.RLock()

    def _key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    @staticmethod
    def init_schema(conn: sqlite3.Connection) -> None:
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
            CREATE INDEX IF NOT EXISTS idx_usersess_user
                ON user_sessions(platform, user_id, archived, last_active DESC);

            CREATE TABLE IF NOT EXISTS active_sessions (
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                session_name TEXT NOT NULL,
                PRIMARY KEY (platform, user_id)
            );
        """)
        conn.commit()

    def _save_session(self, sess: UserSession) -> None:
        conn = _get_conn()
        if sess.db_id:
            conn.execute(
                """UPDATE user_sessions SET title=?, pinned=?, archived=?,
                   last_active=?, message_count=? WHERE id=?""",
                (sess.title, int(sess.pinned), int(sess.archived),
                 sess.last_active, sess.message_count, sess.db_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO user_sessions
                   (platform, user_id, name, title, pinned, archived, created_at, last_active, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(platform, user_id, name) DO UPDATE SET
                       title=excluded.title, pinned=excluded.pinned, archived=excluded.archived,
                       last_active=excluded.last_active, message_count=excluded.message_count""",
                (sess.platform, sess.user_id, sess.name, sess.title,
                 int(sess.pinned), int(sess.archived),
                 sess.created_at, sess.last_active, sess.message_count),
            )
            if cur.lastrowid:
                sess.db_id = cur.lastrowid
        conn.commit()

    def _save_active(self, platform: str, user_id: str, session_name: str) -> None:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO active_sessions (platform, user_id, session_name)
               VALUES (?, ?, ?)
               ON CONFLICT(platform, user_id) DO UPDATE SET session_name=excluded.session_name""",
            (platform, user_id, session_name),
        )
        conn.commit()

    def _load_sessions(self, platform: str, user_id: str) -> list[UserSession]:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT id, name, title, pinned, archived, created_at, last_active, message_count
               FROM user_sessions WHERE platform=? AND user_id=?
               ORDER BY pinned DESC, last_active DESC""",
            (platform, user_id),
        ).fetchall()
        sessions = []
        for r in rows:
            sessions.append(UserSession(
                name=r[1], platform=platform, user_id=user_id,
                db_id=r[0], title=r[2], pinned=bool(r[3]), archived=bool(r[4]),
                created_at=r[5], last_active=r[6], message_count=r[7],
            ))
        return sessions

    def _load_active_name(self, platform: str, user_id: str) -> str:
        conn = _get_conn()
        row = conn.execute(
            "SELECT session_name FROM active_sessions WHERE platform=? AND user_id=?",
            (platform, user_id),
        ).fetchone()
        return row[0] if row else ""

    def _ensure_loaded(self, platform: str, user_id: str) -> list[UserSession]:
        with self._lock:
            key = self._key(platform, user_id)
            if key not in self._cache:
                self._cache[key] = self._load_sessions(platform, user_id)
                active_name = self._load_active_name(platform, user_id)
                if active_name:
                    self._active[key] = active_name
            return self._cache[key]

    def get_or_create_active(self, platform: str, user_id: str) -> UserSession:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            key = self._key(platform, user_id)
            active_name = self._active.get(key, "")

            # Never hand back an archived session: prefer a live one with the
            # active name, then fall back to the first live session.
            live = [s for s in sessions if not s.archived]
            if active_name:
                for s in live:
                    if s.name == active_name:
                        return s

            if live:
                self._active[key] = live[0].name
                self._save_active(platform, user_id, live[0].name)
                return live[0]

            sess = UserSession("default", platform, user_id)
            sessions.append(sess)
            self._save_session(sess)
            self._active[key] = "default"
            self._save_active(platform, user_id, "default")
            return sess

    def get_all(self, platform: str, user_id: str, include_archived: bool = False) -> list[UserSession]:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            if include_archived:
                return sessions
            return [s for s in sessions if not s.archived]

    def get_active_index(self, platform: str, user_id: str) -> int:
        with self._lock:
            sessions = self.get_all(platform, user_id)
            key = self._key(platform, user_id)
            active_name = self._active.get(key, "")
            for i, s in enumerate(sessions):
                if s.name == active_name:
                    return i
            return 0

    def create(self, platform: str, user_id: str, name: str) -> UserSession:
        with self._lock:
            key = self._key(platform, user_id)
            sessions = self._ensure_loaded(platform, user_id)

            active_sessions = [s for s in sessions if not s.archived]
            if len(active_sessions) >= _MAX_SESSIONS:
                evictable = sorted(
                    (s for s in active_sessions if not s.pinned and not s.is_busy),
                    key=lambda s: s.last_active,
                )
                if evictable:
                    self._archive_session(evictable[0])

            sess = UserSession(name, platform, user_id)
            sessions.append(sess)
            self._save_session(sess)
            self._active[key] = name
            self._save_active(platform, user_id, name)
            return sess

    def switch_to(self, platform: str, user_id: str, index: int) -> UserSession | None:
        with self._lock:
            sessions = self.get_all(platform, user_id)
            if 0 <= index < len(sessions):
                key = self._key(platform, user_id)
                self._active[key] = sessions[index].name
                self._save_active(platform, user_id, sessions[index].name)
                return sessions[index]
            return None

    def switch_to_name(self, platform: str, user_id: str, name: str) -> UserSession | None:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            for s in sessions:
                if s.name == name:
                    key = self._key(platform, user_id)
                    self._active[key] = name
                    self._save_active(platform, user_id, name)
                    if s.archived:
                        s.archived = False
                        self._save_session(s)
                    return s
            return None

    def rename(self, platform: str, user_id: str, old_name: str, new_name: str) -> UserSession | None:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            sess = next((s for s in sessions if s.name == old_name), None)
            if not sess:
                return None
            if any(s.name == new_name for s in sessions):
                return None

            old_uid = f"{user_id}:{old_name}" if old_name else user_id
            new_uid = f"{user_id}:{new_name}"
            conn = _get_conn()
            conn.execute(
                "UPDATE conversations SET user_id=? WHERE platform=? AND user_id=?",
                (new_uid, platform, old_uid),
            )
            conn.execute(
                "UPDATE user_sessions SET name=? WHERE id=?",
                (new_name, sess.db_id),
            )
            conn.commit()

            key = self._key(platform, user_id)
            if self._active.get(key) == old_name:
                self._active[key] = new_name
                self._save_active(platform, user_id, new_name)

            sess.name = new_name
            _invalidate_history(platform, old_uid)
            return sess

    def set_title(self, platform: str, user_id: str, name: str, title: str) -> UserSession | None:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            sess = next((s for s in sessions if s.name == name), None)
            if not sess:
                return None
            sess.title = title.strip()[:100]
            self._save_session(sess)
            return sess

    def pin(self, platform: str, user_id: str, name: str, pinned: bool = True) -> UserSession | None:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            sess = next((s for s in sessions if s.name == name), None)
            if not sess:
                return None
            sess.pinned = pinned
            self._save_session(sess)
            return sess

    def _archive_session(self, sess: UserSession) -> None:
        sess.archived = True
        self._save_session(sess)

    def _activate_replacement(
        self, platform: str, user_id: str, sessions: list[UserSession], key: str
    ) -> None:
        """Point the active pointer at a live session after the current active
        one was archived/deleted: the first remaining un-archived session, or a
        fresh ``"default"`` if none remain.

        Note: the companion fix lives in ``get_or_create_active``, which never
        returns an archived session — so even though archiving the sole
        ``"default"`` transiently leaves an archived + a fresh ``"default"`` in
        the cache (they collapse to one row on the next DB reload), the active
        pointer can never resolve to the archived copy.
        """
        live = [s for s in sessions if not s.archived]
        if live:
            self._active[key] = live[0].name
            self._save_active(platform, user_id, live[0].name)
            return
        default = UserSession("default", platform, user_id)
        sessions.append(default)
        self._save_session(default)
        self._active[key] = "default"
        self._save_active(platform, user_id, "default")

    def archive(self, platform: str, user_id: str, name: str) -> UserSession | None:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            sess = next((s for s in sessions if s.name == name), None)
            if not sess or sess.is_busy:
                return None
            self._archive_session(sess)

            key = self._key(platform, user_id)
            if self._active.get(key) == name:
                self._activate_replacement(platform, user_id, sessions, key)
            return sess

    def unarchive(self, platform: str, user_id: str, name: str) -> UserSession | None:
        with self._lock:
            return self.switch_to_name(platform, user_id, name)

    def delete(self, platform: str, user_id: str, index: int) -> bool:
        with self._lock:
            sessions = self.get_all(platform, user_id)
            if not sessions or index < 0 or index >= len(sessions):
                return False
            return self.delete_by_name(platform, user_id, sessions[index].name)

    def delete_by_name(self, platform: str, user_id: str, name: str) -> bool:
        with self._lock:
            key = self._key(platform, user_id)
            sessions = self._ensure_loaded(platform, user_id)
            sess = next((s for s in sessions if s.name == name), None)
            if not sess or sess.is_busy:
                return False

            effective_uid = f"{user_id}:{name}" if name else user_id
            conn = _get_conn()
            conn.execute(
                "DELETE FROM conversations WHERE platform=? AND user_id=?",
                (platform, effective_uid),
            )
            if sess.db_id:
                conn.execute("DELETE FROM user_sessions WHERE id=?", (sess.db_id,))
            conn.commit()
            _invalidate_history(platform, effective_uid)

            sessions.remove(sess)

            if self._active.get(key) == name:
                self._activate_replacement(platform, user_id, sessions, key)
            return True

    def search(self, platform: str, user_id: str, query: str) -> list[UserSession]:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            q = query.lower()
            # First pass: match by name/title (no DB hit)
            name_matched = set()
            results = []
            for s in sessions:
                if q in s.name.lower() or q in s.title.lower():
                    results.append(s)
                    name_matched.add(s.name)

            # Second pass: single query for content matches across all remaining sessions
            remaining = [s for s in sessions if s.name not in name_matched]
            if remaining:
                uids = [f"{user_id}:{s.name}" if s.name else user_id for s in remaining]
                placeholders = ",".join("?" for _ in uids)
                conn = _get_conn()
                rows = conn.execute(
                    f"SELECT DISTINCT user_id FROM conversations WHERE platform=? AND user_id IN ({placeholders}) AND content LIKE ?",
                    [platform, *uids, f"%{query}%"],
                ).fetchall()
                matched_uids = {r[0] for r in rows}
                uid_to_sess = {(f"{user_id}:{s.name}" if s.name else user_id): s for s in remaining}
                for uid_val in matched_uids:
                    if uid_val in uid_to_sess:
                        results.append(uid_to_sess[uid_val])
            return results

    def auto_archive_idle(self, platform: str, user_id: str) -> list[str]:
        with self._lock:
            sessions = self._ensure_loaded(platform, user_id)
            cutoff = time.time() - (_SESSION_IDLE_DAYS * 86400)
            archived = []
            for s in sessions:
                if not s.archived and not s.pinned and not s.is_busy and s.last_active < cutoff:
                    self._archive_session(s)
                    archived.append(s.name)
            return archived

    def touch_active(self, platform: str, user_id: str) -> None:
        with self._lock:
            sess = self.get_or_create_active(platform, user_id)
            sess.touch()
            self._save_session(sess)

    def clear_active(self, platform: str, user_id: str):
        with self._lock:
            sess = self.get_or_create_active(platform, user_id)
            sess.claude_session_id = None
            sess.message_count = 0
            self._save_session(sess)


_session_mgr = SessionManager()


# ─── Legacy convenience wrappers ────────────────────────────────────────────────

def get_session_id(platform: str, user_id: str) -> str | None:
    sess = _session_mgr.get_or_create_active(platform, user_id)
    return sess.claude_session_id if sess.cli_session_valid else None


def set_session_id(platform: str, user_id: str, session_id: str):
    sess = _session_mgr.get_or_create_active(platform, user_id)
    sess.claude_session_id = session_id
    sess.touch()
    _session_mgr._save_session(sess)


def clear_session(platform: str, user_id: str):
    _session_mgr.clear_active(platform, user_id)
    _invalidate_history(platform, user_id)


def get_history(platform: str, user_id: str, limit: int = 20, session_name: str = "") -> list[dict]:
    return load_history(platform, user_id, limit=limit, session_name=session_name)
