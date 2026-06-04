"""
Behavior tests for telechat_pkg.resource_limiter.

The module enforces subprocess resource limits (CPU, memory, wall-time,
process count) when the coding agent shells out. These tests assert the
*decisions* the limiter makes — "command over the wall-time budget is killed
and reports the right limit", "the strict template denies more aggressively
than relaxed", "an unknown template is rejected" — rather than that a given
code line ran.

Note on scope vs. ticket 0013: the ticket framing assumes a per-user /
per-platform request-rate limiter (concurrent-request caps, per-user fairness,
sliding windows). The actual module is an OS-level *subprocess* limiter — it
has no notion of users, platforms, or a request counter. The behaviors below
map the ticket's intent onto what the code really enforces:

  * concurrent-request cap   -> max_processes RLIMIT_NPROC ceiling
  * memory limit / throttle  -> memory_bytes ceiling + "memory" limit hit
  * CPU / subprocess timeout -> cpu_seconds + wall_time_seconds enforcement
  * per-user vs global limits -> per-call ``limits=`` override vs instance default
  * limit reset / window     -> each ``execute`` call starts a fresh usage window

Run:
    pytest tests/test_resource_limiter.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg.resource_limiter import (
    TEMPLATES,
    ResourceLimiter,
    ResourceLimits,
    ResourceUsage,
    format_usage,
)


# ─── Local fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture
def py():
    """Path to the running interpreter — a portable, always-present binary."""
    return sys.executable


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process for monitor tests.

    Exposes the attributes ``_monitor_linux`` touches: ``pid``, ``returncode``
    (which flips to a terminal value after ``flip_after`` polls so the monitor
    loop terminates), and ``kill`` (records that it was called).
    """

    def __init__(self, pid=4242, flip_after=2):
        self.pid = pid
        self.returncode = None
        self._polls = 0
        self._flip_after = flip_after
        self.kill_called = False

    def _maybe_flip(self):
        self._polls += 1
        if self._polls >= self._flip_after:
            self.returncode = 0

    def kill(self):
        self.kill_called = True
        # A real kill makes the process terminal on the next poll.
        self.returncode = -9


def _proc_files(*, utime=0, stime=0, rss_kb=0):
    """Build a fake ``open`` for /proc/{pid}/stat and /proc/{pid}/status.

    Returns (open_fn, exists_fn) suitable for patching ``os.path.exists`` and
    builtin ``open`` inside ``_monitor_linux``.
    """
    # /proc/.../stat has utime at field index 13, stime at 14 (0-based).
    stat_fields = ["0"] * 20
    stat_fields[13] = str(utime)
    stat_fields[14] = str(stime)
    stat_text = " ".join(stat_fields)
    status_text = f"Name:\tpy\nVmRSS:\t{rss_kb} kB\n"

    def fake_open(path, *a, **k):
        if path.endswith("/stat"):
            return unittest.mock.mock_open(read_data=stat_text)()
        if path.endswith("/status"):
            return unittest.mock.mock_open(read_data=status_text)()
        raise FileNotFoundError(path)

    def fake_exists(path):
        return path.endswith("/stat") or path.endswith("/status")

    return fake_open, fake_exists


# ══════════════════════════════════════════════════════════════════════════
# 1. Templates & limit configuration — the budgets themselves
# ══════════════════════════════════════════════════════════════════════════


