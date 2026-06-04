"""
Behavior tests for the natural-language auto scheduler (telechat_pkg.auto_scheduler).

Covers: interval parsing, schedule-request parsing, task CRUD + persistence,
due-task detection, run accounting (incl. one-shot exhaustion), the async tick
loop with a fire callback, error isolation, and formatting helpers.

Run:
    pytest tests/test_auto_scheduler.py -v
"""

import asyncio
import os
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import auto_scheduler as asched
from telechat_pkg.auto_scheduler import (
    AutoScheduler,
    AutoTask,
    parse_interval,
    parse_schedule_request,
    _format_interval,
)


@pytest.fixture
def sched(tmp_path):
    return AutoScheduler(db_path=str(tmp_path / "auto.db"))


# ══════════════════════════════════════════════════════════════════════════════
# 1. parse_interval
# ══════════════════════════════════════════════════════════════════════════════


class TestParseInterval:
    def test_seconds(self):
        assert parse_interval("every 30 seconds") == 30

    def test_minutes(self):
        assert parse_interval("every 5 minutes") == 300

    def test_hours(self):
        assert parse_interval("every 2 hours") == 7200

    def test_days(self):
        assert parse_interval("every 3 days") == 3 * 86400

    def test_half_hour(self):
        assert parse_interval("every half hour") == 1800

    def test_hourly(self):
        assert parse_interval("hourly") == 3600

    def test_daily(self):
        assert parse_interval("daily") == 86400

    def test_weekly(self):
        assert parse_interval("weekly") == 604800

    def test_every_morning(self):
        assert parse_interval("every morning") == 86400

    def test_every_evening(self):
        assert parse_interval("every evening") == 86400

    def test_twice_a_day(self):
        assert parse_interval("twice a day") == 43200

    def test_in_n_minutes_one_shot(self):
        assert parse_interval("in 10 minutes") == 600

    def test_once_in_hours(self):
        assert parse_interval("once 2 hours") == 7200

    def test_no_match_returns_none(self):
        assert parse_interval("do something useful") is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. parse_schedule_request
# ══════════════════════════════════════════════════════════════════════════════


