"""Behavior tests for store.py internals (ticket 0016).

store.py already has heavy feature-area coverage via test_main.py and the
session/feature tests. This file deliberately targets the *internals* that
those higher-level tests don't pin down:

  - the background DB writer thread (_db_writer / _ensure_writer)
  - the non-blocking write queue and its sync fallback (_enqueue_write)
  - flush_writes() drain semantics
  - _history_cache TTL + LRU eviction behaviour
  - rate-limit bucket bookkeeping and stale-key cleanup

Every test runs against an isolated temp DB. Because store.py keeps
module-level globals (DB_PATH, thread-local connection, writer thread, write
queue, caches), each test snapshots and restores the relevant globals so it
neither leaks into nor is polluted by other tests or modules.
"""

import queue as _queue_mod
import sqlite3
import sys
import threading
import time

import pytest

from telechat_pkg import store


@pytest.fixture
def fresh_store(tmp_path):
    """Point store globals at a virgin temp DB and restore them afterwards."""
    orig_db = store.DB_PATH
    orig_local = store._local
    orig_writer = store._writer_thread
    orig_queue = store._write_queue
    orig_cache = dict(store._history_cache)
    orig_rate = dict(store._rate_state)

    # Stop any writer left over from a previous DB before repointing DB_PATH:
    # the writer reads the module-level queue, so a survivor would race this
    # test's writer for ops and commit them to the wrong database.
    store.shutdown_writer(timeout=2.0)

    store.DB_PATH = str(tmp_path / "store_test.db")
    store._local = threading.local()
    store._writer_thread = None
    store._write_queue = None
    store._history_cache.clear()
    store._pending_writes.clear()
    store._rate_state.clear()
    store.init_db()

    yield store

    # Tear down the writer thread + connection for this DB, then restore.
    try:
        store.shutdown_writer(timeout=2.0)
    except Exception:
        pass
    store._reset_conn_state()
    store._pending_writes.clear()
    store._writer_thread = None
    store._write_queue = None
    store.DB_PATH = orig_db
    store._local = orig_local
    store._writer_thread = orig_writer
    store._write_queue = orig_queue
    store._history_cache.clear()
    store._history_cache.update(orig_cache)
    store._rate_state.clear()
    store._rate_state.update(orig_rate)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Writer thread + write queue
# ══════════════════════════════════════════════════════════════════════════════


class TestWriterThread:
    def test_ensure_writer_starts_thread(self, fresh_store):
        fresh_store._ensure_writer()
        assert fresh_store._writer_thread is not None
        assert fresh_store._writer_thread.is_alive()
        assert fresh_store._writer_thread.daemon is True
        assert fresh_store._write_queue is not None

    def test_ensure_writer_idempotent(self, fresh_store):
        fresh_store._ensure_writer()
        t1 = fresh_store._writer_thread
        fresh_store._ensure_writer()
        assert fresh_store._writer_thread is t1  # not restarted while alive

    def test_ensure_writer_restarts_dead_thread(self, fresh_store):
        fresh_store._ensure_writer()
        # Stop the real writer before swapping the global: overwriting
        # _writer_thread while it is alive orphans the thread, and an orphan
        # keeps polling the *module-level* queue — so it drains queues that
        # later tests install and expect to stay untouched.
        assert fresh_store.shutdown_writer(timeout=3.0) is True
        # Simulate a dead writer thread.
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        fresh_store._writer_thread = dead
        fresh_store._ensure_writer()
        assert fresh_store._writer_thread is not dead
        assert fresh_store._writer_thread.is_alive()

    def test_writer_drains_queued_writes(self, fresh_store):
        fresh_store._ensure_writer()
        fresh_store.save_turn("tg", "writer1", "hello", "hi there")
        assert fresh_store.flush_writes(timeout=3.0) is True
        # Bypass the history cache to read straight from DB.
        fresh_store._invalidate_history("tg", "writer1")
        hist = fresh_store.load_history("tg", "writer1")
        contents = [m["content"] for m in hist]
        assert "hello" in contents
        assert "hi there" in contents

    def test_writer_reopens_when_db_path_moves(self, fresh_store, tmp_path):
        # The writer opens its connection on the first op and caches it for the
        # life of the thread. If DB_PATH is repointed afterwards, that cached
        # handle still addresses the *old* file — so every queued write lands in
        # a database nothing reads any more, while callers see no error at all.
        # replace_history/clear_history go through the queue, so this is total
        # silent loss for them, not just a stale row or two.
        fresh_store._ensure_writer()
        fresh_store.save_turn("tg", "moved", "first", "reply")
        assert fresh_store.flush_writes(timeout=3.0) is True

        second_db = str(tmp_path / "moved.db")
        fresh_store.DB_PATH = second_db
        fresh_store._reset_conn_state()
        fresh_store.init_db()

        fresh_store.replace_history("tg", "moved", [{"role": "user", "content": "after"}])
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        assert [m["content"] for m in fresh_store.load_history("tg", "moved")] == ["after"]

        # And it really is the new file that holds it.
        rows = sqlite3.connect(second_db).execute(
            "SELECT content FROM conversations WHERE user_id=?", ("moved",)
        ).fetchall()
        assert [r[0] for r in rows] == ["after"]

    def test_writer_batches_multiple_ops(self, fresh_store):
        fresh_store._ensure_writer()
        for i in range(5):
            fresh_store.track_usage("tg", "batch_user", in_tok=10, out_tok=5)
        assert fresh_store.flush_writes(timeout=3.0) is True
        usage = fresh_store.get_usage("tg", "batch_user")
        assert usage["messages"] == 5
        assert usage["input"] == 50
        assert usage["output"] == 25

    def test_writer_survives_bad_sql(self, fresh_store, caplog):
        # A malformed op should be logged but must not kill the writer thread.
        fresh_store._ensure_writer()
        fresh_store._write_queue.put(("INSERT INTO nonexistent_table VALUES (?)", (1,)))
        time.sleep(0.2)
        # Thread still alive and still processing good ops afterwards.
        assert fresh_store._writer_thread.is_alive()
        fresh_store.track_usage("tg", "after_error", in_tok=1, out_tok=1)
        assert fresh_store.flush_writes(timeout=3.0) is True
        assert fresh_store.get_usage("tg", "after_error")["messages"] == 1

    def test_bad_op_does_not_discard_the_rest_of_its_batch(self, fresh_store):
        # Item 29: the old handler dropped the whole batch on any failure, so a
        # single bad op silently took its neighbours' writes with it.
        q = fresh_store._write_queue
        good = (
            "INSERT INTO usage (platform, user_id, message_count, input_tokens, "
            "output_tokens) VALUES (?,?,1,5,5)"
        )
        q.put((good, ("tg", "batch_survivor_a")))
        q.put(("INSERT INTO nonexistent_table VALUES (?)", (1,)))
        q.put((good, ("tg", "batch_survivor_b")))
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        assert fresh_store.get_usage("tg", "batch_survivor_a")["messages"] == 1
        assert fresh_store.get_usage("tg", "batch_survivor_b")["messages"] == 1

    def test_permanent_failure_is_counted_and_logged(self, fresh_store, caplog):
        import logging

        before = fresh_store.write_failures
        with caplog.at_level(logging.ERROR, logger="telechat_pkg.store"):
            fresh_store._write_queue.put(("INSERT INTO no_such_table VALUES (?)", (1,)))
            fresh_store.flush_writes(timeout=3.0)
            time.sleep(0.3)
        assert fresh_store.write_failures > before
        # The offending SQL is in the log, not just the exception text.
        assert any("no_such_table" in r.message for r in caplog.records)

    def test_transient_error_is_retried(self, fresh_store):
        import sqlite3 as _sq

        calls = {"n": 0}

        class FlakyConn:
            def execute(self, sql, params):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise _sq.OperationalError("database is locked")

            def commit(self):
                pass

            def rollback(self):
                pass

        before = fresh_store.write_retries
        op = fresh_store._WriteOp([("INSERT INTO x VALUES (?)", (1,))])
        assert fresh_store._apply_op(FlakyConn(), op) is True
        assert calls["n"] == 3  # two transient failures, then success
        assert fresh_store.write_retries == before + 2

    def test_permanent_error_is_not_retried(self, fresh_store):
        import sqlite3 as _sq

        calls = {"n": 0}

        class BadSqlConn:
            def execute(self, sql, params):
                calls["n"] += 1
                raise _sq.OperationalError("no such table: nope")

            def commit(self):
                pass

            def rollback(self):
                pass

        op = fresh_store._WriteOp([("INSERT INTO nope VALUES (?)", (1,))])
        assert fresh_store._apply_op(BadSqlConn(), op) is False
        assert calls["n"] == 1  # given up immediately, queue not wedged

    def test_malformed_queue_item_is_discarded(self, fresh_store):
        fresh_store._write_queue.put("not-a-tuple")
        fresh_store.track_usage("tg", "after_malformed", in_tok=1, out_tok=1)
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        assert fresh_store._writer_thread.is_alive()
        assert fresh_store.get_usage("tg", "after_malformed")["messages"] == 1


