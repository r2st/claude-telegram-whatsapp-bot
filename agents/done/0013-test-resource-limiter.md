---
id: 0013
title: Write behavior-organized tests for telechat_pkg/resource_limiter.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Create `tests/test_resource_limiter.py` covering the resource_limiter module's real behaviors. Coverage dropped 100% → **51%** after ticket 0012 deleted `test_security_full.py` — 72 lines now unexercised, the single biggest drop from that ticket.

## Why it matters

resource_limiter enforces per-user / per-platform constraints on Claude calls — CPU time, concurrent requests, memory, subprocess spawn rate. When it gets thresholds wrong (or skips them silently), the bot either rate-limits aggressively for honest users or leaks resources to abuse. The padding tests were structured as `class TestResourceLimiterExecute`, `class LastMileResourceLimiter` — line-pegged at the highest resolution. They covered the lines but didn't assert the actual rate-limiting decisions. Replace with behavior-organized tests that assert: "when over budget → request denied with reason X", not "the budget-check code path executed".

## Acceptance criteria

- [ ] `tests/test_resource_limiter.py` created, organized by behavior
- [ ] Coverage of `telechat_pkg/resource_limiter.py` returns to ≥95%
- [ ] No class named `LastMile*`, `*Execute`, or any branch-pegged form
- [ ] `pytest -q tests/test_resource_limiter.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_resource_limiter.py` (new)
- No source changes expected

## Notes

Key behaviors to cover, derived from the module surface and CODE_REVIEW.md context:

1. Concurrent-request cap: Nth request beyond cap → rejected
2. Memory limit: process over threshold → throttled with the right kind of error
3. CPU time / subprocess timeout enforcement
4. Per-user vs global limits — fairness invariant
5. Limit reset / window expiry

Created from ticket 0012.

## Outcome — 2026-06-03

- File: `tests/test_resource_limiter.py` (new)
- Tests: 36, all green; `tests/test_resource_limiter.py` passes 100%.
- Coverage of `telechat_pkg/resource_limiter.py`: **51% → 100%** (148 stmts, 0 missed).
- Verify cmd: `COVERAGE_FILE=/tmp/.cov_0013 python -m pytest -q tests/test_resource_limiter.py --cov=telechat_pkg.resource_limiter --cov-report=term-missing`

### Behaviors covered (mapped to ticket's 5)

The module is an OS-level **subprocess** resource limiter, not a per-user /
per-platform request-rate limiter. There is no user/platform concept, no
concurrent-request counter, and no sliding window. The ticket's 5 behaviors were
mapped onto what the code actually enforces:

1. Concurrent-request cap → `max_processes` (RLIMIT_NPROC) ceiling; asserted via
   template ordering + that the Linux `preexec_fn` requests RLIMIT_NPROC.
2. Memory limit / throttle → `memory_bytes` ceiling; the `/proc` monitor kills
   the process and records `limits_hit == ["memory"]` when RSS exceeds budget.
3. CPU / subprocess timeout → CPU ceiling kills + flags `cpu`; wall-time timeout
   kills the process and surfaces `"Wall-time limit exceeded"` / `wall_time`.
4. Per-user vs global → per-call `limits=` override beats the instance default
   (`test_per_call_limit_override_is_what_gets_enforced`).
5. Limit reset / window → each `execute` call opens a fresh `ResourceUsage`
   window; wall-time is measured per call from a fresh `start`.

Linux-only branches (`_monitor_linux`, `preexec_fn`, the Linux `execute` path)
are exercised on this macOS host by patching `_is_linux=True` and driving a fake
`/proc` (mocked `os.path.exists` / `open` / `os.sysconf`) plus a fake process.

### Uncovered lines
None — 100%.

### Source bugs found
None. Two observations (documented, not fixed, no source edits made):
- `wall_time_seconds`, `cpu_seconds`, etc. are typed `int` but the code accepts
  floats fine; tests pass sub-second float budgets to force fast timeouts.
- `_monitor_linux` catches `(FileNotFoundError, IOError, PermissionError)` but
  the polling `asyncio.sleep(0.5)` means a real over-budget process can run up
  to ~0.5s past its limit before being killed — expected for a poll-based
  monitor, not a defect.