class TestParseScheduleRequest:
    def test_recurring(self):
        parsed = parse_schedule_request("remind me to check deploys every 2 hours")
        assert parsed["interval"] == 7200
        assert parsed["max_runs"] == 0
        assert "check deploys" in parsed["description"]

    def test_one_shot(self):
        parsed = parse_schedule_request("remind me to call mom in 30 minutes")
        assert parsed["interval"] == 1800
        assert parsed["max_runs"] == 1

    def test_no_interval_returns_none(self):
        assert parse_schedule_request("just a regular message") is None

    def test_empty_description_defaults(self):
        parsed = parse_schedule_request("every 5 minutes")
        assert parsed["description"] == "scheduled task"

    def test_prompt_mirrors_description(self):
        parsed = parse_schedule_request("schedule water the plants daily")
        assert parsed["prompt"] == parsed["description"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. Task CRUD + persistence
# ══════════════════════════════════════════════════════════════════════════════


class TestCRUD:
    def test_create_task(self, sched):
        task = sched.create_task("telegram", "u1", "check", "check deploys", 60)
        assert task.id > 0
        assert task.platform == "telegram"
        assert task.next_run > time.time()

    def test_parse_and_create(self, sched):
        task = sched.parse_and_create("telegram", "u1", "remind me to stretch every 1 hour")
        assert task is not None
        assert task.interval_seconds == 3600

    def test_parse_and_create_no_match(self, sched):
        assert sched.parse_and_create("telegram", "u1", "hello there") is None

    def test_list_tasks(self, sched):
        sched.create_task("telegram", "u1", "a", "a", 60)
        sched.create_task("telegram", "u1", "b", "b", 120)
        tasks = sched.list_tasks("telegram", "u1")
        assert len(tasks) == 2
        # ordered by next_run ascending
        assert tasks[0].next_run <= tasks[1].next_run

    def test_list_isolates_users(self, sched):
        sched.create_task("telegram", "u1", "a", "a", 60)
        sched.create_task("telegram", "u2", "b", "b", 60)
        assert len(sched.list_tasks("telegram", "u1")) == 1

    def test_delete_task(self, sched):
        task = sched.create_task("telegram", "u1", "a", "a", 60)
        assert sched.delete_task(task.id, "telegram", "u1") is True
        assert sched.list_tasks("telegram", "u1") == []

    def test_delete_wrong_user(self, sched):
        task = sched.create_task("telegram", "u1", "a", "a", 60)
        assert sched.delete_task(task.id, "telegram", "u2") is False

    def test_persistence_across_instances(self, tmp_path):
        db = str(tmp_path / "persist.db")
        s1 = AutoScheduler(db_path=db)
        s1.create_task("telegram", "u1", "persisted", "p", 60)
        s2 = AutoScheduler(db_path=db)
        assert len(s2.list_tasks("telegram", "u1")) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Due detection + run accounting
# ══════════════════════════════════════════════════════════════════════════════


class TestDueAndRun:
    def test_get_due_tasks(self, sched):
        # next_run in the past
        task = sched.create_task("telegram", "u1", "a", "a", 60)
        sched._conn().execute(
            "UPDATE auto_scheduled_tasks SET next_run = ? WHERE id = ?",
            (time.time() - 10, task.id),
        )
        sched._conn().commit()
        due = sched.get_due_tasks()
        assert any(t.id == task.id for t in due)

    def test_not_due_excluded(self, sched):
        sched.create_task("telegram", "u1", "future", "f", 3600)
        assert sched.get_due_tasks() == []

    def test_mark_run_increments(self, sched):
        task = sched.create_task("telegram", "u1", "a", "a", 60)
        sched.mark_run(task.id)
        row = sched._conn().execute(
            "SELECT run_count, last_run FROM auto_scheduled_tasks WHERE id = ?", (task.id,)
        ).fetchone()
        assert row["run_count"] == 1
        assert row["last_run"] > 0

    def test_mark_run_one_shot_disables(self, sched):
        task = sched.create_task("telegram", "u1", "once", "o", 60, max_runs=1)
        sched.mark_run(task.id)
        row = sched._conn().execute(
            "SELECT enabled FROM auto_scheduled_tasks WHERE id = ?", (task.id,)
        ).fetchone()
        assert row["enabled"] == 0
        # disabled tasks drop out of list_tasks
        assert sched.list_tasks("telegram", "u1") == []

    def test_mark_run_unknown_id_noop(self, sched):
        sched.mark_run(99999)  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 5. AutoTask properties
# ══════════════════════════════════════════════════════════════════════════════


class TestAutoTaskProps:
    def test_is_due_true(self):
        t = AutoTask(id=1, platform="t", user_id="u", description="d", prompt="p",
                     interval_seconds=60, next_run=time.time() - 5)
        assert t.is_due is True

    def test_is_due_false_when_disabled(self):
        t = AutoTask(id=1, platform="t", user_id="u", description="d", prompt="p",
                     interval_seconds=60, next_run=time.time() - 5, enabled=False)
        assert t.is_due is False

    def test_is_exhausted(self):
        t = AutoTask(id=1, platform="t", user_id="u", description="d", prompt="p",
                     interval_seconds=60, max_runs=2, run_count=2)
        assert t.is_exhausted is True

    def test_not_exhausted_unlimited(self):
        t = AutoTask(id=1, platform="t", user_id="u", description="d", prompt="p",
                     interval_seconds=60, max_runs=0, run_count=100)
        assert t.is_exhausted is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. Async tick loop
# ══════════════════════════════════════════════════════════════════════════════


class TestTickLoop:
    @pytest.mark.asyncio
    async def test_start_disabled_no_loop(self, sched, monkeypatch):
        monkeypatch.setattr(asched, "AUTO_SCHEDULER_ENABLED", False)
        await sched.start()
        assert sched._task is None

    @pytest.mark.asyncio
    async def test_tick_fires_callback_and_marks_run(self, sched, monkeypatch):
        monkeypatch.setattr(asched, "AUTO_SCHEDULER_ENABLED", True)
        task = sched.create_task("telegram", "u1", "fire", "do it", 60)
        sched._conn().execute(
            "UPDATE auto_scheduled_tasks SET next_run = ? WHERE id = ?",
            (time.time() - 1, task.id),
        )
        sched._conn().commit()

        fired = asyncio.Event()
        seen = []

        async def on_fire(t):
            seen.append(t.id)
            fired.set()

        sched.set_callback(on_fire)

        # Make the post-tick sleep short so the loop iterates quickly.
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0.01)

        monkeypatch.setattr(asched.asyncio, "sleep", fast_sleep)

        await sched.start()
        await asyncio.wait_for(fired.wait(), timeout=2)
        await sched.stop()
        assert task.id in seen

    @pytest.mark.asyncio
    async def test_tick_isolates_callback_error(self, sched, monkeypatch):
        monkeypatch.setattr(asched, "AUTO_SCHEDULER_ENABLED", True)
        task = sched.create_task("telegram", "u1", "fire", "do it", 60)
        sched._conn().execute(
            "UPDATE auto_scheduled_tasks SET next_run = ? WHERE id = ?",
            (time.time() - 1, task.id),
        )
        sched._conn().commit()

        attempts = {"n": 0}

        async def boom(t):
            attempts["n"] += 1
            raise RuntimeError("callback failed")

        sched.set_callback(boom)
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0.01)

        monkeypatch.setattr(asched.asyncio, "sleep", fast_sleep)
        await sched.start()
        for _ in range(50):
            if attempts["n"] >= 1:
                break
            await real_sleep(0.02)
        await sched.stop()
        # task still marked run despite callback error
        row = sched._conn().execute(
            "SELECT run_count FROM auto_scheduled_tasks WHERE id = ?", (task.id,)
        ).fetchone()
        assert row["run_count"] >= 1

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, sched):
        await sched.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self, sched, monkeypatch):
        """A task parked in a long sleep is cancelled and the CancelledError
        swallowed by stop()."""
        monkeypatch.setattr(asched, "AUTO_SCHEDULER_ENABLED", True)
        sched.set_callback(None)
        await sched.start()
        # Loop is now parked on asyncio.sleep(30); stop() must cancel it.
        await asyncio.sleep(0.02)
        await sched.stop()
        assert sched._running is False

    @pytest.mark.asyncio
    async def test_stop_swallows_cancelled_error(self, sched):
        """stop() awaits the cancelled task and swallows CancelledError that
        propagates out of it (covers the except branch)."""
        async def bare_loop():
            # No internal CancelledError handling → cancel propagates to await.
            while True:
                await asyncio.sleep(3600)

        sched._running = True
        sched._task = asyncio.create_task(bare_loop())
        await asyncio.sleep(0.01)
        await sched.stop()
        assert sched._task.cancelled()

    @pytest.mark.asyncio
    async def test_tick_loop_recovers_from_unexpected_error(self, sched, monkeypatch):
        """If get_due_tasks raises, the loop logs, sleeps, and keeps running."""
        monkeypatch.setattr(asched, "AUTO_SCHEDULER_ENABLED", True)
        calls = {"n": 0}
        orig = sched.get_due_tasks

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return orig()

        monkeypatch.setattr(sched, "get_due_tasks", flaky)
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0.01)

        monkeypatch.setattr(asched.asyncio, "sleep", fast_sleep)
        await sched.start()
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await real_sleep(0.02)
        await sched.stop()
        assert calls["n"] >= 2


