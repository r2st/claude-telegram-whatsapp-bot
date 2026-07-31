"""
Behavior tests for the cron-like scheduler (telechat_pkg.scheduled_tasks).

Covers: ScheduledTask serialization + due/next_run props, callback registration,
task add/remove/get/list (incl. per-user), JSON persistence (save/load), and the
async run loop (callback dispatch, success/failure/exception, missing callback).

Run:
    pytest tests/test_scheduled_tasks.py -v
"""

import asyncio
import os
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import scheduled_tasks as st
from telechat_pkg.scheduled_tasks import ScheduledTask, Scheduler


def _task(**kw):
    base = dict(id="t1", name="Task", interval_seconds=10, callback_name="cb")
    base.update(kw)
    return ScheduledTask(**base)


# ══════════════════════════════════════════════════════════════════════════════
# 1. ScheduledTask dataclass
# ══════════════════════════════════════════════════════════════════════════════


class TestScheduledTask:
    def test_next_run(self):
        t = _task(last_run=100.0, interval_seconds=50)
        assert t.next_run == 150.0

    def test_is_due_true(self):
        t = _task(last_run=0.0, interval_seconds=1)
        assert t.is_due is True

    def test_is_due_false(self):
        t = _task(last_run=time.time() + 1000, interval_seconds=10)
        assert t.is_due is False

    def test_to_dict_roundtrip(self):
        t = _task(platform="telegram", user_id="u1", run_count=3, extra={"k": "v"})
        d = t.to_dict()
        restored = ScheduledTask.from_dict(d)
        assert restored == t

    def test_from_dict_defaults(self):
        t = ScheduledTask.from_dict({
            "id": "x", "name": "n", "interval_seconds": 5, "callback_name": "cb",
        })
        assert t.platform == ""
        assert t.enabled is True
        assert t.run_count == 0
        assert t.extra == {}


# ══════════════════════════════════════════════════════════════════════════════
# 2. Task management
# ══════════════════════════════════════════════════════════════════════════════


class TestTaskManagement:
    def test_add_and_get(self):
        s = Scheduler()
        s.add_task(_task(id="a"))
        assert s.get_task("a").id == "a"

    def test_get_missing_returns_none(self):
        assert Scheduler().get_task("nope") is None

    def test_remove_task(self):
        s = Scheduler()
        s.add_task(_task(id="a"))
        assert s.remove_task("a") is True
        assert s.get_task("a") is None

    def test_remove_missing_returns_false(self):
        assert Scheduler().remove_task("nope") is False

    def test_list_tasks(self):
        s = Scheduler()
        s.add_task(_task(id="a"))
        s.add_task(_task(id="b"))
        assert len(s.list_tasks()) == 2

    def test_list_user_tasks(self):
        s = Scheduler()
        s.add_task(_task(id="a", platform="telegram", user_id="u1"))
        s.add_task(_task(id="b", platform="telegram", user_id="u2"))
        s.add_task(_task(id="c", platform="slack", user_id="u1"))
        result = s.list_user_tasks("telegram", "u1")
        assert [t.id for t in result] == ["a"]

    def test_register_callback(self):
        s = Scheduler()

        async def cb(task):
            return True

        s.register_callback("cb", cb)
        assert s._callbacks["cb"] is cb


# ══════════════════════════════════════════════════════════════════════════════
# 3. Persistence
# ══════════════════════════════════════════════════════════════════════════════


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        f = str(tmp_path / "tasks.json")
        s = Scheduler(tasks_file=f)
        s.add_task(_task(id="a", platform="telegram", user_id="u1"))
        # add_task triggers _save
        assert os.path.exists(f)

        s2 = Scheduler(tasks_file=f)
        s2._load()
        assert s2.get_task("a") is not None

    def test_no_file_save_noop(self):
        s = Scheduler(tasks_file="")
        s.add_task(_task(id="a"))  # should not raise

    def test_no_file_load_noop(self):
        s = Scheduler(tasks_file="")
        s._load()  # should not raise
        assert s.list_tasks() == []

    def test_load_corrupt_json_ignored(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        s = Scheduler(tasks_file=str(f))
        s._load()  # swallows JSONDecodeError
        assert s.list_tasks() == []

    def test_load_missing_file_ignored(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "missing.json"))
        s._load()  # swallows OSError
        assert s.list_tasks() == []

    def test_save_failure_swallowed(self, tmp_path, monkeypatch):
        s = Scheduler(tasks_file=str(tmp_path / "x.json"))

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(st.Path, "write_text", boom)
        s.add_task(_task(id="a"))  # _save must swallow the error