class TestWriterShutdown:
    def test_shutdown_stops_the_thread(self, fresh_store):
        fresh_store._ensure_writer()
        thread = fresh_store._writer_thread
        assert fresh_store.shutdown_writer(timeout=3.0) is True
        assert not thread.is_alive()
        assert fresh_store._writer_thread is None

    def test_shutdown_drains_pending_writes_first(self, fresh_store):
        fresh_store._ensure_writer()
        for i in range(20):
            fresh_store.track_usage("tg", "shutdown_drain", in_tok=1, out_tok=1)
        assert fresh_store.shutdown_writer(timeout=5.0) is True
        assert fresh_store.get_usage("tg", "shutdown_drain")["messages"] == 20

    def test_shutdown_is_idempotent(self, fresh_store):
        assert fresh_store.shutdown_writer(timeout=2.0) is True
        assert fresh_store.shutdown_writer(timeout=2.0) is True

    def test_ensure_writer_restarts_after_shutdown(self, fresh_store):
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._ensure_writer()
        assert fresh_store._writer_thread.is_alive()
        fresh_store.track_usage("tg", "restarted", in_tok=2, out_tok=2)
        assert fresh_store.flush_writes(timeout=3.0) is True
        assert fresh_store.get_usage("tg", "restarted")["messages"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. _enqueue_write fallback paths
# ══════════════════════════════════════════════════════════════════════════════


class TestEnqueueWrite:
    def test_enqueue_without_queue_writes_synchronously(self, fresh_store):
        # No queue -> sync write path. init_db() starts a writer, so stop it
        # *before* dropping the queue: shutdown_writer signals the thread
        # through that queue, so nulling it first strands the writer alive and
        # it leaks into the following tests, where it drains their queues.
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._write_queue = None
        fresh_store._enqueue_write(
            "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) "
            "VALUES (?,?,?,?,?)",
            ("tg", "sync_user", "user", "synced", time.time()),
        )
        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE platform=? AND user_id=?",
            ("tg", "sync_user"),
        ).fetchall()
        assert any(r[0] == "synced" for r in rows)

    def test_enqueue_full_queue_falls_back_to_sync(self, fresh_store, monkeypatch):
        # A queue that stays full past the timeout still falls back to a sync
        # write rather than losing the row. Short timeout so the test is quick.
        #
        # The writer must *look* alive, so _enqueue_op takes the blocking path,
        # while nothing actually drains the queue. Leaving the real writer
        # running instead let it consume the filler item, so room appeared
        # inside the timeout and the write went async — the row then arrived
        # after the assertion below had already read the table.
        monkeypatch.setattr(fresh_store, "_WRITE_QUEUE_TIMEOUT", 0.05)
        fresh_store.shutdown_writer(timeout=2.0)

        class _AliveButIdle:
            """_enqueue_op only asks the writer whether it is alive."""

            @staticmethod
            def is_alive():
                return True

        class _AlwaysFullQueue:
            """Never accepts a write, and never yields one to a reader.

            A real bounded Queue isn't enough here: a writer thread leaked by an
            earlier test module also reads the module-level queue, and draining
            the filler item would make room and send the write down the async
            path — which is what the ordering test below covers instead.
            """

            def put(self, item, timeout=None):
                if timeout:
                    time.sleep(timeout)
                raise _queue_mod.Full()

            def put_nowait(self, item):
                raise _queue_mod.Full()

            def get(self, timeout=None):
                raise _queue_mod.Empty()

            def get_nowait(self):
                raise _queue_mod.Empty()

            def empty(self):
                return True

        monkeypatch.setattr(fresh_store, "_writer_thread", _AliveButIdle())
        monkeypatch.setattr(fresh_store, "_write_queue", _AlwaysFullQueue())
        fresh_store._enqueue_write(
            "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) "
            "VALUES (?,?,?,?,?)",
            ("tg", "full_user", "user", "fallback", time.time()),
        )
        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE user_id=?", ("full_user",)
        ).fetchall()
        assert any(r[0] == "fallback" for r in rows)

    def test_enqueue_blocks_for_room_instead_of_jumping_the_queue(self, fresh_store):
        # Item 30: the old fallback wrote synchronously the instant the queue was
        # full, committing ahead of writes already queued and inverting order.
        # Now the caller waits for room, so the queued write lands first.
        fresh_store.shutdown_writer(timeout=2.0)
        q = _queue_mod.Queue(maxsize=1)
        fresh_store._write_queue = q

        insert = (
            "INSERT INTO conversations (platform,user_id,role,content,ts) "
            "VALUES (?,?,?,?,?)"
        )
        q.put((insert, ("tg", "order_user", "user", "first", 1.0)))  # queue now full

        # Restart the writer so the queued op drains and makes room.
        fresh_store._ensure_writer()
        fresh_store._enqueue_write(insert, ("tg", "order_user", "user", "second", 2.0))
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)

        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE user_id=? ORDER BY ts",
            ("order_user",),
        ).fetchall()
        assert [r[0] for r in rows] == ["first", "second"]

    def test_enqueue_does_not_block_when_writer_is_dead(self, fresh_store):
        # Nothing is draining, so blocking would hang the caller forever —
        # the sync fallback must still apply immediately.
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._write_queue = _queue_mod.Queue(maxsize=1)
        fresh_store._write_queue.put(("noop", ()))  # full, and no writer

        started = time.monotonic()
        fresh_store._enqueue_write(
            "INSERT OR IGNORE INTO conversations (platform,user_id,role,content,ts) "
            "VALUES (?,?,?,?,?)",
            ("tg", "dead_writer", "user", "sync", time.time()),
        )
        assert time.monotonic() - started < 1.0
        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE user_id=?", ("dead_writer",)
        ).fetchall()
        assert any(r[0] == "sync" for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
# 3. flush_writes
# ══════════════════════════════════════════════════════════════════════════════


class TestFlushWrites:
    def test_flush_no_queue_returns_true(self, fresh_store):
        fresh_store.flush_writes(timeout=2.0)
        fresh_store._write_queue = None
        assert fresh_store.flush_writes() is True

    def test_flush_empty_queue_returns_true(self, fresh_store):
        fresh_store._ensure_writer()
        assert fresh_store.flush_writes(timeout=2.0) is True

    def test_flush_drains_pending(self, fresh_store):
        fresh_store._ensure_writer()
        for i in range(10):
            fresh_store.track_usage("tg", "flush_user", in_tok=1, out_tok=1)
        assert fresh_store.flush_writes(timeout=3.0) is True
        assert fresh_store._write_queue.empty()

    def test_flush_timeout_returns_queue_state(self, fresh_store):
        # A queue that never reports empty must make flush_writes time out and
        # return False. Use a stub queue so no live writer thread can drain it.
        class NeverEmptyQueue:
            def empty(self):
                return False

        fresh_store._write_queue = NeverEmptyQueue()
        result = fresh_store.flush_writes(timeout=0.2)
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# 4. _history_cache TTL + LRU eviction
# ══════════════════════════════════════════════════════════════════════════════


class _GatedQueue(_queue_mod.Queue):
    """A real write queue that yields nothing to a writer until :meth:`open`.

    Tests that pin down the *in-flight* window — the gap between an op being
    enqueued and the writer committing it — need that window to stay open for
    as long as they say. A plain Queue can't guarantee that: any live writer
    thread reads the module-level queue global, so one left behind by another
    test (or another test module) would drain the op immediately and close the
    window before the assertion ran. Gating the *read* side keeps the op parked
    no matter how many writers are polling.
    """

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize=maxsize)
        self._gate = threading.Event()

    def open(self):
        self._gate.set()

    def get(self, block=True, timeout=None):
        # Burn the writer's poll interval waiting on the gate rather than
        # spinning, then fall through to the real get once it opens.
        if not self._gate.wait(timeout if block else 0):
            raise _queue_mod.Empty()
        return super().get(block=block, timeout=timeout)

    def get_nowait(self):
        if not self._gate.is_set():
            raise _queue_mod.Empty()
        return super().get_nowait()