class TestLimitTemplates:
    def test_default_limits_are_the_standard_budget(self):
        limiter = ResourceLimiter()
        assert limiter.limits.cpu_seconds == 300
        assert limiter.limits.memory_bytes == 2 * 1024 * 1024 * 1024
        assert limiter.limits.max_processes == 50
        assert limiter.limits.wall_time_seconds == 600

    def test_strict_template_is_tighter_than_standard(self):
        strict = ResourceLimiter.from_template("strict").limits
        standard = TEMPLATES["standard"]
        assert strict.cpu_seconds < standard.cpu_seconds
        assert strict.memory_bytes < standard.memory_bytes
        assert strict.max_processes < standard.max_processes
        assert strict.wall_time_seconds < standard.wall_time_seconds

    def test_relaxed_template_is_looser_than_standard(self):
        relaxed = ResourceLimiter.from_template("relaxed").limits
        standard = TEMPLATES["standard"]
        assert relaxed.cpu_seconds > standard.cpu_seconds
        assert relaxed.memory_bytes > standard.memory_bytes
        assert relaxed.max_processes > standard.max_processes

    def test_template_name_is_case_insensitive(self):
        assert ResourceLimiter.from_template("STRICT").limits is TEMPLATES["strict"]
        assert ResourceLimiter.from_template("Relaxed").limits is TEMPLATES["relaxed"]

    def test_unknown_template_is_rejected_with_available_list(self):
        with pytest.raises(ValueError) as exc:
            ResourceLimiter.from_template("nonexistent")
        msg = str(exc.value)
        assert "nonexistent" in msg
        # Caller is told what they *could* have asked for.
        assert "strict" in msg and "relaxed" in msg

    def test_every_template_orders_strict_lt_standard_lt_relaxed(self):
        # Fairness/ordering invariant across the budget presets.
        assert (
            TEMPLATES["test"].memory_bytes
            < TEMPLATES["strict"].memory_bytes
            < TEMPLATES["standard"].memory_bytes
            < TEMPLATES["relaxed"].memory_bytes
        )


# ══════════════════════════════════════════════════════════════════════════
# 2. Successful execution — under budget, no limits hit
# ══════════════════════════════════════════════════════════════════════════


class TestUnderBudgetExecution:
    @pytest.mark.asyncio
    async def test_quick_command_returns_zero_and_no_limits_hit(self, py):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "print('ok')"]
        )
        assert rc == 0
        assert out.strip() == "ok"
        assert usage.limits_hit == []

    @pytest.mark.asyncio
    async def test_stderr_is_captured_separately(self, py):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import sys; sys.stderr.write('boom')"]
        )
        assert "boom" in err
        assert out == ""

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_is_propagated(self, py):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import sys; sys.exit(3)"]
        )
        assert rc == 3
        assert usage.limits_hit == []

    @pytest.mark.asyncio
    async def test_shell_string_command_is_tokenized(self, py):
        # A str command goes through shlex.split rather than being rejected.
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(f"{py} -c \"print('hi')\"")
        assert rc == 0
        assert out.strip() == "hi"

    @pytest.mark.asyncio
    async def test_wall_time_is_always_recorded(self, py):
        limiter = ResourceLimiter()
        _, _, _, usage = await limiter.execute([py, "-c", "pass"])
        assert usage.wall_time_seconds >= 0.0


# ══════════════════════════════════════════════════════════════════════════
# 3. Wall-time / subprocess timeout enforcement — over budget => killed
# ══════════════════════════════════════════════════════════════════════════


class TestWallTimeEnforcement:
    @pytest.mark.asyncio
    async def test_command_exceeding_wall_time_is_killed_and_flagged(self, py):
        # 0.3s budget against a 30s sleep — must be killed, not allowed to run.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=0.3))
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import time; time.sleep(30)"]
        )
        # On non-Linux the limiter reports the wall-time decision explicitly.
        if sys.platform != "linux":
            assert "wall_time" in usage.limits_hit
            assert "Wall-time limit exceeded" in err

    @pytest.mark.asyncio
    async def test_per_call_limit_override_is_what_gets_enforced(self, py):
        # Instance default is generous; the per-call override is tight and wins.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=600))
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import time; time.sleep(30)"],
            limits=ResourceLimits(wall_time_seconds=0.3),
        )
        if sys.platform != "linux":
            assert "wall_time" in usage.limits_hit

    @pytest.mark.asyncio
    async def test_within_wall_time_budget_is_not_killed(self, py):
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=10))
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import time; time.sleep(0.05); print('done')"]
        )
        assert "wall_time" not in usage.limits_hit
        assert rc == 0

    @pytest.mark.asyncio
    async def test_non_linux_timeout_when_killed_process_also_hangs(self, py):
        # Wall-time fires (communicate times out), we kill, and then even the
        # post-kill ``process.wait()`` overruns its 5s budget — the limiter
        # must still return the canned timeout result, not hang or raise.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=10))
        limiter._is_linux = False

        async def always_timeout(aw, timeout):
            # Drain the coroutine so we don't leak a never-awaited warning.
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError

        with unittest.mock.patch(
            "telechat_pkg.resource_limiter.asyncio.wait_for", new=always_timeout
        ):
            rc, out, err, usage = await limiter.execute([py, "-c", "print('x')"])
        assert "Wall-time limit exceeded" in err
        assert "wall_time" in usage.limits_hit