# ══════════════════════════════════════════════════════════════════════════════
# 4. Run loop
# ══════════════════════════════════════════════════════════════════════════════


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_start_idempotent(self, monkeypatch):
        s = Scheduler()
        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        assert s._running is True
        # Calling start again is a no-op (already running).
        first_task = s._loop_task
        s.start()
        assert s._loop_task is first_task
        s.stop()

    @pytest.mark.asyncio
    async def test_due_task_fires_callback(self, monkeypatch):
        s = Scheduler()
        fired = asyncio.Event()

        async def cb(task):
            fired.set()
            return True

        s.register_callback("cb", cb)
        s.add_task(_task(id="a", last_run=0.0, interval_seconds=1, callback_name="cb"))

        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        await asyncio.wait_for(fired.wait(), timeout=2)
        s.stop()
        assert s.get_task("a").run_count >= 1

    @pytest.mark.asyncio
    async def test_disabled_task_skipped(self, monkeypatch):
        s = Scheduler()
        calls = {"n": 0}

        async def cb(task):
            calls["n"] += 1
            return True

        s.register_callback("cb", cb)
        s.add_task(_task(id="a", last_run=0.0, interval_seconds=1, enabled=False, callback_name="cb"))

        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        await real_sleep(0.1)
        s.stop()
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_missing_callback_warned_no_run(self, monkeypatch):
        s = Scheduler()
        s.add_task(_task(id="a", last_run=0.0, interval_seconds=1, callback_name="unknown"))
        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        await real_sleep(0.1)
        s.stop()
        # No callback → run_count stays 0
        assert s.get_task("a").run_count == 0

    @pytest.mark.asyncio
    async def test_callback_returns_false_logged(self, monkeypatch):
        s = Scheduler()
        done = asyncio.Event()

        async def cb(task):
            done.set()
            return False  # failure path

        s.register_callback("cb", cb)
        s.add_task(_task(id="a", last_run=0.0, interval_seconds=1, callback_name="cb"))
        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        await asyncio.wait_for(done.wait(), timeout=2)
        s.stop()
        # still counted as run despite returning False
        assert s.get_task("a").run_count >= 1

    @pytest.mark.asyncio
    async def test_callback_exception_isolated(self, monkeypatch):
        s = Scheduler()
        done = asyncio.Event()

        async def cb(task):
            done.set()
            raise RuntimeError("callback boom")

        s.register_callback("cb", cb)
        s.add_task(_task(id="a", last_run=0.0, interval_seconds=1, callback_name="cb"))
        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        await asyncio.wait_for(done.wait(), timeout=2)
        await real_sleep(0.02)
        s.stop()
        assert s.get_task("a").run_count >= 1

    @pytest.mark.asyncio
    async def test_loop_recovers_from_unexpected_error(self, monkeypatch):
        s = Scheduler()
        calls = {"n": 0}
        real_values = dict.values

        # Make the first iteration's `self._tasks.values()` raise.
        class _FlakyDict(dict):
            def values(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("iteration boom")
                return real_values(self)

        s._tasks = _FlakyDict()
        real_sleep = asyncio.sleep
        monkeypatch.setattr(st.asyncio, "sleep", lambda _: real_sleep(0.01))
        s.start()
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await real_sleep(0.02)
        s.stop()
        assert calls["n"] >= 2

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        s = Scheduler()
        s.stop()  # no loop task; must not raise
        assert s._running is False