class TestHistoryCache:
    def test_load_history_caches_result(self, fresh_store):
        fresh_store.replace_history("tg", "cache1", [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ])
        first = fresh_store.load_history("tg", "cache1")
        key = fresh_store._cache_key("tg", "cache1")
        assert key in fresh_store._history_cache
        # Second call returns the same cached list object.
        second = fresh_store.load_history("tg", "cache1")
        assert second is fresh_store._history_cache[key][1]
        assert first == second

    def test_cache_hit_skips_db(self, fresh_store):
        fresh_store.replace_history("tg", "cache2", [{"role": "user", "content": "x"}])
        fresh_store.load_history("tg", "cache2")
        # Inject a sentinel into the cache; a hit should return it verbatim.
        key = fresh_store._cache_key("tg", "cache2")
        sentinel = [{"role": "user", "content": "SENTINEL"}]
        fresh_store._history_cache[key] = (time.time(), sentinel)
        assert fresh_store.load_history("tg", "cache2") is sentinel

    def test_expired_cache_reloads_from_db(self, fresh_store):
        fresh_store.replace_history("tg", "cache3", [{"role": "user", "content": "real"}])
        key = fresh_store._cache_key("tg", "cache3")
        # Stale entry (timestamp far in the past) must be ignored.
        fresh_store._history_cache[key] = (
            time.time() - fresh_store._HISTORY_TTL - 10,
            [{"role": "user", "content": "STALE"}],
        )
        result = fresh_store.load_history("tg", "cache3")
        assert result == [{"role": "user", "content": "real"}]

    def test_invalidate_removes_entry(self, fresh_store):
        fresh_store.replace_history("tg", "cache4", [{"role": "user", "content": "z"}])
        fresh_store.load_history("tg", "cache4")
        key = fresh_store._cache_key("tg", "cache4")
        assert key in fresh_store._history_cache
        fresh_store._invalidate_history("tg", "cache4")
        assert key not in fresh_store._history_cache

    def test_save_turn_appends_to_cached_history(self, fresh_store):
        # The append has to be observable *while the write is still in flight*
        # — that window is the whole point of it. Once the writer settles the op
        # it drops the key (item 33) and the next read re-queries, so this test
        # holds the op on a gated queue rather than racing the writer.
        fresh_store.replace_history("tg", "cache5", [{"role": "user", "content": "seed"}])
        assert fresh_store.flush_writes(timeout=3.0) is True
        fresh_store.load_history("tg", "cache5")  # populate cache
        fresh_store.shutdown_writer(timeout=2.0)
        gate = _GatedQueue(maxsize=1000)
        fresh_store._write_queue = gate

        fresh_store.save_turn("tg", "cache5", "newq", "newa")
        key = fresh_store._cache_key("tg", "cache5")
        cached = fresh_store._history_cache[key][1]
        contents = [m["content"] for m in cached]
        assert contents == ["seed", "newq", "newa"]

        # And once the write actually lands, a fresh read agrees with the cache.
        gate.open()
        fresh_store._ensure_writer()
        assert fresh_store.flush_writes(timeout=3.0) is True
        assert [m["content"] for m in fresh_store.load_history("tg", "cache5")] == [
            "seed", "newq", "newa",
        ]

    def test_cached_history_trimmed_to_20(self, fresh_store):
        # Pre-seed a cache with 20 entries, then a save_turn adds 2 more.
        # Same in-flight window as above — inspect the cache before the writer
        # settles the op and invalidates the key.
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._write_queue = _GatedQueue(maxsize=1000)
        key = fresh_store._cache_key("tg", "cache6")
        seed = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        fresh_store._history_cache[key] = (time.time(), seed)
        fresh_store.save_turn("tg", "cache6", "qq", "aa")
        cached = fresh_store._history_cache[key][1]
        assert len(cached) == 20
        # Oldest entries dropped, newest kept.
        assert cached[-1]["content"] == "aa"
        assert cached[-2]["content"] == "qq"

    def test_eviction_drops_stale_when_cache_full(self, fresh_store):
        # Fill cache to the max with stale entries, then a fresh load triggers
        # the stale-eviction branch.
        old = time.time() - fresh_store._HISTORY_TTL - 100
        for i in range(fresh_store._HISTORY_CACHE_MAX):
            fresh_store._history_cache[f"stale:{i}"] = (old, [])
        assert len(fresh_store._history_cache) == fresh_store._HISTORY_CACHE_MAX
        fresh_store.replace_history("tg", "evict1", [{"role": "user", "content": "fresh"}])
        fresh_store.load_history("tg", "evict1")
        # Stale entries should have been purged.
        assert not any(k.startswith("stale:") for k in fresh_store._history_cache)
        assert fresh_store._cache_key("tg", "evict1") in fresh_store._history_cache

    def test_eviction_is_oldest_first_when_nothing_is_stale(self, fresh_store):
        # Item 33: with no stale entries to reclaim the old code fell back to
        # `_history_cache.clear()`, so one over-cap insert threw away every
        # other conversation's history — and under sustained load it did that
        # on every load. Eviction is now oldest-first and drops only what the
        # cap requires.
        cap = fresh_store._HISTORY_CACHE_MAX
        now = time.time()
        for i in range(cap):
            fresh_store._history_cache[f"fresh:{i}"] = (now, [])
        fresh_store.replace_history("tg", "evict2", [{"role": "user", "content": "kept"}])
        fresh_store.load_history("tg", "evict2")

        keys = list(fresh_store._history_cache.keys())
        assert len(keys) == cap                  # still full, not emptied
        assert "fresh:0" not in keys             # the oldest went
        assert f"fresh:{cap - 1}" in keys        # the newest stayed
        assert sum(k.startswith("fresh:") for k in keys) == cap - 1
        assert fresh_store._cache_key("tg", "evict2") in keys

    def test_read_waits_for_a_turn_the_writer_has_not_committed(self, fresh_store):
        # Item 33: the writer commits on its own connection. A read taken in
        # the window between save_turn enqueueing and the writer committing
        # used to query the table, miss the turn, and then cache that truncated
        # conversation for the full TTL — the bot "forgetting" what was just
        # said. The read must now wait for the pending write.
        fresh_store.shutdown_writer(timeout=2.0)
        gate = _GatedQueue(maxsize=1000)
        fresh_store._write_queue = gate
        fresh_store.save_turn("tg", "pending", "what did I say?", "you said hi")
        # Nothing has drained the queue yet, so the rows are not in the table.
        assert gate.qsize() == 1
        assert fresh_store._pending_writes[fresh_store._cache_key("tg", "pending")] == 1

        # Let the write through only after the reader is already blocked on it.
        fresh_store._ensure_writer()
        threading.Timer(0.3, gate.open).start()
        started = time.monotonic()
        hist = fresh_store.load_history("tg", "pending")
        assert time.monotonic() - started >= 0.25  # it really waited
        assert [m["content"] for m in hist] == ["what did I say?", "you said hi"]

    def test_writer_invalidates_the_cache_it_did_not_populate(self, fresh_store):
        # The cache is filled on a reader's connection; the writer commits on
        # its own and nothing told the reader the rows had changed. A committed
        # write must drop the key so the next read re-queries.
        key = fresh_store._cache_key("tg", "xconn")
        fresh_store.save_turn("tg", "xconn", "q1", "a1")
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        hist = fresh_store.load_history("tg", "xconn")
        assert key in fresh_store._history_cache
        assert len(hist) == 2

        fresh_store.save_turn("tg", "xconn", "q2", "a2")
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        # The writer settled the op, so the entry it could not update is gone.
        assert key not in fresh_store._history_cache
        assert [m["content"] for m in fresh_store.load_history("tg", "xconn")] == [
            "q1", "a1", "q2", "a2",
        ]

    def test_dropped_write_still_releases_waiting_readers(self, fresh_store):
        # A write the writer finally gives up on must still settle, or a reader
        # waits out the entire timeout for a turn that is never coming.
        fresh_store.save_turn("tg", "doomed", "q", "a")
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        assert fresh_store._pending_writes == {}

        # Same, but for an op that can never succeed.
        fresh_store._enqueue_op(fresh_store._WriteOp(
            [("INSERT INTO no_such_table VALUES (?)", (1,))],
            cache_keys=(fresh_store._cache_key("tg", "doomed"),),
        ))
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.3)
        assert fresh_store._pending_writes == {}

    def test_read_gives_up_waiting_rather_than_hanging(self, fresh_store, monkeypatch):
        # A wedged writer must not deadlock reads; serving slightly stale
        # history is the better failure.
        monkeypatch.setattr(fresh_store, "_HISTORY_WAIT_TIMEOUT", 0.1)
        fresh_store.shutdown_writer(timeout=2.0)
        # Gated and never opened — the write stays wedged however many writers
        # are polling the queue global.
        fresh_store._write_queue = _GatedQueue(maxsize=1000)
        fresh_store.save_turn("tg", "wedged", "q", "a")
        started = time.monotonic()
        hist = fresh_store.load_history("tg", "wedged")
        elapsed = time.monotonic() - started
        assert 0.05 < elapsed < 1.5
        assert hist == []  # the write never landed, and nothing hung

    def test_session_name_scopes_cache_key(self, fresh_store):
        fresh_store.replace_history("tg", "u1", [{"role": "user", "content": "main"}],
                                    session_name="")
        fresh_store.replace_history("tg", "u1", [{"role": "user", "content": "sidebar"}],
                                    session_name="work")
        main = fresh_store.load_history("tg", "u1")
        work = fresh_store.load_history("tg", "u1", session_name="work")
        assert main[0]["content"] == "main"
        assert work[0]["content"] == "sidebar"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Rate limiting bookkeeping