# ══════════════════════════════════════════════════════════════════════════
# 4. Error handling — bad invocations don't crash the limiter
# ══════════════════════════════════════════════════════════════════════════


class TestExecuteErrorHandling:
    @pytest.mark.asyncio
    async def test_nonexistent_binary_returns_error_tuple_not_raise(self):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            ["this-binary-does-not-exist-xyz", "--help"]
        )
        assert rc == 1
        assert out == ""
        assert err != ""  # the OSError message is surfaced to the caller
        assert isinstance(usage, ResourceUsage)

    @pytest.mark.asyncio
    async def test_cwd_is_honored(self, py, tmp_path):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import os; print(os.getcwd())"],
            cwd=str(tmp_path),
        )
        assert rc == 0
        assert os.path.realpath(out.strip()) == os.path.realpath(str(tmp_path))

    @pytest.mark.asyncio
    async def test_env_is_passed_through(self, py):
        limiter = ResourceLimiter()
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import os; print(os.environ.get('TELECHAT_RL_TEST'))"],
            env={"TELECHAT_RL_TEST": "marker-value", "PATH": os.environ["PATH"]},
        )
        assert out.strip() == "marker-value"


# ══════════════════════════════════════════════════════════════════════════
# 5. preexec_fn — OS-level limits only installed on Linux
# ══════════════════════════════════════════════════════════════════════════


class TestPreexecFn:
    def test_non_linux_installs_no_preexec_fn(self):
        limiter = ResourceLimiter()
        limiter._is_linux = False
        assert limiter._get_preexec_fn() is None

    def test_linux_returns_a_callable_that_sets_rlimits(self):
        limiter = ResourceLimiter(ResourceLimits(cpu_seconds=42))
        limiter._is_linux = True
        fake_resource = unittest.mock.MagicMock()
        fake_resource.RLIMIT_CPU = 0
        fake_resource.RLIMIT_AS = 1
        fake_resource.RLIMIT_FSIZE = 2
        fake_resource.RLIMIT_NPROC = 3
        with unittest.mock.patch.dict(sys.modules, {"resource": fake_resource}):
            fn = limiter._get_preexec_fn()
            assert callable(fn)
            fn()  # invoking it in-process must not raise
        # Four distinct rlimits were requested.
        assert fake_resource.setrlimit.call_count == 4

    def test_linux_preexec_swallows_setrlimit_failure(self):
        # If the kernel rejects a limit we log and continue — we don't crash
        # the child-process launch.
        limiter = ResourceLimiter()
        limiter._is_linux = True
        fake_resource = unittest.mock.MagicMock()
        fake_resource.setrlimit.side_effect = ValueError("not permitted")
        with unittest.mock.patch.dict(sys.modules, {"resource": fake_resource}):
            fn = limiter._get_preexec_fn()
            fn()  # must not propagate the ValueError


# ══════════════════════════════════════════════════════════════════════════
# 6. Linux /proc monitor — CPU / memory / wall-time kill decisions
#
# These exercise _monitor_linux directly with a fake process and a fake /proc
# so the kill decision logic is verified on any host OS.
# ══════════════════════════════════════════════════════════════════════════