class TestDefaultDbPath:
    def test_defaults_to_store_db(self, monkeypatch):
        """When db_path is None the scheduler shares store.DB_PATH."""
        from telechat_pkg import store

        s = AutoScheduler()  # db_path=None branch
        assert s._db_path == store.DB_PATH


# ══════════════════════════════════════════════════════════════════════════════
# 7. Formatting
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatting:
    def test_format_interval_seconds(self):
        assert _format_interval(45) == "45s"

    def test_format_interval_minutes(self):
        assert _format_interval(300) == "5m"

    def test_format_interval_hours(self):
        assert _format_interval(7200) == "2h"

    def test_format_interval_days(self):
        assert _format_interval(172800) == "2d"

    def test_format_task_list_empty(self, sched):
        assert sched.format_task_list([]) == "No scheduled tasks."

    def test_format_task_list_recurring(self, sched):
        task = sched.create_task("telegram", "u1", "check deploys", "p", 3600)
        out = sched.format_task_list([task])
        assert "check deploys" in out
        assert "every 1h" in out
        assert "runs" in out

    def test_format_task_list_one_shot(self, sched):
        task = sched.create_task("telegram", "u1", "once thing", "p", 60, max_runs=1)
        out = sched.format_task_list([task])
        assert "(0/1)" in out