# ══════════════════════════════════════════════════════════════════════════════


class TestRateLimit:
    def test_allows_under_limit(self, fresh_store):
        for _ in range(fresh_store.RATE_LIMIT_REQUESTS):
            assert fresh_store.check_rate_limit("rl_user_a") is True

    def test_blocks_over_limit(self, fresh_store):
        for _ in range(fresh_store.RATE_LIMIT_REQUESTS):
            fresh_store.check_rate_limit("rl_user_b")
        assert fresh_store.check_rate_limit("rl_user_b") is False

    def test_window_expiry_allows_again(self, fresh_store, monkeypatch):
        key = "rl_user_c"
        for _ in range(fresh_store.RATE_LIMIT_REQUESTS):
            fresh_store.check_rate_limit(key)
        assert fresh_store.check_rate_limit(key) is False
        # Age out every timestamp past the window.
        old = [t - fresh_store.RATE_LIMIT_WINDOW - 1 for t in fresh_store._rate_state[key]]
        fresh_store._rate_state[key] = old
        assert fresh_store.check_rate_limit(key) is True

    def test_stale_key_cleanup(self, fresh_store):
        # Seed a stale key, then force the periodic-cleanup branch by resetting
        # the last-cleanup marker.
        fresh_store._rate_state["dead_key"] = [time.time() - 10_000]
        fresh_store._rate_last_cleanup = time.time() - 10_000
        assert fresh_store.check_rate_limit("live_key") is True
        assert "dead_key" not in fresh_store._rate_state

    def test_cleanup_keeps_the_key_being_checked(self, fresh_store):
        # The sweep must never drop the bucket this call just wrote, or the
        # caller's own request stops counting toward its limit.
        fresh_store._rate_last_cleanup = time.time() - 10_000
        assert fresh_store.check_rate_limit("swept_key") is True
        assert fresh_store._rate_state.get("swept_key")

    def test_concurrent_checks_do_not_exceed_the_limit(self, fresh_store, monkeypatch):
        # Item 32: read-filter-publish-append is not atomic, so two threads can
        # each admit a request against the same bucket state.
        #
        # This is a real reproduction, not just an invariant. Two things make
        # the race land reliably under the GIL: a switch interval short enough
        # that the interpreter preempts inside the racy window, and a limit high
        # enough that the whole run stays in the admitting phase (once a bucket
        # is full every call takes the cheap early-return path and nothing
        # races). Verified to admit every one of the 1600 attempts when the lock
        # is removed, and exactly `limit` with it.
        key = "rl_concurrent"
        limit = 500
        monkeypatch.setattr(fresh_store, "RATE_LIMIT_REQUESTS", limit)
        threads_n, per_thread = 16, 100  # 1600 attempts against a limit of 500
        allowed = []
        lock = threading.Lock()
        start = threading.Barrier(threads_n)

        def worker():
            start.wait()
            mine = [fresh_store.check_rate_limit(key) for _ in range(per_thread)]
            with lock:
                allowed.extend(mine)

        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            threads = [threading.Thread(target=worker) for _ in range(threads_n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(old_interval)

        assert len(allowed) == threads_n * per_thread
        assert sum(allowed) == limit  # exactly the limit, no lost updates
        assert len(fresh_store._rate_state[key]) == limit

    def test_state_is_only_mutated_while_the_lock_is_held(self, fresh_store, monkeypatch):
        # The deterministic half of item 32: whatever the scheduler does, every
        # mutation of the shared dict must happen inside the lock.
        unguarded = []

        class _Watched(dict):
            def _check(self, how):
                if not fresh_store._rate_lock.locked():
                    unguarded.append(how)

            def __setitem__(self, k, v):
                self._check(f"__setitem__({k!r})")
                super().__setitem__(k, v)

            def __delitem__(self, k):
                self._check(f"__delitem__({k!r})")
                super().__delitem__(k)

            def pop(self, k, *a):
                self._check(f"pop({k!r})")
                return super().pop(k, *a)

        monkeypatch.setattr(fresh_store, "_rate_state", _Watched())
        fresh_store.check_rate_limit("guarded")
        fresh_store._rate_last_cleanup = 0.0  # force the sweep branch too
        fresh_store.check_rate_limit("guarded")
        assert unguarded == []

    def test_sweep_does_not_race_concurrent_appends(self, fresh_store):
        # The periodic sweep used to `del` keys other threads could be
        # mid-append on, and iterated the dict while other threads inserted
        # into it. Hammer both paths together and require no exception.
        #
        # Unlike the two tests above this one is a smoke test, not a
        # reproduction: it passes with the lock removed too. "RuntimeError:
        # dictionary changed size during iteration" needs a preemption inside
        # the sweep's comprehension, which is too narrow to force reliably.
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    if i % 10 == 0:
                        # Force the cleanup branch from several threads at once.
                        fresh_store._rate_last_cleanup = 0.0
                    fresh_store.check_rate_limit(f"sweep_{n}_{i % 3}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ══════════════════════════════════════════════════════════════════════════════
# 6. Conversation persistence functions
# ══════════════════════════════════════════════════════════════════════════════


class TestConversationFunctions:
    def test_replace_history(self, fresh_store):
        fresh_store.save_turn("tg", "rh1", "old q", "old a")
        fresh_store.flush_writes(timeout=2.0)
        fresh_store.replace_history("tg", "rh1", [
            {"role": "user", "content": "compacted"},
        ])
        hist = fresh_store.load_history("tg", "rh1")
        assert hist == [{"role": "user", "content": "compacted"}]

    def test_replace_history_defaults_missing_keys(self, fresh_store):
        fresh_store.replace_history("tg", "rh2", [{}])  # no role/content
        hist = fresh_store.load_history("tg", "rh2")
        assert hist == [{"role": "user", "content": ""}]

    def test_clear_history(self, fresh_store):
        fresh_store.save_turn("tg", "ch1", "q", "a")
        fresh_store.flush_writes(timeout=2.0)
        fresh_store.clear_history("tg", "ch1")
        assert fresh_store.load_history("tg", "ch1") == []

    def test_save_turn_trims_old_rows(self, fresh_store):
        # 15 turns = 30 rows; the trim DELETE keeps roughly the last 20
        # (OFFSET 20 + the boundary row), so history stays bounded well below 30.
        for i in range(15):
            fresh_store.save_turn("tg", "trim", f"q{i}", f"a{i}")
        fresh_store.flush_writes(timeout=3.0)
        fresh_store._invalidate_history("tg", "trim")
        hist = fresh_store.load_history("tg", "trim", limit=100)
        assert len(hist) <= 22  # bounded, not the full 30
        # The most recent turn survives.
        assert any(m["content"] == "a14" for m in hist)

    def test_save_turn_is_one_atomic_op(self, fresh_store):
        # Item 31: the two INSERTs and the trim DELETE used to be three
        # independent enqueues, so a crash or a full queue between them left a
        # half-saved conversation. They must travel as a single transaction.
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._write_queue = _queue_mod.Queue(maxsize=1000)
        fresh_store.save_turn("tg", "atomic", "q", "a")
        assert fresh_store._write_queue.qsize() == 1
        op = fresh_store._write_queue.get_nowait()
        assert isinstance(op, fresh_store._WriteOp)
        assert len(op.statements) == 3
        assert op.statements[0][0].startswith("INSERT INTO conversations")
        assert op.statements[2][0].lstrip().startswith("DELETE FROM conversations")

    def test_rapid_turns_are_not_silently_dropped(self, fresh_store):
        # Both INSERTs used to be `INSERT OR IGNORE` keyed on
        # (platform, user_id, ts) with the assistant row at now + 0.001, so two
        # turns inside a millisecond collided and the second vanished.
        for i in range(6):
            fresh_store.save_turn("tg", "rapid", f"q{i}", f"a{i}")
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE platform=? AND user_id=? ORDER BY ts",
            ("tg", "rapid"),
        ).fetchall()
        contents = [r[0] for r in rows]
        assert len(contents) == 12
        for i in range(6):
            assert f"q{i}" in contents
            assert f"a{i}" in contents

    def test_save_turn_no_longer_uses_or_ignore(self, fresh_store):
        # A genuine key conflict should raise (and be logged by the writer)
        # rather than be swallowed.
        fresh_store.shutdown_writer(timeout=2.0)
        fresh_store._write_queue = _queue_mod.Queue(maxsize=1000)
        fresh_store.save_turn("tg", "strict", "q", "a")
        op = fresh_store._write_queue.get_nowait()
        assert "OR IGNORE" not in op.statements[0][0]
        assert "OR IGNORE" not in op.statements[1][0]


class TestRowTimestamps:
    def test_next_ts_is_strictly_increasing(self, fresh_store):
        stamps = [fresh_store._next_ts()[0] for _ in range(500)]
        assert all(b > a for a, b in zip(stamps, stamps[1:]))

    def test_next_ts_batch_is_strictly_increasing(self, fresh_store):
        stamps = fresh_store._next_ts(5)
        assert len(stamps) == 5
        assert all(b > a for a, b in zip(stamps, stamps[1:]))

    def test_next_ts_is_unique_across_threads(self, fresh_store):
        seen: list[float] = []
        lock = threading.Lock()

        def worker():
            local = [fresh_store._next_ts()[0] for _ in range(100)]
            with lock:
                seen.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == 800
        assert len(set(seen)) == 800  # no collisions → no lost rows

    def test_replace_history_uses_the_same_allocator(self, fresh_store):
        # A compaction must not hand out a timestamp a concurrent turn reuses.
        fresh_store.replace_history("tg", "compact", [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ])
        fresh_store.save_turn("tg", "compact", "c", "d")
        assert fresh_store.flush_writes(timeout=3.0) is True
        time.sleep(0.2)
        rows = fresh_store._get_conn().execute(
            "SELECT content FROM conversations WHERE platform=? AND user_id=? ORDER BY ts",
            ("tg", "compact"),
        ).fetchall()
        assert [r[0] for r in rows] == ["a", "b", "c", "d"]

    def test_track_usage_and_get(self, fresh_store):
        fresh_store.track_usage("tg", "usage1", in_tok=100, out_tok=50)
        fresh_store.flush_writes(timeout=2.0)
        u = fresh_store.get_usage("tg", "usage1")
        assert u == {"messages": 1, "input": 100, "output": 50}

    def test_get_usage_missing_user(self, fresh_store):
        assert fresh_store.get_usage("tg", "nobody") == {"messages": 0, "input": 0, "output": 0}

    def test_track_tool_usage(self, fresh_store):
        fresh_store.track_tool_usage("tg", "tool1", ["Read", "Bash"])
        fresh_store.flush_writes(timeout=2.0)
        rows = fresh_store._get_conn().execute(
            "SELECT tool_name FROM tool_usage WHERE user_id=?", ("tool1",)
        ).fetchall()
        assert {r[0] for r in rows} == {"Read", "Bash"}

    def test_track_tool_usage_empty_noop(self, fresh_store):
        fresh_store.track_tool_usage("tg", "tool2", [])
        fresh_store.flush_writes(timeout=2.0)
        rows = fresh_store._get_conn().execute(
            "SELECT COUNT(*) FROM tool_usage WHERE user_id=?", ("tool2",)
        ).fetchone()
        assert rows[0] == 0

    def test_track_cost(self, fresh_store):
        fresh_store.track_cost("tg", "cost1", 10, 5, 0.25)
        fresh_store.track_cost("tg", "cost1", 20, 10, 0.50)
        fresh_store.flush_writes(timeout=2.0)
        row = fresh_store._get_conn().execute(
            "SELECT requests, cost_usd FROM cost_tracking WHERE user_id=?", ("cost1",)
        ).fetchone()
        assert row[0] == 2
        assert row[1] == pytest.approx(0.75)


# ══════════════════════════════════════════════════════════════════════════════
# 7. init_db migration from legacy single-platform schema
# ══════════════════════════════════════════════════════════════════════════════


class TestInitDbMigration:
    def test_migrates_legacy_schema(self, tmp_path):
        import sqlite3 as _sq
        db = str(tmp_path / "legacy_store.db")
        conn = _sq.connect(db)
        conn.executescript(
            """CREATE TABLE conversations (
                   user_id INTEGER, role TEXT, content TEXT, timestamp REAL);
               CREATE TABLE usage (
                   user_id INTEGER, message_count INTEGER,
                   total_input_tokens INTEGER, total_output_tokens INTEGER);
               INSERT INTO conversations VALUES (42, 'user', 'legacy msg', 1.0);
               INSERT INTO usage VALUES (42, 3, 100, 50);"""
        )
        conn.commit()
        conn.close()

        orig_db = store.DB_PATH
        orig_local = store._local
        orig_writer = store._writer_thread
        orig_queue = store._write_queue
        try:
            store.DB_PATH = db
            store._local = threading.local()
            store._writer_thread = None
            store._write_queue = None
            store.init_db()
            conn = store._get_conn()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
            assert "platform" in cols
            row = conn.execute(
                "SELECT platform, user_id, content FROM conversations"
            ).fetchone()
            assert row[0] == "telegram"
            assert row[1] == "42"  # cast to TEXT
            assert row[2] == "legacy msg"
            urow = conn.execute(
                "SELECT platform, user_id, input_tokens FROM usage"
            ).fetchone()
            assert urow[0] == "telegram"
            assert urow[2] == 100
        finally:
            try:
                store.flush_writes(timeout=2.0)
            except Exception:
                pass
            store._reset_conn_state()
            store.DB_PATH = orig_db
            store._local = orig_local
            store._writer_thread = orig_writer
            store._write_queue = orig_queue
            store.init_db()


# ══════════════════════════════════════════════════════════════════════════════
# 8. UserSession value object
# ══════════════════════════════════════════════════════════════════════════════


class TestUserSession:
    def test_defaults(self, fresh_store):
        s = fresh_store.UserSession("default", "tg", "u1")
        assert s.name == "default"
        assert s.message_count == 0
        assert s.pinned is False
        assert s.archived is False
        assert s.last_active > 0
        assert s.created_at > 0

    def test_cli_session_valid_no_session_id(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        assert s.cli_session_valid is False

    def test_cli_session_valid_when_busy(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        s.claude_session_id = "sess"
        s.is_busy = True
        s.last_active = 0  # ancient, but busy keeps it valid
        assert s.cli_session_valid is True

    def test_cli_session_valid_within_ttl(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        s.claude_session_id = "sess"
        s.last_active = time.time()
        assert s.cli_session_valid is True

    def test_cli_session_invalid_after_ttl(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        s.claude_session_id = "sess"
        s.last_active = time.time() - fresh_store._SESSION_TTL - 10
        assert s.cli_session_valid is False

    def test_touch_increments(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        before = s.last_active
        time.sleep(0.01)
        s.touch()
        assert s.message_count == 1
        assert s.last_active >= before

    def test_display_name_prefers_title(self, fresh_store):
        s = fresh_store.UserSession("name", "tg", "u1", title="My Title")
        assert s.display_name == "My Title"
        s2 = fresh_store.UserSession("name", "tg", "u1")
        assert s2.display_name == "name"

    def test_age_str_buckets(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        s.last_active = time.time()
        assert s.age_str() == "just now"
        s.last_active = time.time() - 120
        assert s.age_str().endswith("m ago")
        s.last_active = time.time() - 7200
        assert s.age_str().endswith("h ago")
        s.last_active = time.time() - 2 * 86400
        assert s.age_str().endswith("d ago")

    def test_status_emoji_variants(self, fresh_store):
        s = fresh_store.UserSession("x", "tg", "u1")
        s.is_busy = True
        assert s.status_emoji()  # busy
        s.is_busy = False
        s.archived = True
        assert s.status_emoji()  # archived
        s.archived = False
        s.pinned = True
        assert s.status_emoji()  # pinned
        s.pinned = False
        s.claude_session_id = "sess"
        s.last_active = time.time()
        assert s.status_emoji()  # active
        s.claude_session_id = None
        assert s.status_emoji()  # idle

    def test_summary_line(self, fresh_store):
        s = fresh_store.UserSession("proj", "tg", "u1", title="Project", pinned=True)
        line = s.summary_line()
        assert "proj" in line
        assert "Project" in line


# ══════════════════════════════════════════════════════════════════════════════
# 9. SessionManager lifecycle
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mgr(fresh_store):
    return fresh_store.SessionManager()


class TestSessionManager:
    def test_get_or_create_creates_default(self, mgr):
        s = mgr.get_or_create_active("tg", "u1")
        assert s.name == "default"

    def test_get_or_create_returns_existing_active(self, mgr):
        s1 = mgr.get_or_create_active("tg", "u1")
        s2 = mgr.get_or_create_active("tg", "u1")
        assert s1 is s2

    def test_create_named_session(self, mgr):
        s = mgr.create("tg", "u1", "work")
        assert s.name == "work"
        assert mgr.get_or_create_active("tg", "u1").name == "work"

    def test_get_all_excludes_archived(self, mgr):
        mgr.create("tg", "u1", "a")
        mgr.create("tg", "u1", "b")
        mgr.archive("tg", "u1", "a")
        names = {s.name for s in mgr.get_all("tg", "u1")}
        assert "a" not in names
        assert "b" in names
        assert "a" in {s.name for s in mgr.get_all("tg", "u1", include_archived=True)}

    def test_get_or_create_active_stale_active_name(self, mgr):
        # active_name points at a session that no longer exists -> falls back to
        # the first available session (lines 656-659).
        mgr.create("tg", "u1", "real")
        key = mgr._key("tg", "u1")
        mgr._active[key] = "ghost-session"
        active = mgr.get_or_create_active("tg", "u1")
        assert active.name == "real"
        assert mgr._active[key] == "real"

    def test_get_or_create_active_no_active_name(self, mgr):
        # Sessions exist but no active name recorded -> picks first (661-664).
        mgr.create("tg", "u1", "only")
        key = mgr._key("tg", "u1")
        mgr._active.pop(key, None)
        active = mgr.get_or_create_active("tg", "u1")
        assert active.name == "only"

    def test_get_active_index_falls_back_to_zero(self, mgr):
        mgr.create("tg", "u1", "a")
        key = mgr._key("tg", "u1")
        mgr._active[key] = "not-present"
        assert mgr.get_active_index("tg", "u1") == 0

    def test_get_active_index(self, mgr):
        mgr.create("tg", "u1", "first")
        mgr.create("tg", "u1", "second")  # becomes active
        idx = mgr.get_active_index("tg", "u1")
        sessions = mgr.get_all("tg", "u1")
        assert sessions[idx].name == "second"

    def test_switch_to_index(self, mgr):
        mgr.create("tg", "u1", "alpha")
        mgr.create("tg", "u1", "beta")
        sessions = mgr.get_all("tg", "u1")
        target = sessions[0].name
        switched = mgr.switch_to("tg", "u1", 0)
        assert switched.name == target

    def test_switch_to_out_of_range(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.switch_to("tg", "u1", 99) is None

    def test_switch_to_name(self, mgr):
        mgr.create("tg", "u1", "named")
        mgr.create("tg", "u1", "other")
        s = mgr.switch_to_name("tg", "u1", "named")
        assert s.name == "named"

    def test_switch_to_name_missing(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.switch_to_name("tg", "u1", "ghost") is None

    def test_switch_to_name_unarchives(self, mgr):
        mgr.create("tg", "u1", "arch")
        mgr.create("tg", "u1", "keep")
        mgr.archive("tg", "u1", "arch")
        s = mgr.switch_to_name("tg", "u1", "arch")
        assert s.archived is False

    def test_rename(self, mgr):
        mgr.create("tg", "u1", "oldname")
        s = mgr.rename("tg", "u1", "oldname", "newname")
        assert s.name == "newname"

    def test_rename_missing_returns_none(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.rename("tg", "u1", "nope", "x") is None

    def test_rename_to_existing_returns_none(self, mgr):
        mgr.create("tg", "u1", "a")
        mgr.create("tg", "u1", "b")
        assert mgr.rename("tg", "u1", "a", "b") is None

    def test_rename_moves_conversation_rows(self, fresh_store, mgr):
        mgr.create("tg", "u1", "src")
        fresh_store.save_turn("tg", "u1", "hi", "yo", session_name="src")
        fresh_store.flush_writes(timeout=2.0)
        mgr.rename("tg", "u1", "src", "dst")
        hist = fresh_store.load_history("tg", "u1", session_name="dst")
        assert any(m["content"] == "hi" for m in hist)

    def test_set_title(self, mgr):
        mgr.create("tg", "u1", "s")
        s = mgr.set_title("tg", "u1", "s", "  My Long Title  ")
        assert s.title == "My Long Title"

    def test_set_title_missing(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.set_title("tg", "u1", "ghost", "t") is None

    def test_pin_unpin(self, mgr):
        mgr.create("tg", "u1", "p")
        assert mgr.pin("tg", "u1", "p").pinned is True
        assert mgr.pin("tg", "u1", "p", pinned=False).pinned is False

    def test_pin_missing(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.pin("tg", "u1", "ghost") is None

    def test_archive_busy_returns_none(self, mgr):
        s = mgr.create("tg", "u1", "busy")
        s.is_busy = True
        assert mgr.archive("tg", "u1", "busy") is None

    def test_archive_active_picks_new_active(self, mgr):
        mgr.create("tg", "u1", "keep")
        mgr.create("tg", "u1", "active")  # active now
        mgr.archive("tg", "u1", "active")
        new_active = mgr.get_or_create_active("tg", "u1")
        assert new_active.name == "keep"

    def test_archive_last_session_active_is_never_archived(self, mgr):
        # Archiving the sole 'default' leaves the user on a LIVE 'default'.
        # Ticket 0019 fix: get_or_create_active never returns an archived
        # session, even though the cache transiently holds an archived + a fresh
        # 'default' (the previously-reported bug returned the archived copy).
        archived = mgr.get_or_create_active("tg", "u1")  # 'default'
        result = mgr.archive("tg", "u1", "default")
        # archive() still genuinely archives and returns the archived session.
        assert result is archived
        assert result.archived is True
        # A live 'default' is available and is the one handed back as active.
        active = mgr.get_or_create_active("tg", "u1")
        assert active.name == "default"
        assert active.archived is False
        assert active is not result

    def test_unarchive(self, mgr):
        mgr.create("tg", "u1", "a")
        mgr.create("tg", "u1", "b")
        mgr.archive("tg", "u1", "a")
        s = mgr.unarchive("tg", "u1", "a")
        assert s.archived is False

    def test_delete_by_index(self, mgr):
        mgr.create("tg", "u1", "del_me")
        mgr.create("tg", "u1", "stay")
        sessions = mgr.get_all("tg", "u1")
        assert mgr.delete("tg", "u1", 0) is True
        assert len(mgr.get_all("tg", "u1")) == len(sessions) - 1

    def test_delete_out_of_range(self, mgr):
        mgr.get_or_create_active("tg", "u1")
        assert mgr.delete("tg", "u1", 99) is False

    def test_delete_by_name(self, mgr):
        mgr.create("tg", "u1", "gone")
        mgr.create("tg", "u1", "kept")
        assert mgr.delete_by_name("tg", "u1", "gone") is True
        assert "gone" not in {s.name for s in mgr.get_all("tg", "u1")}

    def test_delete_busy_returns_false(self, mgr):
        s = mgr.create("tg", "u1", "busy")
        s.is_busy = True
        assert mgr.delete_by_name("tg", "u1", "busy") is False

    def test_delete_active_last_creates_default(self, mgr):
        mgr.get_or_create_active("tg", "u1")  # default
        assert mgr.delete_by_name("tg", "u1", "default") is True
        active = mgr.get_or_create_active("tg", "u1")
        assert active.name == "default"

    def test_delete_active_picks_new(self, mgr):
        mgr.create("tg", "u1", "keep")
        mgr.create("tg", "u1", "active")
        mgr.delete_by_name("tg", "u1", "active")
        assert mgr.get_or_create_active("tg", "u1").name == "keep"

    def test_search_by_name(self, mgr):
        mgr.create("tg", "u1", "project-x")
        mgr.create("tg", "u1", "other")
        results = mgr.search("tg", "u1", "project")
        assert any(s.name == "project-x" for s in results)

    def test_search_by_title(self, mgr):
        mgr.create("tg", "u1", "s1")
        mgr.set_title("tg", "u1", "s1", "Quarterly Report")
        results = mgr.search("tg", "u1", "quarterly")
        assert any(s.name == "s1" for s in results)

    def test_search_by_content(self, fresh_store, mgr):
        mgr.create("tg", "u1", "chatty")
        fresh_store.save_turn("tg", "u1", "tell me about pangolins", "ok", session_name="chatty")
        fresh_store.flush_writes(timeout=2.0)
        results = mgr.search("tg", "u1", "pangolins")
        assert any(s.name == "chatty" for s in results)

    def test_auto_archive_idle(self, mgr):
        s = mgr.create("tg", "u1", "stale")
        mgr.create("tg", "u1", "current")
        s.last_active = time.time() - (store._SESSION_IDLE_DAYS + 1) * 86400
        mgr._save_session(s)
        archived = mgr.auto_archive_idle("tg", "u1")
        assert "stale" in archived

    def test_auto_archive_skips_pinned(self, mgr):
        s = mgr.create("tg", "u1", "pinned_old")
        s.pinned = True
        s.last_active = time.time() - 1000 * 86400
        mgr._save_session(s)
        archived = mgr.auto_archive_idle("tg", "u1")
        assert "pinned_old" not in archived

    def test_touch_active(self, mgr):
        s = mgr.get_or_create_active("tg", "u1")
        mgr.touch_active("tg", "u1")
        assert s.message_count == 1

    def test_clear_active(self, mgr):
        s = mgr.get_or_create_active("tg", "u1")
        s.claude_session_id = "sid"
        s.message_count = 5
        mgr.clear_active("tg", "u1")
        assert s.claude_session_id is None
        assert s.message_count == 0

    def test_create_evicts_when_over_max(self, mgr):
        # Fill to _MAX_SESSIONS, then one more triggers eviction of the
        # least-recently-active unpinned session.
        for i in range(store._MAX_SESSIONS):
            mgr.create("tg", "u1", f"s{i}")
        active_before = [s for s in mgr.get_all("tg", "u1")]
        assert len(active_before) == store._MAX_SESSIONS
        mgr.create("tg", "u1", "overflow")
        active_after = mgr.get_all("tg", "u1")
        # Still capped at the max (one archived to make room).
        assert len(active_after) <= store._MAX_SESSIONS
        assert any(s.name == "overflow" for s in active_after)


# ══════════════════════════════════════════════════════════════════════════════
# 10. Legacy convenience wrappers
# ══════════════════════════════════════════════════════════════════════════════


class TestLegacyWrappers:
    def test_set_and_get_session_id(self, fresh_store):
        fresh_store.set_session_id("tg", "legacy1", "session-abc")
        assert fresh_store.get_session_id("tg", "legacy1") == "session-abc"

    def test_get_session_id_none_when_unset(self, fresh_store):
        assert fresh_store.get_session_id("tg", "legacy2") is None

    def test_clear_session(self, fresh_store):
        fresh_store.set_session_id("tg", "legacy3", "sid")
        fresh_store.clear_session("tg", "legacy3")
        assert fresh_store.get_session_id("tg", "legacy3") is None

    def test_get_history_wrapper(self, fresh_store):
        fresh_store.save_turn("tg", "legacy4", "q", "a")
        fresh_store.flush_writes(timeout=2.0)
        fresh_store._invalidate_history("tg", "legacy4")
        hist = fresh_store.get_history("tg", "legacy4")
        assert any(m["content"] == "q" for m in hist)