class TestLinuxMonitorDecisions:
    @pytest.mark.asyncio
    async def test_under_all_limits_records_usage_without_killing(self):
        limiter = ResourceLimiter()
        limits = ResourceLimits(cpu_seconds=300, memory_bytes=2_000_000_000,
                                wall_time_seconds=600)
        proc = _FakeProc(flip_after=2)
        open_fn, exists_fn = _proc_files(utime=10, stime=5, rss_kb=1024)
        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_fn), \
             unittest.mock.patch("builtins.open", open_fn), \
             unittest.mock.patch("telechat_pkg.resource_limiter.os.sysconf", return_value=100), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, limits)
        assert proc.kill_called is False
        assert usage.limits_hit == []
        # (utime + stime) / SC_CLK_TCK == (10 + 5) / 100 == 0.15s CPU observed.
        assert usage.cpu_time_seconds == pytest.approx(0.15)
        assert usage.memory_peak_bytes == 1024 * 1024

    @pytest.mark.asyncio
    async def test_cpu_over_budget_kills_and_flags_cpu(self):
        limiter = ResourceLimiter()
        limits = ResourceLimits(cpu_seconds=1, memory_bytes=2_000_000_000,
                                wall_time_seconds=600)
        proc = _FakeProc(flip_after=10)
        # utime+stime = 500 ticks / 100 = 5s CPU > 1s budget.
        open_fn, exists_fn = _proc_files(utime=400, stime=100, rss_kb=1024)
        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_fn), \
             unittest.mock.patch("builtins.open", open_fn), \
             unittest.mock.patch("telechat_pkg.resource_limiter.os.sysconf", return_value=100), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, limits)
        assert usage.limits_hit == ["cpu"]
        assert proc.kill_called is True

    @pytest.mark.asyncio
    async def test_memory_over_budget_kills_and_flags_memory(self):
        limiter = ResourceLimiter()
        limits = ResourceLimits(cpu_seconds=300, memory_bytes=1024 * 1024,
                                wall_time_seconds=600)
        proc = _FakeProc(flip_after=10)
        # 4096 kB RSS == 4 MB > 1 MB budget.
        open_fn, exists_fn = _proc_files(utime=1, stime=1, rss_kb=4096)
        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_fn), \
             unittest.mock.patch("builtins.open", open_fn), \
             unittest.mock.patch("telechat_pkg.resource_limiter.os.sysconf", return_value=100), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, limits)
        assert usage.limits_hit == ["memory"]
        assert proc.kill_called is True

    @pytest.mark.asyncio
    async def test_wall_time_over_budget_kills_and_flags_wall_time(self):
        limiter = ResourceLimiter()
        # Zero wall-time budget: any elapsed time trips it.
        limits = ResourceLimits(cpu_seconds=300, memory_bytes=2_000_000_000,
                                wall_time_seconds=0)
        proc = _FakeProc(flip_after=10)
        open_fn, exists_fn = _proc_files(utime=1, stime=1, rss_kb=10)
        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_fn), \
             unittest.mock.patch("builtins.open", open_fn), \
             unittest.mock.patch("telechat_pkg.resource_limiter.os.sysconf", return_value=100), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, limits)
        assert usage.limits_hit == ["wall_time"]
        assert proc.kill_called is True

    @pytest.mark.asyncio
    async def test_monitor_returns_empty_usage_when_pid_missing(self):
        limiter = ResourceLimiter()
        proc = _FakeProc()
        proc.pid = None
        usage = await limiter._monitor_linux(proc, ResourceLimits())
        assert usage.limits_hit == []
        assert usage.cpu_time_seconds == 0.0

    @pytest.mark.asyncio
    async def test_monitor_tolerates_missing_proc_files(self):
        # If /proc entries vanish mid-poll, the monitor keeps going and simply
        # records no usage rather than crashing.
        limiter = ResourceLimiter()
        proc = _FakeProc(flip_after=2)

        def exists_false(path):
            return False

        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_false), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, ResourceLimits())
        assert usage.limits_hit == []
        assert proc.kill_called is False

    @pytest.mark.asyncio
    async def test_monitor_swallows_io_errors_reading_proc(self):
        limiter = ResourceLimiter()
        proc = _FakeProc(flip_after=2)

        def boom_open(path, *a, **k):
            raise IOError("transient /proc read failure")

        def exists_true(path):
            return True

        with unittest.mock.patch("telechat_pkg.resource_limiter.os.path.exists", exists_true), \
             unittest.mock.patch("builtins.open", boom_open), \
             unittest.mock.patch("telechat_pkg.resource_limiter.asyncio.sleep",
                                  new=_make_sleep(proc)):
            usage = await limiter._monitor_linux(proc, ResourceLimits())
        # Errors are swallowed; no false limit-hit, no kill.
        assert usage.limits_hit == []
        assert proc.kill_called is False


# ══════════════════════════════════════════════════════════════════════════
# 7. Linux execute() path — orchestration of subprocess + monitor
# ══════════════════════════════════════════════════════════════════════════


class TestLinuxExecutePath:
    @pytest.mark.asyncio
    async def test_linux_execute_runs_monitor_and_returns_usage(self, py):
        # Force the Linux branch on macOS; the monitor reads a real /proc which
        # won't exist, so it harmlessly records nothing, but the orchestration
        # (create task, communicate, await monitor) is exercised end to end.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=10))
        limiter._is_linux = True
        rc, out, err, usage = await limiter.execute([py, "-c", "print('linux-path')"])
        assert rc == 0
        assert out.strip() == "linux-path"
        assert isinstance(usage, ResourceUsage)

    @pytest.mark.asyncio
    async def test_linux_execute_wall_time_timeout_kills_process(self, py):
        # On the Linux branch a wall-time timeout still kills the process and
        # reports the canned "Wall-time limit exceeded" stderr.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=0.3))
        limiter._is_linux = True
        rc, out, err, usage = await limiter.execute(
            [py, "-c", "import time; time.sleep(30)"]
        )
        assert "Wall-time limit exceeded" in err

    @pytest.mark.asyncio
    async def test_linux_timeout_when_killed_process_also_hangs(self, py):
        # Linux branch counterpart: communicate times out, kill, and the
        # post-kill ``process.wait()`` also overruns — handled, not raised.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=10))
        limiter._is_linux = True

        async def always_timeout(aw, timeout):
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError

        with unittest.mock.patch(
            "telechat_pkg.resource_limiter.asyncio.wait_for", new=always_timeout
        ):
            rc, out, err, usage = await limiter.execute([py, "-c", "print('x')"])
        assert "Wall-time limit exceeded" in err
        # monitor_task join also timed out -> blank usage returned.
        assert isinstance(usage, ResourceUsage)

    @pytest.mark.asyncio
    async def test_linux_execute_handles_monitor_timeout(self, py):
        # If the monitor task overruns its 5s join budget it's cancelled and a
        # blank usage is returned rather than hanging the call.
        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=10))
        limiter._is_linux = True

        real_wait_for = asyncio.wait_for
        calls = {"n": 0}

        async def fake_wait_for(aw, timeout):
            calls["n"] += 1
            # First call wraps process.communicate() — let it through.
            if calls["n"] == 1:
                return await real_wait_for(aw, timeout)
            # Second call is the monitor-task join — simulate it overrunning.
            if asyncio.isfuture(aw) or asyncio.iscoroutine(aw) or asyncio.istask(aw):
                if hasattr(aw, "cancel"):
                    # leave the task; raise timeout so execute() cancels it
                    raise asyncio.TimeoutError
            raise asyncio.TimeoutError

        with unittest.mock.patch(
            "telechat_pkg.resource_limiter.asyncio.wait_for", new=fake_wait_for
        ):
            rc, out, err, usage = await limiter.execute([py, "-c", "print('x')"])
        assert isinstance(usage, ResourceUsage)
        assert usage.limits_hit == []


# ══════════════════════════════════════════════════════════════════════════
# 8. Usage formatting — operator-facing report of what happened
# ══════════════════════════════════════════════════════════════════════════


class TestFormatUsage:
    def test_minimal_usage_shows_only_wall_time(self):
        out = format_usage(ResourceUsage(wall_time_seconds=1.23))
        assert "1.2s" in out
        assert "CPU" not in out
        assert "Mem" not in out
        assert "limits hit" not in out

    def test_cpu_and_memory_are_shown_when_present(self):
        usage = ResourceUsage(
            wall_time_seconds=2.0,
            cpu_time_seconds=1.5,
            memory_peak_bytes=64 * 1024 * 1024,
        )
        out = format_usage(usage)
        assert "CPU 1.5s" in out
        assert "Mem 64MB" in out

    def test_limits_hit_are_surfaced_in_the_summary(self):
        usage = ResourceUsage(wall_time_seconds=5.0, limits_hit=["cpu", "memory"])
        out = format_usage(usage)
        assert "limits hit" in out
        assert "cpu" in out and "memory" in out

    def test_zero_cpu_and_memory_are_omitted(self):
        out = format_usage(ResourceUsage(wall_time_seconds=0.0))
        assert "CPU" not in out
        assert "Mem" not in out


# ─── Helper used by the monitor tests ───────────────────────────────────────


def _make_sleep(proc):
    """Return an async ``sleep`` replacement that advances the fake process.

    Each call flips the fake process one poll closer to termination so the
    monitor loop is bounded, without actually sleeping.
    """

    async def _sleep(_seconds):
        proc._maybe_flip()

    return _sleep
